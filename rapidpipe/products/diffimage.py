"""`dev`'s difference-image bits, as code.

- `catalog_outcome_bit` is `dev`'s six-bit mask (one bit per differencer
  and sign, set when no Photutils catalog was produced), registered as
  `diffimages.infobitssci`.
- :data:`SOURCE_FLAG_BRIGHT_FOOTPRINT` and
  :data:`INFORMATIONAL_SOURCE_FLAGS`: the ``sources.flags`` bit RAPID sets
  and the bits that do not reject a source.

This module imports nothing from `rapidpipe` outside `rapidpipe.products`,
matching the products page's own dependency rule.
"""

from __future__ import annotations


class DifferenceImageRegistrationError(ValueError):
    """No catalog-outcome bit for a (differencer, sign)."""


#: `dev`'s catalog-outcome bits, kept (products page, "`infobitssci`'s six
#: bits"): one bit per differencer and sign, set when no Photutils catalog
#: was produced for that difference image and sign.
CATALOG_OUTCOME_BITS: dict[tuple[str, str], int] = {
    ("zogy", "positive"): 0,
    ("zogy", "negative"): 1,
    ("sfft", "positive"): 2,
    ("sfft", "negative"): 3,
    ("naive", "positive"): 4,
    ("naive", "negative"): 5,
}


#: ``sources.flags`` bit RAPID sets beside photutils' fit-condition bits
#: (photutils 3.0 uses bits up to ``1 << 11``; ``sources.flags`` is a
#: smallint, so ``1 << 14`` is the highest free bit): the source lies in a
#: bright reference source's footprint
#: (:mod:`rapidpipe.science.difference.metrics`). Informational.
SOURCE_FLAG_BRIGHT_FOOTPRINT = 1 << 14
#: The ``sources.flags`` bits that do not reject a source: a source with no
#: other bit set is associated and alerted as ``flags = 0`` ones are, its
#: flag carried into the packet's ``psfFitFlags``. Every eligibility
#: predicate reads this one mask (``flags & ~INFORMATIONAL_SOURCE_FLAGS = 0``).
INFORMATIONAL_SOURCE_FLAGS = SOURCE_FLAG_BRIGHT_FOOTPRINT


def catalog_outcome_bit(differencer: str, sign: str) -> int:
    """The mask value (``2**bit``) for no Photutils catalog on (differencer, sign)."""
    try:
        return 1 << CATALOG_OUTCOME_BITS[(differencer, sign)]
    except KeyError as exc:
        raise DifferenceImageRegistrationError(
            f"no catalog-outcome bit for differencer {differencer!r}, "
            f"sign {sign!r}") from exc
