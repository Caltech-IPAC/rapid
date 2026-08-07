"""Building the bundle an attempt died before writing.

The observability design's rule is unconditional: *every terminal attempt ends
with exactly one compressed per-attempt diagnostics bundle in S3*, and for an
attempt that never wrote one — abrupt loss, or never started — *the reconciler
builds the bundle from the attempt's CloudWatch stream at classification time
and marks it reconstructed. Either way the bundle exists before the attempt is
closed, whichever way it died.*

The second half of that sentence had no implementation (round-3 finding #5).
`service._stamp_bundle` noticed the absence, incremented a counter, logged a
warning, and returned — and the caller went straight on to the terminal
transition. An abruptly killed job therefore became permanently terminal with
no diagnostics at all, and terminal rows are outside the open set, so nothing
ever revisited it. The evidence for a real execution was gone, and the only
trace was a counter on a service that had already moved on.

WHAT A RECONSTRUCTION IS NOT. It is not the bundle the attempt would have
written. The runtime's own `build_bundle` tars a staging directory holding
stdout, stderr, per-stage logs and tool outputs; that directory died with the
container. What CloudWatch retains is the attempt's console stream, which is
one of those things and not the others. So a reconstructed bundle is honestly
smaller, and it says so — `reconstructed.json` inside it records what was
recovered, from where, and what could not be. The marked-degradation
convention this follows is `closure.build_closure_record`'s `reconstructed_from`
list, for the same reason: a consumer must be able to tell a complete bundle
from a salvaged one without opening it.

WHY IT DOES NOT DEFER FOREVER. The obvious alternative — defer the attempt
until a bundle appears — cannot terminate. Deferral in this service is
unbounded: there is no defer counter, no backoff and no dead-letter, just the
open set being re-polled. A bundle that is absent because the container was
killed will never appear, and worse, the CloudWatch stream itself expires
(14 days per the observability retention table), so the raw material for the
reconstruction is on a clock. An attempt deferred past that horizon can never
be closed by anyone. Reconstruct-or-record-the-gap is therefore the posture:
recover what still exists, write the bundle, mark what is missing, and let the
attempt close with its account complete about being incomplete.
"""

import io
import json
import logging
import tarfile

from pipeline.runtime import termination

logger = logging.getLogger("rapid.reconciler.reconstruction")

#: How many CloudWatch events a reconstruction pulls. Deliberately far larger
#: than `closure.read_log_stream`'s 200-event tail, which exists to give a
#: RECORD a few closing lines: this is the bundle, and the bundle is meant to
#: be the diagnostic artefact somebody actually reads when asking why an
#: attempt died. Still bounded — a runaway job can emit millions of lines, and
#: a reconstruction that tries to hold all of them in memory turns one dead
#: attempt into a dead reconciler.
RECONSTRUCTION_EVENT_LIMIT = 10000

#: Marks the bundle as salvage rather than the real thing. Read by anything
#: that opens a bundle and needs to know what it is holding.
MANIFEST_MEMBER = "reconstructed.json"
LOG_MEMBER = "cloudwatch-stream.log"


def read_stream_events(logs_client, log_group, log_stream,
                       limit=RECONSTRUCTION_EVENT_LIMIT):
    """Pull up to `limit` events from the head of the attempt's stream.

    From the HEAD, unlike `closure.read_log_stream`'s tail. The two want
    different things from the same stream: a closure record wants how the
    attempt ended, and a bundle wants how it ran — the traceback that matters
    is usually preceded by the stage that caused it.

    Returns `(events, error)`. A read failure is reported rather than raised
    because a reconstruction with no log is still worth writing: the bundle's
    existence and its manifest are themselves the record that the attempt ran
    and its diagnostics were unrecoverable. Raising here would put us back in
    the deferral trap this module exists to avoid.
    """
    if logs_client is None or not log_group or not log_stream:
        return [], "no log stream was recorded for this attempt"

    events = []
    token = None
    try:
        while len(events) < limit:
            kwargs = {"logGroupName": log_group, "logStreamName": log_stream,
                      "startFromHead": True,
                      "limit": min(limit - len(events), 1000)}
            if token is not None:
                kwargs["nextToken"] = token
            response = logs_client.get_log_events(**kwargs)
            batch = list(response.get("events", ()))
            if not batch:
                break
            events.extend(batch)
            following = response.get("nextForwardToken")
            # CloudWatch signals the end of a forward scan by returning the
            # SAME token it was given; without this check the loop spins on a
            # finished stream until it hits `limit`.
            if following is None or following == token:
                break
            token = following
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        logger.warning("could not read log stream %s/%s for reconstruction: "
                       "%s", log_group, log_stream, exc)
        return events, f"the log stream could not be read: {exc}"

    return events, None


def build_reconstructed_bundle(row, observation, events, read_error=None,
                               stages=None, now=None):
    """Tar up what could be recovered, with a manifest saying what that was.

    Built with the same determinism `termination.build_bundle` uses — sorted
    members, zeroed mtimes and gzip header — so that if this runs twice for one
    attempt the second build produces identical bytes and the create-once
    upload sees a replay rather than a collision.
    """
    attempt_id = row.get("attempt_id")
    lines = []
    for event in events or ():
        message = event.get("message")
        if message is None:
            continue
        lines.append(str(message).rstrip("\n"))
    log_body = ("\n".join(lines) + "\n").encode("utf-8") if lines else b""

    manifest = {
        "reconstructed": True,
        "reconstructed_by": "reconciler",
        "attempt_id": attempt_id,
        "run_id": row.get("run_id"),
        "logical_job_id": row.get("logical_job_id"),
        "scheduler_job_id": row.get("scheduler_job_id"),
        "scheduler_state": (observation.state
                            if observation is not None else None),
        "scheduler_observed_exit": (observation.exit_code
                                    if observation is not None else None),
        "log_stream": (observation.log_stream
                       if observation is not None else None),
        "events_recovered": len(lines),
        "event_limit": RECONSTRUCTION_EVENT_LIMIT,
        "truncated": len(lines) >= RECONSTRUCTION_EVENT_LIMIT,
        # The honest part. A consumer comparing this against the bundle an
        # attempt writes for itself must be able to see exactly what is
        # missing rather than inferring it from absence.
        "recovered": [name for name, present in
                      ((LOG_MEMBER, bool(lines)),
                       ("attempt_stages", bool(stages))) if present],
        "unrecoverable": [
            "stdout/stderr as separate streams (CloudWatch interleaves them)",
            "per-stage log files",
            "tool outputs and intermediate products",
        ],
    }
    if read_error:
        manifest["read_error"] = read_error
    if now is not None:
        manifest["reconstructed_at"] = now
    if stages:
        manifest["attempt_stages"] = stages

    # `default=termination._json_default`, NOT `default=str`: the stages come
    # from `closure.read_attempt_stages` as raw psycopg2 rows, where
    # `attempt_stages.duration_ms` is `numeric NOT NULL` and arrives as a
    # `Decimal`. `str` would keep the manifest writable while silently
    # retyping a numeric field to a string under every consumer that reads it
    # — the exact failure `_json_default`'s own docstring warns against, and
    # which was fixed in `ClosureRecord.to_bytes` but not here.
    members = [(MANIFEST_MEMBER,
                json.dumps(manifest, indent=2, sort_keys=True,
                           default=termination._json_default,
                           ).encode("utf-8"))]
    if log_body:
        members.append((LOG_MEMBER, log_body))

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", compresslevel=6,
                      format=tarfile.PAX_FORMAT) as tar:
        for name, body in sorted(members):
            info = tarfile.TarInfo(name=name)
            info.size = len(body)
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(body))
    return buffer.getvalue(), manifest


def reconstruct_bundle(store, key, row, observation, logs_client, log_group,
                       stages=None, now=None):
    """Build and upload the missing bundle. Returns the upload result, or None.

    None means the bundle could not be created at all — the store refused the
    write for a reason that is not "it already exists". The caller treats that
    as a deferral, because unlike an unrecoverable log it is a condition that
    a later poll can genuinely find resolved.

    Uploaded through `termination.upload_bundle`, the same create-once path the
    application uses, so a bundle that turned up between the check and now is
    KEPT and this reconstruction discarded. The real thing always wins over the
    salvage — and that race is not hypothetical: an attempt whose container was
    slow to flush can land its own bundle while the reconciler is building this
    one.
    """
    events, read_error = read_stream_events(
        logs_client, log_group,
        observation.log_stream if observation is not None else None)

    body, manifest = build_reconstructed_bundle(
        row, observation, events, read_error=read_error, stages=stages,
        now=now)

    try:
        result = termination.upload_bundle(store, key, body)
    except Exception as exc:  # noqa: BLE001 - the caller defers on None
        logger.warning(
            "could not upload a reconstructed bundle for attempt %s at %s: "
            "%s", row.get("attempt_id"), key, exc)
        return None

    logger.info(
        "reconstructed the diagnostics bundle for attempt %s at %s from %d "
        "CloudWatch event(s)%s; marked reconstructed",
        row.get("attempt_id"), key, manifest["events_recovered"],
        " (the stream could not be read)" if read_error else "")
    return result
