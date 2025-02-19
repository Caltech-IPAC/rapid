# Intended process path for doing Solar system object association for RAPID

Joe Masiero
2025-01-15

##Background:

RAPID will be performing image subtracting in near-realtime in order
to send alerts to the community about transient events. In order to
remove potential contamination from Solar system objects, the RAPID
pipeline will provide the predicted positions for asteroids while
processing is underway to enable labelling of these potential
contaminants in the alert stream.

Roman is 2.4 m telescope that will observe primarily at near-infrared
wavelengths, which is the trough in the SED for objects interior to
Jupiter. The wide field imager is designed to obtain high resolution
images of large regions of sky, but the small pixel size means that
the vast majority of Solar system objects would be expected to move
over the course of a single exposure sequence, and thus trailed across
multiple pixels. These features combine to mean that Roman is not
likely to detect many objects that are not previously known, which
drives the frequency of updates needed for the known object catalog.

RAPID Known Object Name Association (KONA) heavily leverages design
and software developed at IPAC for NEO Surveyor. This includes the
kete software package which will do much of the heavy lifting in terms
of N-body computations for predicting the on-sky positions of the MPC
orbit catalog. The current catalog contains approximately 1 million
orbits, and in the years following the beginning of the Vera Rubin
survey this is expected to grow by a factor of ~3-6. While the
majority of these objects will not be visible to Roman, it is not
possible to drastically reduce the input catalog as orbital
eccentricity means that objects can be rarely, but not never, visible.

##Complications:

The default processing of the Roman data, in particular the derivation
of Resultants and the automated jump detection used to translate these
into flux values, means that many moving objects will not necessarily
be visible in the primary images used by RAPID, and instead will
result in a cluster of jump flags being recorded in the image
metadata. However, it is not clear at this time what the impact will
be of a moderate-brightness asteroid passing over a background
star/galaxy, and if this will result in an increase in the recorded
flux that could be confused as a transient. Further, experience from
JWST (which uses fundamentally the same image processing software)
indicates that this jump removal performs less well in the wings of
bright objects compared to the cores, and so may still leave residual
flux in the images.

##Intended Operations Procedure:

RAPID will need to maintain a current catalog of orbits of Solar
system objects. Kete provides software tools that can fetch the
currently known orbit catalog from the MPC. This catalog is typically
fully refreshed on ~100 day timescales, with new discoveries being
added daily. For the purposes of RAPID, this catalog needs only to be
updated on a daily-to-monthly cadence, the specific timing of which
will depend on the availability of processing resources. Roman is
unlikely to observe objects discovered within the last few days, and
the orbits of these objects are likely uncertain enough to make
predicting them into the Roman field of view a less-than-fruitful
endeavor. Note, currently kete queries the MPC flat files for this
information, however the MPC will be rolling out new tooling allowing
API queries of their complete orbit database in the near future. While
these two data pathways are expected to have the same information, and
persist for the duration of the Roman mission, it may be worth
exploring switching to the new database query if it offers a
significant improvement for performance.

```
import kete
mpc_orbits = kete.mpc.fetch_known_orbit_data()
mpc_states = kete.mpc.table_to_states(mpc_orbits)
```

With a current orbit catalog in hand, the next step is to propagate
all state vectors to the median JD of the images to be processed using
full N-body calculations. These ‘local’ states will then become the
basis of all processing done that day, and this preparatory step
should be done on an at-least-daily basis. This is needed to reduce
the run time for KONA, as querying each image using a stale state file
will result in significant slow downs in run time. If a new file is
not downloaded, the previous day’s local state file can be used to
initialize today’s states, and so the local state file should be saved
in the processing directory. These should persist for a few days to
support reprocessing or to recover a corrupt file, but do not need to
be saved long-term.

```
mpc_states_local = kete.propagate_n_body(mpc_states, median_jd)
```

Once this is accomplished, kete will take the JD of the image in
question, the central RA/Dec of the image, the spacecraft position
with respect to Earth at that JD, and the local state file, and return
all objects falling in (or near) the field of view. This list of
name-RA-Dec would then be added into the ASDF file produced by RAPID as
an ancillary data product.