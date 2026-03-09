import numpy as np

""" 
Light curve models for generating fake source injections
"""

def SinusoidalLightCurve(time, mean_value, amplitude, period, phase):
    """
    Generate a sinusoidal light curve model.

    Parameters:
    time (array-like): Array of time points at which to evaluate the light curve.
    t0 (float): Time of the first maximum (phase shift).
    amplitude (float): Amplitude of the sinusoidal variation (relative to the mean value).
    period (float): Period of the sinusoidal variation.
    phase (float): Phase shift of the sinusoidal variation (from 0 to 1).
    
    Returns:
    array-like: The sinusoidal light curve values at the given time points.
    """
    return mean_value + amplitude * np.sin(2 * np.pi * (time / period + phase))

def GaussianLightCurve(time, peak_time, peak_amplitude, sigma, static_value, time_bounds=None):
    """
    Generate a Gaussian light curve model

    Parameters:
    time (array-like): Array of time points at which to evaluate the light curve.
    peak_time (float): Time of the peak (mean of the Gaussian).
    peak_amplitude (float): Peak value of the Gaussian (relative to the baseline).
    sigma (float): Standard deviation of the Gaussian (width of the peak).
    static_value (float): Baseline level (quiescent value of the light curve far from the peak).
    time_bounds (tuple, optional): If provided, a tuple (t_min, t_max) that specifies to include the guassian component, otherwise set to static_flux. 
    
    Returns:
    array-like: The Gaussian light curve values at the given time points.
    """

    if time_bounds is not None:
        t_min, t_max = time_bounds
        gaussian_component = np.where((time >= t_min) & (time <= t_max), 
                                      peak_amplitude * np.exp(-0.5 * ((time - peak_time) / sigma) ** 2), 
                                      0)
    else:
        gaussian_component = peak_amplitude * np.exp(-0.5 * ((time - peak_time) / sigma) ** 2)
    
    return static_value + gaussian_component
