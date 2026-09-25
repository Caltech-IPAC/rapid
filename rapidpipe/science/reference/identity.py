"""The reference-image instance's logical key and selection digest (R5).

The key is ``{"field", "filter", "recipe", "version"}``; its ``version``
is the selection digest -- the first 16 hex digits of SHA-256 over the
sorted constituent ``l2-image`` instance ids joined by newlines, a
newline, and the resolved settings hash. The same selection rebuilt with
the same settings is another instance of one logical product; a different
selection (or settings) is a new logical product. ``refimages.version``
is not this: it is the legacy per-(field, fid, ppid) counter `register`
allocates.
"""

from __future__ import annotations

import hashlib
from typing import Iterable

RECIPE = "awaicgen"
DIGEST_HEX_DIGITS = 16


def selection_digest(constituents: Iterable[str], settings_hash: str) -> str:
    """First 16 hex of SHA-256 over sorted ids, newline-joined, then the settings hash.

    ``settings_hash`` is taken as given (with or without a ``sha256:``
    prefix; the prefix is stripped so either form digests the same).
    """
    ids = sorted(str(i) for i in constituents)
    text = "\n".join(ids) + "\n" + str(settings_hash).removeprefix("sha256:")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:DIGEST_HEX_DIGITS]


def logical_key(*, field: int | str, filter_name: str, digest: str) -> dict[str, str]:
    """The reference-image logical key."""
    return {"field": str(field), "filter": str(filter_name), "recipe": RECIPE,
            "version": digest}
