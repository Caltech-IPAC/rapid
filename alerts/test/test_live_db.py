"""Section D of the test plan: live-database integration tests.

These need the DB environment variables (DBSERVER/DBPORT/DBNAME), DB
credentials (RAPID_DB_SECRET_ID, or DBUSER/DBPASS as a fallback), and AWS
credentials for the product bucket (the ambient role/profile -- never
AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY env vars). When those are
missing, or the database cannot be reached (VPN down, security group,
...), the tests emit a visible pytest WARNING and then skip -- a quiet
skip could hide the fact that the live invariants have not been checked
in a long time.

The priority test here is the pixel-convention sentinel (plan D17). The
+1 conversion in AlertDataProvider.get_cutouts() rests on an empirical
fact about the pipeline: sources.ra/dec equals the difference image's
TPV WCS evaluated at (xfit+1, yfit+1), i.e. xfit/yfit are 0-based. If
the pipeline ever changes convention, this test fails loudly and tells
us to update the conversion -- without it, that change would silently
shift every cutout by one pixel.

Also here: the end-to-end production round trip (D16, partial) -- a real
alert for a pinned source (ROUNDTRIP_SID, chosen for determinism),
assembled through AlertDataProvider (DB rows + S3-staged cutouts),
Avro-serialized, and decoded back -- and the KONA --kona-file wiring
against the same alert.

TODO (test plan, not yet implemented):
  D16 remainder: each clip's WCS puts the DB ra/dec at the stamp center
      (< 80 mas), cross-clip agreement < 1 mas
  D18 batch-level: batch_produce(pid) count equals the sources row count
      for that pid; decode a sample of the produced alerts
"""

import io
import json
import math
import os
import sys
import tempfile
import warnings
from pathlib import Path

import fastavro
import fitsio
import pytest

from alerts.cli import load_kona_predictions
from alerts.param_registry import VERSION
from alerts.produce import assemble_alert, load_schema, serialize_alert
from alerts.providers import (REF_MATCH_RADIUS_ARCSEC,
                              SS_CANDIDATE_SEP_ARCSEC, STAMP_HALF_WIDTH,
                              AlertDataProvider)
from wcs_eval import separation_mas, tpv_pixel_to_sky

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from database.modules.utils.rapid_db import get_db_credentials

DB_ENV_VARS = ("DBSERVER", "DBPORT", "DBNAME")

# how closely TPV(xfit+1, yfit+1) must reproduce sources.ra/dec; measured
# at 0.0 mas on real sources, so 20 mas is generous slack for float32
# column storage while staying far below the 106 mas one-pixel signature
CONVENTION_TOLERANCE_MAS = 20.0
N_SOURCES = 20


def connect_db():
    """One psycopg2 connection from the environment, 5 s timeout."""
    import psycopg2
    dbuser, dbpass = get_db_credentials()
    return psycopg2.connect(
        host=os.environ["DBSERVER"], port=os.environ["DBPORT"],
        dbname=os.environ["DBNAME"], user=dbuser,
        password=dbpass, connect_timeout=5)


# Availability is checked once per pytest run and cached: every dependent
# test would otherwise re-eat the 5 s connection timeout whenever the
# database is down, which adds up as section D grows.
_db_status = {}


def db_unavailable_reason():
    """None if the live DB is usable, else a human-readable reason."""
    if "reason" not in _db_status:
        missing = [v for v in DB_ENV_VARS if not os.getenv(v)]
        dbuser, dbpass = get_db_credentials()
        if dbuser is None or dbpass is None:
            missing.append("RAPID_DB_SECRET_ID (or DBUSER/DBPASS)")
        if missing:
            _db_status["reason"] = ("environment variables not set: "
                                    + ", ".join(missing))
        else:
            try:
                connect_db().close()
                _db_status["reason"] = None
            except Exception as exc:
                _db_status["reason"] = f"database not reachable: {exc}"
    return _db_status["reason"]


def test_database_reachable():
    """Connectivity canary. This is the test that WARNS when the live-DB
    suite cannot run, so the warning is attributed to the connection
    itself -- not to whichever invariant test happened to request the
    db_conn fixture first. The invariant tests then skip quietly,
    pointing here."""
    reason = db_unavailable_reason()
    if reason:
        warnings.warn("live-DB tests NOT run: " + reason)
        pytest.skip(reason)


@pytest.fixture(scope="module")
def db_conn():
    """Skips (quietly -- test_database_reachable owns the warning) when
    the environment is incomplete or the database is down."""
    reason = db_unavailable_reason()
    if reason:
        pytest.skip(f"{reason} (see test_database_reachable)")
    conn = connect_db()
    yield conn
    conn.close()


def fetch_s3_fits_header(url):
    """Read just the primary FITS header of an s3:// file via ranged GETs
    (the products are ~67 MB; the header is a few 2880-byte blocks)."""
    import boto3
    bucket, _, key = url[len("s3://"):].partition("/")
    client = boto3.client("s3")
    nblocks = 4
    while True:
        raw = client.get_object(
            Bucket=bucket, Key=key,
            Range=f"bytes=0-{nblocks * 2880 - 1}")["Body"].read()
        cards = [raw[i:i + 80].decode("ascii", errors="replace")
                 for i in range(0, len(raw), 80)]
        if any(c.startswith("END") for c in cards):
            break
        nblocks *= 2                      # header longer than the fetch
    header = {}
    for card in cards:
        if card.startswith("END"):
            break
        if "=" not in card[:10]:
            continue
        name = card[:8].strip()
        value = card[10:].split("/")[0].strip().strip("'").strip()
        try:
            header[name] = float(value)
        except ValueError:
            header[name] = value
    return header


# ---------------------------------------------------------------------------
# End-to-end production round trip (D16, partial)
# ---------------------------------------------------------------------------

class _DBShim:
    """Duck-types RAPIDDB for AlertDataProvider: just .conn.cursor()."""

    def __init__(self, conn):
        self.conn = conn


@pytest.fixture(scope="module")
def live_provider(db_conn):
    return AlertDataProvider(_DBShim(db_conn))


# Pinned round-trip source (Emily, Aug 2026): a well-populated detection
# (28-source object, near the ecliptic) kept fixed so runs are
# deterministic and comparable. If reprocessing ever drops it, this
# fixture fails loudly -- pick a new sid and update the pin.
ROUNDTRIP_SID = 2240034736


@pytest.fixture(scope="module")
def roundtrip_sid(db_conn):
    """The pinned sid, verified to still exist with a difference image."""
    with db_conn.cursor() as cur:
        cur.execute("""
            SELECT s.sid FROM sources s JOIN diffimages d ON s.pid = d.pid
            WHERE s.sid = %s
        """, (ROUNDTRIP_SID,))
        row = cur.fetchone()
    assert row, (
        f"pinned sid {ROUNDTRIP_SID} no longer exists with a difference "
        f"image (reprocessing?) -- choose a new sid and update "
        f"ROUNDTRIP_SID in {__name__}")
    return ROUNDTRIP_SID


@pytest.fixture(scope="module")
def live_alert(live_provider, roundtrip_sid):
    """One real alert, assembled once and shared by the tests below.

    Module-scoped because assembly stages the chip's three ~67 MB products
    from S3; the provider caches them per chip, so every dependent test
    reuses one download.
    """
    return assemble_alert(live_provider, roundtrip_sid)


def test_live_alert_roundtrip(live_alert, roundtrip_sid):
    """A real alert serializes and decodes intact, cutouts included."""
    schema = load_schema()
    blob = serialize_alert(live_alert, schema=schema)
    decoded = fastavro.schemaless_reader(io.BytesIO(blob), schema)

    assert decoded["diaSourceId"] == roundtrip_sid
    assert decoded["schemaVersion"] == VERSION
    side = 2 * STAMP_HALF_WIDTH + 1
    for field in ("cutoutDifference", "cutoutScience", "cutoutReference"):
        assert decoded[field], f"{field} missing from live alert"
        # the bytes must be a real FITS image of the expected stamp size
        fd, tmp = tempfile.mkstemp(suffix=".fits")
        try:
            os.close(fd)
            with open(tmp, "wb") as f:
                f.write(decoded[field])
            assert fitsio.read(tmp).shape == (side, side), field
        finally:
            os.unlink(tmp)
    print(f"live round trip: sid={roundtrip_sid}, {len(blob)} bytes")


def test_live_kona_file_wiring(live_provider, roundtrip_sid, tmp_path):
    """--kona-file plumbing over the live DB: a predictions file keyed by
    the real exposure yields populated ssMatches and the candidate flag."""
    detection = live_provider.get_detection(roundtrip_sid)
    sep_in = 0.5 * SS_CANDIDATE_SEP_ARCSEC
    dra = sep_in / 3600.0 / math.cos(math.radians(detection.dec))
    kona_file = tmp_path / "kona.json"
    kona_file.write_text(json.dumps({
        str(detection.expid): {"FAKE 1": [detection.ra + dra,
                                          detection.dec, 21.7]}}))

    provider = AlertDataProvider(
        _DBShim(live_provider.db.conn),
        kona_lookup=load_kona_predictions(kona_file).get)
    alert = assemble_alert(provider, roundtrip_sid)

    assert alert["diaSource"]["isSSCandidate"] is True
    assert alert["ssMatches"][0]["designation"] == "FAKE 1"
    assert alert["ssMatches"][0]["sep"] == pytest.approx(sep_in, abs=0.01)


def test_live_refcat_matching(live_provider, live_alert, roundtrip_sid):
    """Reference-catalog cross-match over the live DB and S3: the pinned
    chip's pid -> rfid -> refimcatalogs join resolves, the mosaic catalog
    stages and parses, and the alert carries the three-state arrays.

    Uses the module-scoped live_alert, so this shares the provider's
    staged catalog and images with the round-trip test.
    """
    detection = live_provider.get_detection(roundtrip_sid)
    matches = live_provider.get_ref_matches(detection)
    if matches is None:
        pytest.skip("no reference catalog registered for the pinned "
                    "chip's rfid (refimcatalogs); matching reported "
                    "'not run' as designed")

    stars, galaxies = matches
    for match_list, alert_key in ((stars, "refStarMatches"),
                                  (galaxies, "refGalaxyMatches")):
        packed = live_alert[alert_key]
        assert packed is not None
        assert [m.source_id for m in match_list] == \
            [m["sourceId"] for m in packed]
        # nearest-first, all within the radius
        seps = [m["sep"] for m in packed]
        assert seps == sorted(seps)
        assert all(s <= REF_MATCH_RADIUS_ARCSEC for s in seps)
    print(f"live refcat match: sid={roundtrip_sid}, "
          f"{len(stars)} stars, {len(galaxies)} galaxies")


def test_db_positions_match_wcs_at_xfit_plus_one(db_conn):
    """The pixel-convention sentinel (see module docstring)."""
    with db_conn.cursor() as cur:
        # newest sources via the sid primary key (sources has no index on
        # pid, so anything pid-shaped would full-scan the table)
        cur.execute("""
            SELECT s.sid, s.xfit, s.yfit, s.ra, s.dec, d.filename
            FROM sources s JOIN diffimages d ON s.pid = d.pid
            ORDER BY s.sid DESC LIMIT %s
        """, (N_SOURCES,))
        rows = cur.fetchall()
    assert rows, "no sources in the database"

    headers = {}                          # one ranged fetch per distinct file
    worst = 0.0
    for sid, xfit, yfit, ra, dec, filename in rows:
        if filename not in headers:
            headers[filename] = fetch_s3_fits_header(filename)
        ra_wcs, dec_wcs = tpv_pixel_to_sky(headers[filename],
                                           xfit + 1.0, yfit + 1.0)
        sep = separation_mas(ra_wcs, dec_wcs, ra, dec)
        worst = max(worst, sep)
        assert sep < CONVENTION_TOLERANCE_MAS, (
            f"sid={sid}: ra/dec is {sep:.1f} mas from TPV(xfit+1, yfit+1) "
            f"-- has the pipeline's pixel convention changed? If sources "
            f"switched to 1-based coordinates, remove the +1 in "
            f"AlertDataProvider.get_cutouts().")
    print(f"convention sentinel: worst offset {worst:.2f} mas "
          f"over {len(rows)} sources")
