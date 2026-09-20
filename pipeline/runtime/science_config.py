"""
File:    science_config.py

The read path for release-versioned science configuration.

The batch-payload co-design's Principles 4 gives every configuration fact
exactly one home, and sorts the first two homes by one question: "anything
that can alter a science product is release content (scientific identity,
per the release design); the tree carries only operationally tunable
values."

This module is the reader for that third home. The file it reads —
``cdf/science/pipeline.toml`` — ships inside the image, so the image
digest already recorded in every attempt's provenance identifies its
contents exactly. That is the property the whole arrangement exists for,
and it is why nothing here can fetch, override, or merge: a value that
could be supplied from outside the image would break the identification
the moment it were used.

**No overrides, no defaults, no merge — except the one, explicit exception
a scratch run needs.** ``load()`` reads one file and returns what is in
it: no environment override, no parameter-tree fallback, no per-key
default. Each of those would let a science value differ from what the
image digest says it is while still claiming that digest, which is the
one thing this module exists to prevent. A missing file or a missing key
is a fault, raised as ``ConfigError``, not a silent default — the same
fail-loud posture ``environment.py`` takes for the same reason.

The sole exception is ``load_with_digest``'s ``overlay`` parameter, and it
does not weaken the rule above — it is scoped so narrowly that the rule
still holds everywhere production runs. A "scratch" run — personal,
never published, never promoted to a community surface — may pass a
mapping of science values to try without rebuilding the image, so a
scientist can iterate on a parameter without waiting on a release. Three
things keep this from becoming the silent-divergence hazard the rule
above forbids: the caller must ask for it explicitly (``overlay=None`` is
the only production behaviour — omitting the argument or passing ``None``
reproduces today's read, cache, and digest exactly, byte for byte); the
merge never touches the module's cached content, so the image's own
`load()`/`load_with_digest()` behaviour for every other caller in the same
process is untouched; and the returned digest is computed OVER THE MERGED
CONTENT, never the image's own digest, so the recorded digest always
identifies what the computation actually used rather than lying about it
being the release's stock content. An overlaid run's provenance therefore
never claims the image digest's identity for content the image did not
supply — it gets its own, different, honestly-computed digest instead.

**Why a content digest as well as the image digest.** The image digest
identifies the file, but only to someone holding the image. The content
digest is a direct, checkable statement of what the science configuration
*was*, recordable in the attempt record beside the image digest and
comparable across attempts without pulling images. It is computed the same
canonical way as the parameter tree's configuration digest
(``submission.startup.configuration_digest``) so the two are read the same
way by anyone looking at provenance: sorted keys, fixed separators,
SHA-256. The two digests are not interchangeable and are recorded
separately — one covers the mutable tree, the other the release content.

**Temporal values are strings, and a native one is refused at load.** TOML
parses an unquoted ``2026-01-01`` into a `datetime.date`, and the digest
canonicalizes with ``json.dumps(default=str)``, which stringifies it. So
``d = 2026-01-01`` and ``d = "2026-01-01"`` — two materially different
configurations — canonicalize identically and produce the same digest. That
collapses two configurations onto one provenance identity, which is the one
property this module exists to guarantee. `load()` therefore refuses any
native date/time/datetime, naming the key: temporal facts enter science
configuration as quoted strings only. Refusal is chosen over canonicalizing
the values explicitly, because canonicalization would change the digest of
any existing release whose TOML carried a temporal value, and no recorded
digest may move.

**The SExtractor auxiliary files are covered but not read here.** The
``.conv``, ``.nnw``, and ``.inp`` files in ``cdf/`` are release content by
location: they ship in the image and change only with a release. They are
consumed by the tools directly, as file paths, so this module does not
parse them; ``auxiliary_identity()`` records their identity for provenance
by naming the image digest that fixes them, which is the honest statement
of what pins them.
"""

import datetime
import functools
import hashlib
import json
import os
import tomllib
from typing import Any, Mapping

from pipeline.runtime.errors import ConfigError

# The release-content file, relative to the software root. Resolved
# against RAPID_SW so the same code reads the installed tree in the image
# and a checkout in a test, without either being a special case.
SCIENCE_CONFIG_RELATIVE_PATH = os.path.join("cdf", "science", "pipeline.toml")

# The schema version this reader understands. A file declaring anything
# else is refused rather than read on a guess: the point of the file is to
# say exactly what a product was made with.
SUPPORTED_SCHEMA_VERSION = 1

# Env var naming the software root. The image sets it; a test can point it
# at a checkout.
ENV_SOFTWARE_ROOT = "RAPID_SW"

# Optional override for the auxiliary-file directory. Plumbing — a path —
# whose default is derived from the root, not compiled in.
ENV_CONFIG_DIRECTORY = "RAPID_CFG"


def software_root() -> str:
    """The installed software root, or raise naming the variable.

    The one fail-loud read of ``RAPID_SW`` for code that needs the root
    itself rather than the science configuration under it. Payload read
    sites call this instead of ``os.environ.get("RAPID_SW", "/code")``:
    that default was the payload surface's divergence from the rest of
    the operational path, and it is the shape the environment policy
    names — "substituting a value for an unset variable without any
    record is prohibited in operational code". A container built without
    the root set would have silently run every tool out of a ``/code``
    that may not be the tree it was given.
    """
    root = os.getenv(ENV_SOFTWARE_ROOT)
    if not root:
        raise ConfigError(
            f"{ENV_SOFTWARE_ROOT} is not set, so the installed software "
            "root cannot be located; it is set by the image and there is "
            "no default — a guessed root runs whatever binaries happen to "
            "be at that path")
    return root


def config_directory() -> str:
    """The auxiliary-file directory beside the software root, or raise.

    ``RAPID_CFG`` overrides it — process-level plumbing, a path, with the
    safe direction as its default — but the default derives from
    :func:`software_root`, so an unset root fails loud here too rather
    than resolving to ``/code/cdf``.
    """
    override = os.getenv(ENV_CONFIG_DIRECTORY)
    if override:
        return override
    return os.path.join(software_root(), "cdf")


def config_path(software_root: str | None = None) -> str:
    """Absolute path to the science configuration file.

    Parameters
    ----------
    software_root : str, optional
        Root of the installed software. Defaults to ``$RAPID_SW``.

    Raises
    ------
    ConfigError
        If no root is given and ``RAPID_SW`` is unset. There is
        deliberately no fallback to the current directory: a job that
        found its science configuration by where it happened to be
        running would be reading configuration nobody can identify.
    """
    root = software_root if software_root is not None else os.getenv(ENV_SOFTWARE_ROOT)
    if not root:
        raise ConfigError(
            f"{ENV_SOFTWARE_ROOT} is not set, so the release's science "
            "configuration cannot be located; it is release content and "
            "must be read from the installed tree, not guessed at from the "
            "working directory")
    return os.path.join(root, SCIENCE_CONFIG_RELATIVE_PATH)


# The types tomllib produces for TOML's native temporal forms: local date,
# local time, and both local and offset date-times. `datetime` subclasses
# `date`, so the pair covers all four; `bool`/`int` are unrelated here.
TEMPORAL_TYPES = (datetime.date, datetime.time)


def _refuse_temporal_values(content: Mapping[str, Any], resolved: str) -> None:
    """Raise if any value in the loaded content is a native date or time.

    Walks nested tables and arrays, because a temporal value anywhere under
    the document reaches the digest through the same canonicalization. The
    key is named in dotted form so the fix is a single edit the reader can
    find without hunting.

    Raises
    ------
    ConfigError
        Naming the first offending key and the quoting that fixes it.
    """

    def walk(node: Any, trail: tuple[str, ...]) -> None:
        if isinstance(node, TEMPORAL_TYPES):
            key = ".".join(trail) if trail else "<document root>"
            raise ConfigError(
                f"{key} in the science configuration at {resolved} is a "
                f"native TOML {type(node).__name__} ({node!s}); temporal "
                "facts must be quoted strings. An unquoted date and the "
                "same date quoted canonicalize to the identical "
                "configuration digest, so the two would share one "
                f"provenance identity. Quote it: {trail[-1] if trail else key}"
                f' = "{node!s}"')
        if isinstance(node, dict):
            for name, value in node.items():
                walk(value, trail + (str(name),))
        elif isinstance(node, (list, tuple)):
            for index, value in enumerate(node):
                walk(value, trail + (f"[{index}]",))

    walk(content, ())


def load(path: str | None = None,
         software_root: str | None = None) -> dict[str, Any]:
    """Read the science configuration.

    Parameters
    ----------
    path : str, optional
        Explicit file path. Injected in tests; production passes nothing
        and lets the path resolve from the software root.
    software_root : str, optional
        Root to resolve against when `path` is not given.

    Returns
    -------
    dict
        Section name -> {key: typed value}, exactly as the file states it.

    Raises
    ------
    ConfigError
        The file is missing, unparseable, carries a native date/time
        value, or declares a schema version this reader does not
        implement.
    """
    resolved = path if path is not None else config_path(software_root)

    try:
        with open(resolved, "rb") as handle:
            content = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(
            f"the release's science configuration is missing at {resolved}; "
            "it ships with the image, so its absence means the image is "
            "not the one this code expects") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            f"could not parse the science configuration at {resolved}: {exc}"
        ) from exc
    except OSError as exc:
        raise ConfigError(
            f"could not read the science configuration at {resolved}: {exc}"
        ) from exc

    # Before anything reads a value: a native temporal type would reach the
    # digest as a string and collide with its quoted twin. Checked ahead of
    # the schema-version gate so the fault is reported on its own terms
    # rather than depending on which version the file declares.
    _refuse_temporal_values(content, resolved)

    version = content.get("release", {}).get("schema_version")
    if version != SUPPORTED_SCHEMA_VERSION:
        raise ConfigError(
            f"science configuration at {resolved} declares schema_version "
            f"{version!r}, not {SUPPORTED_SCHEMA_VERSION}; refusing to read "
            "a layout this code does not implement")

    return content


def digest(content: Mapping[str, Any]) -> str:
    """Content hash of a loaded science configuration, for provenance.

    Canonical in the same way as the parameter tree's configuration
    digest: keys sorted at every level, fixed separators, SHA-256 over
    UTF-8. Two jobs reading the same release produce the same digest.

    Returns
    -------
    str
        Hex SHA-256 over the canonical form.
    """
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def section(content: Mapping[str, Any], name: str) -> dict[str, Any]:
    """One section of the science configuration.

    Raises
    ------
    ConfigError
        If the section is absent. A stage asking for a section the
        release does not carry is a mismatch between code and release,
        which is a deployment fault, not a case to default through.
    """
    if name not in content:
        raise ConfigError(
            f"section {name!r} is not in the release's science "
            f"configuration (it carries: "
            f"{', '.join(sorted(k for k in content if k != 'release'))})")
    value = content[name]
    if not isinstance(value, dict):
        raise ConfigError(
            f"{name!r} is not a section of the science configuration; "
            f"it is a {type(value).__name__}")
    return dict(value)


def value(content: Mapping[str, Any], section_name: str, key: str) -> Any:
    """One value, by section and key.

    Raises
    ------
    ConfigError
        If either the section or the key is absent. There is no default
        parameter by design — see the module docstring.
    """
    values = section(content, section_name)
    if key not in values:
        raise ConfigError(
            f"{section_name}.{key} is not in the release's science "
            "configuration; a missing science parameter is a release fault, "
            "and defaulting it would make the recorded configuration digest "
            "describe configuration the job did not use")
    return values[key]


#: The role the difference-image consumers resolve. Named once here so a
#: consumer spells the ROLE, never an algorithm.
DIFFERENCE_IMAGE_ROLE = "difference_image"


def product_roles(content: Mapping[str, Any]) -> dict[str, str]:
    """Every role this release binds, as role → product name.

    Recorded whole into the attempt's provenance so the record is
    self-describing: registration resolves the role from what the attempt
    carried, not from the release content of whatever image happens to be
    running when the replay passes.
    """
    return dict(section(content, "product_roles"))


def product_role(content: Mapping[str, Any], role: str) -> str:
    """The published product bound to `role` by this release.

    A role is a stable contract name; the release binds it to the concrete
    product that fills it (design/catalog.md § Promotion, "Product roles
    bind in the declared set"). Every consumer of a role-named product —
    registration, the registered measurement variant, the alert cutouts —
    comes through here, which is what keeps the binding a single knob.

    Raises
    ------
    ConfigError
        If the release binds no such role. Refusing beats defaulting to an
        algorithm: a consumer that silently picked one would register a
        product this release never nominated, and the recorded release
        digest would describe a binding the job did not use.
    """
    bound = value(content, "product_roles", role)
    if not isinstance(bound, str) or not bound:
        raise ConfigError(
            f"product_roles.{role} binds to {bound!r}, which is not a "
            "product name; a role binds to exactly one published product")
    return bound


def auxiliary_identity(image_digest: str | None = None) -> dict[str, str]:
    """What pins the SExtractor auxiliary files, for the provenance record.

    The ``.conv`` / ``.nnw`` / ``.inp`` files are release content by
    location: they are in ``cdf/``, they ship in the image, and the tools
    read them as paths rather than through this module. What identifies
    them is therefore the image digest and nothing else — this function
    says so explicitly rather than leaving provenance to imply it.

    Parameters
    ----------
    image_digest : str, optional
        The running image's digest, from the attempt's submission-time
        execution binding.

    Returns
    -------
    dict
        Provenance fields naming what fixes the auxiliary content.
    """
    return {
        "auxiliary_content_root": "cdf",
        "auxiliary_identified_by": "image_digest",
        "image_digest": image_digest or "",
    }


# Package-scope aliases. Inside this module `load` and `digest` are
# unambiguous; re-exported from `pipeline.runtime` they would not be, so
# the package surface names what they load and digest.
load_science_config = load
science_config_digest = digest


@functools.lru_cache(maxsize=1)
def _cached(resolved_path: str) -> tuple[dict[str, Any], str]:
    content = load(path=resolved_path)
    return content, digest(content)


def _deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]
                ) -> dict[str, Any]:
    """`overlay` merged over `base`, recursively through nested tables.

    Neither argument is mutated — every dict on the path from the root to
    an overlaid leaf is freshly copied, so the caller's `overlay` and (via
    `load_with_digest`'s copy-before-merge) the module's cached content are
    both left exactly as they were. A key present only in `overlay` is
    added; a key present in both is replaced by `overlay`'s value unless
    both sides are themselves mappings, in which case the merge recurses
    instead of one table clobbering the other whole. `overlay` is the
    scratch author's explicit ask, so it always wins a conflict — there is
    no third case where `base` wins.
    """
    merged = dict(base)
    for key, overlay_value in overlay.items():
        base_value = merged.get(key)
        if isinstance(base_value, Mapping) and isinstance(overlay_value, Mapping):
            merged[key] = _deep_merge(base_value, overlay_value)
        else:
            merged[key] = overlay_value
    return merged


def load_with_digest(path: str | None = None,
                     software_root: str | None = None,
                     overlay: Mapping[str, Any] | None = None
                     ) -> tuple[dict[str, Any], str]:
    """Load once per process and return (content, digest).

    Cached because every stage wants the same immutable file and the
    digest goes into provenance once per attempt; re-reading and
    re-hashing per stage would be pure waste and would open a window
    where two stages could disagree about what the release said.

    Parameters
    ----------
    path, software_root : str, optional
        As :func:`load`/:func:`config_path`.
    overlay : Mapping, optional
        A scratch run's per-key science overrides, deep-merged over the
        loaded content before it is returned. ``None`` (the default) is
        the ONLY production behaviour: this parameter did not exist before
        the scratch run kind needed it, and every existing caller that
        passes nothing gets exactly today's cached content and today's
        digest, unchanged. A non-empty `overlay` is the one documented
        exception in this module's docstring — see there for why it is
        safe: the merge never reaches the cache (`overlay` is combined
        with a byte-for-byte JSON round-trip COPY of the cached content,
        the same copy every caller already gets to protect the cache from
        mutation, so the cached dict itself never sees an overlay key),
        the merged content is re-checked for native temporal values with
        the identical refusal `load()` applies at read time (an overlay is
        as capable of introducing the date/string digest collision as the
        file is, so it gets the identical guard, naming the same key), and
        the digest returned is computed over the MERGED content rather
        than reused from the cache — an overlaid run's provenance must
        never claim the image's own digest for content the image did not
        supply.

    Returns
    -------
    tuple
        `(content, content_digest)`. Without `overlay`, `content_digest`
        is the release's own digest, identical across every call in the
        process. With a non-empty `overlay`, `content_digest` identifies
        the merged content actually used by THIS call, and is not cached
        or shared with any other caller.
    """
    resolved = path if path is not None else config_path(software_root)
    content, content_digest = _cached(resolved)
    # A copy per caller: the cache holds one dict, and a caller mutating
    # it would silently change what every later stage reads. This copy is
    # also what keeps the overlay path below from poisoning the cache —
    # the merge below is performed on this fresh copy, never on `content`
    # itself, so `_cached`'s stored dict never gains an overlay key no
    # matter how many overlaid calls follow it in the same process.
    own_copy = json.loads(json.dumps(content, default=str))
    if not overlay:
        # The only production path, and it is unchanged: same object
        # shape, same digest, same everything a caller before this
        # parameter existed would have seen.
        return own_copy, content_digest

    merged = _deep_merge(own_copy, overlay)
    # Same refusal `load()` applies to the file, applied again to the
    # merged result: an overlay value is exactly as capable of being a
    # native TOML-shaped date/time (a caller building the overlay from
    # another parsed TOML document, say) as the file itself, and letting
    # one slip through here would reopen the digest collision the module
    # docstring's "Temporal values are strings" section exists to close —
    # for overlaid content specifically, which the file-only check never
    # sees.
    _refuse_temporal_values(merged, f"{resolved} (with overlay)")
    return merged, digest(merged)
