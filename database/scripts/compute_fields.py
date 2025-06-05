"""
This script is executed on Streetfighter, outside of a container, with python3 command.
sqlite3 --header /home/ubuntu/work/test_20250530/roman_tessellation_nside512.db
select min(rtid),max(rtid) from skytiles;

min(rtid)|max(rtid)
1|6291458

"""
import os
import healpy as hp

import database.modules.utils.roman_tessellation_db as sqlite

swname = "compute_fields.py"
swvers = "1.0"

level6 = 6
nside6 = 2**level6

level9 = 9
nside9 = 2**level9


# Ensure sqlite database that defines the Roman sky tessellation is available.

roman_tessellation_dbname = os.getenv('ROMANTESSELLATIONDBNAME')

if roman_tessellation_dbname is None:

    print("*** Error: Env. var. ROMANTESSELLATIONDBNAME not set; quitting...")
    exit(64)

roman_tessellation_db = sqlite.RomanTessellationNSIDE512()

output_file = swname.replace(".py",".dat")

try:
    fh = open(output_file, 'w', encoding="utf-8")
except:
    print(f"*** Error: Could not open output file {output_file} for writing; quitting...")
    exit(64)


for i in range(6291458):


    # Field number is a one-based index.

    field = i + 1


    # Get sky positions of center and four corners of sky tile.

    roman_tessellation_db.get_center_sky_position(field)
    ra0 = roman_tessellation_db.ra0
    dec0 = roman_tessellation_db.dec0
    roman_tessellation_db.get_corner_sky_positions(field)
    ra1 = roman_tessellation_db.ra1
    dec1 = roman_tessellation_db.dec1
    ra2 = roman_tessellation_db.ra2
    dec2 = roman_tessellation_db.dec2
    ra3 = roman_tessellation_db.ra3
    dec3 = roman_tessellation_db.dec3
    ra4 = roman_tessellation_db.ra4
    dec4 = roman_tessellation_db.dec4


    # Compute level-6 healpix index (NESTED pixel ordering).

    hp6 = hp.ang2pix(nside6,ra0,dec0,nest=True,lonlat=True)


    # Compute level-9 healpix index (NESTED pixel ordering).

    hp9 = hp.ang2pix(nside9,ra0,dec0,nest=True,lonlat=True)


    # Write sky positions to output file for ingesting into PostgreSQL database.

    fh.write(f"{field}\t{hp6}\t{hp9}\t{ra1}\t{dec1}\t{ra2}\t{dec2}\t{ra3}\t{dec3}\t{ra4}\t{dec4}\t{ra0}\t{dec0}\n")


fh.close()

exit(0)
