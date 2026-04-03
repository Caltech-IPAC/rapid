# Imports
import matplotlib.pyplot as plt
import pysiaf
from pysiaf.utils.rotations import attitude

# Read in the Roman SIAF
rsiaf = pysiaf.Siaf('Roman')

# Print information about the WFI01_FULL aperture
wfi01 = rsiaf['WFI01_FULL']
print(f'WFI01 Xsci Ref: {wfi01.XSciRef}')
print(f'WFI01 Ysci Ref: {wfi01.YSciRef}')
print(f'WFI01 V2 Ref: {wfi01.V2Ref}')
print(f'WFI01 V3 Ref: {wfi01.V3Ref}')


# Plot the Roman apertures on the telescope ("V") frame
roman_apertures = [f'WFI{i + 1:02}_FULL' for i in range(18)]
roman_apertures.append('CGI_CEN')

fig, ax = plt.subplots()

for rap in roman_apertures:

    print("rap =",rap)

    color = 'salmon' if 'WFI' in rap else 'cyan'
    rsiaf[rap].plot(color=color)

# Add guide lines for boresight (V2, V3) = (0, 0)
ylim = ax.get_ylim()
xlim = ax.get_xlim()
ax.plot([0, 0], ylim, color='black', linestyle=':', alpha=0.3)
ax.plot(xlim, [0, 0], color='black', linestyle=':', alpha=0.3)

# Set the axis limits and invert the X-axis such that V2 is
# positive to the left
ax.set_xlim(xlim)
ax.set_ylim(ylim)
ax.invert_xaxis()

# Save figure
plt.savefig('pysiaf_roman_apertures.png')

