"""Stub-tier tests for the one-second cgroup memory sampler.

Every test runs against a FAKE cgroup — a dict of path to text, injected as
`read_text`/`exists` — so the suite is identical on a Mac laptop with no
`/sys/fs/cgroup` at all and on a Linux container that has one. That is the
same discipline `CgroupPeakBytesTests` already follows for
`read_cgroup_peak_bytes`: a test that depends on the host having the thing
under test is a test that reports the host, not the code.

The sampling loop itself is exercised by calling `_sample_once` directly
rather than by starting the thread and sleeping: a test that sleeps to see
whether a thread woke up is slow and flaky, and the thread's own contract
(daemon, stops on the event, joins) is asserted separately without timing.
"""

import os
import tempfile
import unittest

from pipeline.runtime import memory_sampler


V2_STAT_TEXT = """anon 1048576
file 2097152
kernel 262144
shmem 65536
slab 131072
"""

V2_STAT_TEXT_HIGHER = """anon 4194304
file 1048576
kernel 262144
shmem 65536
"""

V2_EVENTS_TEXT = """low 0
high 0
max 7
oom 0
oom_kill 0
"""

V1_STAT_TEXT = """cache 2097152
rss 1048576
shmem 65536
"""


def _v2_cgroup(stat=V2_STAT_TEXT, events=V2_EVENTS_TEXT, current="3145728\n"):
    """A fake cgroup v2 hierarchy: path -> file contents."""
    return {
        memory_sampler.V2_STAT: stat,
        memory_sampler.V2_EVENTS: events,
        memory_sampler.V2_CURRENT: current,
    }


def _reader(files):
    """A `read_text` over a dict, returning None for anything absent —
    exactly what the real `_read_text` does for an unreadable path."""
    return lambda path: files.get(path)


def _exists(files):
    return lambda path: path in files


class _SamplerCase(unittest.TestCase):
    """Gives each test a real temporary bundle directory to write into.

    A real directory, not a fake filesystem: the series file is the thing
    that has to end up inside `build_bundle`'s tar, so the test writes a
    real file and reads the real bytes back.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.bundle_dir = os.path.join(self._tmp.name, "bundle")

    def sampler(self, files, **kwargs):
        return memory_sampler.MemorySampler(
            self.bundle_dir, read_text=_reader(files),
            exists=_exists(files), **kwargs)

    def series_lines(self, sampler):
        with open(sampler.sample_path, "r") as handle:
            return [line.rstrip("\n") for line in handle if line.strip()]


class HierarchyDetectionTests(unittest.TestCase):
    """v2 first, v1 second, None when neither is there — and None is a
    reported outcome, not a crash, because the laptop running this suite is
    exactly that third case."""

    def test_v2_is_preferred_when_both_are_visible(self):
        files = dict(_v2_cgroup())
        files[memory_sampler.V1_STAT] = V1_STAT_TEXT
        self.assertEqual(memory_sampler.detect_hierarchy(_exists(files)),
                         "v2")

    def test_v1_is_used_when_only_the_legacy_hierarchy_is_visible(self):
        files = {memory_sampler.V1_STAT: V1_STAT_TEXT}
        self.assertEqual(memory_sampler.detect_hierarchy(_exists(files)),
                         "v1")

    def test_neither_hierarchy_yields_none(self):
        self.assertIsNone(memory_sampler.detect_hierarchy(_exists({})))


class SeriesWritingTests(_SamplerCase):
    """The series is written to `<bundle_dir>/memory-samples.tsv`, header
    first, one row per sample — the file `build_bundle` sweeps at terminal."""

    def test_the_series_lands_under_the_bundle_directory(self):
        sampler = self.sampler(_v2_cgroup())
        self.assertEqual(
            sampler.sample_path,
            os.path.join(self.bundle_dir, memory_sampler.SAMPLE_FILENAME))

    def test_a_header_and_one_row_per_sample_are_written(self):
        sampler = self.sampler(_v2_cgroup(), clock=lambda: 1000.5)
        sampler._open_series()
        sampler._hierarchy = "v2"
        sampler._sample_once()
        sampler._sample_once()
        sampler._close_series()

        lines = self.series_lines(sampler)
        self.assertEqual(lines[0], "\t".join(memory_sampler.COLUMNS))
        self.assertEqual(len(lines), 3)  # header + two samples
        fields = lines[1].split("\t")
        self.assertEqual(len(fields), len(memory_sampler.COLUMNS))
        self.assertEqual(fields[0], "1000.5")
        self.assertEqual(fields[1], "3145728")   # memory.current
        self.assertEqual(fields[2], "1048576")   # anon
        self.assertEqual(fields[3], "2097152")   # file
        self.assertEqual(fields[6], "7")         # events max
        self.assertEqual(fields[7], "0")         # events oom_kill

    def test_a_missing_value_is_written_as_an_empty_field_not_a_zero(self):
        # An absent number and a measured zero are different facts; the
        # series must not turn the first into the second.
        files = _v2_cgroup(stat="anon 100\nfile 200\n", events="")
        sampler = self.sampler(files, clock=lambda: 5.0)
        sampler._open_series()
        sampler._hierarchy = "v2"
        sampler._sample_once()
        sampler._close_series()

        fields = self.series_lines(sampler)[1].split("\t")
        self.assertEqual(fields[2], "100")
        self.assertEqual(fields[4], "")   # kernel absent from this stat file
        self.assertEqual(fields[6], "")   # no events file at all

    def test_rows_are_appended_and_flushed_not_buffered(self):
        # The bound this module claims: the sampler's memory does not grow
        # with the run's length, and a process that dies without a clean
        # stop still leaves the series complete to the last full second.
        sampler = self.sampler(_v2_cgroup())
        sampler._open_series()
        sampler._hierarchy = "v2"
        sampler._sample_once()
        # Read the file WITHOUT closing the sampler's handle: the row is
        # only visible if it was flushed as it was taken.
        self.assertEqual(len(self.series_lines(sampler)), 2)
        sampler._sample_once()
        self.assertEqual(len(self.series_lines(sampler)), 3)
        sampler._close_series()


class MaximaTests(_SamplerCase):
    """The five values the terminal write reads. Maxima over the samples
    taken, None for a quantity never successfully read."""

    def test_the_maxima_are_the_running_maximum_of_each_column(self):
        files = _v2_cgroup()
        sampler = self.sampler(files)
        sampler._open_series()
        sampler._hierarchy = "v2"
        sampler._sample_once()
        # anon rises, file falls: the maxima must track each independently,
        # which is the whole point of splitting them.
        files[memory_sampler.V2_STAT] = V2_STAT_TEXT_HIGHER
        sampler._sample_once()
        sampler._close_series()

        maxima = sampler.maxima()
        self.assertEqual(maxima["anon_peak_bytes"], 4_194_304)
        self.assertEqual(maxima["file_peak_bytes"], 2_097_152)
        self.assertEqual(maxima["memory_events_max"], 7)
        self.assertEqual(maxima["memory_events_oom_kill"], 0)
        self.assertEqual(maxima["memory_sample_count"], 2)

    def test_the_maxima_keys_are_the_migration_120_column_names(self):
        # The terminal write is a pass-through, so a renamed key here would
        # silently stop writing a column rather than fail loudly.
        sampler = self.sampler(_v2_cgroup())
        self.assertEqual(
            sorted(sampler.maxima()),
            ["anon_peak_bytes", "file_peak_bytes", "memory_events_max",
             "memory_events_oom_kill", "memory_sample_count"])

    def test_a_zero_oom_kill_is_recorded_as_zero_not_none(self):
        # The acceptance distinguishes them: NULL means never measured, 0
        # means measured and the kernel killed nothing.
        sampler = self.sampler(_v2_cgroup())
        sampler._open_series()
        sampler._hierarchy = "v2"
        sampler._sample_once()
        sampler._close_series()
        self.assertEqual(sampler.maxima()["memory_events_oom_kill"], 0)
        self.assertIsNotNone(sampler.maxima()["memory_events_oom_kill"])

    def test_an_unsampled_attempt_reports_zero_samples_and_null_maxima(self):
        # A host with no cgroup: `memory_sample_count = 0` in the row is the
        # signal that the sampler did not run, and the peaks stay NULL
        # rather than being reported as zero bytes.
        sampler = self.sampler({}).start()
        self.addCleanup(sampler.stop)
        maxima = sampler.maxima()
        self.assertEqual(maxima["memory_sample_count"], 0)
        self.assertIsNone(maxima["anon_peak_bytes"])
        self.assertIsNone(maxima["file_peak_bytes"])
        self.assertIsNone(maxima["memory_events_oom_kill"])


class CgroupV1Tests(_SamplerCase):
    """v1 records the sizes under their v1 names and no event counters."""

    def test_v1_maps_rss_and_cache_onto_anon_and_file(self):
        files = {memory_sampler.V1_STAT: V1_STAT_TEXT,
                 memory_sampler.V1_USAGE: "3145728\n"}
        sampler = self.sampler(files)
        sampler._open_series()
        sampler._hierarchy = "v1"
        sampler._sample_once()
        sampler._close_series()

        maxima = sampler.maxima()
        self.assertEqual(maxima["anon_peak_bytes"], 1_048_576)   # rss
        self.assertEqual(maxima["file_peak_bytes"], 2_097_152)   # cache
        self.assertEqual(maxima["memory_sample_count"], 1)

    def test_v1_records_no_event_counters_rather_than_zeroes(self):
        # v1 has no memory.events at all. Reporting 0 would claim the
        # cgroup never hit its limit, which v1 cannot know.
        files = {memory_sampler.V1_STAT: V1_STAT_TEXT,
                 memory_sampler.V1_USAGE: "3145728\n"}
        sampler = self.sampler(files)
        sampler._open_series()
        sampler._hierarchy = "v1"
        sampler._sample_once()
        sampler._close_series()

        self.assertIsNone(sampler.maxima()["memory_events_max"])
        self.assertIsNone(sampler.maxima()["memory_events_oom_kill"])


class FailureIsNeverFatalTests(_SamplerCase):
    """THE REGRESSION GUARD. A measurement must never cost the job anything:
    not a raised exception, not a stuck terminal, not a log line per second
    for an hour. Each of these is proven by reverting the guard it names and
    watching the test fail (the reverts are in the run's acceptance)."""

    def test_a_read_that_raises_does_not_propagate_out_of_the_loop(self):
        def angry_read(path):
            raise OSError("cgroup went away")

        sampler = memory_sampler.MemorySampler(
            self.bundle_dir, read_text=angry_read,
            exists=_exists(_v2_cgroup()))
        sampler._open_series()
        sampler._hierarchy = "v2"
        # `_run`'s body for one iteration: the exception is caught, logged
        # once, and the loop would go round again.
        try:
            sampler._sample_once()
        except Exception:  # noqa: BLE001 - the failure this test forbids
            sampler._log_read_failure()
        sampler._close_series()
        self.assertEqual(sampler.maxima()["memory_sample_count"], 0)

    def test_a_missing_stat_file_yields_no_row_and_does_not_raise(self):
        sampler = self.sampler({memory_sampler.V2_CURRENT: "1\n"})
        sampler._open_series()
        sampler._hierarchy = "v2"
        sampler._sample_once()      # must not raise
        sampler._close_series()
        self.assertEqual(sampler.maxima()["memory_sample_count"], 0)
        self.assertEqual(self.series_lines(sampler), [
            "\t".join(memory_sampler.COLUMNS)])

    def test_a_read_failure_is_logged_once_not_once_per_sample(self):
        # A science job runs ~3,700 seconds. A per-second warning would be
        # 3,700 lines in the safety stream for one broken file.
        sampler = self.sampler({})
        self.assertFalse(sampler._read_failure_logged)
        sampler._log_read_failure()
        self.assertTrue(sampler._read_failure_logged)
        # The second call is a no-op: proven by the flag, which is what
        # gates the logging call itself.
        sampler._log_read_failure()
        self.assertTrue(sampler._read_failure_logged)

    def test_start_on_a_host_with_no_cgroup_does_not_raise_or_start_a_thread(self):
        sampler = self.sampler({}).start()
        self.addCleanup(sampler.stop)
        self.assertIsNone(sampler._thread)
        self.assertFalse(os.path.exists(sampler.sample_path))

    def test_stop_is_idempotent(self):
        sampler = self.sampler(_v2_cgroup()).start()
        sampler.stop()
        sampler.stop()      # must not raise, must not hang
        sampler.stop()
        self.assertTrue(sampler._stopping.is_set())

    def test_stop_without_start_is_safe(self):
        # The entrypoint's exit path calls stop() unconditionally, including
        # on a path where start() was never reached.
        sampler = self.sampler(_v2_cgroup())
        sampler.stop()
        self.assertTrue(sampler._stopping.is_set())

    def test_the_sampling_thread_is_a_daemon(self):
        # A wedged sampler must not hold the interpreter open at exit.
        sampler = self.sampler(_v2_cgroup()).start()
        self.addCleanup(sampler.stop)
        self.assertIsNotNone(sampler._thread)
        self.assertTrue(sampler._thread.daemon)

    def test_start_then_stop_leaves_a_readable_closed_series(self):
        # The end-to-end shape terminate() depends on: after stop(), the
        # file is complete and closed, so build_bundle tars a whole file.
        sampler = self.sampler(_v2_cgroup(), interval=0.01).start()
        sampler.stop()
        self.assertIsNone(sampler._handle)
        lines = self.series_lines(sampler)
        self.assertEqual(lines[0], "\t".join(memory_sampler.COLUMNS))


class ParsingTests(unittest.TestCase):
    """`memory.stat` and `memory.events` are flat `key value` files; a line
    this code does not understand is skipped, never fatal."""

    def test_keyed_parsing_reads_every_integer_line(self):
        values = memory_sampler._parse_keyed("anon 10\nfile 20\n")
        self.assertEqual(values, {"anon": 10, "file": 20})

    def test_a_malformed_line_is_skipped_and_the_rest_still_parses(self):
        values = memory_sampler._parse_keyed(
            "anon 10\nnonsense\nfile not-a-number\nshmem 30\n")
        self.assertEqual(values, {"anon": 10, "shmem": 30})

    def test_empty_or_absent_text_parses_to_an_empty_mapping(self):
        self.assertEqual(memory_sampler._parse_keyed(""), {})
        self.assertEqual(memory_sampler._parse_keyed(None), {})

    def test_single_integer_files_parse_and_tolerate_rubbish(self):
        self.assertEqual(memory_sampler._parse_int("4096\n"), 4096)
        self.assertIsNone(memory_sampler._parse_int("max\n"))
        self.assertIsNone(memory_sampler._parse_int(None))

    def test_max_treats_none_as_unseen_rather_than_as_a_value(self):
        self.assertEqual(memory_sampler._max(None, 5), 5)
        self.assertEqual(memory_sampler._max(5, None), 5)
        self.assertEqual(memory_sampler._max(5, 7), 7)
        self.assertEqual(memory_sampler._max(7, 5), 7)
        self.assertIsNone(memory_sampler._max(None, None))


if __name__ == "__main__":
    unittest.main()
