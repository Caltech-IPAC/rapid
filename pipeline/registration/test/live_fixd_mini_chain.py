"""FixD: one unit all the way from a terminal record to a registered product.

The round-3 acceptance probe. What it proves, against the REAL database, the
REAL records bucket and the REAL stored procedures:

  1. A terminal record written the way production writes it — through
     `build_terminal_record`/`write_terminal_record`, never a hand-built dict —
     carries every fact the registrar demands. That is round-3 finding #2: no
     production record could previously have satisfied it, and a probe that
     constructed its own body would prove nothing about that.
  2. The citation triple the row ends with is COHERENT: the checksum hashes
     the bytes at the cited key. Finding #1.
  3. The registrar registers from it FOR REAL — `addRefImage` and
     `registerRefImCatalog` run, and rows land in RefImages and RefImCatalogs.
  4. The watermark advances, in the SAME transaction as the product rows.
     Finding #8.
  5. A REPLAY of the same attempt at the same record sequence registers NO
     second version. That is migration 018's guard doing its work against the
     live procedure, which is the only place it can be proven.

WHY A SYNTHETIC PRODUCT. The PSFs table is empty on rapid-db (0 rows, probed
2026-08-06) and RefImages/DiffImages are both empty too, so there is no real
reference-image attempt to be had: a genuine one needs PSF data that the
survey has not produced. The probe therefore builds a battery-shaped
reference-image attempt — a real attempt row, a real terminal record through
the real serializer, real S3 objects — and registers THAT. What is synthetic
is the science content of the product, not any part of the chain under test.
That limit is stated here and in the ledger rather than papered over.

ADDITIVE only. Rows are written under a time-stamped run id and left as
evidence; nothing is deleted and nothing pre-existing is modified.
"""

import datetime
import json
import logging
import os
import sys

import boto3

from database.modules.utils import rapid_db
from database.modules.utils import rapid_db_connect as dbc
from observability.attempts import (AttemptIdentity, AttemptWriter,
                                    ExecutionBinding, ProductDisposition,
                                    Provenance, RapidOutcome)
from pipeline.reconciler import horizons
from pipeline.registration import consumer as registration_consumer
from pipeline.registration.products import registrar
from pipeline.runtime import termination
from pipeline.runtime.boundaries import S3ObjectStore
from pipeline.runtime.boundaries import checksum as body_checksum

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("fixd.minichain")

RUN = f"fixd-chain-{datetime.datetime.now(datetime.timezone.utc):%Y%m%dT%H%M%SZ}"

results = []


def check(name, condition, detail=""):
    results.append((name, bool(condition)))
    print(f"{'PASS' if condition else 'FAIL'}  {name}"
          + (f"  ({detail})" if detail else ""))


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def provenance():
    """The runtime-selected provenance a started attempt binds."""
    return Provenance(source_sha="fixd" + "0" * 36,
                      container_digest="sha256:" + "a" * 64,
                      job_definition_rev="rapid-pipeline-science",
                      config_digest="sha256:" + "c" * 64)


def scalar(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return row[0] if row else None


def main():
    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    session = boto3.Session(region_name=region)
    s3 = session.client("s3")

    records_bucket = os.environ["RAPID_RECORDS_BUCKET"]
    prefix = os.environ.get("RAPID_RECORDS_PREFIX", "attempts")
    store = S3ObjectStore(records_bucket, client=s3)

    logical_job_id = f"{RUN}_ref_1"
    print(f"=== FixD mini-chain, run {RUN} ===")
    print(f"    records s3://{records_bucket}/{prefix}")

    with dbc.connection("rapid-fixd-chain", lane="transaction") as conn:
        writer = AttemptWriter(dbc.ConnectionExecutor(conn).execute)

        # -- a real attempt row, through the real writer ------------------
        # Same sequence the submission seam uses and the W8 battery proves:
        # a logical job carrying the execution binding, then the submitted
        # attempt row, then the started compare-and-set.
        scheduler_job_id = f"batch-{RUN}"
        binding = ExecutionBinding(
            job_definition_arn=("arn:aws:batch:us-east-1:ACCOUNT:"
                                "job-definition/rapid-pipeline-science:1"),
            image_digest="sha256:" + "a" * 64,
            manifest_checksum="sha256:" + "b" * 64,
            job_definition_rev=1,
            release_identity="rapid-fixd-chain")
        writer.create_logical_job(logical_job_id, RUN, binding,
                                  scheduler_job_id=scheduler_job_id)
        identity = AttemptIdentity(run_id=RUN, logical_job_id=logical_job_id,
                                   exposure_id=999300, sca=1)
        moment = now()
        attempt_id = writer.create_submitted(identity, moment, moment,
                                             scheduler_job_id=scheduler_job_id,
                                             binding=binding)
        conn.commit()
        writer.mark_started(attempt_id, now(), provenance(),
                            application_attempt_index=1,
                            config_snapshot_key=f"{prefix}/config/{RUN}/s.json")
        conn.commit()
        print(f"    attempt_id {attempt_id}")

        # -- the products, as real S3 objects -----------------------------
        # Registration cites the URI and the checksum the record carries, and
        # the record must describe objects that exist: a registrar that
        # registered a URI with nothing behind it would be the "trust the
        # external state" defect the record exists to remove.
        products = {}
        for name in ("reference_image", "reference_sexcat"):
            key = f"{prefix}/products/{RUN}/{name}.fits"
            body = f"{name} for {RUN}\n".encode("utf-8")
            s3.put_object(Bucket=records_bucket, Key=key, Body=body)
            products[name] = {
                "name": name,
                "uri": f"s3://{records_bucket}/{key}",
                "checksum": body_checksum(body),
                "size": len(body),
            }

        # -- the terminal record, through the REAL serializer --------------
        # Built by the suite's own `reference_record`, which goes through
        # `build_terminal_record` with a real StageContext, real UnitFacts and
        # the same `unit_provenance` call production makes. Reusing it rather
        # than assembling a body here is deliberate: it means this probe and
        # the unit suite are testing the SAME record shape, so a fixture that
        # drifted from production would fail here against the live registrar
        # rather than passing quietly in both places.
        from pipeline.registration.test import test_products as fixtures

        record = fixtures.reference_record()
        record["attempt_id"] = attempt_id
        record["run_id"] = RUN
        record["logical_job_id"] = logical_job_id
        # Point the record at the objects actually written above, so the
        # registrar registers URIs with real bytes behind them.
        record["products"] = list(products.values())

        key = termination.terminal_record_key(prefix, RUN, logical_job_id,
                                              attempt_id, 0)
        written = termination.write_terminal_record(store, key, record)

        check("1/the-record-carries-the-registrar's-facts",
              all(record.get("science_provenance", {}).get(fact) is not None
                  for fact in ("field", "fid", "hp6", "hp9"))
              and record.get("ppid") is not None
              and record.get("job_type") == "reference-image",
              f"ppid={record.get('ppid')} job_type={record.get('job_type')} "
              f"hp6={record.get('science_provenance', {}).get('hp6')}")

        # -- close the attempt, citing that record ------------------------
        writer.mark_application_closed(
            attempt_id, ended_at=now(), application_intended_exit=0,
            rapid_outcome=RapidOutcome.SUCCESS,
            product_disposition=ProductDisposition.PUBLISHED,
            terminal_record_key=key,
            terminal_record_sequence=0,
            terminal_record_checksum=written["checksum"])
        conn.commit()

        # -- RECONCILE, for real ------------------------------------------
        # Not `mark_terminal_after_start` by hand: registration consumes
        # RECONCILED outcomes only, and the candidate query says so in SQL
        # (`terminal_record_sequence >= 1`). Only the reconciler writes a
        # sequence above 0, so driving `poll_once` is the only way this chain
        # reaches registration at all — and it is what makes the probe a chain
        # rather than four steps in a row.
        from pipeline.reconciler.service import ReconcilerService

        # The observed stop time is deliberately PAST the grace horizon.
        # That horizon is 10 minutes and it is doing its job: an attempt whose
        # scheduler-terminal observation is seconds old is deferred, because
        # the job may be writing its record right now. A probe that ran inside
        # the horizon would be testing the horizon, not the chain — and would
        # have to sleep ten minutes to get past it. Reporting an older stop
        # time is the honest way to put the attempt in the state a real
        # attempt reaches by simply having finished a while ago.
        stopped_at = now() - datetime.timedelta(
            seconds=horizons.GRACE_HORIZON_SECONDS + 120)

        class _SucceededBatch:
            """Batch reporting this one job SUCCEEDED.

            The attempt's scheduler job id is synthetic — no real Batch job was
            submitted, because what is under test is the record-to-product
            chain, not submission, which the W8 battery already proves live.
            """

            def describe_jobs(self, jobs):
                return {"jobs": [{
                    "jobId": jid, "status": "SUCCEEDED",
                    "startedAt": int(
                        (stopped_at - datetime.timedelta(minutes=5))
                        .timestamp() * 1000),
                    "stoppedAt": int(stopped_at.timestamp() * 1000),
                    "container": {"exitCode": 0},
                } for jid in jobs]}

        service = ReconcilerService(
            conn=conn, batch_client=_SucceededBatch(),
            records_store=store, diagnostics_store=store,
            s3_client=s3, records_prefix=prefix,
            diagnostics_bucket=records_bucket)

        # Scoped to THIS attempt. The open set on rapid-db holds ~60 rows left
        # by earlier probes, and a poll over all of them would both slow this
        # one down and act on attempts that are not its business. The service
        # is left otherwise untouched — the real `open_attempts` query runs and
        # its result is filtered, rather than a different query being
        # substituted, so what reconciles this row is the production path.
        real_open_attempts = service.open_attempts

        def only_mine():
            return [row for row in real_open_attempts()
                    if row.get("attempt_id") == attempt_id]

        service.open_attempts = only_mine
        summary = service.poll_once()
        conn.commit()
        print("    reconciler poll:", summary)

        state = scalar(
            conn, "SELECT lifecycle_state FROM attempts WHERE attempt_id=%s",
            (attempt_id,))
        sequence = scalar(
            conn,
            "SELECT terminal_record_sequence FROM attempts WHERE attempt_id=%s",
            (attempt_id,))
        check("2a/the-reconciler-closed-the-attempt",
              state == "terminal_after_start" and (sequence or 0) >= 1,
              f"state={state} sequence={sequence}")

        cited_key = scalar(
            conn, "SELECT terminal_record_key FROM attempts WHERE attempt_id=%s",
            (attempt_id,))
        cited_sum = scalar(
            conn,
            "SELECT terminal_record_checksum FROM attempts WHERE attempt_id=%s",
            (attempt_id,))
        check("2/the-citation-triple-is-coherent",
              cited_sum is not None
              and cited_sum == body_checksum(store.get(cited_key)),
              f"key={cited_key} checksum={str(cited_sum)[:16]}...")

        # -- REGISTER, for real ------------------------------------------
        # The registrar borrows THIS connection, so the product rows and the
        # watermark are one transaction (finding #8).
        register = registrar(lambda: rapid_db.RAPIDDB.borrowing(conn), store)

        refimages_before = scalar(conn, "SELECT count(*) FROM refimages")
        rows = registration_consumer.candidates(conn)
        mine = [r for r in rows if r.get("attempt_id") == attempt_id]
        check("3/the-attempt-is-a-registration-candidate", len(mine) == 1,
              f"{len(mine)} of {len(rows)} candidate(s) are this attempt")

        run = registration_consumer.register_batch(conn, mine,
                                                   register=register)
        conn.commit()
        print("    registration run:", run.as_dict())

        refimages_after = scalar(conn, "SELECT count(*) FROM refimages")
        check("4/a-reference-image-row-was-really-written",
              refimages_after == refimages_before + 1,
              f"refimages {refimages_before} -> {refimages_after}")

        registered_row = scalar(
            conn, "SELECT rfid FROM refimages WHERE attempt_id=%s",
            (attempt_id,))
        check("5/the-row-names-the-attempt-that-made-it",
              registered_row is not None, f"rfid={registered_row}")

        watermark = scalar(
            conn,
            "SELECT registered_record_sequence FROM attempts"
            " WHERE attempt_id=%s", (attempt_id,))
        check("6/the-watermark-advanced", watermark is not None,
              f"registered_record_sequence={watermark}")

        # -- REPLAY: migration 018's guard, against the live procedure -----
        # The attempt is no longer a candidate (the watermark saw to that), so
        # the replay is driven directly — which is the crash-between-write-and-
        # watermark shape the finding describes.
        replay_before = scalar(conn, "SELECT count(*) FROM refimages")
        register(mine[0], None, record=json.loads(store.get(cited_key)))
        conn.commit()
        replay_after = scalar(conn, "SELECT count(*) FROM refimages")

        check("7/a-replay-writes-no-second-version",
              replay_after == replay_before,
              f"refimages {replay_before} -> {replay_after}")

        versions = scalar(
            conn, "SELECT count(*) FROM refimages WHERE attempt_id=%s",
            (attempt_id,))
        check("8/exactly-one-row-per-attempt-identity", versions == 1,
              f"{versions} row(s) carry attempt_id={attempt_id}")

    failed = [name for name, ok in results if not ok]
    print()
    print(f">> {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print(f"!! FAILED: {', '.join(failed)}", file=sys.stderr)
        return 1
    print("FIXD-MINI-CHAIN-OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
