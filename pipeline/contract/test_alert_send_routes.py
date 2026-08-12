"""
File:    test_alert_send_routes.py

THE ONLY-ROUTE ASSERTION: after brief E, production transport construction is
reachable from the publisher entry point and from nowhere else.

Rule 14 splits alert delivery so that packets reach the broker through the
outbox and the publisher. That split is only real if no OTHER route survives —
a second component that constructs a producer would bypass the outbox, the
delivery policy check, the pinned schema version and every clock, and would be
indistinguishable at the broker from a legitimate packet.

**A REPO-WIDE SCAN, NOT A UNIT TEST.** The property is "nothing else in the
tree does this", which is not a property of any one module and cannot be
asserted by importing one. So this walks the source and reads it — the same
shape as any other structural contract test, and the only shape that can
notice a NEW send route added next month by someone who never read this file.

**IT SCANS CODE, NOT PROSE.** Comments and docstrings are stripped before the
search, and this is not a nicety: the alert-production stage's own comments
explain at length why it no longer builds a producer, and NAME the factories
while doing so. A scan that tripped on those would punish the file for
documenting the very rule it obeys — so the first version of this test, which
matched raw text, failed on `alert_production.py:151`. Stripping is done with
`tokenize`, so it follows Python's own lexer rather than a regex that would
mistake a `#` inside a string for a comment.

**IT IS STILL A SOURCE SCAN, AND THAT IS A DELIBERATE LIMITATION**, stated
rather than hidden: a caller could defeat it with `getattr(module, "make_" +
"producer")`. The scan catches what an engineer writes, not what an adversary
hides, which is the right threat model for a guard against accidental
reintroduction. Nothing here should be read as sandboxing.

**WHY IT LIVES IN THE CONTRACT TIER** with no database: the tier is where
properties of the deployed system live, and "how many ways can this system
reach the broker" is exactly that. It carries no `has_table` probe because it
needs no schema — it therefore runs in CI too, and CI is where a
reintroduction is most likely to be caught early.
"""

import io
import pathlib
import tokenize
import unittest

#: The repository root: this file is `<root>/pipeline/contract/<name>.py`.
ROOT = pathlib.Path(__file__).resolve().parents[2]

#: The trees that ship in the wheel (`pyproject.toml`'s package list). Sims,
#: docs and the C tree are not scanned: nothing there is installed, imported by
#: a service, or able to reach a broker in production.
SCANNED_TREES = ("pipeline", "alerts", "submission", "observability",
                 "database", "modules", "aws")

#: The names that MEAN "a live producer is being built" or "bytes are going to
#: a broker". `make_producer` and `make_transport` are the two factories in
#: `alerts/kafka_producer.py`; `KafkaProducer` is kafka-python's own class, in
#: case someone bypasses the factories entirely.
PRODUCTION_TRANSPORT_NAMES = ("make_producer", "make_transport",
                              "KafkaProducer")

#: Where a production transport may legitimately be constructed or named.
#:
#: `alerts/kafka_producer.py` DEFINES the factories — it is the implementation,
#: not a caller. `pipeline/publisher/service.py` is the one entry point that
#: calls them. Everything else is a violation.
#:
#: The tests below are allowed to NAME them (they assert about them), which is
#: why test files are excluded from the scan rather than allowlisted one by
#: one: a test tree that could not mention a forbidden symbol could not test
#: for it.
ALLOWED_CONSTRUCTION_SITES = (
    "alerts/kafka_producer.py",
    "pipeline/publisher/service.py",
)


def code_without_comments(source):
    """`source` with comments and docstrings removed, for scanning.

    THE REASON THIS EXISTS is recorded in the module docstring: a raw-text scan
    punished `alert_production.py` for EXPLAINING why it no longer builds a
    producer, because the explanation names the factory. Observed live on the
    first acceptance run of this branch.

    `tokenize` is used rather than a regex because it is Python's own lexer: a
    regex for `#.*$` deletes the contents of any string containing a hash, and
    a regex for triple-quoted blocks cannot tell a docstring from a multi-line
    string literal that matters. String LITERALS are kept — a send route built
    from `getattr(mod, "make_producer")` should still be caught — except where
    the string is a statement on its own, which is what a docstring is.

    A file that does not tokenize (a syntax error) returns unchanged rather
    than raising: this scan is not the place to discover that, and failing here
    would attribute a parse error to the send-route rule.
    """
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return source

    kept = []
    previous_type = tokenize.INDENT
    for token in tokens:
        if token.type == tokenize.COMMENT:
            continue
        # A docstring is a STRING token standing alone as a statement — that
        # is, one preceded by NEWLINE, INDENT, DEDENT, or nothing at all.
        if token.type == tokenize.STRING and previous_type in (
                tokenize.NEWLINE, tokenize.NL, tokenize.INDENT,
                tokenize.DEDENT, tokenize.ENCODING):
            previous_type = token.type
            continue
        kept.append(token.string)
        if token.type not in (tokenize.NL, tokenize.COMMENT):
            previous_type = token.type
    return "\n".join(kept)


def _python_sources():
    """Every shipped .py file outside the test trees, as (path, code).

    The text yielded is COMMENT- AND DOCSTRING-FREE (see
    `code_without_comments`), so the scan reads what the module does rather
    than what it says about itself.

    Test files are excluded wholesale: they legitimately name the forbidden
    symbols, both to stub them and — in this very module — to assert about
    them. `conftest.py` is excluded for the same reason.
    """
    for tree in SCANNED_TREES:
        base = ROOT / tree
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            relative = path.relative_to(ROOT).as_posix()
            parts = set(path.parts)
            if "test" in parts or "tests" in parts:
                continue
            if path.name.startswith("test_") or path.name == "conftest.py":
                continue
            yield relative, code_without_comments(
                path.read_text(encoding="utf-8", errors="replace"))


class OnlyRouteTests(unittest.TestCase):
    """Production transport construction is the publisher's alone."""

    def test_no_module_outside_the_publisher_builds_a_producer(self):
        offenders = []
        for relative, text in _python_sources():
            if relative in ALLOWED_CONSTRUCTION_SITES:
                continue
            for name in PRODUCTION_TRANSPORT_NAMES:
                if name in text:
                    offenders.append(f"{relative} names {name!r}")
        self.assertEqual(
            offenders, [],
            "production transport construction must be reachable from the "
            "publisher entry point alone (rule 14: the outbox and the "
            "publisher are the only delivery route). A module that builds a "
            "producer bypasses the outbox, the delivery policy, the pinned "
            "schema version and every latency clock. Offending references: "
            + "; ".join(offenders))

    def test_the_publisher_entry_point_does_build_one(self):
        # THE COMPLEMENT, and it matters: an assertion that "nothing
        # constructs a producer" would pass vacuously if the publisher stopped
        # doing so too — a system that delivers nothing satisfies "only one
        # route" perfectly. This is the test that would fail if E's whole
        # delivery path were deleted.
        text = (ROOT / "pipeline/publisher/service.py").read_text()
        self.assertIn("make_producer", text)

    def test_the_alert_production_stage_constructs_no_producer(self):
        # NAMED SPECIFICALLY, beyond the sweep above, because this module is
        # where the send used to be (`_make_internal_producer`, removed by
        # brief E2) and it is the single most likely place for one to come
        # back — a future edit "restoring" in-job publishing would look
        # locally reasonable and would silently reopen the whole gap.
        text = code_without_comments(
            (ROOT / "pipeline/stages/alert_production.py").read_text())
        for name in PRODUCTION_TRANSPORT_NAMES:
            self.assertNotIn(
                name, text,
                f"the alert-production stage names {name!r}: after brief E it "
                f"assembles packets and commits them to the outbox, and the "
                f"publisher owns the wire")
        self.assertNotIn(
            "_make_internal_producer", text,
            "`_make_internal_producer` is back in the alert-production "
            "stage; delivery belongs to rapid-publisher (rule 14)")

    def test_the_alerts_cli_cannot_publish(self):
        # The CLI's `--kafka` mode was the OTHER live route. The flag is
        # retained deliberately (so an operator typing it gets an explanation
        # rather than "unrecognized arguments"), so the assertion is about
        # BEHAVIOUR — it must not be able to build a producer — rather than
        # about the flag's absence.
        text = code_without_comments((ROOT / "alerts/cli.py").read_text())
        for name in PRODUCTION_TRANSPORT_NAMES:
            self.assertNotIn(
                name, text,
                f"alerts/cli.py names {name!r}: the CLI assembles alerts and "
                f"writes archives, and must not be a second way onto the wire")

    def test_the_publisher_frames_from_stored_fields_not_the_registry(self):
        # The registry lookup is what makes wire bytes depend on WHEN a packet
        # is sent (`LatestVersion: True`), so its absence from the send path is
        # the mechanism behind "identical bytes on resend". Asserted
        # structurally here as well as behaviourally in the outbox contract
        # tests, because this is the property most likely to be undone by an
        # innocent-looking refactor that "simplifies" framing.
        text = code_without_comments(
            (ROOT / "pipeline/publisher/cycle.py").read_text())
        self.assertIn("frame_alert", text)
        # THE FORBIDDEN NAMES ARE THE REGISTRY'S, not the row's. An earlier
        # version of this test forbade the substring `schema_version_id(`,
        # which matched the cycle passing its own row-derived
        # `schema_version_id` variable to a helper — a false positive on the
        # correct implementation. What must be absent is any way to ASK the
        # registry: its class, and the API shape that requests the latest
        # version.
        for forbidden in ("GlueSchemaRegistry", "LatestVersion",
                          "get_schema_version"):
            self.assertNotIn(
                forbidden, text,
                f"the publisher cycle names {forbidden!r}: framing on the "
                f"send path must use the row's PINNED schema version, never a "
                f"registry lookup, or a resend after a registry bump would "
                f"produce different wire bytes")


if __name__ == "__main__":
    unittest.main()
