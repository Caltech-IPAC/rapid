"""
This script is executed on Streetfighter, outside of a container, with python3 command.
sqlite3 --header /home/ubuntu/work/test_20250530/roman_tessellation_nside512.db
select min(rtid),max(rtid) from skytiles;

min(rtid)|max(rtid)
1|6291458

"""
import os
import database.modules.utils.roman_tessellation_db as sqlite

swname = "compute_fields.py"
swvers = "1.0"


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

    field = i + 1


    # Get sky positions of center and four corners of sky tile.

    roman_tessellation_db.get_center_sky_position(field)
    ra0_field = roman_tessellation_db.ra0
    dec0_field = roman_tessellation_db.dec0
    roman_tessellation_db.get_corner_sky_positions(field)
    ra1_field = roman_tessellation_db.ra1
    dec1_field = roman_tessellation_db.dec1
    ra2_field = roman_tessellation_db.ra2
    dec2_field = roman_tessellation_db.dec2
    ra3_field = roman_tessellation_db.ra3
    dec3_field = roman_tessellation_db.dec3
    ra4_field = roman_tessellation_db.ra4
    dec4_field = roman_tessellation_db.dec4


    # Write sky positions to output file for ingesting into PostgreSQL database.

    fh.write(f"{ra1_field}\t{dec1_field}\t{ra2_field}\t{dec2_field}\t{ra3_field}\t{dec3_field}\t{ra4_field}\t{dec4_field}\t{ra0_field}\t{dec0_field}\n")


fh.close()

exit(0)
