import sys
import csv
import pandas as pd
from astropy.table import QTable, join
import matplotlib.pyplot as plt
from astropy.coordinates import SkyCoord
import astropy.units as u
import re

from astropy.time import Time
import datetime as dt

import numpy as np

import matplotlib.animation as animation

import matplotlib.colors as mcolors


# Methods.

def convert_to_jd(input):

    # Sample input: 2026.365:07:22:37

    dt_match = re.match(r'(\d+)\.(\d+)\:(\d+)\:(\d+)\:(\d+)', input)
    year_val = int(dt_match.group(1))
    day_of_year_val = int(dt_match.group(2))
    hour = int(dt_match.group(3))
    minute = int(dt_match.group(4))
    sec = int(dt_match.group(5))

    #print(f"====> {year_val}{day_of_year_val}{hour}{minute}{sec}")

    target_date = dt.datetime(year_val, 1, 1) + dt.timedelta(days=day_of_year_val - 1)
    target_datetime = target_date.replace(hour=hour, minute=minute, second=sec)

    t = Time(target_datetime, scale='utc')

    #print(f"date/time = {target_datetime}")
    #print(f"Julian Date using astropy: {t.jd}")

    return t.jd


def get_visit_id_dict():

    schedule_file = "schedule-R25279MHM1-run_id_R25279MHM1.csv"

    # (base) laher@Russs-MacBook-Pro plots % cat schedule-R25279MHM1-run_id_R25279MHM1.csv|wc
    #   10153   10156 1415018

    schedule_file_ra = []
    schedule_file_dec = []
    visit_id_dict = {}

    with open(schedule_file, mode='r', newline='') as file:
        reader = csv.DictReader(file)
        for row in reader:
            #print(row) # Access data using row['column_name']
            schedule_file_ra.append(float(row["ra"]))
            schedule_file_dec.append(float(row["dec"]))
            #print(f"schedule file: ra, dec = {schedule_file_ra[0]}, {schedule_file_dec[0]}")

            start = row["start"]

            jd_start = convert_to_jd(start)

            end = row["end"]

            jd_end = convert_to_jd(end)

            visit_id_dict[row["visit-id"]] = []
            visit_id_dict[row["visit-id"]].append(jd_start)
            visit_id_dict[row["visit-id"]].append(jd_end)

    return visit_id_dict


def parse_pid_file(input_file,visit_id_dict,include_bandpasses):

    pid_match = re.match(r'.+?(\d+).+', input_file)
    pid = pid_match.group(1)
    print(f"pid = {pid}")


    survey_qtable = QTable.read(input_file,format='ecsv')

    # Example visit_id = 0099401112183

    start_obs = []
    end_obs = []
    durations = []
    bandpasses = []
    pids = []
    ra = []
    dec =[]
    ra_dec_dict = {}

    n_not_scheduled = 0

    for row in survey_qtable:

        #print(row)
        #print(f'{row["RA"]} {row["DEC"]}')

        if include_bandpasses[0] == "ALL":
            pass
        else:
            if row["BANDPASS"] not in include_bandpasses:
                continue

        try:
            ra_dec_dict[str(row["RA"])+str(row["DEC"])] += 1
        except:
            ra_dec_dict[str(row["RA"])+str(row["DEC"])] = 1

        vid = pid.zfill(5) + str(row["PLAN"]).zfill(2) + str(row["PASS"]).zfill(3) + str(row["SEGMENT"]).zfill(3)

        #print(f"vid = {vid}")


        try:

            start_end = visit_id_dict[vid]

            #print(f"start_end = {start_end}")

            start_obs.append(start_end[0])
            end_obs.append(start_end[1])


            ra.append(row["RA"] * u.deg)
            dec.append(row["DEC"] * u.deg)
            durations.append(row["DURATION"])
            bandpasses.append(row["BANDPASS"])
            pids.append(pid)

        except:

            #print(f"Not in schedule file: {vid}")

            n_not_scheduled += 1


    # The ra_dec_dict dictionary stores the number of exposures at each sky position.
    #print(ra_dec_dict)

    print(f"ra = {ra[0]}")

    print("type(ra) =", type(ra[0]))

    npts = len(ra)
    print(f"npts = {npts}")

    print(f"pid,n_not_scheduled = {pid},{n_not_scheduled}")

    return ra,dec,start_obs,end_obs,durations,bandpasses,pids



def animated_aitoff(ra1,dec1,jd_obs1,pids1,ra2,dec2,jd_obs2,pids2,ra3,dec3,jd_obs3,pids3,ra4,dec4,jd_obs4,pids4):

    jds = []
    ras = []
    decs = []
    pids = []

    for ra,dec,jd_obs,pid in zip(ra1,dec1,jd_obs1,pids1):
        jds.append(jd_obs)
        ras.append(ra)
        decs.append(dec)
        pids.append(pid)

    for ra,dec,jd_obs,pid in zip(ra2,dec2,jd_obs2,pids2):
        jds.append(jd_obs)
        ras.append(ra)
        decs.append(dec)
        pids.append(pid)

    for ra,dec,jd_obs,pid in zip(ra3,dec3,jd_obs3,pids3):
        jds.append(jd_obs)
        ras.append(ra)
        decs.append(dec)
        pids.append(pid)

    for ra,dec,jd_obs,pid in zip(ra4,dec4,jd_obs4,pids4):
        jds.append(jd_obs)
        ras.append(ra)
        decs.append(dec)
        pids.append(pid)


    # Sort input arrays by time.

    np_jds = np.array(jds)

    sort_indices = np.argsort(np_jds)


    # Convert coordinates to a SkyCoord object (optional, but good practice)
    coords = SkyCoord(ras, decs, frame='icrs')

    my_coords = []

    #print("pids = ",pids)

    total_number_of_exposures = len(ras)
    print("total_number_of_exposures = ",total_number_of_exposures)


    for i in range(len(ras)):

        sort_index = sort_indices[i]

        coord = coords[sort_index]

        pid = int(pids[sort_index])

        if pid == 980:
            color = [1,0,0,0.01]   # Red
        elif pid == 992:
            color = [0,1,0,0.01]   # Green
        elif pid == 994:
            color = [1,1,0,0.01]   # Yellow
        elif pid == 996:
            color = [0,0,1,0.01]   # Blue
        else:
            print(f"*** Error: pid has unexpected value (i={i},pid={pid}); quitting...")
            exit(0)

        my_coord = [coord,color,i]

        #print(my_coord)

        my_coords.append(my_coord)


    # Create the plot with the Aitoff projection
    fig = plt.figure(figsize=(10, 6))
    # Matplotlib accepts "aitoff" as a projection argument
    ax = plt.subplot(111, projection="aitoff")


    # Add grid and labels
    ax.grid(True)
    ax.set_title("Sky Coverage Map (Aitoff Projection)", pad=20)

    plt.figtext(0.3,
                0.05,
                'GPS: Red, GBTDS: Green, HLWAS: Yellow, HLTDS: Blue',
                bbox=dict(facecolor='lightblue', alpha=0.7, pad=2))


    # Customize tick labels (optional, to show degrees instead of radians)
    ax.set_xticklabels(['150°', '120°', '90°', '60°', '30°', '0°',
                        '-30°', '-60°', '-90°', '-120°', '-150°'])

    # Example: Moving point
    point = ax.scatter([], [], c = "purple", s=10)

    lons, lats, clrs = [], [], []




    num_exposures_to_plot_per_frame = 1000

    frames = len(ras) // num_exposures_to_plot_per_frame
    rem = len(ras) % num_exposures_to_plot_per_frame



    #for i in range(0,frames):
    def animate_chunks_keep(i):

        start_slice = i * num_exposures_to_plot_per_frame
        end_slice = start_slice + num_exposures_to_plot_per_frame

        if i == frames - 1: end_slice += rem

        s_coords = my_coords[start_slice:end_slice]

        for s_coord in s_coords:

            coord = s_coord[0]

            # Update logic: long/lat in radians
            lon = float(coord.ra.wrap_at(180*u.deg).radian)
            lat = float(coord.dec.radian)

            color = s_coord[1]
            exposure_number = s_coord[2]

            lons.append(lon)
            lats.append(lat)
            clrs.append(color)

        point.set_offsets(np.c_[lons, lats])
        point.set_facecolor(clrs)

        # Print the current frame number to the console
        print(f"Current exposure: {exposure_number} ({i} {start_slice} {end_slice})", flush=True)

        return point,


    print("Animation ready to begin: Hit return key to continue...")
    print("(After animation has ended, close graphics window to terminate.)")

    for line in sys.stdin:
        if line.rstrip() is not None:
            break
        print(f'Input : {line}')


    ani = animation.FuncAnimation(fig,
                                  animate_chunks_keep,
                                  interval=1,
                                  frames=frames,
                                  blit=True,
                                  repeat=False)

    # To save as an MP4 file (requires ffmpeg)
    # On laptop, use brew install ffmpeg-full
    print("Saving animation as MP4 file...")
    ani.save('roman_space_telescope_observing_animation.mp4', fps=30, extra_args=['-vcodec', 'libx264']) #
    print("MP4 saved.")


    #plt.show()


#-------------------------------------------------------------------------------------------------------------
# Main program.
#-------------------------------------------------------------------------------------------------------------

if __name__ == '__main__':


    '''
    PID980.sim.ecsv    # GPS,25586 rows, Aitoff is best
    PID992.sim.ecsv    # GBTDS,288968 rows, Cartesian is best
    PID994.sim.ecsv    # HLWAS,285108 rows, Aitoff is best
    PID996.sim.ecsv    # HLTDS,54531 rows,Cartesian is best (in two different sky spots);
                       # change all <no name> to <no_name>
    '''


    input_files = ["PID980.sim.ecsv","PID992.sim.ecsv","PID994.sim.ecsv","PID996.sim.ecsv"]
    labels = ["GPS","GBTDS","HLWAS","HLTDS"]
    #include_bandpasses = ["F087"]
    include_bandpasses = ["ALL"]

    visit_id_dict = get_visit_id_dict()

    ra1,dec1,start_obs1,end_obs1,durations1,bandpasses1,pids1 = parse_pid_file(input_files[0],visit_id_dict,include_bandpasses)
    ra2,dec2,start_obs2,end_obs2,durations2,bandpasses2,pids2 = parse_pid_file(input_files[1],visit_id_dict,include_bandpasses)
    ra3,dec3,start_obs3,end_obs3,durations3,bandpasses3,pids3 = parse_pid_file(input_files[2],visit_id_dict,include_bandpasses)
    ra4,dec4,start_obs4,end_obs4,durations4,bandpasses4,pids4 = parse_pid_file(input_files[3],visit_id_dict,include_bandpasses)


    # Convert coordinates to a SkyCoord object (optional, but good practice)
    coords1 = SkyCoord(ra1, dec1, frame='icrs')
    coords2 = SkyCoord(ra2, dec2, frame='icrs')
    coords3 = SkyCoord(ra3, dec3, frame='icrs')
    coords4 = SkyCoord(ra4, dec4, frame='icrs')

    # Create the plot with the Aitoff projection
    plt.figure(figsize=(10, 6))
    # Matplotlib accepts "aitoff" as a projection argument
    ax = plt.subplot(1,1,1, projection="aitoff")
    #ax = plt.subplot(1,1,1)

    # Plot the data (convert degrees to radians for the plot function)
    ax.plot(coords1.ra.wrap_at(180*u.deg).radian, coords1.dec.radian,
            '.', markersize=6, alpha=0.5, color='red', label=labels[0], markerfacecolor='red')
    ax.plot(coords2.ra.wrap_at(180*u.deg).radian, coords2.dec.radian,
            '.', markersize=6, alpha=0.5, color='green', label=labels[1], markerfacecolor='green')
    ax.plot(coords3.ra.wrap_at(180*u.deg).radian, coords3.dec.radian,
            '.', markersize=6, alpha=0.5, color='yellow', label=labels[2], markerfacecolor='yellow')
    ax.plot(coords4.ra.wrap_at(180*u.deg).radian, coords4.dec.radian,
            '.', markersize=6, alpha=0.5, color='blue', label=labels[3], markerfacecolor='blue')

    # Add grid and labels
    ax.grid(True)
    ax.set_title("Sky Coverage Map (Aitoff Projection)", pad=20)

    # Customize tick labels (optional, to show degrees instead of radians)
    ax.set_xticklabels(['150°', '120°', '90°', '60°', '30°', '0°',
                        '-30°', '-60°', '-90°', '-120°', '-150°'])

    # Call legend() to display the legend
    ax.legend(loc='upper right', bbox_to_anchor=(1.1, 1.0))

    # Output plot to PNG file.
    plt.savefig(f'roman_scheduled_obs_aitoff.png')

    # Display the plot
    #plt.show()



    '''
    Time-based plot
    '''

    np_start_obs = np.array(start_obs1)
    np_end_obs = np.array(end_obs1)
    np_mid_obs1 = (np_end_obs + np_start_obs) / 2.0

    np_start_obs = np.array(start_obs2)
    np_end_obs = np.array(end_obs2)
    np_mid_obs2 = (np_end_obs + np_start_obs) / 2.0

    np_start_obs = np.array(start_obs3)
    np_end_obs = np.array(end_obs3)
    np_mid_obs3 = (np_end_obs + np_start_obs) / 2.0

    np_start_obs = np.array(start_obs4)
    np_end_obs = np.array(end_obs4)
    np_mid_obs4 = (np_end_obs + np_start_obs) / 2.0

    np_durations1 = np.array(durations1)
    np_durations2 = np.array(durations2)
    np_durations3 = np.array(durations3)
    np_durations4 = np.array(durations4)


    # Set up the plot.

    plt.figure(figsize=(10, 6))

    # Add labels and a title
    plt.xlabel("Observation JD (day)")
    plt.ylabel("Exposure duration (seconds)")
    plt.title("Exposure Duration vs. Observation JD")

    ax = plt.subplot(1,1,1)
    ax.plot(np_mid_obs1,np_durations1,
            '.', markersize=6, alpha=0.5, color='red', label=labels[0], markerfacecolor='None')
    ax.plot(np_mid_obs2,np_durations2,
            '.', markersize=6, alpha=0.5, color='green', label=labels[1], markerfacecolor='None')
    ax.plot(np_mid_obs3,np_durations3,
            '.', markersize=6, alpha=0.5, color='yellow', label=labels[2], markerfacecolor='None')
    ax.plot(np_mid_obs4,np_durations4,
            '.', markersize=6, alpha=0.5, color='blue', label=labels[3], markerfacecolor='None')

    # Call legend() to display the legend
    ax.legend(loc='upper right', bbox_to_anchor=(1.0, 1.0))

    # Output plot to PNG file.
    plt.savefig(f'roman_scheduled_obs_dur_vs_jd.png')

    # Display the plot
    #plt.show()



    '''
    Histogram plot
    '''

    np_durations1 = np.array(durations1) / (3600.0 * 24.0)
    np_durations2 = np.array(durations2) / (3600.0 * 24.0)
    np_durations3 = np.array(durations3) / (3600.0 * 24.0)
    np_durations4 = np.array(durations4) / (3600.0 * 24.0)

    plt.figure(figsize=(10, 6))

    ax = plt.subplot(1,1,1)

    # Add labels and a title
    plt.xlabel("Observation JD (day)")
    plt.ylabel("Resource Use (days)")
    plt.title("Histogram of Observation JDs Weighted by Exposure Duration")

    # Use 'alpha' for transparency to see overlaps
    plt.hist(np_mid_obs1, weights=np_durations1, bins=1200, alpha=0.75, color='red', label=labels[0])
    plt.hist(np_mid_obs2, weights=np_durations2, bins=1200, alpha=0.5, color='green', label=labels[1])
    plt.hist(np_mid_obs3, weights=np_durations3, bins=1200, alpha=0.5, color='yellow', label=labels[2])
    plt.hist(np_mid_obs4, weights=np_durations4, bins=1200, alpha=0.5, color='blue', label=labels[3])

    # Call legend() to display the legend
    ax.legend(loc='upper right', bbox_to_anchor=(0.5, 1.0))

    # Output plot to PNG file.
    plt.savefig(f'roman_scheduled_obs_histogram.png')

    # Display the plot
    #plt.show()




    '''
    Animated Aitoff plot.
    '''

    animated_aitoff(ra1,dec1,start_obs1,pids1,
                    ra2,dec2,start_obs2,pids2,
                    ra3,dec3,start_obs3,pids3,
                    ra4,dec4,start_obs4,pids4)


