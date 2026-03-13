import sys
import csv
import re
from astropy.table import QTable
from astropy.time import Time
import datetime as dt
import numpy as np


'''
PID980.sim.ecsv    # GPS,25586 rows, Aitoff is best
PID992.sim.ecsv    # GBTDS,288968 rows, Cartesian is best
PID994.sim.ecsv    # HLWAS,285108 rows, Aitoff is best
PID996.sim.ecsv    # HLTDS,54531 rows,Cartesian is best (in two different sky spots);
                   # change all <no name> to <no_name>
'''


# Input parameters.

input_files = ["PID980.sim.ecsv","PID992.sim.ecsv","PID994.sim.ecsv","PID996.sim.ecsv"]
surveys_dict = {980:"GPS",992:"GBTDS",994:"HLWAS",996:"HLTDS"}


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
            visit_id_dict[row["visit-id"]].append(row["visit-id"])

    return visit_id_dict


def parse_pid_file(input_file,visit_id_dict):

    pid_match = re.match(r'.+?(\d+).+', input_file)
    pid = pid_match.group(1)
    print(f"pid = {pid}")


    survey_qtable = QTable.read(input_file,format='ecsv')

    # Example visit_id = 0099401112183

    start_jd_obs = []
    end_jd_obs = []
    durations = []
    bandpasses = []
    pids = []
    ra = []
    dec =[]
    visit_id_list = []
    exposure_times = []
    ra_dec_dict = {}

    n_not_scheduled = 0

    for row in survey_qtable:

        #print(row)
        #print(f'{row["RA"]} {row["DEC"]}')

        try:
            ra_dec_dict[str(row["RA"])+str(row["DEC"])] += 1
        except:
            ra_dec_dict[str(row["RA"])+str(row["DEC"])] = 1

        vid = pid.zfill(5) + str(row["PLAN"]).zfill(2) + str(row["PASS"]).zfill(3) + str(row["SEGMENT"]).zfill(3)

        #print(f"vid = {vid}")


        try:

            visit_jds = visit_id_dict[vid]

            #print(f"visit_jds = {visit_jds}")

            start_jd_obs.append(visit_jds[0])
            end_jd_obs.append(visit_jds[1])
            visit_id_list.append(visit_jds[2])

            ra.append(row["RA"])
            dec.append(row["DEC"])
            durations.append(row["DURATION"])
            bandpasses.append(row["BANDPASS"])
            pids.append(pid)
            exposure_times.append(row["EXPOSURE_TIME"])

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

    return ra,dec,start_jd_obs,end_jd_obs,durations,bandpasses,pids,visit_id_list,exposure_times



def consolidate_surveys_in_schedule(ra1,dec1,visit_start_jd_obs1,visit_end_jd_obs1,durations1,bandpasses1,pids1,visits1,exposure_times1,
                                    ra2,dec2,visit_start_jd_obs2,visit_end_jd_obs2,durations2,bandpasses2,pids2,visits2,exposure_times2,
                                    ra3,dec3,visit_start_jd_obs3,visit_end_jd_obs3,durations3,bandpasses3,pids3,visits3,exposure_times3,
                                    ra4,dec4,visit_start_jd_obs4,visit_end_jd_obs4,durations4,bandpasses4,pids4,visits4,exposure_times4):
    visit_start_jds = []
    visit_end_jds = []
    durations = []
    ras = []
    decs = []
    pids = []
    visits = []
    bandpasses = []
    durations = []
    exposure_times = []

    for ra,dec,visit_start_jd_obs,visit_end_jd_obs,duration,bandpass,pid,visit,exposure_time in zip(ra1,
                                                                                                    dec1,
                                                                                                    visit_start_jd_obs1,
                                                                                                    visit_end_jd_obs1,
                                                                                                    durations1,
                                                                                                    bandpasses1,
                                                                                                    pids1,
                                                                                                    visits1,
                                                                                                    exposure_times1):
        visit_start_jds.append(visit_start_jd_obs)
        visit_end_jds.append(visit_end_jd_obs)
        ras.append(ra)
        decs.append(dec)
        pids.append(pid)
        visits.append(visit)
        durations.append(duration)
        bandpasses.append(bandpass)
        exposure_times.append(exposure_time)

    for ra,dec,visit_start_jd_obs,visit_end_jd_obs,duration,bandpass,pid,visit,exposure_time in zip(ra2,
                                                                                                    dec2,
                                                                                                    visit_start_jd_obs2,
                                                                                                    visit_end_jd_obs2,
                                                                                                    durations2,
                                                                                                    bandpasses2,
                                                                                                    pids2,
                                                                                                    visits2,
                                                                                                    exposure_times2):
        visit_start_jds.append(visit_start_jd_obs)
        visit_end_jds.append(visit_end_jd_obs)
        ras.append(ra)
        decs.append(dec)
        pids.append(pid)
        visits.append(visit)
        durations.append(duration)
        bandpasses.append(bandpass)
        exposure_times.append(exposure_time)

    for ra,dec,visit_start_jd_obs,visit_end_jd_obs,duration,bandpass,pid,visit,exposure_time in zip(ra3,
                                                                                                    dec3,
                                                                                                    visit_start_jd_obs3,
                                                                                                    visit_end_jd_obs3,
                                                                                                    durations3,
                                                                                                    bandpasses3,
                                                                                                    pids3,
                                                                                                    visits3,
                                                                                                    exposure_times3):
        visit_start_jds.append(visit_start_jd_obs)
        visit_end_jds.append(visit_end_jd_obs)
        ras.append(ra)
        decs.append(dec)
        pids.append(pid)
        visits.append(visit)
        durations.append(duration)
        bandpasses.append(bandpass)
        exposure_times.append(exposure_time)

    for ra,dec,visit_start_jd_obs,visit_end_jd_obs,duration,bandpass,pid,visit,exposure_time in zip(ra4,
                                                                                                    dec4,
                                                                                                    visit_start_jd_obs4,
                                                                                                    visit_end_jd_obs4,
                                                                                                    durations4,
                                                                                                    bandpasses4,
                                                                                                    pids4,
                                                                                                    visits4,
                                                                                                    exposure_times4):
        visit_start_jds.append(visit_start_jd_obs)
        visit_end_jds.append(visit_end_jd_obs)
        ras.append(ra)
        decs.append(dec)
        pids.append(pid)
        visits.append(visit)
        durations.append(duration)
        bandpasses.append(bandpass)
        exposure_times.append(exposure_time)


    # Sort input arrays by time.

    np_visit_start_jds = np.array(visit_start_jds)

    sort_indices = np.argsort(np_visit_start_jds)


    schedule_rows = []


    # Headers for CVS output file.

    sr = ["index","visit","pid","survey","ra","dec","visit_start_jd","visit_end_jd",
          "exposure_duration[day]","bandpass","exposure_start_obs_jd","exposure_end_obs_jd",
          "exposure_time[s]","delta_jd"]
    schedule_rows.append(sr)


    # Loop over all exposures.

    #print("pids = ",pids)

    total_number_of_exposures = len(ras)
    print("total_number_of_exposures = ",total_number_of_exposures)

    visit_last = "N/A"
    exceedance_count = 0
    visit_count = 0
    n_exposure_exceedances_per_visit_dict = {}
    delta_jd = 0
    sum_last_delta_jd = 0

    for i in range(len(ras)):
        sort_index = sort_indices[i]
        visit = visits[sort_index]
        n_exposure_exceedances_per_visit_dict[visit] = 0


    j = 0

    for i in range(len(ras)):

        sort_index = sort_indices[i]

        ra = ras[sort_index]
        dec = decs[sort_index]
        pid = int(pids[sort_index])
        survey = surveys_dict[pid]
        visit = visits[sort_index]
        visit_start_jd = visit_start_jds[sort_index]
        visit_end_jd = visit_end_jds[sort_index]
        duration = durations[sort_index] / (3600.0 * 24.0)
        bandpass = bandpasses[sort_index]
        exposure_time = exposure_times[sort_index]

        print(f"i,visit,visit_last = {i},{visit},{visit_last}")

        if visit == visit_last:
            exposure_start_obs_jd = exposure_end_obs_jd
            exposure_end_obs_jd = exposure_start_obs_jd + duration
        else:
            exposure_start_obs_jd = visit_start_jd
            exposure_end_obs_jd = exposure_start_obs_jd + duration
            visit_last = visit
            visit_count +=1
            sum_last_delta_jd += delta_jd
            print(f"i,sum_last_delta_jd = {i},{sum_last_delta_jd}")

        delta_jd = exposure_end_obs_jd - visit_end_jd


        if exposure_end_obs_jd >= visit_end_jd:

            print(f"*** Error: Exposure end obs. JD ({exposure_end_obs_jd}) is greater than" +\
                  f"or equal to visit end JD ({visit_end_jd}); quitting....")
            exceedance_count += 1
            n_exposure_exceedances_per_visit_dict[visit] += 1

        else:

            j += 1
            sr = [j,visit,pid,survey,ra,dec,visit_start_jd,visit_end_jd,
                  duration,bandpass,exposure_start_obs_jd,exposure_end_obs_jd,
                  exposure_time,delta_jd]
            print(sr)
            schedule_rows.append(sr)

        i += 1

    total_num_exposures = i
    total_num_exposures_that_fit = j

    print(f"total_num_exposures,visit_count,total_num_exposures_that_fit,exceedance_count = {total_num_exposures},{visit_count},{total_num_exposures_that_fit},{exceedance_count}")


    # Output CSV file of exposure timeline versus observation JD.

    file_path = 'consolidated_roman_scheduled_observations.csv'

    with open(file_path, mode='w', newline='') as file:
        writer = csv.writer(file)
        # Write the header and rows
        writer.writerows(schedule_rows)

    print(f"CSV file '{file_path}' created successfully using csv.writer.")


    # Compute statistics for number of exposure exceedances.

    exceedance_count_check = 0
    n_visits_exceeded = 0
    n_exposure_exceedances_list = []
    for key in n_exposure_exceedances_per_visit_dict:
       if n_exposure_exceedances_per_visit_dict[key] > 0:
           n_exposure_exceedances = n_exposure_exceedances_per_visit_dict[key]
           exceedance_count_check += n_exposure_exceedances
           n_visits_exceeded += 1
           n_exposure_exceedances_list.append(n_exposure_exceedances)
           print(f"visit,n_exposure_exceedances = {key},{n_exposure_exceedances_per_visit_dict[key]}")

    print(f"exceedance_count_check = {exceedance_count_check}")
    print(f"n_visits_exceeded = {n_visits_exceeded}")

    np_n_exposure_exceedances_list = np.array(n_exposure_exceedances_list)

    mean_n = np.mean(np_n_exposure_exceedances_list)
    std_n = np.std(np_n_exposure_exceedances_list)
    median_n = np.median(np_n_exposure_exceedances_list)
    min_n = np.min(np_n_exposure_exceedances_list)
    max_n = np.max(np_n_exposure_exceedances_list)

    print("\nExposure-number-exceedance statistics:")
    print(f"mean_n = {mean_n}")
    print(f"std_n = {std_n}")
    print(f"median_n = {median_n}")
    print(f"min_n = {min_n}")
    print(f"max_n = {max_n}")


#-------------------------------------------------------------------------------------------------------------
# Main program.
#-------------------------------------------------------------------------------------------------------------

if __name__ == '__main__':


    # Parse schedule file.

    visit_id_dict = get_visit_id_dict()


    # Parse PID<nnn>.sim.ecsv

    ra1,dec1,start_jd_obs1,end_jd_obs1,durations1,bandpasses1,pids1,visits1,exposure_times1 = \
        parse_pid_file(input_files[0],visit_id_dict)
    ra2,dec2,start_jd_obs2,end_jd_obs2,durations2,bandpasses2,pids2,visits2,exposure_times2 = \
        parse_pid_file(input_files[1],visit_id_dict)
    ra3,dec3,start_jd_obs3,end_jd_obs3,durations3,bandpasses3,pids3,visits3,exposure_times3 = \
        parse_pid_file(input_files[2],visit_id_dict)
    ra4,dec4,start_jd_obs4,end_jd_obs4,durations4,bandpasses4,pids4,visits4,exposure_times4 = \
        parse_pid_file(input_files[3],visit_id_dict)


    '''
    Consolidate surveys in schedule.
    '''

    consolidate_surveys_in_schedule(ra1,dec1,start_jd_obs1,end_jd_obs1,durations1,bandpasses1,pids1,visits1,exposure_times1,
                                    ra2,dec2,start_jd_obs2,end_jd_obs2,durations2,bandpasses2,pids2,visits2,exposure_times2,
                                    ra3,dec3,start_jd_obs3,end_jd_obs3,durations3,bandpasses3,pids3,visits3,exposure_times3,
                                    ra4,dec4,start_jd_obs4,end_jd_obs4,durations4,bandpasses4,pids4,visits4,exposure_times4)


