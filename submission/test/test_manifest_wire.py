"""The manifest wire format after rule 11 (brief D, criterion 7).

"Serialized JSON has no `fields` key, no non-applicable `exposure`/`sca`,
exact closed payload keys per job type; version-3 payloads refused; no
production reader of `ProcessingUnit.fields` remains and no serialized unit
carries a `fields` key; the V25 dedup regression stays green."

**ASSERTED OVER THE SERIALIZED TEXT, not over the objects.** The rule is
about the physical representation — "the sentinel-shaped carrier persists
as the physical representation" is exactly what the conformance report
scored rule 11 PARTIAL for — so a test that only inspected Python objects
would be checking the half that was already repaired.

A stub-tier test: the manifest is pure Python and needs no database.
"""

import json

import pytest

from submission import payloads
from submission.manifest import Manifest, ProcessingUnit, UnitFacts
from submission.routes import (JOB_TYPE_ALERT_PRODUCTION,
                               JOB_TYPE_CATALOG_LOAD, JOB_TYPE_CROSSMATCH,
                               JOB_TYPE_REFERENCE_IMAGE, JOB_TYPE_SCIENCE,
                               JOB_TYPE_STATISTICS)

#: One representative unit per job type, with the arguments each payload
#: actually requires. Built once and reused so every assertion below runs
#: over the same set — a per-test ad-hoc unit would let one job type quietly
#: drop out of the coverage.
UNITS = {
    JOB_TYPE_SCIENCE: dict(exposure=90001, sca=3),
    JOB_TYPE_REFERENCE_IMAGE: dict(exposure=90002, sca=4),
    JOB_TYPE_ALERT_PRODUCTION: dict(
        exposure=90003, sca=5, promoted_attempt_id=77,
        release_identity="release-1", difference_image_pid=1234),
    JOB_TYPE_CATALOG_LOAD: dict(proc_date="20260812", sca=6,
                                target_table="sources_20260812_6"),
    JOB_TYPE_CROSSMATCH: dict(proc_date="20260812", field=4242,
                              target_tables=("astroobjects_4242",
                                             "merges_4242")),
    JOB_TYPE_STATISTICS: dict(field=4242,
                              target_table="astroobjects_4242"),
}

#: Which components each grain legitimately carries. `exposure` and `sca`
#: appear ONLY where the grain declares them — that is the property under
#: test, written out explicitly rather than derived from the code it checks,
#: so a mistake in the code cannot make this table agree with it.
EXPECTED_PAYLOAD_KEYS = {
    JOB_TYPE_SCIENCE: {"grain", "exposure", "sca"},
    JOB_TYPE_REFERENCE_IMAGE: {"grain", "exposure", "sca"},
    JOB_TYPE_ALERT_PRODUCTION: {"grain", "exposure", "sca",
                                "promoted_attempt_id", "release_identity",
                                "difference_image_pid"},
    JOB_TYPE_CATALOG_LOAD: {"grain", "proc_date", "sca", "target_table",
                            "product_inputs"},
    JOB_TYPE_CROSSMATCH: {"grain", "proc_date", "field", "target_tables"},
    JOB_TYPE_STATISTICS: {"grain", "field", "target_table"},
}


def _unit(job_type):
    return ProcessingUnit(payload=payloads.build(job_type, **UNITS[job_type]))


def _manifest(job_type):
    return Manifest([_unit(job_type)], batch_id="run-wire-1",
                    job_type=job_type)


@pytest.mark.parametrize("job_type", sorted(UNITS))
def test_serialized_manifest_has_no_fields_key_anywhere(job_type):
    """No `fields` key exists at any depth of the serialized manifest.

    Scanned recursively rather than checked at the unit level: the open
    dict's whole problem was that it could carry anything anywhere, so the
    absence has to be asserted everywhere.
    """
    text = _manifest(job_type).to_json()
    document = json.loads(text)

    def walk(node, trail="$"):
        if isinstance(node, dict):
            assert "fields" not in node, (
                f"a `fields` key survives at {trail} in the {job_type} "
                f"manifest: {node!r}")
            for key, value in node.items():
                walk(value, f"{trail}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{trail}[{index}]")

    walk(document)


@pytest.mark.parametrize("job_type", sorted(UNITS))
def test_serialized_unit_carries_exactly_its_declared_payload_keys(job_type):
    """The payload is CLOSED: exactly the declared keys, no more, no less."""
    document = json.loads(_manifest(job_type).to_json())
    payload = document["units"][0]["payload"]
    assert set(payload) == EXPECTED_PAYLOAD_KEYS[job_type], (
        f"{job_type} serialized {sorted(payload)}, expected "
        f"{sorted(EXPECTED_PAYLOAD_KEYS[job_type])}")


@pytest.mark.parametrize("job_type", [JOB_TYPE_CROSSMATCH,
                                      JOB_TYPE_STATISTICS])
def test_non_exposure_grains_serialize_no_exposure_and_no_sca(job_type):
    """The sentinel is GONE from the wire, not merely routed around.

    THE DEFECT, precisely. A crossmatch unit used to serialize
    `exposure=<date ordinal>, sca=0` — self-described in the source as "NOT
    this unit's identity, only its shape". A field-grained unit used to
    serialize the FIELD NUMBER in `exposure`. Neither key exists now.
    """
    document = json.loads(_manifest(job_type).to_json())
    payload = document["units"][0]["payload"]
    assert "exposure" not in payload
    assert "sca" not in payload


def test_catalog_load_keeps_sca_because_its_grain_declares_one():
    """The complement, so the rule is not read as "never carry an sca".

    Date/SCA is a real grain with a real SCA — `(proc_date, sca)`,
    `subjects.py`'s `GRAIN_DATE_SCA`. What rule 11 prohibits is a component
    a grain does not declare, not a component named `sca`.
    """
    document = json.loads(_manifest(JOB_TYPE_CATALOG_LOAD).to_json())
    payload = document["units"][0]["payload"]
    assert payload["sca"] == 6
    assert "exposure" not in payload


@pytest.mark.parametrize("job_type", sorted(UNITS))
def test_a_manifest_round_trips_through_json(job_type):
    """Serialize, parse, and get an equal unit back.

    The property that makes the wire format usable at all: the submitter
    writes it and the job reads it, and a shape that could not round-trip
    would fail in production at exactly the point where nothing can be
    recovered.
    """
    original = _manifest(job_type)
    restored = Manifest.from_json(original.to_json())
    assert restored.job_type == job_type
    assert len(restored.units) == 1
    assert restored.units[0].payload == original.units[0].payload
    assert restored.units[0].dedup_key() == original.units[0].dedup_key()


def test_the_schema_version_is_four():
    assert Manifest.SCHEMA_VERSION == 4
    document = json.loads(_manifest(JOB_TYPE_SCIENCE).to_json())
    assert document["schema_version"] == 4


def test_a_version_three_manifest_is_refused():
    """Version 3 is refused, not translated.

    Brief D: "strict refusal of version-3 payloads ... and NO compatibility
    parser that silently rebuilds typed subjects from sentinel
    exposure/SCA." Asserted with a real version-3 document — the sentinel
    shape a live submitter would have written — rather than with a bare
    version number, so the test would fail if a compatibility parser were
    ever added that read the units regardless of the version check.
    """
    version_three = {
        "schema_version": 3,
        "batch_id": "run-old-1",
        "job_type": JOB_TYPE_CROSSMATCH,
        "array_size": 1,
        "units": [{"exposure": 20260812, "sca": 0,
                   "fields": {"proc_date": "20260812", "field": 4242,
                              "job_type": JOB_TYPE_CROSSMATCH}}],
    }
    with pytest.raises(ValueError) as raised:
        Manifest.from_dict(version_three)
    assert "schema_version" in str(raised.value)


def test_a_version_four_unit_without_a_payload_is_refused():
    """The other half: right version, old unit shape.

    A document that claimed version 4 while carrying version-3 units would
    slip past the version check, so the unit reader refuses independently
    rather than trusting the envelope.
    """
    with pytest.raises(ValueError) as raised:
        ProcessingUnit.from_dict({"exposure": 1, "sca": 2, "fields": {}},
                                 JOB_TYPE_SCIENCE)
    assert "payload" in str(raised.value)


def test_no_compatibility_parser_rebuilds_a_subject_from_a_sentinel():
    """There is no code path that turns exposure/sca back into a subject.

    Asserted by source inspection, because the claim is about what does NOT
    exist and no behavioural test can demonstrate the absence of a path.
    What it looks for is a reader that accepts the old keys: if someone
    later adds `raw.get("exposure")` to a unit parser, this fails.
    """
    import os
    import re

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    source = open(os.path.join(root, "manifest.py"), encoding="utf-8").read()

    # `from_dict` is the only parser. It must not read the retired keys.
    start = source.index("    def from_dict(cls, raw: dict[str, Any], "
                         "job_type: str = None")
    end = source.index("class Manifest", start)
    unit_parser = source[start:end]

    # COMMENTS AND DOCSTRINGS ARE EXCLUDED, and the exclusion is the point
    # of this being a code check rather than a text search. That method's
    # docstring NAMES the retired shape — `{"exposure": ..., "sca": ...,
    # "fields": {...}}` — in order to explain what it refuses and why, which
    # is exactly the documentation this rule deserves. A scanner that
    # flagged the explanation would push the next author to delete the
    # explanation rather than the behaviour.
    code_only = []
    in_docstring = False
    for line in unit_parser.splitlines():
        stripped = line.strip()
        if stripped.count('"""') == 1:
            in_docstring = not in_docstring
            continue
        if in_docstring or stripped.startswith("#"):
            continue
        code_only.append(line)
    code = "\n".join(code_only)

    for retired in ('raw["exposure"]', 'raw.get("exposure")',
                    'raw["sca"]', 'raw.get("sca")',
                    'raw["fields"]', 'raw.get("fields"'):
        assert retired not in code, (
            f"the unit parser reads {retired}: a compatibility path that "
            f"rebuilds a typed subject from the sentinel carrier is exactly "
            f"what rule 11 and brief D forbid")


def test_the_v25_dedup_regression_stays_fixed():
    """Two crossmatch fields of one date remain distinct. (The V25 defect.)

    The regression this whole area exists for, re-asserted against the new
    representation. It used to be defended by routing dedup around the
    sentinel; now the two units have no shared representation to collide
    in — but the assertion is the same one, because the property that
    matters to the pipeline is unchanged.
    """
    first = ProcessingUnit(payload=payloads.build(
        JOB_TYPE_CROSSMATCH, proc_date="20260812", field=101,
        target_tables=("astroobjects_101",)))
    second = ProcessingUnit(payload=payloads.build(
        JOB_TYPE_CROSSMATCH, proc_date="20260812", field=202,
        target_tables=("astroobjects_202",)))

    assert first.dedup_key() != second.dedup_key()
    assert first.key != second.key
    assert (first.logical_job_key("run-1", JOB_TYPE_CROSSMATCH)
            != second.logical_job_key("run-1", JOB_TYPE_CROSSMATCH))


def test_exposure_sca_keys_keep_their_exact_previous_spelling():
    """The storage key for exposure/SCA units is byte-identical to before.

    Load-bearing: that string is embedded in every product key ever written
    under `product_prefix()`, so a change to its shape would orphan every
    existing object. Zero-padded 6 and 2 digits, exactly as the storage
    design's key schema fixes it.
    """
    unit = ProcessingUnit(payload=payloads.build(
        JOB_TYPE_SCIENCE, exposure=90001, sca=3))
    assert unit.key == "090001/03"


def test_no_production_module_reads_a_units_fields_attribute():
    """No production reader of `ProcessingUnit.fields` remains.

    Scoped to UNIT readers, per brief D's own parenthetical: unrelated APIs
    named `fields` — `dataclasses.fields`, a repository's `.fields()`
    method, a local variable — are not in scope, and the pattern below
    matches only an attribute access on something named like a unit.
    """
    import os
    import re

    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    pattern = re.compile(r"\b(unit|self)\.fields\b")

    offenders = []
    for directory in ("submission", "pipeline", "alerts", "observability"):
        base = os.path.join(root, directory)
        if not os.path.isdir(base):
            continue
        for current, _dirs, files in os.walk(base):
            if os.path.basename(current) in ("test", "__pycache__"):
                continue
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(current, name)
                # `errors="replace"` because the tree contains at least one
                # legacy source file that is not valid UTF-8, and a scanner
                # that dies on it would silently stop checking every file
                # after it — the worst failure mode a completeness check can
                # have. A replacement character cannot create a false match
                # for the ASCII pattern below.
                with open(path, encoding="utf-8", errors="replace") as handle:
                    for number, line in enumerate(handle, 1):
                        stripped = line.strip()
                        # Comments and docstring prose legitimately DISCUSS
                        # the retired dict — this file does too. Only code
                        # is in scope.
                        if stripped.startswith("#"):
                            continue
                        if pattern.search(line):
                            offenders.append(
                                f"{os.path.relpath(path, root)}:{number}")

    assert not offenders, (
        f"these production lines still read a unit's `fields` attribute, "
        f"which no longer exists: {offenders}")
