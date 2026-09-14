"""A one-second cgroup memory sampler, riding the diagnostics bundle.

Every successful Prove science attempt recorded `attempts.cgroup_peak_bytes`
= 16,384 MiB — exactly the container's hard limit. That is not a measurement
of what the job needs; it is a measurement of where the kernel was told to
stop it. AWS Batch's `MEMORY` resource requirement is a hard limit, so the
job runs up against the cap and the kernel holds it there by reclaiming file
cache. A single terminal peak reading cannot tell the two apart, and the
number a hard limit must exceed is not the peak at all — it is the
**anonymous** working set, the part that cannot be reclaimed.

So this module samples, once a second, the split that `memory.peak` collapses:

    memory.stat  anon       the working set a hard limit must exceed
    memory.stat  file       reclaimable page cache — demand, not need
    memory.stat  kernel     kernel memory charged to the cgroup
    memory.stat  shmem      shared memory, charged like anon but reclaimable
    memory.current          the total the limit is actually compared against
    memory.events max       times the cgroup hit its limit and had to reclaim
    memory.events oom_kill  times the kernel killed something in it

Three small file reads per second, appended as one TSV row to
`<workdir.bundle_dir>/memory-samples.tsv`. That directory is swept into the
diagnostics bundle by `termination.build_bundle` at terminal, so the series
reaches the diagnostics bucket through the mechanism that already exists —
no new S3 layout, no new upload path, no second thing to keep alive. The
running maxima are kept in memory alongside, and
`termination.capture_resource_usage` reads them at terminal to write the
`attempts` columns (migration 120). The series answers "sustained or spike,
and at which stage" (joined to `attempt_stages` by timestamp); the columns
answer "how big, and did it ever hit the wall" without opening a bundle.

## Why a thread rather than a subprocess or an external agent

The sampler must observe the job's OWN cgroup, and it must stop when the job
stops — a sidecar would need its own lifecycle, its own failure mode and its
own way of finding the right cgroup. A daemon thread in the job process has
the cgroup by construction (`/sys/fs/cgroup` is the container's own view
under cgroup v2), dies with the process, and costs three `open`/`read`/
`close` pairs a second against tmpfs.

## Never at the job's expense

Every read is inside a try/except and a failure is logged ONCE (not once a
second — a broken `/sys/fs/cgroup` would otherwise write a log line per
second for the hour a science job runs). The thread is a daemon, so a wedged
sampler cannot hold the interpreter open at exit. `stop()` is idempotent and
never raises. Nothing in this module can fail a stage, delay terminal, or
cost an attempt its record: the measurement exists to inform a sizing
ruling, and a measurement that can break the thing it measures is worth less
than no measurement.

## cgroup v1

Every current job container runs cgroup v2, which is what the v2 paths above
describe. On a v1 host the same quantities live in different files under
different names (`memory.usage_in_bytes`, and `memory.stat`'s `rss`/`cache`
rather than `anon`/`file`), and v1 has no `memory.events` at all — so a v1
sampler records the sizes and no event counters, and says so in the log. On
a host with neither hierarchy the sampler records nothing and says that too:
an empty series with a logged reason is honest, where a series of zeros
would be a lie that reads as a measurement.
"""

import os
import threading
import time

from pipeline.runtime.logging_setup import get_logger

_logger = get_logger("memory_sampler")

#: The file the series is appended to, under `workdir.bundle_dir`. Named in
#: the brief's acceptance and read by the analysis pass, so it is a constant
#: here rather than a string at the call site.
SAMPLE_FILENAME = "memory-samples.tsv"

#: cgroup v2, the container's own view. A job container's `/sys/fs/cgroup`
#: is its own cgroup's directory (the namespace is mounted at the leaf), so
#: these are read directly rather than resolved through `/proc/self/cgroup`.
V2_CURRENT = "/sys/fs/cgroup/memory.current"
V2_STAT = "/sys/fs/cgroup/memory.stat"
V2_EVENTS = "/sys/fs/cgroup/memory.events"

#: cgroup v1, for a host that still runs the legacy hierarchy. `rss` and
#: `cache` are v1's names for what v2 calls `anon` and `file`; v1 has no
#: events file, so `max`/`oom_kill` are simply absent from such a series.
V1_USAGE = "/sys/fs/cgroup/memory/memory.usage_in_bytes"
V1_STAT = "/sys/fs/cgroup/memory/memory.stat"

#: The TSV's columns, in order. Written as a header line so the file is
#: self-describing in the bundle — an analysis six weeks later should not
#: have to find this module to know what column four is.
COLUMNS = ("ts_utc", "current_bytes", "anon_bytes", "file_bytes",
           "kernel_bytes", "shmem_bytes", "events_max", "events_oom_kill")

DEFAULT_INTERVAL_SECONDS = 1.0


def _read_text(path):
    """The whole of a small pseudo-file, or None if it cannot be read.

    `/sys/fs/cgroup` files are tmpfs-backed and read atomically in one go;
    there is no partial-read case to handle. A missing file is routine (v1
    on a v2 host and vice versa) and is not an error here — the caller
    decides what an absent file means.
    """
    try:
        with open(path, "r") as handle:
            return handle.read()
    except Exception:  # noqa: BLE001 - an unreadable pseudo-file is routine
        return None


def _parse_keyed(text):
    """`memory.stat`/`memory.events` — whitespace-separated `key value` lines.

    Returns a dict of the integer values, skipping any line that does not
    parse. Both files are flat key/value throughout; a line that is not two
    fields or whose value is not an integer is a kernel version this code
    does not know, and skipping it is better than refusing the whole file.
    """
    values = {}
    if not text:
        return values
    for line in text.splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        try:
            values[fields[0]] = int(fields[1])
        except ValueError:
            continue
    return values


def _parse_int(text):
    """A single-integer pseudo-file's value, or None."""
    if text is None:
        return None
    try:
        return int(text.strip())
    except (TypeError, ValueError):
        return None


def detect_hierarchy(exists=os.path.exists):
    """Which cgroup hierarchy this container can see: "v2", "v1" or None.

    v2 is checked first because that is what every current job container
    runs; the v1 branch exists so a sampler on a legacy host records real
    sizes rather than nothing, and `None` is the honest answer on a host
    with neither (a Mac laptop running the unit tests, for one).
    """
    if exists(V2_STAT):
        return "v2"
    if exists(V1_STAT):
        return "v1"
    return None


def _max(current, candidate):
    """`max` that treats None as "not yet seen" rather than as a value.

    A column stays NULL until the sampler actually reads a number for it, so
    a NULL `memory_events_oom_kill` means "never measured" and a 0 means
    "measured, and the kernel killed nothing" — a distinction the acceptance
    checks for explicitly (`is distinct from 0`).
    """
    if candidate is None:
        return current
    if current is None:
        return candidate
    return max(current, candidate)


class MemorySampler:
    """Samples this container's cgroup memory once a second until stopped.

    `bundle_dir` is `workdir.bundle_dir` — the directory `build_bundle`
    sweeps at terminal. The sampler writes one file into it and keeps the
    running maxima in memory for `capture_resource_usage` to read.

    `read_text`, `exists` and `clock` are injectable so the tests run against
    a fake cgroup directory on any host, rather than depending on the machine
    under the test having a cgroup hierarchy at all.
    """

    def __init__(self, bundle_dir, interval=DEFAULT_INTERVAL_SECONDS,
                 read_text=_read_text, exists=os.path.exists,
                 clock=time.time):
        self._bundle_dir = bundle_dir
        self._interval = interval
        self._read_text = read_text
        self._exists = exists
        self._clock = clock
        self._hierarchy = None
        self._thread = None
        self._stopping = threading.Event()
        self._handle = None
        self._read_failure_logged = False
        # The running maxima, and the event counters as last seen. The
        # counters are monotonic within a cgroup's life, so "last seen" and
        # "maximum" coincide for them — kept as maxima anyway so a counter
        # that resets (it should not) cannot walk the recorded value back.
        self.anon_peak_bytes = None
        self.file_peak_bytes = None
        self.current_peak_bytes = None
        self.events_max = None
        self.events_oom_kill = None
        self.sample_count = 0

    @property
    def sample_path(self):
        """Where the series is written — under the bundle, so it ships."""
        return os.path.join(self._bundle_dir, SAMPLE_FILENAME)

    def start(self):
        """Begin sampling in a daemon thread. Never raises.

        On a host with no cgroup hierarchy this records the fact in the log
        and starts no thread: `sample_count` stays 0, which is exactly what
        `memory_sample_count = 0` means in the `attempts` row — the sampler
        did not run, as distinct from a sampler that ran and saw nothing.
        """
        try:
            self._hierarchy = detect_hierarchy(self._exists)
            if self._hierarchy is None:
                _logger.info(
                    "no cgroup memory hierarchy visible at %s or %s; this "
                    "attempt records no memory series and its "
                    "memory_sample_count stays 0", V2_STAT, V1_STAT)
                return self
            if self._hierarchy == "v1":
                _logger.info(
                    "cgroup v1 hierarchy detected; sampling "
                    "memory.usage_in_bytes and memory.stat rss/cache. v1 has "
                    "no memory.events, so this attempt records no max or "
                    "oom_kill counters")
            self._open_series()
            self._thread = threading.Thread(
                target=self._run, name="memory-sampler", daemon=True)
            self._thread.start()
        except Exception:  # noqa: BLE001 - measurement is never fatal
            _logger.warning(
                "could not start the memory sampler; this attempt records no "
                "memory series", exc_info=True)
        return self

    def stop(self, timeout=5.0):
        """Stop sampling and close the series. Idempotent, never raises.

        Called before `terminate` builds the bundle, so the file is complete
        and closed by the time it is swept in. A sampler that never started
        (no hierarchy, or a failed start) stops cleanly too — that is what
        makes this safe to call unconditionally from the entrypoint's exit
        path.
        """
        try:
            self._stopping.set()
            thread = self._thread
            if thread is not None and thread.is_alive():
                thread.join(timeout=timeout)
            self._close_series()
        except Exception:  # noqa: BLE001 - measurement is never fatal
            _logger.warning("could not cleanly stop the memory sampler",
                            exc_info=True)
        return self

    def maxima(self):
        """The five values `capture_resource_usage` writes to `attempts`.

        Keys match the migration-120 column names exactly, so the terminal
        write is a pass-through rather than a re-mapping that can drift.
        """
        return {
            "anon_peak_bytes": self.anon_peak_bytes,
            "file_peak_bytes": self.file_peak_bytes,
            "memory_events_max": self.events_max,
            "memory_events_oom_kill": self.events_oom_kill,
            "memory_sample_count": self.sample_count,
        }

    # -- internals ---------------------------------------------------------

    def _open_series(self):
        os.makedirs(self._bundle_dir, exist_ok=True)
        self._handle = open(self.sample_path, "a")
        self._handle.write("\t".join(COLUMNS) + "\n")
        self._handle.flush()

    def _close_series(self):
        handle = self._handle
        self._handle = None
        if handle is not None:
            try:
                handle.flush()
                handle.close()
            except Exception:  # noqa: BLE001 - closing is best-effort too
                pass

    def _run(self):
        """The sampling loop. Any exception skips a sample, never the job.

        `Event.wait` rather than `sleep` so `stop()` returns promptly instead
        of waiting out a whole interval — a terminal path should not spend a
        second waiting for a sampler to notice.
        """
        while not self._stopping.is_set():
            try:
                self._sample_once()
            except Exception:  # noqa: BLE001 - a bad sample skips, no more
                self._log_read_failure()
            self._stopping.wait(self._interval)

    def _log_read_failure(self):
        """Log a read failure ONCE per attempt, not once per second."""
        if self._read_failure_logged:
            return
        self._read_failure_logged = True
        _logger.warning(
            "a cgroup memory read failed; the sampler keeps going and this "
            "attempt's series may have gaps", exc_info=True)

    def _sample_once(self):
        """One row: read, record the maxima, append. Appended, not buffered.

        The row is written and flushed as it is taken, so the series on disk
        is complete up to the last full second even if the process dies
        without a clean stop — and so the sampler's memory cost does not grow
        with the length of the run, which is the bound this module claims
        (three reads and one short line per second, no accumulation).
        """
        if self._hierarchy == "v2":
            row = self._sample_v2()
        else:
            row = self._sample_v1()
        if row is None:
            self._log_read_failure()
            return
        self._record_maxima(row)
        handle = self._handle
        if handle is not None:
            handle.write("\t".join(
                "" if value is None else str(value) for value in row) + "\n")
            handle.flush()
        self.sample_count += 1

    def _sample_v2(self):
        stat = _parse_keyed(self._read_text(V2_STAT))
        if not stat:
            return None
        events = _parse_keyed(self._read_text(V2_EVENTS))
        current = _parse_int(self._read_text(V2_CURRENT))
        return (self._timestamp(), current, stat.get("anon"),
                stat.get("file"), stat.get("kernel"), stat.get("shmem"),
                events.get("max"), events.get("oom_kill"))

    def _sample_v1(self):
        stat = _parse_keyed(self._read_text(V1_STAT))
        if not stat:
            return None
        current = _parse_int(self._read_text(V1_USAGE))
        # v1's `rss` and `cache` are the same quantities v2 calls `anon` and
        # `file`; `kernel` and the event counters have no v1 equivalent and
        # stay empty in the series rather than being invented as zero.
        return (self._timestamp(), current, stat.get("rss"),
                stat.get("cache"), None, stat.get("shmem"), None, None)

    def _timestamp(self):
        """Seconds since the epoch, UTC, to the millisecond.

        A float rather than an ISO string: the series is joined to
        `attempt_stages.started_at` by time, and a number the analysis can
        subtract beats a string it has to parse 3,700 times.
        """
        return round(self._clock(), 3)

    def _record_maxima(self, row):
        _ts, current, anon, file_bytes, _kernel, _shmem, ev_max, ev_oom = row
        self.current_peak_bytes = _max(self.current_peak_bytes, current)
        self.anon_peak_bytes = _max(self.anon_peak_bytes, anon)
        self.file_peak_bytes = _max(self.file_peak_bytes, file_bytes)
        self.events_max = _max(self.events_max, ev_max)
        self.events_oom_kill = _max(self.events_oom_kill, ev_oom)
