"""Section D of the test plan: live-database integration tests.

These need the DB environment variables (DBSERVER/DBPORT/DBNAME/DBUSER/
DBPASS) and AWS credentials for the product bucket. When those are
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

TODO (test plan, not yet implemented):
  D16 single-alert live check: produce a real alert, all three cutouts
      present, each clip's WCS puts the DB ra/dec at the stamp center
      (< 80 mas), cross-clip agreement < 1 mas
  D18 batch-level: batch_produce(pid) count equals the sources row count
      for that pid; decode a sample of the produced alerts
"""

import os
import warnings

import pytest

from wcs_eval import separation_mas, tpv_pixel_to_sky

DB_ENV_VARS = ("DBSERVER", "DBPORT", "DBNAME", "DBUSER", "DBPASS")
AWS_ENV_VARS = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")

# how closely TPV(xfit+1, yfit+1) must reproduce sources.ra/dec; measured
# at 0.0 mas on real sources, so 20 mas is generous slack for float32
# column storage while staying far below the 106 mas one-pixel signature
CONVENTION_TOLERANCE_MAS = 20.0
N_SOURCES = 20


def connect_db():
    """One psycopg2 connection from the environment, 5 s timeout."""
    import psycopg2
    return psycopg2.connect(
        host=os.environ["DBSERVER"], port=os.environ["DBPORT"],
        dbname=os.environ["DBNAME"], user=os.environ["DBUSER"],
        password=os.environ["DBPASS"], connect_timeout=5)


# Availability is checked once per pytest run and cached: every dependent
# test would otherwise re-eat the 5 s connection timeout whenever the
# database is down, which adds up as section D grows.
_db_status = {}


def db_unavailable_reason():
    """None if the live DB is usable, else a human-readable reason."""
    if "reason" not in _db_status:
        missing = [v for v in DB_ENV_VARS + AWS_ENV_VARS if not os.getenv(v)]
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
