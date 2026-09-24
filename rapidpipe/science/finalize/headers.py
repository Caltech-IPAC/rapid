"""The finalized difference image's primary-header stamp.

`dev`'s post-processing pipeline (ppid 17) rewrites the difference image
with informational keywords and astropy's ``CHECKSUM``/``DATASUM``
(``modules/utils/rapid_pipeline_subs.py`` ``addKeywordsToFITSHeader``,
lines 1891-1926: ``writeto(..., checksum=True)``). The rebuild keeps that
write and replaces `dev`'s database-id keywords (``PID``, ``RID``,
``EXPID``, ``FID``, ``DIFIMVER``) and S3 keywords (``S3BUCKN``,
``S3OBJPRF``) with the run model's own identifiers: `register` holds the
legacy ids, and the output location is one keyword (supervisor ruling,
2026-09-24). ``PPID``, ``INFOBITS``, ``FIELD``, ``DIFFILEN`` and ``DATE``
keep `dev`'s names and meanings.

:func:`stamp_cards` is pure: it turns :class:`StampValues` into the
ordered ``(keyword, value, comment)`` cards. :func:`write_stamped` is the
one FITS write. Only the primary HDU's header is changed; the data and
every other HDU are written back unchanged, and astropy recomputes
``CHECKSUM``/``DATASUM`` for each HDU.

Imports nothing from ``rapidpipe`` (stage contract, dependency direction).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

#: The value stamped for a provenance field the difference attempt's
#: execution record does not carry (absent record, or a ``null`` value).
UNKNOWN = "unknown"

_KEYWORD_RE = re.compile(r"^[A-Z0-9_-]{1,8}$")


@dataclass(frozen=True)
class StampValues:
    """Everything the stamp records, gathered by the stage.

    ``run``/``attempt``/``instance`` are this finalize attempt's run,
    attempt and the new difference-image instance; ``finalized_from`` is
    the input instance. ``l2_instance``/``reference_instance`` come from
    the difference manifest's ``inputs.products``; ``differencer`` and
    ``settings_hash`` from the input instance's logical key;
    ``source_revision``/``image_digest`` from the difference attempt's
    execution record; ``output_location`` is this attempt's ``--outputs``
    as given. ``ppid`` maps the differencer to its `pipelines` row,
    ``infobits`` is ``registration.catalog_outcome_bits``, ``field`` the
    tessellation field of ``registration.centre``, ``diff_filename`` the
    primary member's base name, ``date`` the stamp time (:func:`utc_date`).
    """

    run: str
    attempt: str
    instance: str
    finalized_from: str
    l2_instance: str
    reference_instance: str
    differencer: str
    settings_hash: str
    source_revision: str
    image_digest: str
    output_location: str
    ppid: int
    infobits: int
    field: int
    diff_filename: str
    date: str


#: The keyword table, in stamp order: (keyword, StampValues field, comment);
#: a field of None is RPSTAGE, whose value is always :data:`STAGE_NAME`.
KEYWORDS: tuple[tuple[str, str | None, str], ...] = (
    ("RPRUN", "run", "RAPID run id"),
    ("RPATTMPT", "attempt", "RAPID finalize attempt id"),
    ("RPINST", "instance", "RAPID difference-image instance id"),
    ("RPSTAGE", None, "RAPID stage that wrote this file"),
    ("RPFINFRM", "finalized_from", "RAPID input instance finalized"),
    ("RPL2INST", "l2_instance", "RAPID l2-image instance id"),
    ("RPREFINS", "reference_instance", "RAPID reference-image instance id"),
    ("RPDIFFER", "differencer", "Differencer"),
    ("RPSETHSH", "settings_hash", "Difference settings hash"),
    ("RPSRCREV", "source_revision", "Difference code revision"),
    ("RPIMGDIG", "image_digest", "Difference attempt image digest"),
    ("RPOUTLOC", "output_location", "Finalize attempt output location"),
    ("PPID", "ppid", "Pipeline id of the differencer"),
    ("INFOBITS", "infobits", "Catalog-outcome bits (infobitssci)"),
    ("FIELD", "field", "Roman tessellation field of the image centre"),
    ("DIFFILEN", "diff_filename", "Difference image file name"),
    ("DATE", "date", "UTC time the header was stamped"),
)

#: ``RPSTAGE``'s value: the stage that stamps.
STAGE_NAME = "finalize"


def stamp_cards(values: StampValues) -> list[tuple[str, Any, str]]:
    """The ``(keyword, value, comment)`` cards for ``values``, in table order."""
    cards = []
    for keyword, attr, comment in KEYWORDS:
        value = STAGE_NAME if attr is None else getattr(values, attr)
        cards.append((keyword, value, comment))
    return cards


def utc_date(now: datetime | None = None) -> str:
    """``DATE``'s value: UTC, ISO 8601 to the second (the FITS ``DATE`` form)."""
    moment = now if now is not None else datetime.now(timezone.utc)
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def ppid_for(differencer: str, pipelines: Mapping[str, int]) -> int:
    """The `pipelines` row id ``PPID`` records for ``differencer``.

    Raises :class:`ValueError` for a differencer the map does not name.
    """
    try:
        return int(pipelines[differencer])
    except KeyError as exc:
        raise ValueError(
            f"no [pipelines] entry for differencer {differencer!r}; "
            f"known: {sorted(pipelines)}") from exc


def provenance_value(value: Any) -> str:
    """A provenance string, or :data:`UNKNOWN` when absent or empty."""
    if value is None or value == "":
        return UNKNOWN
    return str(value)


def check_keywords() -> None:
    """Every keyword is FITS-legal (<= 8 characters, A-Z 0-9 _ -) and unique."""
    names = [keyword for keyword, _, _ in KEYWORDS]
    bad = [n for n in names if not _KEYWORD_RE.match(n)]
    if bad:
        raise ValueError(f"FITS-illegal keyword names: {bad}")
    if len(set(names)) != len(names):
        raise ValueError("duplicate keyword names in the stamp table")


def check_readable(path: str | Path) -> None:
    """Open ``path`` as FITS and read every HDU's header and data.

    Raises :class:`OSError` or astropy's own exceptions for a file that is
    not readable FITS; the stage maps those to InputRejected, so a failure
    in :func:`write_stamped` afterwards is the stage's own (exit 70).
    """
    from astropy.io import fits

    with fits.open(path, memmap=False) as hdul:
        if len(hdul) == 0:
            raise OSError(f"{path}: no HDUs")
        for hdu in hdul:
            _ = hdu.header
            _ = hdu.data


def write_stamped(source: str | Path, destination: str | Path,
                  cards: list[tuple[str, Any, str]]) -> None:
    """Write ``source`` to ``destination`` with ``cards`` set on the primary header.

    As `dev`'s ``addKeywordsToFITSHeader``: header updated in memory, file
    written with ``checksum=True`` so astropy adds ``CHECKSUM`` and
    ``DATASUM``. Unlike `dev`, the whole HDU list is written back with its
    data unchanged (`dev` rebuilt a lone float32 ``PrimaryHDU``); a
    difference member is already float32 in HDU 0, so the result is the
    same for the files `dev` stamped. ``destination`` must not exist.
    Call :func:`check_readable` on ``source`` first to tell an unreadable
    input from a failed write.
    """
    from astropy.io import fits

    with fits.open(source, memmap=False) as hdul:
        header = hdul[0].header
        for keyword, value, comment in cards:
            if keyword in header:
                del header[keyword]
            header.append(fits_card(keyword, value, comment))
        hdul.writeto(destination, checksum=True, overwrite=False)


def fits_card(keyword: str, value: Any, comment: str):
    """One header card for ``(keyword, value, comment)``, the comment never cut.

    astropy writes a string of 60 to 68 characters on one card and
    truncates its comment to fit (a ``VerifyWarning``); longer strings go
    on ``CONTINUE`` cards with the comment on the last. An output location
    can be any length, so such a value is written in the long-string form
    here instead: the first 60 characters with ``&`` on the keyword's
    card, the rest and the comment on one ``CONTINUE`` card. A string with
    a quote in it keeps astropy's own card.
    """
    import warnings

    from astropy.io import fits
    from astropy.io.fits.verify import VerifyWarning

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", VerifyWarning)
        card = fits.Card(keyword, value, comment)
        _ = card.image
    truncated = any("truncated" in str(w.message) for w in caught)
    if not truncated or not isinstance(value, str) or "'" in value:
        return card
    first, rest = value[:60], value[60:]
    image = (f"{keyword:<8}= '{first}&'".ljust(80)
             + f"CONTINUE  '{rest}' / {comment}".ljust(80))
    return fits.Card.fromstring(image)
