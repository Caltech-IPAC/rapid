"""
File:    facts.py

The unit's identity, in the vocabulary the operations schema registers under.

**The defect this closes (round-3 finding #2).** `StageContext.provenance`
started empty and the entrypoint seeded it with exactly three keys —
`release_content_digest`, `tessellation_version`, `tessellation_digest`. The
manifest's `UnitFacts` were never copied into it, and `sca` lives on the UNIT
rather than on the facts, so it was not even reachable from there. Everything
the registrar asks of `science_provenance` about WHICH piece of sky this
attempt was — `field`, `fid`, `rid`, `sca`, the reference identity, the ten
sky-position numbers — was therefore absent from every record production could
author. The registrar was not merely untested against production records: no
production record could ever have satisfied it. `MissingRecordFact` would have
fired on the first real candidate, the attempt would have stayed a candidate,
and the next pass would have failed on it again forever.

`hp6` and `hp9` were worse than absent: they existed nowhere in the pipeline at
all — not in `UnitFacts`, not in a stage, not in gathering. They are DERIVED
here, from the science image's own centre, using the convention the legacy
loader used and no other (see `healpix_indices`).

**Why this is a module and not a line in the entrypoint.** It follows the
precedent `_ppid_of` set in `pipeline/runtime/termination.py`: a fact the record
must carry, which is a pure function of something the attempt already knows, is
derived ONCE at the moment the record's content is assembled, in a named place
with the derivation written down. The alternative — translating names on the
way OUT, inside the registrar — was rejected deliberately. The registrar reads
the record and nothing else; if it also had to know that `dxrmsfin` is spelled
`gainmatch_dxrms_measured` in provenance, then the record would no longer be
self-describing and a second consumer would have to learn the same mapping.
The record carries the operations schema's own names, because those are the
names its contents mean.
"""

import logging

logger = logging.getLogger("rapid.registration.facts")

#: Whether the absent-healpy warning has already been emitted in this process.
_WARNED_NO_HEALPY = False

#: HEALPix levels, and the nsides derived from them exactly as the legacy code
#: derives them. Taken verbatim from `pipeline/loadPSFCatIntoDBSourcesTable.py`
#: lines 26-30, which is the file that actually populated the `hp6`/`hp9`
#: columns these values are registered into; `database/scripts/compute_fields.py`
#: and the three `database/sims/db_register_*.py` loaders all agree with it.
#:
#: These are NOT a free choice. The columns are already populated with indices
#: computed at these levels in this scheme, and an index computed at a
#: different nside — or in the ring scheme, or with lat/lon swapped — is a
#: valid integer that silently points at the wrong patch of sky. That is why
#: the derivation below is a transcription rather than an implementation.
LEVEL6 = 6
NSIDE6 = 2 ** LEVEL6

LEVEL9 = 9
NSIDE9 = 2 ** LEVEL9

#: The sky-position keys `add_diffimage` takes: the tile centre and its four
#: corners, in the order the legacy call passed them.
SKY_POSITION_KEYS = ("ra0", "dec0", "ra1", "dec1", "ra2", "dec2",
                     "ra3", "dec3", "ra4", "dec4")


def healpix_indices(ra0, dec0):
    """The nested HEALPix indices at levels 6 and 9 for one sky position.

    `hp.ang2pix(nside, ra, dec, nest=True, lonlat=True)` — the exact call the
    legacy loader makes (`loadPSFCatIntoDBSourcesTable.py:509-510`), with the
    exact keyword arguments. Every part of it is load-bearing:

    - `nest=True` selects the NESTED numbering. The ring scheme numbers the
      same sphere differently, and an index from one read as the other is a
      plausible integer naming the wrong sky.
    - `lonlat=True` says the two angles are longitude and latitude in DEGREES,
      which is what `ra0`/`dec0` are. Without it healpy reads them as
      colatitude and longitude in RADIANS — so a position would not merely
      shift, it would land in a different hemisphere.
    - The position is the image CENTRE (`ra0`, `dec0`), not a corner. The
      legacy registration body did the same (`registerCompletedJobsInDB.py`
      lines 393-398, both indices from `ra0,dec0`).

    Returns `(hp6, hp9)` as plain ints, or `(None, None)` where the centre is
    absent — an absent index is a finding for the registrar to name, and
    inventing 0 here would register the product against a real patch of sky
    that it has nothing to do with.
    """
    if ra0 is None or dec0 is None:
        return None, None

    # Imported here rather than at module scope, and its absence tolerated.
    # healpy is a container dependency (`docker/Dockerfile_ubuntu*`) and is not
    # in requirements.txt, so it is present wherever science runs and may be
    # absent on a host that only reads records. Seeding provenance must not be
    # the thing that kills an attempt: an absent index becomes an absent key,
    # which the registrar names as `MissingRecordFact("hp6")` — a finding
    # against the attempt rather than a crash in the entrypoint before any
    # stage has run.
    try:
        import healpy as hp
    except ImportError:
        # Warned once per process, not once per call: this is a property of the
        # environment, and an attempt derives two indices per unit — repeating
        # it per call would bury the one line that matters under duplicates of
        # itself in a log someone is reading to find out why registration
        # refused.
        global _WARNED_NO_HEALPY
        if not _WARNED_NO_HEALPY:
            _WARNED_NO_HEALPY = True
            logger.warning(
                "healpy is not importable, so hp6/hp9 cannot be derived; "
                "records written here will carry neither and registration "
                "will name them as missing facts rather than registering "
                "products against an invented sky position")
        return None, None

    # int() is not cosmetic: `ang2pix` returns a numpy integer, and the
    # terminal record is serialized with `json.dumps`, which raises on one.
    # That would fail inside the termination protocol — the one place with
    # nowhere left to record an outcome — and take the exit code to 70.
    hp6 = hp.ang2pix(NSIDE6, float(ra0), float(dec0), nest=True, lonlat=True)
    hp9 = hp.ang2pix(NSIDE9, float(ra0), float(dec0), nest=True, lonlat=True)
    return int(hp6), int(hp9)


def unit_provenance(unit, job_type=None):
    """The record vocabulary for one processing unit's identity.

    Everything here is a fact the SUBMITTER established — it is in the
    manifest, or derivable from it — as opposed to a measurement a stage made.
    Returned as a plain dict so the caller merges it into provenance rather
    than this reaching into a context.

    Absent facts are OMITTED rather than written as null, matching
    `UnitFacts.to_dict`'s adopted absent-not-sentinel rule. A key that is not
    there is a fact the manifest did not carry, and the registrar's
    `MissingRecordFact` says so by name; a key present and null would be a
    claim that the value is known to be nothing.
    """
    facts = getattr(unit, "facts", None)
    provenance = {}

    # `sca` comes from the UNIT, not from `UnitFacts` — it is half of the
    # unit's identity (`exposure/sca`) rather than a queried attribute of it.
    # It was the one required registrar fact with nowhere at all to come from,
    # because nothing ever looked at the unit itself.
    if getattr(unit, "sca", None) is not None:
        provenance["sca"] = int(unit.sca)
    if getattr(unit, "exposure", None) is not None:
        provenance["expid"] = int(unit.exposure)

    if facts is None:
        return provenance

    # The identity facts the operations tables key on. Named individually
    # rather than copied wholesale from `UnitFacts.to_dict()`: the manifest
    # carries things a record has no business asserting — the coadd-input CSV's
    # checksum, the overlapping-field lists — and a record that grew a new key
    # every time the manifest schema did would make "what does a record carry"
    # unanswerable.
    for name in ("rid", "fid", "field", "rtid", "mjdobs", "exptime",
                 "infobits", "reference_image_id", "reference_image_infobits",
                 "reference_image_version", "pid", "difference_image_version"):
        value = getattr(facts, name, None)
        if value is not None:
            provenance[name] = value

    # The footprint. `add_diffimage` takes the centre and all four corners and
    # a partial one silently mis-matches every later spatial query, so it is
    # carried whole or not at all.
    position = getattr(facts, "sky_position", None)
    if position:
        provenance["sky_position"] = {key: position[key]
                                      for key in SKY_POSITION_KEYS
                                      if position.get(key) is not None}
        hp6, hp9 = healpix_indices(position.get("ra0"), position.get("dec0"))
        if hp6 is not None:
            provenance["hp6"] = hp6
            provenance["hp9"] = hp9

    return provenance
