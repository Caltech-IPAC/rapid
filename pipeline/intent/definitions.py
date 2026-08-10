"""
File:    definitions.py

The workflow definitions that ship with the release: reading the reviewed
`cdf/workflow/*-v1.toml` files, loading them through migration 039's audited
mutation function, and the startup completeness check that refuses to run a
work stream whose definition is not loaded.

**WHY THIS MODULE EXISTS** (rule 12). Migration 039 created
`derived.load_workflow_definition` — the sanctioned, audited, checksum-tied
write path to `workflow_definitions` — and nothing in this repository ever
called it. The consequences ran deeper than an unused function:

  * No job type had a loaded definition row, so `work_units`'s FK to
    `workflow_definitions` could never be satisfied, so every work unit
    creation would have failed. The seam therefore SWALLOWED that FK
    violation, which made the whole intent layer silently optional — the
    entire work-unit/attempt identity chain was inert, `work_unit_id` was
    NULL on every attempt row, and the closure discipline in
    `pipeline.reconciler.service` was a no-op nobody could observe.
  * The swallow was keyed on a MESSAGE SUBSTRING, the second prohibition in
    rule 12.
  * Nothing verified that an enabled work stream had a definition at all.
    Discovery was lazy and per-pass: a stream with no definition simply did
    not get work units, forever, silently.

Three things replace that: `definition_files`/`read_definition` (what the
release ships), `load_definitions` (the deployment step that invokes the
loader — idempotent, audited), and `verify_work_stream_completeness` (one
systemic startup check that fails closed).

**THE CHECKSUM IS OVER THE FILE'S BYTES**, as each definition file's own
header states: "the checksum recorded at load is the sha256 of this file's
bytes, so an edit here is visible as a checksum mismatch against every
version already loaded". Deliberately NOT the canonicalized-parsed-mapping
digest that `science_config.digest` and `startup.configuration_digest`
compute — those exist to make semantically identical configurations compare
equal, which is the opposite of what is wanted here. A comment-only edit to a
reviewed, checksummed definition file must NOT pass as the same version: the
whole file is the reviewed artifact, comments included, and version
immutability is enforced by the loader raising on a same-version different-
checksum load.

**ONE STARTUP CHECK, NOT LAZY PER-PASS DISCOVERY** (rule 12, verbatim: "One
startup completeness check verifies every enabled work stream has one
coherent specification, route and definition"). `verify_work_stream_
completeness` cross-references the four registries that must agree —
the gatherer registry (which (job_type, operational_class) pairs are
enabled), the route matrix, the stage sequences, and the loaded definition
rows — and raises with the offending stream named.

**"NOTHING ENABLED" IS NOT AN ERROR.** A live defect on 2026-08-08 taught
that a supervised service with nothing to do must IDLE, not exit: exiting
turns an empty work list into a restart loop. So this check fails closed on a
stream that is enabled but incoherent, and says nothing about a deployment
where no stream is enabled at all — that is a valid, quiet configuration, not
a fault.
"""

import hashlib
import logging
import os
import tomllib

from pipeline.runtime.errors import ConfigError

logger = logging.getLogger(__name__)

#: The definition files' home, relative to the repository/software root. They
#: ship with the release (rule 12: "Task and process specifications ship with
#: the application release"), so this is a path inside the image, not a
#: configurable location.
DEFINITION_SUBDIR = os.path.join("cdf", "workflow")

#: The filename suffix a v1 definition carries. The version is in the NAME as
#: well as the body so a reviewer sees it in a directory listing; the body is
#: authoritative and `read_definition` cross-checks the two.
DEFINITION_SUFFIX = "-v1.toml"

#: The state machine v1 definitions declare. A file naming a different one is
#: refused rather than loaded: this reader implements exactly the six-state
#: work-unit machine migration 036 created.
STATE_MACHINE_V1 = "work-unit-v1"


class DefinitionError(ConfigError):
    """A definition file is missing, unparseable, or internally inconsistent.

    A subclass of `ConfigError` so the existing fail-closed handling at every
    service and payload entry point catches it with no new wiring — the repo
    already maps `ConfigError` to a nonzero start-failed exit.
    """


class WorkStreamIncomplete(ConfigError):
    """An enabled work stream is missing a route, a sequence or a definition.

    The startup completeness check's failure. Also a `ConfigError` for the
    same reason as above: fail-closed is the existing contract, and this is
    exactly a configuration fault.
    """


def definitions_root(software_root=None):
    """Where the shipped definition files live.

    `software_root` is injected in tests; production passes nothing and the
    path resolves from this module's own location, which is inside the image
    the definitions shipped with — the same resolution style
    `science_config.config_path` uses.
    """
    if software_root is not None:
        return os.path.join(software_root, DEFINITION_SUBDIR)
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(os.path.dirname(here))
    return os.path.join(repo_root, DEFINITION_SUBDIR)


def definition_files(software_root=None):
    """Every shipped definition file, sorted by name.

    Sorted so the deployment step's audit trail is in a stable order run to
    run — an operator diffing two loads should see the same sequence, not
    filesystem order.
    """
    root = definitions_root(software_root)
    try:
        names = sorted(name for name in os.listdir(root)
                       if name.endswith(DEFINITION_SUFFIX))
    except FileNotFoundError as exc:
        raise DefinitionError(
            f"the release's workflow definitions are missing at {root}; they "
            "ship with the image, so their absence means the image is not "
            "the one this code expects") from exc
    return [os.path.join(root, name) for name in names]


def read_definition(path):
    """One reviewed definition file: its declared identity and byte checksum.

    Returns a dict with `job_type`, `version`, `state_machine`,
    `description`, `checksum` (sha256 of the file's bytes) and `source_path`
    (the basename — what gets recorded, since the absolute path is a property
    of the machine that ran the load, not of the reviewed artifact).

    Every failure is raised as `DefinitionError` with a sentence saying why
    it is a fault, matching `science_config.load`'s three-arm idiom rather
    than letting a `KeyError` or a `TOMLDecodeError` escape untyped.
    """
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except FileNotFoundError as exc:
        raise DefinitionError(
            f"workflow definition {path} disappeared between listing and "
            "reading; the image's definition directory is not stable") from exc

    checksum = hashlib.sha256(raw).hexdigest()

    try:
        content = tomllib.loads(raw.decode("utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise DefinitionError(
            f"could not parse workflow definition {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise DefinitionError(
            f"workflow definition {path} is not valid UTF-8") from exc

    section = content.get("definition")
    if not isinstance(section, dict):
        raise DefinitionError(
            f"workflow definition {path} has no [definition] table; the "
            "reviewed shape is one [definition] table with job_type, "
            "version, state_machine and description")

    missing = [key for key in ("job_type", "version", "state_machine",
                               "description")
               if key not in section]
    if missing:
        raise DefinitionError(
            f"workflow definition {path} is missing "
            f"{', '.join(sorted(missing))}; all four keys are required "
            "because the loaded row is keyed on (job_type, version) and "
            "audited with its description")

    job_type = section["job_type"]
    version = section["version"]
    state_machine = section["state_machine"]

    if not isinstance(job_type, str) or not job_type.strip():
        raise DefinitionError(
            f"workflow definition {path} declares a non-string or empty "
            f"job_type ({job_type!r})")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise DefinitionError(
            f"workflow definition {path} declares version {version!r}; "
            "versions are integers >= 1 (migration 036's "
            "workflow_definitions_version_ck)")
    if state_machine != STATE_MACHINE_V1:
        raise DefinitionError(
            f"workflow definition {path} declares state_machine "
            f"{state_machine!r}; this reader implements only "
            f"{STATE_MACHINE_V1!r} — a different machine is a new "
            "implementation, not a configuration value")

    # THE NAME AND THE BODY MUST AGREE. The version appears in the filename
    # for a reviewer's benefit and in the body for the loader's; a file whose
    # two disagree is ambiguous about which version is being reviewed, and
    # guessing either way would load content under a version nobody approved.
    expected_name = f"{job_type}-v{version}.toml"
    actual_name = os.path.basename(path)
    if actual_name != expected_name:
        raise DefinitionError(
            f"workflow definition {actual_name} declares "
            f"job_type={job_type!r} version={version!r}, which names the file "
            f"{expected_name}; the filename and the body must agree so the "
            "reviewed artifact is unambiguous")

    return {
        "job_type": job_type,
        "version": version,
        "state_machine": state_machine,
        "description": section["description"],
        "checksum": checksum,
        "source_path": os.path.join(DEFINITION_SUBDIR, actual_name),
    }


def shipped_definitions(software_root=None):
    """Every shipped definition, keyed by job type.

    Raises `DefinitionError` if two files declare the same job type — the
    registries below key on job type, so a duplicate would make "the
    definition for this stream" ambiguous.
    """
    by_job_type = {}
    for path in definition_files(software_root):
        definition = read_definition(path)
        job_type = definition["job_type"]
        if job_type in by_job_type:
            raise DefinitionError(
                f"two shipped definitions declare job_type={job_type!r} "
                f"({by_job_type[job_type]['source_path']} and "
                f"{definition['source_path']}); the definition for a job "
                "type must be unambiguous")
        by_job_type[job_type] = definition
    return by_job_type


# -- the deployment step -----------------------------------------------------


def load_definitions(execute, *, reason, dry_run=False, dispatcher=None,
                     policy_citation=None, software_root=None):
    """Load every shipped definition through migration 039's loader.

    THE DEPLOYMENT STEP rule 12 requires: "An idempotent, audited loading
    step invokes the loader for every `cdf/workflow/*-v1.toml` at
    deployment; loading is re-runnable (checksum match = no-op; mismatch =
    error, versions are immutable)."

    Idempotence and immutability are NOT re-implemented here — they are
    properties of `derived.load_workflow_definition`, which reads the
    existing checksum back before writing, returns a no-op success on a
    byte-identical reload, and RAISES on the same (job_type, version) with a
    different checksum. This function's job is to invoke it once per shipped
    file with the checksum of that file's bytes, and to report what happened.

    `execute(sql, params)` is the same injected callable the rest of the
    intent layer takes, so this runs inside the caller's transaction and
    needs no driver import. `reason` is mandatory and unvalidated, per the
    mutation contract (migration 030): the caller says why, this does not
    inspect it.

    **THE CALLER MUST HOLD `rapid_operator`.** Migration 039 grants EXECUTE
    on the loader to `rapid_operator` only — deliberately not to either
    service role, because "loading a reviewed definitions file is a human
    review-and-load action". So this is a deploy/operator-invoked step, not
    something a service does to itself at startup. The startup check below
    only READS `workflow_definitions`, which is SELECT-granted broadly.

    Returns the list of per-definition result documents the loader returned,
    in file order.
    """
    if not reason or not str(reason).strip():
        raise ValueError(
            "a reason is mandatory on every mutation (migration 030); "
            "load_definitions will not synthesize one")

    results = []
    for definition in shipped_definitions(software_root).values():
        rows = execute(
            "SELECT derived.load_workflow_definition("
            "  %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [definition["job_type"], definition["version"],
             definition["checksum"], definition["source_path"],
             definition["description"], reason, dry_run, policy_citation,
             dispatcher])
        # The loader RETURNS jsonb; the executor contract yields rows when
        # the statement produced a result set.
        outcome = rows[0][0] if rows else None
        results.append(outcome)
        logger.info(
            "workflow definition %s v%s: %s",
            definition["job_type"], definition["version"], outcome)
    return results


def loaded_definitions(execute):
    """The `(job_type, definition_version)` rows currently loaded.

    Read-only, so any role with SELECT on `workflow_definitions` (migration
    036 grants it broadly) can run the startup check.
    """
    rows = execute(
        "SELECT job_type, definition_version, checksum"
        "  FROM workflow_definitions", [])
    return {(job_type, version): checksum
            for job_type, version, checksum in rows or ()}


# -- the startup completeness check ------------------------------------------


def enabled_work_streams():
    """The `(job_type, operational_class)` pairs this release can run.

    Read from `pipeline.operator.gathering.REGISTRY` — the registry that
    decides which streams are polled at all, and therefore the only honest
    answer to "which work streams are enabled". Imported inside the function
    so this module stays importable on hosts with no science stack: the
    gatherer registry reaches into the stage packages, and `submission/` must
    not depend on those (see `submission.routes`'s own note on the same
    layering rule).
    """
    from pipeline.operator.gathering import REGISTRY

    return tuple((registry_key, class_name, route_job_type)
                 for registry_key, class_name, _gather, route_job_type
                 in REGISTRY)


def verify_work_stream_completeness(execute, software_root=None):
    """Fail closed unless every enabled work stream is completely specified.

    ONE systemic check (rule 12), run at service startup. For each enabled
    stream it verifies four things agree:

      1. a shipped, parseable definition file exists for the route's job type
      2. that definition's version is loaded in `workflow_definitions`
      3. the loaded checksum matches the shipped file's bytes
      4. the route's job type is one the release actually implements

    Raises `WorkStreamIncomplete` naming every offending stream — all of
    them, not just the first: an operator fixing a deployment wants the whole
    list, and failing one-at-a-time turns one restart into five.

    Returns the number of streams verified, so a caller can log it (and so a
    zero — the valid "nothing enabled" deployment — is visible rather than
    indistinguishable from a check that did not run).

    **STAGE SEQUENCES ARE CHECKED VIA THE ROUTE MATRIX, NOT DIRECTLY.**
    `submission.routes.IMPLEMENTED_JOB_TYPES` is maintained to agree with
    `pipeline.stages.sequences.SEQUENCES` and is asserted against it by an
    existing test; reading it here rather than importing the stage packages
    keeps this check runnable in the submission layer's own environment.
    """
    from submission.routes import IMPLEMENTED_JOB_TYPES

    streams = enabled_work_streams()
    if not streams:
        # NOTHING ENABLED IS NOT A FAULT — see the module docstring. A
        # deployment that polls no streams idles; it does not fail to start.
        logger.info(
            "work-stream completeness check: no work streams are enabled in "
            "this release's gatherer registry; nothing to verify")
        return 0

    shipped = shipped_definitions(software_root)
    loaded = loaded_definitions(execute)

    problems = []
    for registry_key, class_name, route_job_type in streams:
        stream = f"{registry_key} (class={class_name})"

        if route_job_type not in IMPLEMENTED_JOB_TYPES:
            problems.append(
                f"{stream}: submits under route job type "
                f"{route_job_type!r}, which this release does not implement")
            continue

        definition = shipped.get(route_job_type)
        if definition is None:
            problems.append(
                f"{stream}: no shipped workflow definition for job type "
                f"{route_job_type!r} (expected "
                f"{DEFINITION_SUBDIR}/{route_job_type}{DEFINITION_SUFFIX})")
            continue

        key = (route_job_type, definition["version"])
        if key not in loaded:
            problems.append(
                f"{stream}: workflow definition {route_job_type} "
                f"v{definition['version']} is shipped but not loaded; run "
                "the definition-loading deployment step "
                "(pipeline.intent.definitions.load_definitions) before "
                "starting this service")
            continue

        if loaded[key] != definition["checksum"]:
            problems.append(
                f"{stream}: workflow definition {route_job_type} "
                f"v{definition['version']} is loaded with checksum "
                f"{loaded[key]} but the shipped file's bytes hash to "
                f"{definition['checksum']}; versions are immutable, so this "
                "image and this database disagree about what v"
                f"{definition['version']} is")

    if problems:
        raise WorkStreamIncomplete(
            "the enabled work streams are not completely specified, so this "
            "service will not start:\n  - " + "\n  - ".join(problems))

    logger.info(
        "work-stream completeness check: %d enabled work streams each have a "
        "coherent route, shipped definition and loaded definition version",
        len(streams))
    return len(streams)
