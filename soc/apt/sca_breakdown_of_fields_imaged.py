import os
import csv
from datetime import datetime, timezone
from dateutil import tz
import time
import numpy as np
import matplotlib.pyplot as plt


to_zone = tz.gettz('America/Los_Angeles')

import database.modules.utils.roman_tessellation_db as sqlite
import modules.utils.rapid_planning_subs as pln

start_time_benchmark = time.time()
start_time_benchmark_at_start = start_time_benchmark


swname = "sca_breakdown_of_fields_imaged.py"
swvers = "1.0"

rapid_sw = "/code"
cfg_path = rapid_sw + "/cdf"


print("swname =", swname)
print("swvers =", swvers)


# Compute processing datetime (UT) and processing datetime (Pacific time).

datetime_utc_now = datetime.utcnow()
proc_utc_datetime = datetime_utc_now.strftime('%Y-%m-%dT%H:%M:%SZ')
datetime_pt_now = datetime_utc_now.replace(tzinfo=timezone.utc).astimezone(tz=to_zone)
proc_pt_datetime_started = datetime_pt_now.strftime('%Y-%m-%dT%H:%M:%S PT')

print("proc_utc_datetime =",proc_utc_datetime)
print("proc_pt_datetime_started =",proc_pt_datetime_started)


# Ensure sqlite database that defines the Roman sky tessellation is available.

roman_tessellation_dbname = os.getenv('ROMANTESSELLATIONDBNAME')

if roman_tessellation_dbname is None:

    print("*** Error: Env. var. ROMANTESSELLATIONDBNAME not set; quitting...")
    exit(64)

roman_tessellation_db = sqlite.RomanTessellationNSIDE512(debug=0)



#-------------------------------------------------------------------------------------------------------------
# Main program.
#-------------------------------------------------------------------------------------------------------------

if __name__ == '__main__':

    consolidated_apt_file = "consolidated_roman_scheduled_observations.csv"
    sca_breakdown_consolidated_apt_file = "sca_breakdown_consolidated_roman_scheduled_observations.csv"

    csvfile = open(sca_breakdown_consolidated_apt_file, 'w', newline='')
    writer = csv.writer(csvfile)

    with open(consolidated_apt_file, mode='r', newline='') as file:
        reader = csv.DictReader(file)

        j = 0

        for row in reader:

            ra = float(row["ra"])
            dec = float(row["dec"])
            pa = float(row["orient"])
            bandpass = row["bandpass"]


            # Output column header contains original header plus
            # SCA number, SCA center and corner sky positions, and the field number (sky tile)
            # that is associated with the SCA center sky position.

            if j == 0:
                line = list(row.keys()) + ["sca_no","sca_ra0","sca_dec0",
                                                    "sca_ra1","sca_dec1",
                                                    "sca_ra2","sca_dec2",
                                                    "sca_ra3","sca_dec3",
                                                    "sca_ra4","sca_dec4","sca_field"]
                writer.writerow(line)


            # For a given pointing (sky position and rotation angle),
            # compute the SCA center and corner sky positions.
            # Assume the entire Roman WFI FOV is projected into a tangent plane with no distortion.

            x0,y0, \
            naxis1,naxis2, \
            x1,y1, \
            x2,y2, \
            x3,y3, \
            x4,y4, \
            ras0,decs0, \
            ras1,decs1, \
            ras2,decs2, \
            ras3,decs3, \
            ras4,decs4, \
            x_wfi_center,y_wfi_center, \
            ra_wfi_center,dec_wfi_center = \
            pln.compute_sca_center_and_corner_sky_positions_from_wfi_center_sky_position(ra,dec,pa)


            # Loop over SCAs.

            for i in range(len(ras0)):

                sca_no = i + 1

                ra0 = ras0[i]
                dec0 = decs0[i]

                ra1 = ras1[i]
                dec1 = decs1[i]

                ra2 = ras2[i]
                dec2 = decs2[i]

                ra3 = ras3[i]
                dec3 = decs3[i]

                ra4 = ras4[i]
                dec4 = decs4[i]

                if ra0 < 0.0:
                    ra0 += 360.0

                if ra1 < 0.0:
                    ra1 += 360.0

                if ra2 < 0.0:
                    ra2 += 360.0

                if ra3 < 0.0:
                    ra3 += 360.0

                if ra4 < 0.0:
                    ra4 += 360.0


                # Compute field.

                roman_tessellation_db.get_rtid(ra0,dec0)
                field = roman_tessellation_db.rtid

                if field is None:
                    print(f"ra0,dec0,pa,field = {ra0},{dec0},{pa},{field}")
                    print("*** Error")
                    exit(64)


                # Compose output line and write it to output file.

                line = list(row[key] for key in row.keys()) + [sca_no,ra0,dec0,
                                                                      ra1,dec1,
                                                                      ra2,dec2,
                                                                      ra3,dec3,
                                                                      ra4,dec4,field]
                writer.writerow(line)

            j = j + 1


    # Close output file.

    csvfile.close()


    # Code-timing benchmark overall.

    end_time_benchmark = time.time()
    print("Total elapsed time in seconds =",
        end_time_benchmark - start_time_benchmark_at_start)


    # Termination.

    terminating_exitcode = 0

    print("terminating_exitcode =",terminating_exitcode)

    exit(terminating_exitcode)

