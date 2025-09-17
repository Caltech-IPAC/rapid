import matplotlib.pyplot as plt
import database.modules.utils.roman_tessellation_db as sqlite

def plot_sca_outlines(ra,dec,symbol="-"):
    for i in range(0,len(ra),4):
        ra_sca = []
        dec_sca = []

        ra_sca.append(ra[i])
        dec_sca.append(dec[i])

        ra_sca.append(ra[i+1])
        dec_sca.append(dec[i+1])

        ra_sca.append(ra[i+2])
        dec_sca.append(dec[i+2])

        ra_sca.append(ra[i+3])
        dec_sca.append(dec[i+3])

        ra_sca.append(ra[i])
        dec_sca.append(dec[i])

        symbol = "-"
        if i == 0:
            symbol = "o"

        plt.plot(ra_sca, dec_sca,symbol)


roman_tessellation_db = sqlite.RomanTessellationNSIDE512(debug=0)

rtid = 6291457
#rtid = 1
#rtid = 6291458
#rtid=2
#rtid=6291434
#rtid=10
#rtid = 3333
#rtid=444444

#my_ra = 0.0
#my_dec = 0.0
my_ra = 180.0
my_dec = 0.0
roman_tessellation_db.get_rtid(my_ra,my_dec)
rtid = roman_tessellation_db.rtid
print("input rtid =",rtid)
print("ra,dec looked up from input rtid =",my_ra,my_dec)

pivot_ra = 180.0
if my_ra >= 170.0 and my_ra < 190.0:
    pivot_ra = 320.0

rtids_list = roman_tessellation_db.get_all_neighboring_rtids(rtid)

ra = []
dec = []

roman_tessellation_db.get_corner_sky_positions(rtid)
ra1 = roman_tessellation_db.ra1
dec1 = roman_tessellation_db.dec1
ra2 = roman_tessellation_db.ra2
dec2 = roman_tessellation_db.dec2
ra3 = roman_tessellation_db.ra3
dec3 = roman_tessellation_db.dec3
ra4 = roman_tessellation_db.ra4
dec4 = roman_tessellation_db.dec4

if ra1 > pivot_ra:
    ra1 = ra1 - 360.0

if ra2 > pivot_ra:
    ra2 = ra2 - 360.0

if ra3 > pivot_ra:
    ra3 = ra3 - 360.0

if ra4 > pivot_ra:
    ra4 = ra4 - 360.0

ra.append(ra1)
dec.append(dec1)

ra.append(ra2)
dec.append(dec2)

ra.append(ra3)
dec.append(dec3)

ra.append(ra4)
dec.append(dec4)

i = 0
print("Neighboring sky tiles (rtid is equivalent to field number):")
print("i,rtid")
for rtid in rtids_list:
    i += 1
    print(f"{i},{rtid}")

    roman_tessellation_db.get_corner_sky_positions(rtid)
    ra1 = roman_tessellation_db.ra1
    dec1 = roman_tessellation_db.dec1
    ra2 = roman_tessellation_db.ra2
    dec2 = roman_tessellation_db.dec2
    ra3 = roman_tessellation_db.ra3
    dec3 = roman_tessellation_db.dec3
    ra4 = roman_tessellation_db.ra4
    dec4 = roman_tessellation_db.dec4

    if ra1 > pivot_ra:
        ra1 = ra1 - 360.0

    if ra2 > pivot_ra:
        ra2 = ra2 - 360.0

    if ra3 > pivot_ra:
        ra3 = ra3 - 360.0

    if ra4 > pivot_ra:
        ra4 = ra4 - 360.0

    ra.append(ra1)
    dec.append(dec1)

    ra.append(ra2)
    dec.append(dec2)

    ra.append(ra3)
    dec.append(dec3)

    ra.append(ra4)
    dec.append(dec4)


plt.figure(figsize=(8, 8))

plot_sca_outlines(ra,dec)

plt.xlabel('Right Ascension (degrees)')
plt.ylabel('Declination (degrees)')

plt.title("Roman Neighboring Sky Tiles")

plt.show()
