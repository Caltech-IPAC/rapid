"""
File:    prove_no_exit_code_convention.py

Prove that the payload carries no exit-code convention — against compiled
code, not source text.

The co-design's deletion list is specific: "the `.sh` wrappers, done-files,
log-grep registration, file-local subprocess copies, and the `>= 64`
convention are removed, not dual-run". Removal has to be checkable, and the
obvious check — grep the source for `terminating_exitcode` — gives the wrong
answer here. Every module under `pipeline/entrypoints/` and `pipeline/stages/`
documents in its header what it replaced and why, which means the phrase
appears in exactly the files that removed it. A source grep would fail on the
documentation of the removal it is verifying, and the only way to make it pass
would be to delete the explanation.

So this compiles each module and inspects its code objects instead. A
docstring is a constant string; an identifier is in `co_names` or
`co_varnames`; a comparison against 64 puts the integer in `co_consts`. The
three are distinguishable, and only the last two are the convention.

What is checked, and why each:

`terminating_exitcode` / `aws_batch_job_exitcode` as identifiers
    The monoliths' entire failure vocabulary. One was set to 4 on an SFFT
    failure and to 0 otherwise; the other was assigned from it only when it
    was >= 64, which it could never be. Their absence is the fail-loud
    posture being real rather than intended.

the literal 64 in code
    The `>= 64` test itself, and the `exit(64)` calls in the monoliths'
    environment-variable preambles. 64 is not otherwise a number this layer
    has any use for, so its presence anywhere in the payload's compiled
    constants is worth failing on.

Run standalone (`python -m pipeline.stages.test.prove_no_exit_code_convention`)
or as part of the in-image runner. Exits 0 when the payload is clean, 1 with
the offending file and reason otherwise.

It is a script rather than a unittest case deliberately: it proves a property
of the tree's contents, not of any function's behaviour, and it needs to run
in the image against the installed code — which is what the runner does.
"""

import pathlib
import sys

# The two packages W5 owns. The launcher and registration scripts are NOT
# swept: the cutover fence keeps them until W6 ("the legacy launcher path
# stays"), and they legitimately still carry the convention.
PAYLOAD_PACKAGES = ("pipeline/entrypoints", "pipeline/stages")

FORBIDDEN_NAMES = ("terminating_exitcode", "aws_batch_job_exitcode")
FORBIDDEN_CONSTANT = 64


def _code_objects(code):
    """Every code object in a module, including nested functions."""
    stack = [code]
    while stack:
        obj = stack.pop()
        yield obj
        for const in obj.co_consts:
            if hasattr(const, "co_names"):
                stack.append(const)


def check(path: pathlib.Path) -> list:
    """Findings for one module. Empty means clean."""
    findings = []
    try:
        code = compile(path.read_text(), str(path), "exec")
    except SyntaxError as exc:
        return [f"{path}: does not compile ({exc})"]

    names = set()
    for obj in _code_objects(code):
        names.update(obj.co_names)
        names.update(obj.co_varnames)
        for const in obj.co_consts:
            # `is` rather than `==` on the bool guard: True == 1 in Python,
            # and a bare `const == 64` would also match numpy scalars.
            if isinstance(const, int) and not isinstance(const, bool) \
                    and const == FORBIDDEN_CONSTANT:
                findings.append(
                    f"{path}: the integer 64 appears in compiled code — the "
                    f"`>= 64` convention or an exit(64)")

    for name in FORBIDDEN_NAMES:
        if name in names:
            findings.append(
                f"{path}: {name} is an identifier in compiled code, not just "
                f"a docstring mention")
    return findings


def main() -> int:
    modules = []
    for package in PAYLOAD_PACKAGES:
        root = pathlib.Path(package)
        if not root.is_dir():
            print(f"!! {package} does not exist", file=sys.stderr)
            return 1
        modules.extend(sorted(root.rglob("*.py")))

    if not modules:
        print("!! no payload modules found — the proof would pass vacuously",
              file=sys.stderr)
        return 1

    findings = []
    for path in modules:
        findings.extend(check(path))

    if findings:
        print("\n".join(findings), file=sys.stderr)
        return 1

    print(f"checked {len(modules)} payload modules in "
          f"{', '.join(PAYLOAD_PACKAGES)}")
    print("no terminating_exitcode, no aws_batch_job_exitcode, no literal 64 "
          "in compiled code")
    return 0


if __name__ == "__main__":
    sys.exit(main())
