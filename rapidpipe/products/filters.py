"""The Roman-to-RAPID filter-name map and normalisation, as `dev` has it.

`dev`'s ``roman_to_rapid_filter_names`` and the ``rapid_filter_name``
lookup it backs (``modules/utils/rapid_pipeline_subs.py``): Roman filter
designations (``F062`` etc.) to the RAPID spelling that FITS ``FILTER``
headers and the `filters` table carry (``R062`` etc.); ``F184`` is spelled
the same either way.

This is the pipeline's one copy. `rapidpipe.science.reference.prep` (WP-A)
and `rapidpipe.products.refimage` (WP-B) each ported it separately before
the two landed on `rebuild` together; it lives here, in
`rapidpipe.products`, because the stage contract's dependency direction
(``tests/unit/test_dependency_direction.py``) lets both
`rapidpipe.science` and `rapidpipe.db` import `rapidpipe.products`, but not
each other. Both of those modules still expose ``rapid_filter_name`` and
``ROMAN_TO_RAPID_FILTER_NAMES`` under their own names, re-exported from
here, so neither existing import path broke (step 8, WP-E).
"""

from __future__ import annotations

#: `dev`'s ``roman_to_rapid_filter_names``: Roman designations to the RAPID
#: names FITS ``FILTER`` headers and the ``filters`` table carry.
ROMAN_TO_RAPID_FILTER_NAMES = {
    "F062": "R062",
    "F087": "Z087",
    "F106": "Y106",
    "F129": "J129",
    "F158": "H158",
    "F184": "F184",
    "F213": "K213",
    "F146": "W146",
}
RAPID_TO_ROMAN_FILTER_NAMES = {v: k for k, v in ROMAN_TO_RAPID_FILTER_NAMES.items()}


def filter_spellings(name: str) -> set[str]:
    """Every spelling of ``name`` `dev` treats as the same filter (upper case)."""
    upper = str(name).strip().upper()
    spellings = {upper}
    alternate = ROMAN_TO_RAPID_FILTER_NAMES.get(upper, RAPID_TO_ROMAN_FILTER_NAMES.get(upper))
    if alternate is not None:
        spellings.add(alternate)
    return spellings


def rapid_filter_name(name: str) -> str:
    """The RAPID spelling of ``name`` (``F146`` -> ``W146``; ``W146`` unchanged).

    FITS ``FILTER`` headers and the ``filters`` table carry the RAPID
    spelling; a name in neither map is returned upper-cased.
    """
    upper = str(name).strip().upper()
    return ROMAN_TO_RAPID_FILTER_NAMES.get(upper, upper)


def same_filter(a: str, b: str) -> bool:
    """True when ``a`` and ``b`` name one filter in either spelling."""
    return bool(filter_spellings(a) & filter_spellings(b))
