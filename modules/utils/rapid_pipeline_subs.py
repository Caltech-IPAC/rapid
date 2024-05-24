import math


#-------------------------------------------------------------------
# Given (R.A., Dec.), compute (x, y, z) on the unit sphere.

def compute_xyz(ra,dec):

    alpha = math.radians(ra);
    delta = math.radians(dec);

    cosalpha = math.cos(alpha);
    sinalpha = math.sin(alpha);
    cosdelta = math.cos(delta);
    sindelta = math.sin(delta);

    x = cosdelta * cosalpha;
    y = cosdelta * sinalpha;
    z = sindelta;

    return x,y,z
