import math


#-------------------------------------------------------------------
# Given (R.A., Dec.), compute (x, y, z) on the unit sphere.

def compute_xyz(ra,dec):

    alpha = math.radians(ra);
    delta = math.radians(dec);

    cosalpha = cos(alpha);
    sinalpha = sin(alpha);
    cosdelta = cos(delta);
    sindelta = sin(delta);

    x = cosdelta * cosalpha;
    y = cosdelta * sinalpha;
    z = sindelta;

    return x,y,z
