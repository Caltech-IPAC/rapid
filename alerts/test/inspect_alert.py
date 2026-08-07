"""Validate a produced alert archive: decode, check fields, verify each
cutout is a real FITS clip whose WCS centers on the source."""
import io
import sys
import warnings
warnings.filterwarnings("ignore")

import fastavro
import numpy as np
from astropy.io import fits
from astropy.wcs import WCS

path = sys.argv[1]
with open(path, "rb") as f:
    reader = fastavro.reader(f)
    writer_schema = reader.writer_schema
    alerts = list(reader)

print(f"file: {path}")
name = writer_schema.get("name") if isinstance(writer_schema, dict) else "?"
ns = writer_schema.get("namespace", "?") if isinstance(writer_schema, dict) else "?"
print(f"schema in header: {ns}.{name}")
print(f"records: {len(alerts)}")

a = alerts[0]
src = a["diaSource"]
print("\n-- top level --")
for k in ("schemaVersion", "diaSourceId", "diaObjectId"):
    if k in a:
        print(f"  {k} = {a[k]!r}")
print(f"  prvDiaSources: {len(a['prvDiaSources']) if a.get('prvDiaSources') else 0}")
print(f"  observation_reason = {a.get('observation_reason')!r}")
print(f"  target_name = {a.get('target_name')!r}")

print("\n-- diaSource --")
for k in ("diaSourceId", "ra", "dec", "x", "y", "snr", "isNegative", "band"):
    if k in src:
        print(f"  {k} = {src[k]!r}")

obj = a.get("diaObject")
if obj:
    print("\n-- diaObject --")
    for k in ("diaObjectId", "ra", "dec", "raErr", "decErr", "nDiaSources",
              "firstDiaSourceMjd", "lastDiaSourceMjd"):
        if k in obj:
            print(f"  {k} = {obj[k]!r}")

print("\n-- cutouts (each should be a 129x129 FITS clip; WCS centers on source) --")
for name in ("cutoutDifference", "cutoutScience", "cutoutTemplate"):
    blob = a.get(name)
    if blob is None:
        print(f"  {name}: None")
        continue
    is_fits = blob[:6] == b"SIMPLE"
    with fits.open(io.BytesIO(blob)) as hdul:
        data = hdul[0].data
        hdr = hdul[0].header
        wcs = WCS(hdr)
        ny, nx = data.shape
        # sky at the stamp center pixel (0-based center), then offset from source
        cx, cy = (nx - 1) / 2, (ny - 1) / 2
        sky = wcs.pixel_to_world(cx, cy)
        sep_mas = sky.separation(
            __import__("astropy.coordinates", fromlist=["SkyCoord"]).SkyCoord(
                src["ra"], src["dec"], unit="deg")).arcsec * 1000
        nan_frac = 100 * np.isnan(data).mean()
    print(f"  {name}: {len(blob)} bytes, FITS={is_fits}, {data.shape}, "
          f"{hdr.get('CTYPE1')}, center->source {sep_mas:.1f} mas, "
          f"NaN {nan_frac:.1f}%")
