"""Import gate run at container build time (see the Containerfile).

Imports the modules the built image must be able to import before it is
usable, and nothing else -- a base environment missing a runtime
dependency fails the build here, not a Batch job later. The module list
is deliberately not the full science stack: it follows requirements.txt
(numpy, astropy and psycopg2-binary are declared there directly) plus the
two things every stage needs regardless of what requirements.txt lists
(``rapidpipe`` itself and its CLI entrypoint). ``healpy`` is imported only
when requirements.txt names it, so this gate does not fail on a base
built before that dependency was added (or on the CI stand-in base, which
never installs it) -- see containers/rapid-pipeline/build.sh and
.github/workflows/container.yml for where each base comes from.

Run as: <python> import-gate.py [requirements.txt]

The Containerfile copies this file to /tmp and runs it after the source
tree has already landed at /code, so the requirements file defaults to
/code/requirements.txt; pass a path explicitly to check a different one
(as build.sh's own local `--python`/dry-run use never does today, but a
future caller might).
"""

from __future__ import annotations

import pathlib
import sys

DEFAULT_REQUIREMENTS_PATH = pathlib.Path("/code/requirements.txt")

#: Always required, regardless of requirements.txt: the package under
#: build and its command-line entrypoint.
ALWAYS = ["rapidpipe", "rapidpipe.cli.main"]

#: Optional modules, imported only when their requirements.txt entry is
#: present. Extend this map if a future dependency needs the same
#: build-time gate; do not hard-code the check for a package this repo
#: has not yet declared as a requirement.
OPTIONAL_BY_REQUIREMENT = {
    "healpy": "healpy",
    "numpy": "numpy",
    "astropy": "astropy",
    "psycopg2-binary": "psycopg2",
}


def _requirement_names(text: str) -> set[str]:
    names = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        # Strip any version specifier; keep just the requirement name.
        name = line
        for sep in (">=", "==", "<=", "~=", "!=", ">", "<"):
            name = name.split(sep, 1)[0]
        names.add(name.strip())
    return names


def modules_to_check(requirements_path: pathlib.Path) -> list[str]:
    modules = list(ALWAYS)
    try:
        requirements = _requirement_names(requirements_path.read_text())
    except FileNotFoundError:
        requirements = set()
    for requirement, module in OPTIONAL_BY_REQUIREMENT.items():
        if requirement in requirements:
            modules.append(module)
    return modules


def main() -> int:
    requirements_path = (
        pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_REQUIREMENTS_PATH
    )
    modules = modules_to_check(requirements_path)
    failures = []
    for module in modules:
        try:
            __import__(module)
        except Exception as exc:  # noqa: BLE001 -- report every failure, don't stop at the first
            failures.append(f"{module}: {exc}")
    if failures:
        sys.stderr.write("import-gate: FAILED to import:\n")
        for failure in failures:
            sys.stderr.write(f"  {failure}\n")
        return 1
    sys.stdout.write(f"import-gate: OK ({', '.join(modules)})\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
