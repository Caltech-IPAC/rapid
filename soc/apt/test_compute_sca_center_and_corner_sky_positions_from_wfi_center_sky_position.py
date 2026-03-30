import matplotlib.pyplot as plt

import modules.utils.rapid_planning_subs as pln


swname = "test_compute_sca_center_and_corner_sky_positions_from_wfi_center_sky_position.py"
swvers = "1.0"

ra = 50.0              # WFI-center RA [deg]
dec = -30.0            # WFI-center Dec [deg]
pa = -45.0             # Position angle [deg]


x0,y0, \
naxis1,naxis2, \
x1,y1, \
x2,y2, \
x3,y3, \
x4,y4, \
ra0,dec0, \
ra1,dec1, \
ra2,dec2, \
ra3,dec3, \
ra4,dec4, \
x_wfi_center,y_wfi_center, \
ra_wfi_center,dec_wfi_center = \
pln.compute_sca_center_and_corner_sky_positions_from_wfi_center_sky_position(ra,dec,pa)

print(f"naxis1,naxis2 = {naxis1},{naxis2}")


# Create the scatter plot.
fig, ax = plt.subplots(figsize=(6, 6))
ax.scatter(x0, y0, c='blue', alpha = 0.3)
ax.scatter(x1, y1, c='green', alpha = 0.3)
ax.scatter(x2, y2, c='red', alpha = 0.3)
ax.scatter(x3, y3, c='orange', alpha = 0.3)
ax.scatter(x4, y4, c='purple', alpha = 0.3)

ax.scatter([x_wfi_center], [y_wfi_center], c='black')          # WFI center


# Add labels and a title.
plt.xlabel("X (pixels)")
plt.ylabel("Y (pixels)")
plt.title("Roman-WFI-SCA Centers And Corners")
plt.axis('square')
plt.xlim(1, naxis1)
plt.ylim(1, naxis2)
plt.tight_layout(pad=1.08, w_pad=0.5, h_pad=0.5)

# Display the plot
plt.show()



# Create the scatter plot.
fig, ax = plt.subplots(figsize=(6, 6))
ax.scatter(ra0, dec0, c='blue', alpha = 0.3)
ax.scatter(ra1, dec1, c='green', alpha = 0.3)
ax.scatter(ra2, dec2, c='red', alpha = 0.3)
ax.scatter(ra3, dec3, c='orange', alpha = 0.3)
ax.scatter(ra4, dec4, c='purple', alpha = 0.3)

ax.scatter([ra_wfi_center],[dec_wfi_center],c='black')                   # WFI center

# Add labels and a title.
plt.xlabel("RA (degrees)")
plt.ylabel("Dec (degrees)")
plt.title("Roman-WFI-SCA Centers And Corners")
plt.axis('square')
#plt.xlim(49.5, 50.5)
#plt.ylim(-30.5, -29.5)
plt.tight_layout(pad=1.08, w_pad=0.5, h_pad=0.5) # You can adjust these values


# Display the plot
plt.show()
