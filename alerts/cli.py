"""
File    : cli.py
Author  : Emily Everetts
Date    : 07/26

Command-line alert production.

Usage:

    python -m alerts.cli <source_id>                  # one alert
    python -m alerts.cli --pid <pid>                  # one diff image by pid
    python -m alerts.cli --exposure <expid> --sca <n> # one diff image by exposure + SCA
"""

import argparse
import contextlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

# Support both `python -m alerts.cli` (module) and `python cli.py` (script).
if __package__:
    from .catalog_readers import (DEFAULT_GAIA_SOURCE, DEFAULT_NED_SOURCE,
                                  GaiaReader, Hp6NedReader, LvsReader)
    from .produce import batch_produce, open_alert_archive, produce_alert
    from .providers import (FORCED_WINDOW_DAYS, MAX_SOURCES_PER_CHIP,
                            AlertDataProvider)
else:
    # Run directly as a script: no package context, so make the package
    # importable by its name and switch to absolute imports.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from alerts.catalog_readers import (DEFAULT_GAIA_SOURCE, DEFAULT_NED_SOURCE,
                                        GaiaReader, Hp6NedReader, LvsReader)
    from alerts.produce import (batch_produce, open_alert_archive,
                                      produce_alert)
    from alerts.providers import (FORCED_WINDOW_DAYS, MAX_SOURCES_PER_CHIP,
                                  AlertDataProvider)

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


def build_ned_reader(ned_source: str | None,
                     enabled: bool = True) -> "Hp6NedReader | None":
    """Open the local NED copy for the provider, or None to leave NED off.

    A copy that cannot be opened (no AWS credentials, wrong or unset
    AWS_DEFAULT_REGION, missing prefix) is logged as a warning and NED
    matching is left off, so the alert still assembles with a null
    nedMatches -- the same degrade-not-fail rule as an unreachable
    reference catalog. The cause is in the log line.

    Parameters
    ----------
    ned_source : str or None
        ``s3://bucket/prefix`` or a local directory holding
        ``objectdir_hp6/`` (see alerts/catalog_store.py). None means off.
    enabled : bool, optional
        False turns NED matching off regardless of `ned_source`.
    """
    if not enabled or not ned_source:
        return None
    try:
        return Hp6NedReader(ned_source)
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "NED matching off: could not open the NED copy at %s (%s: %s)",
            ned_source, type(exc).__name__, exc)
        return None


def build_lvs_reader(ned_source: str | None,
                     enabled: bool = True) -> "LvsReader | None":
    """Open the local NED-LVS table for the provider, or None to leave it off.

    The table lives under the same root as the NED copy (``<ned_source>/lvs/``,
    built by ``alerts.catalog_store ingest-lvs``). Same degrade-not-fail rule
    as build_ned_reader: a table that cannot be opened is logged as a
    warning and lvsMatches stays null.

    Parameters
    ----------
    ned_source : str or None
        The NED root, ``s3://bucket/prefix`` or a local directory. None
        means off.
    enabled : bool, optional
        False turns NED-LVS matching off regardless of `ned_source`.
    """
    if not enabled or not ned_source:
        return None
    try:
        return LvsReader(ned_source)
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "NED-LVS matching off: could not open the NED-LVS table under %s "
            "(%s: %s)", ned_source, type(exc).__name__, exc)
        return None


def build_gaia_reader(gaia_source: str | None,
                      enabled: bool = True) -> "GaiaReader | None":
    """Open the local Gaia copy for the provider, or None to leave it off.

    The copy has its own prefix (``alerts.catalog_store ingest-gaia``).
    Same degrade-not-fail rule as build_ned_reader: a copy that cannot be
    opened is logged as a warning and gaiaMatches stays null.

    Parameters
    ----------
    gaia_source : str or None
        The Gaia root, ``s3://bucket/prefix`` or a local directory. None
        means off.
    enabled : bool, optional
        False turns Gaia matching off regardless of `gaia_source`.
    """
    if not enabled or not gaia_source:
        return None
    try:
        return GaiaReader(gaia_source)
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Gaia matching off: could not open the Gaia copy at %s (%s: %s)",
            gaia_source, type(exc).__name__, exc)
        return None


PIPELINE_INI = "awsBatchSubmitJobs_launchSingleSciencePipeline.ini"


def read_rb_settings(config: Any) -> tuple[float | None, str | None]:
    """The real/bogus threshold and model identifier the alerts should carry.

    Both are facts about the science pipeline's configuration, so they are
    read from its .ini rather than typed per run: the threshold from
    ``[ALERTS] rb_threshold`` (must equal threshold.json of the model that
    scored the detections), the model from ``[ALERTS] rb_version`` or, when
    that is blank, the last path component of ``[RUBRAT] s3_prefix``.

    Parameters
    ----------
    config : configparser.ConfigParser
        The parsed pipeline .ini.

    Returns
    -------
    (float or None, str or None)
        ``(rb_threshold, rb_version)``; None where the .ini says nothing.
    """
    alerts = config["ALERTS"] if config.has_section("ALERTS") else {}
    raw = str(alerts.get("rb_threshold", "")).strip()
    threshold = float(raw) if raw else None
    version = str(alerts.get("rb_version", "")).strip() or None
    if version is None and config.has_section("RUBRAT"):
        prefix = str(config["RUBRAT"].get("s3_prefix", "")).strip().rstrip("/")
        version = prefix.rsplit("/", 1)[-1] or None
    return threshold, version


def default_rb_settings() -> tuple[float | None, str | None]:
    """read_rb_settings() over the pipeline .ini this checkout runs with:
    ``$RAPID_SW/cdf/`` if set, else the repository's ``cdf/``. (None, None)
    when no .ini is found, so the CLI still runs outside a pipeline tree."""
    import configparser
    roots = [os.environ["RAPID_SW"]] if os.environ.get("RAPID_SW") else []
    roots.append(str(Path(__file__).resolve().parents[1]))
    for root in roots:
        path = Path(root) / "cdf" / PIPELINE_INI
        if path.exists():
            config = configparser.ConfigParser()
            config.read(path)
            return read_rb_settings(config)
    return None, None


def make_provider(diff_flavor: str = "sfft",
                  kona_file: str | Path | None = None,
                  refcat: bool = True,
                  ned: bool = True,
                  ned_source: str | None = DEFAULT_NED_SOURCE,
                  lvs: bool = True,
                  gaia: bool = True,
                  gaia_source: str | None = DEFAULT_GAIA_SOURCE,
                  max_sources: int = MAX_SOURCES_PER_CHIP,
                  forced_phot: bool = True,
                  forced_window_days: float | None = FORCED_WINDOW_DAYS,
                  forced_store: str | None = None,
                  rb_threshold: float | None = None,
                  rb_version: str | None = None) -> AlertDataProvider:
    """Connect to the RAPID operations database and wrap it in a provider.
    (see providers.py)

    Parameters
    ----------
    diff_flavor : {"sfft", "zogy"}, optional
        Which differencing algorithm's image feeds ``cutoutDifference``.
    max_sources : int, optional
        Per chip, alert only on this many detections, the highest by
        signal-to-noise (TEMPORARY stand-in for a real-bogus cut, see
        providers.MAX_SOURCES_PER_CHIP). 0 disables the cap.
    forced_phot : bool, optional
        Run forced photometry per chip at the objects' positions over the
        previous images (alerts.forced_phot); on by default. When off,
        prvDiaForcedSources stays null.
    forced_window_days : float or None, optional
        Look-back window of that run; None (default) measures every
        previous image. A per-deployment choice (30 days is the natural
        first value); GBTDS is expected to need a short one.
    forced_store : str or None, optional
        Where the per-chip parquet results are kept and re-read from:
        ``s3://bucket/prefix`` or a local directory. None keeps them in
        memory only.
    kona_file : str or pathlib.Path, optional
        Nightly KONA predictions JSON (see load_kona_predictions). While
        None -- the default until KONA runs operationally -- solar-system
        association is off: ssMatches and isSSCandidate stay null.
    refcat : bool, optional
        Cross-match detections against the field's reference-image
        catalog (on by default; see providers.get_ref_matches). When off,
        refStarMatches and refGalaxyMatches stay null.
    ned : bool, optional
        Cross-match detections against the local copy of the NED object
        directory (alerts/catalog_readers.py; a few parquet reads per chip, no
        web service). On by default, like refcat. When off, nedMatches
        stays null.
    ned_source : str or None, optional
        Where the copy lives (``s3://...`` or a local directory); defaults
        to the pipeline's bucket. A copy that cannot be opened degrades to
        a logged warning and null nedMatches (see build_ned_reader). To
        use a different NED backend, construct AlertDataProvider with
        ``ned_reader=`` directly.
    lvs : bool, optional
        Cross-match detections against the local NED-LVS table under the
        same root (``<ned_source>/lvs/``; the whole table held in memory
        per process). On by default; when off, or when the table cannot be
        opened, lvsMatches stays null (see build_lvs_reader).
    gaia : bool, optional
        Cross-match detections against the local Gaia DR3 copy at
        `gaia_source` (its own prefix; a few parquet row groups per chip).
        On by default; when off, or when the copy cannot be opened,
        gaiaMatches stays null (see build_gaia_reader).
    gaia_source : str, optional
        Where the Gaia copy lives; defaults to the pipeline's bucket.

    Returns
    -------
    providers.AlertDataProvider
        Provider reading from the live RAPID database.

    Raises
    ------
    SystemExit
        If the database connection cannot be established (missing DB
        environment variables, or the database is unreachable).
    """
    kona_lookup = None
    if kona_file is not None:
        kona_lookup = load_kona_predictions(kona_file).get

    # NED is on by default; --no-ned turns it off, and a copy that cannot
    # be opened degrades to null nedMatches with a warning.
    ned_reader = build_ned_reader(ned_source, enabled=ned)
    lvs_reader = build_lvs_reader(ned_source, enabled=lvs)
    gaia_reader = build_gaia_reader(gaia_source, enabled=gaia)

    # RAPIDDB, from rapid/database/modules/utils/rapid_db.py
    repo_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo_root))
    db = RAPIDDB()
    # RAPIDDB reports connection failure by exit_code/conn=None rather than
    # raising; fail here with the actual problem.
    if getattr(db, "conn", None) is None or db.exit_code >= 64:
        raise SystemExit(
            "Cannot connect to the RAPID database: check that DBSERVER, "
            "DBPORT, DBNAME, DBUSER and DBPASS are set in this shell, and "
            "that this machine can reach the DB (VPN up / EC2 security "
            "group allows it)")
    return AlertDataProvider(db, diff_flavor=diff_flavor,
                             kona_lookup=kona_lookup, refcat=refcat,
                             ned_reader=ned_reader, lvs_reader=lvs_reader,
                             gaia_reader=gaia_reader,
                             max_sources=max_sources, forced_phot=forced_phot,
                             forced_window_days=forced_window_days,
                             forced_store=forced_store,
                             rb_threshold=rb_threshold, rb_version=rb_version)


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
    parser.add_argument("--kafka", action="store_true",
                        help="publish to Kafka ($KAFKA_BROKER, default "
                             "localhost:9092)")
    parser.add_argument("--topic", default="alerts", help="Kafka topic")
    parser.add_argument("--save", metavar="FILE",
                        help="also write the run's alerts to one Avro "
                             "object-container file (self-describing; read "
                             "back with fastavro.reader)")
    parser.add_argument("--no-compress", action="store_true",
                        help="store --save archives uncompressed; by "
                             "default they are deflate-compressed, the "
                             "MAST delivery format")
    parser.add_argument("--diff-flavor", choices=["sfft", "zogy"],
                        default="sfft",
                        help="which differencing algorithm's image feeds "
                             "cutoutDifference (default: %(default)s)")
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
    parser.add_argument("--no-ned", action="store_true",
                        help="skip the NED cross-match (nedMatches stays "
                             "null); on by default, reading the local NED "
                             "copy at --ned-source")
    parser.add_argument("--ned-source", metavar="PREFIX",
                        default=DEFAULT_NED_SOURCE,
                        help="the local NED copy: s3://bucket/prefix or a "
                             "directory holding objectdir_hp6/ and lvs/ (built "
                             "by alerts/catalog_store.py); default %(default)s")
    parser.add_argument("--no-lvs", action="store_true",
                        help="skip the NED-LVS cross-match (lvsMatches stays "
                             "null); on by default, reading lvs/nedlvs.parquet "
                             "under --ned-source")
    parser.add_argument("--no-gaia", action="store_true",
                        help="skip the Gaia cross-match (gaiaMatches stays "
                             "null); on by default, reading the local Gaia "
                             "copy at --gaia-source")
    parser.add_argument("--gaia-source", metavar="PREFIX",
                        default=DEFAULT_GAIA_SOURCE,
                        help="the local Gaia DR3 copy: s3://bucket/prefix or a "
                             "directory (built by alerts/catalog_store.py "
                             "ingest-gaia); default %(default)s")
    parser.add_argument("--max-sources", type=int, default=MAX_SOURCES_PER_CHIP,
                        metavar="N",
                        help="per chip, alert only on the N highest-S/N "
                             "detections (TEMPORARY stand-in for a real-bogus "
                             "cut); 0 = no cap (default: %(default)s)")
    parser.add_argument("--no-forced-phot", action="store_true",
                        help="skip per-chip forced photometry "
                             "(prvDiaForcedSources stays null); on by default")
    parser.add_argument("--forced-window-days", type=float, default=FORCED_WINDOW_DAYS,
                        metavar="DAYS",
                        help="look-back window of the forced-photometry run "
                             "(default: %(default)s); 0 = no window, every "
                             "previous image")
    parser.add_argument("--forced-store", metavar="PATH", default=None,
                        help="keep per-chip forced-photometry parquet files here "
                             "(s3://bucket/prefix or a directory) and re-read "
                             "them when present; default: memory only")
    ini_threshold, ini_version = default_rb_settings()
    parser.add_argument("--rb-threshold", type=float, default=ini_threshold,
                        metavar="SCORE",
                        help="real/bogus threshold carried as reliabilityThreshold "
                             "(default: [ALERTS] rb_threshold of the pipeline .ini, "
                             "currently %(default)s)")
    parser.add_argument("--rb-version", default=ini_version, metavar="MODEL",
                        help="real/bogus model identifier carried as reliabilityVersion "
                             "(default: from the pipeline .ini, currently %(default)s)")
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
    provider = make_provider(diff_flavor=args.diff_flavor,
                             kona_file=args.kona_file,
                             refcat=not args.no_refcat,
                             ned=not args.no_ned,
                             ned_source=args.ned_source,
                             lvs=not args.no_lvs,
                             gaia=not args.no_gaia,
                             gaia_source=args.gaia_source,
                             max_sources=args.max_sources,
                             forced_phot=not args.no_forced_phot,
                             forced_window_days=args.forced_window_days or None,
                             forced_store=args.forced_store,
                             rb_threshold=args.rb_threshold,
                             rb_version=args.rb_version)

    # Make producer, if kafka arg is True
    producer = None
    if args.kafka:
        from confluent_kafka import Producer
        producer = Producer({
            "bootstrap.servers": os.environ.get("KAFKA_BROKER",
                                                "localhost:9092"),
            "message.max.bytes": "15728640",
        })

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
