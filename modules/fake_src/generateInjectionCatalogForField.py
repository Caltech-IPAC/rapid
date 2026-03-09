'''
Generate a light curve catalog for fake variable sources to be injectied into a given field (sky tile) and
adjacent fields that overlap with the reference image for that field.
'''

import os
import configparser
import numpy as np
import json
import argparse

import database.modules.utils.rapid_db as db
import modules.utils.rapid_pipeline_subs as util
import database.modules.utils.roman_tessellation_db as sqlite


####### CONFIGURATION #######
### will probably get moved to a config file at some point
cfg_path = os.path.dirname(os.path.abspath(__file__))
cfg_filename_only = "generateInjectionCatalogForField.ini"

config_input_filename = cfg_path + "/" + cfg_filename_only
config_input = configparser.ConfigParser()
config_input.read(config_input_filename)

roman_tessellation_db = sqlite.RomanTessellationNSIDE512()
#needs to be fed the corners of the reference image to find the overlapping tiles, which will be the fields we inject into.

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Generate a catalog of fake sources with light curves parameters to inject into a given field')
    parser.add_argument('field_id', help='ID of the field (rtid of sky tile) to inject sources into')
    parser.add_argument('--config_input_filename', default=config_input_filename, help='INI file with the configuration for generating the injection catalog')

    args = parser.parse_args()
    rtid = args.field_id

    config_input = configparser.ConfigParser()
    config_input.read(args.config_input_filename)

    roman_tessellation_db.get_corner_sky_positions(rtid)
    ra_min = min(roman_tessellation_db.ra1, roman_tessellation_db.ra2, roman_tessellation_db.ra3, roman_tessellation_db.ra4)
    ra_max = max(roman_tessellation_db.ra1, roman_tessellation_db.ra2, roman_tessellation_db.ra3, roman_tessellation_db.ra4)
    dec_min = min(roman_tessellation_db.dec1, roman_tessellation_db.dec2, roman_tessellation_db.dec3, roman_tessellation_db.dec4)
    dec_max = max(roman_tessellation_db.dec1, roman_tessellation_db.dec2, roman_tessellation_db.dec3, roman_tessellation_db.dec4)

    fake_sources_catalog_dict = config_input['FAKE_SOURCES_CATALOG']
    inject_fake_sinusoidal_variables = eval(fake_sources_catalog_dict['inject_fake_sinusoidal_variables'])
    inject_fake_gaussian_variables = eval(fake_sources_catalog_dict['inject_fake_gaussian_variables'])

    injection_catalog = {}
    injection_counter = 0
    if inject_fake_sinusoidal_variables:
        num_sinusoidal_variables = int(fake_sources_catalog_dict['num_sinusoidal_variables'])
        sinusoidal_period_min = float(fake_sources_catalog_dict['sinusoidal_period_min'])
        sinusoidal_period_max = float(fake_sources_catalog_dict['sinusoidal_period_max'])
        sinusoidal_amplitude_min = float(fake_sources_catalog_dict['sinusoidal_amplitude_min'])
        sinusoidal_amplitude_max = float(fake_sources_catalog_dict['sinusoidal_amplitude_max'])
        sinusoidal_magnitude_min = float(fake_sources_catalog_dict['sinusoidal_magnitude_min'])
        sinusoidal_magnitude_max = float(fake_sources_catalog_dict['sinusoidal_magnitude_max'])
        sinusoidal_phase_min = float(fake_sources_catalog_dict['sinusoidal_phase_min'])
        sinusoidal_phase_max = float(fake_sources_catalog_dict['sinusoidal_phase_max'])

        #generate the fake sources catalog
        ras_sinusoidal = np.random.uniform(ra_min, ra_max, num_sinusoidal_variables)
        decs_sinusoidal = np.random.uniform(dec_min, dec_max, num_sinusoidal_variables)
        periods_sinusoidal = np.random.uniform(sinusoidal_period_min, sinusoidal_period_max, num_sinusoidal_variables)
        amplitudes_sinusoidal = np.random.uniform(sinusoidal_amplitude_min, sinusoidal_amplitude_max, num_sinusoidal_variables)
        magnitudes_sinusoidal = np.random.uniform(sinusoidal_magnitude_min, sinusoidal_magnitude_max, num_sinusoidal_variables)
        phases_sinusoidal = np.random.uniform(sinusoidal_phase_min, sinusoidal_phase_max, num_sinusoidal_variables)

        #ID is generated as the rtid followed by a unique number for each source, 0-padded to 5 digits (e.g. 526133100000, 526133100001, etc.)
        for i in range(num_sinusoidal_variables):
            injection_catalog[f"{rtid}{injection_counter:05}"] = {
                'type': 'sinusoidal',
                'ra': float(ras_sinusoidal[i]),
                'dec': float(decs_sinusoidal[i]),
                'parameters': {
                    'period': float(periods_sinusoidal[i]),
                    'amplitude': float(amplitudes_sinusoidal[i]),
                    'magnitude': float(magnitudes_sinusoidal[i]),
                    'phase': float(phases_sinusoidal[i])
                }
            }
            injection_counter += 1

    if inject_fake_gaussian_variables:
        num_gaussian_variables = int(fake_sources_catalog_dict['num_gaussian_variables'])
        gaussian_peak_time_min = float(fake_sources_catalog_dict['gaussian_peak_time_min'])
        gaussian_peak_time_max = float(fake_sources_catalog_dict['gaussian_peak_time_max'])
        gaussian_amplitude_min = float(fake_sources_catalog_dict['gaussian_amplitude_min'])
        gaussian_amplitude_max = float(fake_sources_catalog_dict['gaussian_amplitude_max'])
        gaussian_sigma_min = float(fake_sources_catalog_dict['gaussian_sigma_min'])
        gaussian_sigma_max = float(fake_sources_catalog_dict['gaussian_sigma_max'])
        gaussian_magnitude_min = float(fake_sources_catalog_dict['gaussian_magnitude_min'])
        gaussian_magnitude_max = float(fake_sources_catalog_dict['gaussian_magnitude_max'])

        #generate the fake sources catalog
        ras_gaussian = np.random.uniform(ra_min, ra_max, num_gaussian_variables)
        decs_gaussian = np.random.uniform(dec_min, dec_max, num_gaussian_variables)
        peak_times_gaussian = np.random.uniform(gaussian_peak_time_min, gaussian_peak_time_max, num_gaussian_variables)
        peak_amplitudes_gaussian = np.random.uniform(gaussian_amplitude_min, gaussian_amplitude_max, num_gaussian_variables)
        sigmas_gaussian = np.random.uniform(gaussian_sigma_min, gaussian_sigma_max, num_gaussian_variables)
        magnitudes_gaussian = np.random.uniform(gaussian_magnitude_min, gaussian_magnitude_max, num_gaussian_variables)

        #ID is generated as the rtid followed by a unique number for each source, 0-padded to 5 digits (e.g. 526133100000, 526133100001, etc.)
        for i in range(num_gaussian_variables):
            injection_catalog[f"{rtid}{injection_counter:05}"] = {
                'type': 'gaussian',
                'ra': float(ras_gaussian[i]),
                'dec': float(decs_gaussian[i]),
                'parameters': {
                    'peak_time': float(peak_times_gaussian[i]),
                    'peak_amplitude': float(peak_amplitudes_gaussian[i]),
                    'sigma': float(sigmas_gaussian[i]),
                    'magnitude': float(magnitudes_gaussian[i])
                }
            }
            injection_counter += 1

    #save the injection catalog as a json file
    injection_catalog_filename = f"injection_catalog_rtid{rtid}.json"
    with open(injection_catalog_filename, 'w') as f:
        json.dump(injection_catalog, f, indent=4)
