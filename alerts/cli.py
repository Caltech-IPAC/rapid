"""
File    : cli.py
Author  : Emily Everetts
Date    : 07/26

Command-line alert production.

Usage:

    python -m alerts.cli <source_id>                  # one alert
    python -m alerts.cli --pid <pid>                  # one diff image by pid
    python -m alerts.cli --exposure <expid> --sca <n> # one diff image by exposure + SCA

THIS COMMAND ASSEMBLES ALERTS; IT DOES NOT PUBLISH THEM (brief E, rule 14).
`--kafka` used to construct a producer and send from here, which made this a
second route onto the wire beside the pipeline — bypassing the outbox, the
delivery policy, and the pinned schema version that makes a resend
byte-identical. It now fails with a message naming the real route. Use
`--save FILE` to write the assembled alerts to an Avro archive.

The only delivery route is the outbox: `pipeline/stages/alert_production.py`
commits packets to `alert_outbox` inside its confirmation transaction, and the
`rapid-publisher` service delivers them.
"""

import argparse
import contextlib
import json
import logging
import sys
from pathlib import Path

# Support both `python -m alerts.cli` (module) and `python cli.py` (script).
if __package__:
    from .produce import batch_produce, open_alert_archive, produce_alert
    from .providers import AlertDataProvider
else:
    # Run directly as a script: no package context, so make the package
    # importable by its name and switch to absolute imports.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from alerts.produce import (batch_produce, open_alert_archive,
                                      produce_alert)
    from alerts.providers import AlertDataProvider

from database.modules.utils.rapid_db import RAPIDDB


def load_kona_predictions(path: str | Path) -> dict[int, dict]:
    """Load a nightly KONA predictions file for the alert provider.

    The file is JSON of shape ``{expid: {designation: [ra, dec, vmag]}}``
    (one entry per exposure KONA ran on; vmag may be null). Produced by a
    local run of modules/solarsystem/rapid_kona.py for now -- KONA is not
    running on the operational system yet.

    Parameters
    ----------
    path : str or pathlib.Path
        The predictions JSON file.

    Returns
    -------
    dict
        expid (int) -> ``{designation: [ra, dec, vmag]}``.
    """
    with open(path) as f:
        data = json.load(f)
    # JSON object keys are strings; the provider looks up by integer expid
    return {int(expid): predictions for expid, predictions in data.items()}


def make_provider(db=None, kona_file: str | Path | None = None,
                  refcat: bool = True) -> AlertDataProvider:

    """Connect to the RAPID operations database and wrap it in a provider.
    (see providers.py)

    `db` is the caller's handle — `pipeline.stages.alert_production` passes
    `RAPIDDB.borrowing(<the attempt's own connection>)`, because a Batch
    payload's connection facts live in the parameter tree and its resolved
    context, NOT in DBSERVER/DBPORT/DBNAME environment variables: the
    env-only default below is the interactive CLI's path, and it exits 64
    inside every Batch job (found live at the mock's first alert wave —
    the same env-only-contract class as the registrar's records bucket).

    Parameters
    ----------
    kona_file : str or pathlib.Path, optional
        Nightly KONA predictions JSON (see load_kona_predictions). While
        None -- the default until KONA runs operationally -- solar-system
        association is off: ssMatches and isSSCandidate stay null.
    refcat : bool, optional
        Cross-match detections against the field's reference-image
        catalog (on by default; see providers.get_ref_matches). When off,
        refStarMatches and refGalaxyMatches stay null.

    Returns
    -------
    providers.AlertDataProvider
        Provider reading from the given handle, or from a fresh
        environment-configured connection when none is given (CLI usage).

    Raises
    ------
    SystemExit
        Only on the no-argument path, if the database connection cannot be
        established (missing DB environment variables, or the database is
        unreachable).
    """
    kona_lookup = None
    if kona_file is not None:
        kona_lookup = load_kona_predictions(kona_file).get

    if db is None:
        # RAPIDDB, from rapid/database/modules/utils/rapid_db.py
        repo_root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(repo_root))
        db = RAPIDDB()

        if getattr(db, "conn", None) is None or db.exit_code >= 64:
            raise SystemExit(
                "Cannot connect to the RAPID database: check that DBSERVER, "
                "DBPORT, DBNAME are set in this shell, that credentials are "
                "available via RAPID_DB_SECRET_ID (or DBUSER/DBPASS as a "
                "fallback), and that this machine can reach the DB (VPN up / "
                "EC2 security group allows it)")

    return AlertDataProvider(db, kona_lookup=kona_lookup, refcat=refcat)


def main(argv: list[str] | None = None) -> int:
    """Run alert-production command line options

    Parameters
    ----------
    argv : list of str, optional
        Command-line arguments. None takes input from sys.argv

    Returns
    -------
    int
        Process exit status: 0 on success.
    """
    parser = argparse.ArgumentParser(
        description="Produce RAPID alerts. The selector implies the mode: "
                    "a source ID produces one alert; --pid or "
                    "--exposure/--sca batch-produce a difference image.")
    parser.add_argument("sid", type=int, nargs="?",
                        help="source ID (sources.sid): produce one alert")
    parser.add_argument("--pid", type=int,
                        help="batch-produce every source on this "
                             "difference image (diffimages.pid)")
    parser.add_argument("--exposure", type=int, metavar="EXPID",
                        help="batch-produce one exposure + SCA (needs "
                             "--sca); uses the newest vbest>0 processing")
    parser.add_argument("--sca", type=int,
                        help="SCA number (1-18), goes with --exposure")
    # RETAINED ONLY TO FAIL CLOSED, and deliberately not deleted outright: the
    # flag is in operators' fingers and in older runbooks, and a removed flag
    # produces "unrecognized arguments" — which reads as a CLI version skew and
    # invites someone to find a way around it. Naming the replacement in an
    # error message is what actually redirects the person typing it.
    parser.add_argument("--kafka", action="store_true",
                        help="REMOVED: publishing is the rapid-publisher "
                             "service's job (rule 14). This flag now fails "
                             "with an explanation; use --save to write an "
                             "Avro archive instead")
    parser.add_argument("--topic", default="alerts", help="Kafka topic")
    parser.add_argument("--save", metavar="FILE",
                        help="also write the run's alerts to one Avro "
                             "object-container file (self-describing; read "
                             "back with fastavro.reader)")
    parser.add_argument("--no-compress", action="store_true",
                        help="store --save archives uncompressed; by "
                             "default they are deflate-compressed, the "
                             "MAST delivery format")
    parser.add_argument("--kona-file", metavar="FILE", default=None,
                        help="nightly KONA predictions JSON "
                             "({expid: {designation: [ra, dec, vmag]}}); "
                             "without it, solar-system association is off "
                             "and ssMatches/isSSCandidate are null")
    parser.add_argument("--no-refcat", action="store_true",
                        help="skip the reference-catalog cross-match "
                             "(refStarMatches/refGalaxyMatches stay null); "
                             "on by default, staging one mosaic catalog "
                             "per reference image from S3")
    parser.add_argument("--log-level", default="WARNING",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="diagnostic verbosity on stderr; quiet by "
                             "default, use INFO for per-step progress "
                             "(default: %(default)s)")
    args = parser.parse_args(argv)

    # Log records go to stderr, the final result line goes to stdout
    logging.basicConfig(level=getattr(logging, args.log_level))

    # Validate arguments
    if (args.exposure is None) != (args.sca is None):
        parser.error("--exposure and --sca must be given together")
    selectors = (args.sid is not None) + (args.pid is not None) \
        + (args.exposure is not None)
    if selectors != 1:
        parser.error("give exactly one of: a source ID, --pid, or "
                     "--exposure with --sca")

    # Make provider
    provider = make_provider(kona_file=args.kona_file,
                             refcat=not args.no_refcat)

    # NO SEND ROUTE EXISTS HERE ANY MORE (brief E2, rule 14). This CLI used to
    # construct a real producer and publish, which made it a SECOND way onto
    # the wire beside the pipeline — with no outbox, no delivery policy check,
    # no pinned schema version and therefore no "identical bytes on resend".
    # A packet sent this way would be invisible to every clock and every
    # health view, and indistinguishable at the broker from a real one.
    #
    # The only route is now the outbox: the alert-production job commits
    # packets and `rapid-publisher` delivers them.
    # `pipeline/contract/test_alert_send_routes.py` asserts repo-wide that
    # production transport construction is reachable from the publisher entry
    # point alone, and this branch is why that assertion can hold.
    #
    # FAIL CLOSED, NOT SILENTLY IGNORE. A flag that was accepted and did
    # nothing would let someone believe they had published.
    producer = None
    if args.kafka:
        parser.error(
            "--kafka is removed: this CLI no longer publishes. Alert delivery "
            "goes through the transactional outbox — the alert-production job "
            "commits packets to `alert_outbox` in its confirmation "
            "transaction and the `rapid-publisher` service delivers them "
            "at-least-once with identical bytes on resend (rule 14). This "
            "command still ASSEMBLES alerts: use --save FILE to write them to "
            "an Avro archive.")

    # Alert archive (produce.py)
    archive_ctx = (open_alert_archive(
                       args.save,
                       codec="null" if args.no_compress else "deflate")
                   if args.save else contextlib.nullcontext())

    # Produce alerts, then remove any products staged from S3.
    with provider, archive_ctx as archive:
        if args.sid is not None:
            alert_bytes = produce_alert(provider, args.sid,
                                        producer=producer, topic=args.topic,
                                        archive=archive)
            print(f"Alert produced: {len(alert_bytes)} bytes")
        else:
            pid = (args.pid if args.pid is not None
                   else provider.resolve_pid(args.exposure, args.sca))
            count = batch_produce(provider, pid,
                                  producer=producer, topic=args.topic,
                                  archive=archive)
            print(f"pid={pid}: {count} alerts produced")

    # Save archive file to path
    if args.save:
        print(f"saved to {args.save}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
