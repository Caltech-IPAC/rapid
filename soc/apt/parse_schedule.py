#!/usr/bin/env python
# coding: utf-8
"""
Utilities for parsing and combining Roman Space Telescope APT schedule files
into a single exposure-level schedule table.
"""

from astropy.table import Table
import numpy as np
from astropy import table
import glob
import os
from datetime import datetime, timedelta
from astropy.time import Time
import astropy.units as u
import warnings
import re
import pandas as pd
import argparse
import logging
logger = logging.getLogger(__name__)
logging.captureWarnings(True)

warnings.formatwarning = lambda message, category, *args, **kwargs: f'{category.__name__}: {message}\n'

def read_exp_csv_files(plan_dir: str) ->pd.DataFrame:
    """Read all APT ECSV files from a plan directory and stack them into one DataFrame.

    Parameters
    ----------
    plan_dir : str
        Path to the plan directory containing an ``APT_csv/`` subdirectory.

    Returns
    -------
    df_exp_all : pandas.DataFrame
        Stacked APT exposure table
    """
    exp_list = glob.glob(os.path.join(plan_dir, 'APT_csv/*.ecsv'))
    exp_arr_list = []
    for ifile in exp_list:
        # The HLTDS has target_name as <no name> but the files are space separated so this is interpreted
        #as two columns. Replace the space with and underscore before reading
        with open(ifile, encoding='utf-8') as ofile:
            text = ofile.read()
        text = re.sub(r"(?m) (?P<tag><no name>)$", r' <no_name>', text)
        tmp_tbdata_apt = Table.read(text, format='ascii.ecsv')
        tmp_tbdata_apt.remove_column('PA')
        #Get the program ID from the filename by looking for the first 3 consecutive digits
        filename = os.path.basename(ifile)
        tmp_tbdata_apt['program id'] = int(re.search(r"(\d{3})", filename).group(1))
        exp_arr_list.append(tmp_tbdata_apt)
    tbdata_exp_all = table.vstack(exp_arr_list)
    tbdata_exp_all.rename_columns(['DURATION', 'EXPOSURE_TIME'], ['exposure duration', 'exposure time'])
    tbdata_exp_all.rename_columns(tbdata_exp_all.colnames, [icolname.lower() for icolname in tbdata_exp_all.colnames])
    df_exp_all = tbdata_exp_all.to_pandas()
    df_exp_all['program name'] = df_exp_all['program id'].map(survey_pid_dict)
    return df_exp_all



def read_segment_schedule_file(plan_dir: str) -> pd.DataFrame:
    """Read a segment-level schedule CSV and return a DataFrame.

    Parameters
    ----------
    plan_dir : str
        Path to the directory containing the schedule file.

    Returns
    -------
    df_seg : pandas.DataFrame
        Parsed segment schedule table
    """
    segment_schedule_file = glob.glob(os.path.join(plan_dir,'schedule*.csv'))[0]
    logger.info(f'Log for {segment_schedule_file}')
    tbdata_seg = Table.read(os.path.join(plan_dir, segment_schedule_file), converters={'visit-id': str})
    tbdata_seg.rename_columns(
        [ 'duration', 'start', 'end', 'visit-id'],
        [ 'segment duration', 'segment start', 'segment end', 'segment-id']
        )
    df_seg = tbdata_seg.to_pandas()
    df_seg['pa'] = df_seg['assigned orient'].fillna(df_seg['nominal orient'])
    df_seg = df_seg.drop(columns=['assigned orient', 'nominal orient', 'gap', 'ra', 'dec'])

    # Parse the program, plan, pass, and segment from the visit-id
    #    * PPPPP - zero-padded 5-digit program ID
    #    * ee    - 2-digit execution plan ID
    #    * ppp   - 3-digit pass ID (one iteration of an APT Pass Plan; corresponds to a single repeat of a Survey Step in APT)
    #    * sss   - segment ID within that pass
    df_seg[['program id', 'plan', 'pass', 'segment']] = df_seg['segment-id'].str.extract(
        r'^(?P<program>\d{5})(?P<plan>\d{2})(?P<pass>\d{3})(?P<segment>\d{3})$'
    )
    #The regex outputs a string, convert the whole column to an integer
    for col in ['segment-id', 'program id', 'plan', 'pass', 'segment']:
        df_seg[col] = df_seg[col].astype(int)

    #Convert segment start and end times into datetime objects
    df_seg['segment start'] = pd.to_datetime(df_seg['segment start'], format="%Y.%j:%H:%M:%S")
    df_seg['segment end'] = pd.to_datetime(df_seg['segment end'], format="%Y.%j:%H:%M:%S")
    return df_seg


def combine_tables(df_seg: pd.DataFrame, df_exp_all: pd.DataFrame) -> pd.DataFrame:
    """Join the segment schedule table with the stacked APT exposure table on shared keys.

    Parameters
    ----------
    df_seg : pandas.DataFrame
        Segment schedule DataFrame
    df_exp_all : pandas.DataFrame
        Stacked APT exposure DataFrame

    Returns
    -------
    joint_df : pandas.DataFrame
        Outer-joined DataFrame on segment-id

    Warns
    -----
    UserWarning
        If exposure-level program/pass/segment combinations are absent from
        the segment schedule table. This is likely due to the APT files being overfilled
        and is safe to ignore
    UserWarning
        If segment schedule rows are absent from the exposure table.
    """

    joint_df = df_seg.merge(df_exp_all, how='outer', on=('segment', 'pass', 'program id', 'plan'), indicator=True)

    # Check if all segments in segment level file are in exposure level file and vis versa
    if np.any(joint_df['_merge']=='right_only'):
        warnings.warn('There are Exposure level Program/Pass/Segment combinations that are not present in the Segment Schedule Table')
    if np.any(joint_df['_merge']=='left_only'):
        warnings.warn('There are Segment Schedule Table level Program/Pass/Segment combinations that are not present in the Exposure Table')
    return joint_df


def remove_exposures_not_in_segment_schedule(joint_df: pd.DataFrame) -> pd.DataFrame:
    """Drop rows that are not present in both the segment schedule and exposure tables.

    Parameters
    ----------
    joint_df : pandas.DataFrame
        exposure level DataFrame

    Returns
    -------
    joint_df : pandas.DataFrame
        exposure level DataFrame that contains only rows with segment ids in both tables (equivalent to inner join)
    """
    joint_df = joint_df[joint_df['_merge']=='both']
    joint_df = joint_df.drop(columns=['_merge'])
    return joint_df


def add_exposure_start_col(joint_df: pd.DataFrame) -> pd.DataFrame:
    """Add an absolute start time for each exposure and remove any exposures that overrun the segment end time.

    Parameters
    ----------
    joint_df : pandas.DataFrame
        Joined segment and exposure level schedule table

    Returns
    -------
    joint_df : pandas.DataFrame
        Input table with an ``exposure start`` column added and any exposures
        that exceed their segment end time removed.

    Warns
    -----
    UserWarning
        If one or more exposures are dropped because they overrun the planned
        segment end time, issued once per iteration with the count of affected
        segments.
    """
    grouped_joint_df = joint_df.groupby('segment-id')
    cumsum_exp_duration = grouped_joint_df['exposure duration'].cumsum() #this is exp end but we want start
    cumsum_expstart_duration = cumsum_exp_duration - joint_df['exposure duration']
    joint_df['exposure start'] = joint_df['segment start'] + pd.to_timedelta(cumsum_expstart_duration, unit='s')
    # Remove exposures from end of segment when planned exposures overrun planned segment time
    diff_end = (grouped_joint_df['segment end'].last()-(grouped_joint_df['exposure start'].last() + pd.to_timedelta(grouped_joint_df['exposure time'].last(), unit='s')))
    ndrop = 0
    exceedance_dict = {}
    while np.any(diff_end.dt.total_seconds()<0):
        overtime_indx = diff_end.dt.total_seconds()<0
        for segment_id in overtime_indx.index[overtime_indx]:
            end_seg_indx = (joint_df['segment-id'][joint_df['segment-id']==segment_id]).index.max()
            joint_df.drop(end_seg_indx, inplace=True)
            if segment_id in exceedance_dict.keys():
                exceedance_dict[segment_id] = exceedance_dict[segment_id]+1
            else:
                exceedance_dict[segment_id] = 1
        grouped_joint_df = joint_df.groupby('segment-id')
        diff_end = (grouped_joint_df['segment end'].last()-(grouped_joint_df['exposure start'].last() + pd.to_timedelta(grouped_joint_df['exposure time'].last(), unit='s')))
        warning_message = f'dropping last exposure from {np.sum(overtime_indx)} segments because it over runs the segment end'
        warnings.warn(warning_message)
    exposure_exceedances_arr = np.array(list(exceedance_dict.values()))
    logger.info("\nExposure-number-exceedance statistics:")
    logger.info(f"mean_n = {exposure_exceedances_arr.mean()}")
    logger.info(f"std_n = {exposure_exceedances_arr.std()}")
    logger.info(f"median_n = {np.median(exposure_exceedances_arr)}")
    logger.info(f"min_n = {exposure_exceedances_arr.min()}")
    logger.info(f"max_n = {exposure_exceedances_arr.max()}")
    return joint_df


def write_schedule_file(joint_df: pd.DataFrame, output_dir: str, output_filename: str) -> None:
    """Select, reorder, and write the final exposure-level schedule to a CSV file.

    Parameters
    ----------
    joint_df : pandas.DataFrame
        Fully processed exposure-level schedule table.
    output_dir : str
        Directory in which to write the output file.
    output_filename : str
        Name of the output CSV file.
    """

    joint_df = joint_df[[
        'segment-id', 'ra', 'dec', 'pa', 'bandpass', 'program name', 'exposure start',
        'exposure time', 'target_name', 'plan', 'pass',
        'segment', 'observation', 'visit', 'exposure', 'segment start',
        'segment end', 'segment duration', 'program id',
        'ma_table_number', 'slew']
    ]
    joint_df.to_csv(os.path.join(output_dir, output_filename), index=False)

def make_csv(plan_dir: str) -> None:
    logging.basicConfig(filename="log.log", level=logging.INFO)
    logger.info('Started')
    df_exp_all = read_exp_csv_files(plan_dir)
    df_seg = read_segment_schedule_file(plan_dir)
    joint_df = combine_tables(df_seg, df_exp_all)
    joint_df = remove_exposures_not_in_segment_schedule(joint_df)
    joint_df = add_exposure_start_col(joint_df)
    write_schedule_file(joint_df, plan_dir, f'exposure_level_schedule_{os.path.split(plan_dir)[-1]}.csv')
    logger.info('Finished')
if __name__ == "__main__":
    '''
    Create an exposure level CSV file.

    Example call:
    python parse_schedule_prototype.py -d ../Apr2026_MissionPlan
    '''

    survey_pid_dict = {994:'hlwas',
                  999:'hltds',
                  998:'gbtds',
                  981:'gps'}

    parser = argparse.ArgumentParser()
    parser.add_argument('-d', '--dir', default='./', dest='plan_dir')
    args = parser.parse_args()

    plan_dir = args.plan_dir
    make_csv(plan_dir)
