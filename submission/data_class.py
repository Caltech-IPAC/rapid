"""The data class of a work unit's inputs, and how a mix resolves.

**THE PROVENANCE CHAIN'S ONE COMPUTATION.** The operations design fixes two
values on every input identity at creation — substrate (real | simulated)
and injection (pristine | injected) — and states that "work units, attempts,
products, alerts, and catalog rows inherit their data class from input
identities through the provenance chain, and a mixed derivation takes the
most restrictive class of any input" (`rapid_plan` design/operations.md
§ Continuous validation). This module is that inheritance: reading the class
off the admission manifests covering a unit's inputs, and combining them
when they disagree.

**WHY A MODULE AND NOT A HELPER IN `gathering.py`.** The combination rule is
a claim about SCIENCE AUTHORIZATION, not about gathering. It decides whether
a derived product may ever reach the mission stream, and getting it backwards
would not fail loudly — it would file validation data under a science prefix,
which is precisely the leak the design calls non-waivable. A rule that
consequential is testable on its own terms here, rather than inline in a
loop that is about something else.

**THE ORDERING, AND WHERE IT COMES FROM.** "Most restrictive" is not defined
by the design as a list to copy; it is derived from what the classes MEAN,
and the derivation is stated here rather than assumed:

  * Science is exactly one cell — real AND pristine ("Science is exactly one
    cell: real ∧ pristine; the other three are validation data",
    operations.md). The science gate is non-waivable and prefix-scoped:
    "promotion and publication roles hold no capability outside the science
    prefix, making a validation-data leak an IAM impossibility rather than a
    checked rule" (storage.md).
  * So restrictiveness is per-axis, and each axis has one science-eligible
    value and one that is not: `real` is eligible and `sim` is not;
    `pristine` is eligible and `injected` is not.
  * A derivation from a mixed set must therefore take the NON-ELIGIBLE value
    on every axis where its inputs disagree. Mixing real with simulated gives
    `sim`; mixing pristine with injected gives `injected`. Combining
    `real-pristine` (science) with `sim-injected` yields `sim-injected`, and
    a product built partly from simulated pixels is never science — which is
    the whole point of the rule.

The alternative reading — that "most restrictive" means "hardest to leak
from", making `real-pristine` dominant — inverts the science gate: it would
let a product derived partly from injected sources be filed as science, and
promoted. That reading is refused here explicitly because it is the plausible
one to reach for, and it is the dangerous one.

**THE TOKENS ARE COMPOUND, AND THE COMBINATION IS PER-AXIS.** The registry's
four values are compound tokens (`naming.md` § Token registry: "data-class
values are compound tokens (the `real-pristine` family) that appear only
between `/` delimiters in object keys"), and the storage design gives the
normative model-to-token mapping: substrate `real|simulated` maps to
`real|sim`, injection `pristine|injected` maps to `pristine|injected`. So the
combination splits each token on its one hyphen, resolves each axis
independently, and rejoins — rather than ordering the four tokens as opaque
strings, which would work today only by coincidence of spelling.
"""

# The four registered tokens, split into the two axes the design says they
# are. Kept as the axis pairs rather than a flat list because the
# combination is per-axis: a flat ranking of four opaque strings would
# happen to work for these four spellings and break silently the moment a
# fifth token is ratified.
SUBSTRATES = ("real", "sim")
INJECTIONS = ("pristine", "injected")

#: The science-eligible value on each axis — the one cell the science gate
#: admits. Everything else is validation data.
SCIENCE_SUBSTRATE = "real"
SCIENCE_INJECTION = "pristine"

DATA_CLASSES = tuple(f"{substrate}-{injection}"
                     for substrate in SUBSTRATES
                     for injection in INJECTIONS)


class DataClassError(ValueError):
    """A data class that is not one of the four registered tokens.

    A `ValueError` subclass so a caller that does not care about the
    distinction still catches it, and named so one that does can tell this
    from an ordinary bad argument.
    """


def parse(token):
    """The `(substrate, injection)` pair a registered token names.

    Refuses anything outside the registry rather than splitting whatever it
    is given: this value becomes the LEADING component of an object key, so
    an unregistered token is not a bad string, it is bytes filed under a
    prefix no consumer of that class lists, no lifecycle rule reaches and no
    bucket policy binds.
    """
    if token not in DATA_CLASSES:
        raise DataClassError(
            "%r is not a registered data class; the registry is %s "
            "(rapid_plan design/naming.md, Token registry — a fifth value "
            "is its own ratification, not an append-only change)"
            % (token, ", ".join(DATA_CLASSES)))
    substrate, injection = token.split("-", 1)
    return substrate, injection


def most_restrictive(tokens):
    """The class a derivation from these inputs takes.

    Per-axis, per the module docstring: the non-science value wins on each
    axis where the inputs disagree, because a derivation touching simulated
    pixels or injected sources is not science and must never be filed where
    the promotion roles can reach it.

    Returns None for an empty input set — a unit whose inputs carry no class
    at all inherits nothing, which is the pre-090 state and is handled by the
    builder's fallback, not by inventing a class here. Absence is NOT
    silently resolved to a default: filing an unclassified unit under a
    guessed class is exactly the leak this module exists to prevent.
    """
    pairs = [parse(token) for token in tokens if token is not None]
    if not pairs:
        return None
    substrate = (SCIENCE_SUBSTRATE
                 if all(pair[0] == SCIENCE_SUBSTRATE for pair in pairs)
                 else "sim")
    injection = (SCIENCE_INJECTION
                 if all(pair[1] == SCIENCE_INJECTION for pair in pairs)
                 else "injected")
    return f"{substrate}-{injection}"


def is_science(token):
    """Whether this class is the one cell the science gate admits.

    `real-pristine` and nothing else. Written as a function rather than an
    equality at each call site so the gate reads as the design's own
    sentence, and so a future ratification that changed the cell would change
    it in one place.
    """
    return token == f"{SCIENCE_SUBSTRATE}-{SCIENCE_INJECTION}"
