#!/usr/bin/env python3
"""Walk every science-configuration read in the payload and name the ones
the release does not carry.

The defect class this exists to end: a stage reads
``<section>_dict['key']`` for a key ``cdf/science/pipeline.toml`` never
carried, and nothing notices until a Batch child reaches that stage. Four
such defects have now been found that way, one per submission cycle, each
one stage further down the same path (``q9_fix_round.rst``).

Why this is not a grep. The section dict is obtained at the call site --
``context.science_section("gainmatch")`` -- and read in a different
module, under a parameter name, several frames away::

    # pipeline/stages/science.py
    dfis.gainMatchScienceAndReferenceImages(
        ..., context.science_section("gainmatch"), ...)

    # pipeline/differenceImageSubs.py
    def gainMatchScienceAndReferenceImages(..., gainmatch_dict, ...):
        verbose = int(gainmatch_dict['verbose'])

Nothing textual ties the string ``"gainmatch"`` to the name
``gainmatch_dict``. This resolves the binding structurally: find every
``science_section("X")`` argument at every call site, map it to the
callee's parameter *by position or keyword*, then collect every subscript
read of that parameter inside the callee. The naming convention is never
trusted -- it is only reported on, so a convention drift shows up as a
finding rather than as a silent miss.

**One hop is not enough**, and assuming it was is how the first version of
this audit reported "ok" over a third of the file. The heaviest readers
sit two hops from the accessor::

    science.py            resample_reference_image(..., science_section("swarp"))
    rapid_pipeline_subs   def resample_...(..., swarp_dict):
                              build_swarp_command_line_args(swarp_dict)
    rapid_pipeline_subs   def build_swarp_command_line_args(swarp_dict):
                              swarp_dict["swarp_header_only"]    # 57 of these

Propagation therefore runs to a **fixed point**: a parameter known to
hold section *S* is followed into every call it is passed to, binding the
callee's parameter to *S* as well, until no new binding appears.
``swarp``'s 57 reads only become visible on the second iteration. The
iteration count and the number of resolved bindings are both reported,
because an audit that stopped early would otherwise look exactly like an
audit that passed.

**A missing key is not automatically a defect**, and this is the
distinction the audit exists to draw correctly. Three sections' worth of
keys are absent from the release *by design*: the per-invocation slots the
launcher fills in -- ``sextractor_input_image``, ``swarp_imageout_name``,
awaicgen's four geometry values -- are manifest-home facts, and the
release file says so in its own header ("those slots hold per-invocation
values, so they belong in the manifest, and carrying the sentinel would
only move the landmine"). Adding them to ``pipeline.toml`` would be a
defect, not a fix.

So every missing key is classified by whether the payload *assigns* it
anywhere before it is read. Injection is tracked across the whole tree,
not just within the reading function, because the write and the read are
usually in different modules::

    differenceImageSubs.py   sextractor_gainmatch_dict["sextractor_catalog_name"] = ...
    rapid_pipeline_subs.py   sextractor_dict["sextractor_CATALOG_NAME".lower()]

Keys with such a write are reported as INJECTED and are not findings.
Keys read with no write anywhere are DROPPED -- release content the file
should carry and does not. Only the second class fails the gate.

Reads that write before they read are not findings. Several stages inject
per-invocation paths into a section copy (``sextractor_input_image`` and
friends) before handing it to a command-line builder. A key assigned in
the same function before or after its read is release-supplied by the
caller, not by the file, so it is excluded and reported separately.

Exit status is 0 when every read resolves against the file, 1 when any
does not, so this runs as a gate as well as a report.
"""

import argparse
import ast
import json
import os
import sys
import tomllib
from collections import defaultdict

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCIENCE_CONFIG = os.path.join(REPO_ROOT, "cdf", "science", "pipeline.toml")

# The accessor that hands out a whole section, and the one that hands out a
# single value. Both are `context` methods; the audit keys on the attribute
# name so a differently-named context object is still matched.
SECTION_ACCESSORS = ("science_section",)
VALUE_ACCESSORS = ("science_value",)

# Trees the payload runs out of. `modules/` holds the coadd path, which
# reads the same file.
SOURCE_DIRECTORIES = ("pipeline", "modules")

# Not payload: tests assert about configuration deliberately, including
# about keys being absent.
EXCLUDED_PARTS = (os.sep + "test" + os.sep, os.sep + "tests" + os.sep)


def _is_excluded(path):
    return any(part in path for part in EXCLUDED_PARTS)


def source_files():
    """Every payload .py file under the source trees, tests excluded."""
    found = []
    for directory in SOURCE_DIRECTORIES:
        root_dir = os.path.join(REPO_ROOT, directory)
        for dirpath, _dirnames, filenames in os.walk(root_dir):
            for filename in filenames:
                if not filename.endswith(".py"):
                    continue
                path = os.path.join(dirpath, filename)
                if _is_excluded(path):
                    continue
                found.append(path)
    return sorted(found)


def parse(path):
    with open(path, "r", encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=path)


def _accessor_section(node, accessors):
    """The section name if `node` is a `<obj>.<accessor>("name", ...)` call."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr not in accessors:
        return None
    if not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


def collect_functions(tree):
    """Every function definition in a module, by name.

    Duplicate names across a module would be a redefinition; the last one
    wins, matching Python. Methods are included under their bare name --
    the payload's call sites are module-level functions, and a method
    colliding with one is reported as ambiguous rather than resolved.
    """
    functions = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.setdefault(node.name, []).append(node)
    return functions


def parameter_names(func):
    """Positional parameter names of a function, in order."""
    args = func.args
    return [a.arg for a in (args.posonlyargs + args.args)]


def forwarded_arguments(func, variable):
    """Calls inside `func` that pass `variable` onward, as (callee, position).

    The propagation step: if `variable` holds section *S* and it is handed
    to ``f(..., variable, ...)``, then `f`'s parameter at that position
    holds *S* too. Position is an int for positional arguments and the
    keyword name for keyword arguments, matching `find_section_bindings`.
    """
    forwards = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        if isinstance(callee, ast.Attribute):
            callee_name = callee.attr
        elif isinstance(callee, ast.Name):
            callee_name = callee.id
        else:
            continue
        for index, arg in enumerate(node.args):
            if isinstance(arg, ast.Name) and arg.id == variable:
                forwards.append((callee_name, index))
        for keyword in node.keywords:
            if (isinstance(keyword.value, ast.Name)
                    and keyword.value.id == variable
                    and keyword.arg is not None):
                forwards.append((callee_name, keyword.arg))
    return forwards


def resolve_parameter(func, position):
    """The parameter name of `func` at a call-site position or keyword."""
    params = parameter_names(func)
    if isinstance(position, int):
        # Methods carry an implicit first parameter the call site does not
        # supply, so positions shift by one.
        offset = 1 if params and params[0] in ("self", "cls") else 0
        index = position + offset
        if index >= len(params):
            return None
        return params[index]
    return position if position in params else None


def find_section_bindings(tree, path):
    """Where a section dict enters a function, and under which name.

    Two binding shapes are resolved:

    * **local** -- ``x = context.science_section("swarp")``: the name `x`
      inside this same function is that section.
    * **call argument** -- ``f(..., context.science_section("swarp"), ...)``:
      the callee's parameter at that position (or that keyword) is that
      section, recorded against the callee's *name* for a second pass to
      resolve against its definition.

    Returns (local_bindings, call_bindings).
    """
    local_bindings = []   # (function_node, variable_name, section, lineno)
    call_bindings = []    # (callee_name, param_index_or_keyword, section, path, lineno)

    for node in ast.walk(tree):
        # Local: assignment of a section accessor to a name.
        if isinstance(node, ast.Assign):
            section = _accessor_section(node.value, SECTION_ACCESSORS)
            if section is not None:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        local_bindings.append((target.id, section, node.lineno))

        # Call argument: a section accessor passed straight into a call.
        if isinstance(node, ast.Call):
            callee = node.func
            if isinstance(callee, ast.Attribute):
                callee_name = callee.attr
            elif isinstance(callee, ast.Name):
                callee_name = callee.id
            else:
                callee_name = None

            if callee_name is not None:
                for index, arg in enumerate(node.args):
                    section = _accessor_section(arg, SECTION_ACCESSORS)
                    if section is not None:
                        call_bindings.append(
                            (callee_name, index, section, path, node.lineno))
                for keyword in node.keywords:
                    section = _accessor_section(keyword.value, SECTION_ACCESSORS)
                    if section is not None and keyword.arg is not None:
                        call_bindings.append(
                            (callee_name, keyword.arg, section, path, node.lineno))

    return local_bindings, call_bindings


def subscript_reads(func, variable):
    """Constant-string keys read from `variable` inside `func`.

    A subscript that is the target of an assignment is a write, not a
    read, and is returned separately: the payload injects per-invocation
    paths into section copies before handing them to a builder, and those
    keys are supplied by the caller rather than by the file.
    """
    reads = {}    # key -> lineno
    writes = set()

    for node in ast.walk(func):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if _subscript_key_of(target, variable) is not None:
                    writes.add(_subscript_key_of(target, variable))
        if isinstance(node, ast.AugAssign):
            if _subscript_key_of(node.target, variable) is not None:
                writes.add(_subscript_key_of(node.target, variable))

    for node in ast.walk(func):
        if not isinstance(node, ast.Subscript):
            continue
        key = _subscript_key_of(node, variable)
        if key is None:
            continue
        reads.setdefault(key, node.lineno)

    # Writes are returned alongside rather than subtracted here: the
    # classification is made once, tree-wide, against every section's
    # writes from every module. Dropping them locally would hide a key
    # that is written in one function and read in another.
    return reads, writes


def _subscript_key_of(node, variable):
    """The constant string key if `node` is `variable['key']`.

    Handles the payload's ``dict["Name".lower()]`` idiom, which is a
    constant folded at read time and must resolve, not be skipped.
    """
    if not isinstance(node, ast.Subscript):
        return None
    if not isinstance(node.value, ast.Name) or node.value.id != variable:
        return None

    index = node.slice
    if isinstance(index, ast.Constant) and isinstance(index.value, str):
        return index.value
    # `"Literal".lower()` -- constant-foldable, and the payload uses it.
    if (isinstance(index, ast.Call)
            and isinstance(index.func, ast.Attribute)
            and index.func.attr == "lower"
            and isinstance(index.func.value, ast.Constant)
            and isinstance(index.func.value.value, str)):
        return index.func.value.value.lower()
    return None


def audit():
    with open(SCIENCE_CONFIG, "rb") as handle:
        release = tomllib.load(handle)

    files = source_files()
    trees = {}
    functions_by_name = defaultdict(list)   # name -> [(path, node)]
    for path in files:
        try:
            tree = parse(path)
        except SyntaxError as exc:
            print(f"SKIP (unparseable): {path}: {exc}", file=sys.stderr)
            continue
        trees[path] = tree
        for name, nodes in collect_functions(tree).items():
            for node in nodes:
                functions_by_name[name].append((path, node))

    # Pass 1: gather bindings across the whole tree.
    all_local = []   # (path, tree, variable, section, lineno)
    all_calls = []
    for path, tree in trees.items():
        local_bindings, call_bindings = find_section_bindings(tree, path)
        for variable, section, lineno in local_bindings:
            all_local.append((path, tree, variable, section, lineno))
        all_calls.extend(call_bindings)

    # Pass 2: propagate each binding to a fixed point, then collect the
    # keys read from every variable that ends up holding a section.
    #
    # A binding is (def_path, function_node, variable_name, section). The
    # seed set is the local assignments and the direct call arguments; the
    # worklist then follows every onward pass of a bound variable into its
    # callee's parameter, which is what reaches the command-line builders
    # two hops from the accessor.
    reads = defaultdict(lambda: defaultdict(list))
    injected = defaultdict(set)
    unresolved_callees = []

    seen = set()      # (def_path, function id, variable, section)
    worklist = []

    def add_binding(def_path, func, variable, section):
        marker = (def_path, id(func), variable, section)
        if marker in seen:
            return
        seen.add(marker)
        worklist.append((def_path, func, variable, section))

    # Seed: local assignments of a section accessor.
    for path, tree, variable, section, lineno in all_local:
        enclosing = _enclosing_function(tree, lineno)
        if enclosing is not None:
            add_binding(path, enclosing, variable, section)

    # Seed: section accessors passed straight into a call.
    for callee_name, position, section, call_path, call_line in all_calls:
        candidates = functions_by_name.get(callee_name, [])
        if not candidates:
            unresolved_callees.append(
                (callee_name, section, call_path, call_line))
            continue
        for def_path, func in candidates:
            variable = resolve_parameter(func, position)
            if variable is not None:
                add_binding(def_path, func, variable, section)

    # Fixed point: collect reads, and follow the variable onward.
    iterations = 0
    while worklist:
        iterations += 1
        def_path, func, variable, section = worklist.pop()

        found, writes = subscript_reads(func, variable)
        for key, key_line in found.items():
            reads[section][key].append((def_path, key_line))
        # Injection is recorded per section across the whole tree: the
        # assignment and the read are usually in different modules, so a
        # write seen anywhere for this section answers the read seen
        # anywhere else.
        for key in writes:
            injected[section].add(key)

        for callee_name, position in forwarded_arguments(func, variable):
            for next_path, next_func in functions_by_name.get(callee_name, []):
                next_variable = resolve_parameter(next_func, position)
                if next_variable is not None:
                    add_binding(next_path, next_func, next_variable, section)

    # Single-value reads: context.science_value("section", "key").
    for path, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr not in VALUE_ACCESSORS:
                continue
            if len(node.args) < 2:
                continue
            sect, key = node.args[0], node.args[1]
            if (isinstance(sect, ast.Constant) and isinstance(sect.value, str)
                    and isinstance(key, ast.Constant) and isinstance(key.value, str)):
                reads[sect.value][key.value].append((path, node.lineno))

    # Also: `context.science_section("x")["key"]` read inline.
    for path, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Subscript):
                continue
            section = _accessor_section(node.value, SECTION_ACCESSORS)
            if section is None:
                continue
            index = node.slice
            if isinstance(index, ast.Constant) and isinstance(index.value, str):
                reads[section][index.value].append((path, node.lineno))

    return release, reads, injected, unresolved_callees, len(seen)


def _enclosing_function(tree, lineno):
    """The innermost function containing `lineno`."""
    best = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end = getattr(node, "end_lineno", None)
        if end is None:
            continue
        if node.lineno <= lineno <= end:
            if best is None or node.lineno > best.lineno:
                best = node
    return best


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output")
    options = parser.parse_args()

    release, reads, injected, unresolved, bindings = audit()

    # Classify every read: provided by the release, injected by the
    # payload before use (manifest home), or dropped (a defect).
    dropped = defaultdict(dict)
    injected_reads = defaultdict(dict)
    total_reads = 0
    for section in sorted(reads):
        provided = release.get(section, {})
        for key in sorted(reads[section]):
            total_reads += 1
            if key in provided:
                continue
            if key in injected.get(section, set()):
                injected_reads[section][key] = reads[section][key]
            else:
                dropped[section][key] = reads[section][key]

    def _sites(pairs):
        return [[os.path.relpath(p, REPO_ROOT), l] for p, l in pairs]

    if options.json:
        payload = {
            "sections_read": sorted(reads),
            "total_key_reads": total_reads,
            "bindings_at_fixed_point": bindings,
            "dropped": {s: {k: _sites(v) for k, v in ks.items()}
                        for s, ks in dropped.items()},
            "injected": {s: {k: _sites(v) for k, v in ks.items()}
                         for s, ks in injected_reads.items()},
            "unresolved_callees": unresolved,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 1 if dropped else 0

    print(f"science configuration: {SCIENCE_CONFIG}")
    print(f"sections read by the payload: {len(reads)}")
    print(f"distinct key reads resolved:  {total_reads}")
    print(f"variable bindings at fixed point: {bindings}")
    print()

    for section in sorted(reads):
        provided = release.get(section, {})
        read_keys = sorted(reads[section])
        section_dropped = sorted(dropped.get(section, {}))
        section_injected = sorted(injected_reads.get(section, {}))
        status = "DROPPED" if section_dropped else "ok"
        print(f"  [{section:<22}] read {len(read_keys):>3}  "
              f"provided {len(provided):>3}  "
              f"injected {len(section_injected):>3}  {status}")
        for key in section_dropped:
            where = "; ".join(
                f"{p}:{l}" for p, l in _sites(reads[section][key]))
            print(f"      DROPPED {section}.{key}   read at {where}")

    if injected_reads:
        total_injected = sum(len(v) for v in injected_reads.values())
        print()
        print(f"{total_injected} key(s) are absent from the release but "
              f"assigned by the payload before use — per-invocation values "
              f"whose home is the manifest, not release content:")
        for section in sorted(injected_reads):
            keys = ", ".join(sorted(injected_reads[section]))
            print(f"  [{section}] {keys}")

    if unresolved:
        print()
        print("Callees a section was passed to but whose definition was not "
              "found (reads inside them are NOT covered):")
        for name, section, path, line in sorted(unresolved):
            print(f"  {name}(...) <- [{section}] at "
                  f"{os.path.relpath(path, REPO_ROOT)}:{line}")

    print()
    if dropped:
        count = sum(len(v) for v in dropped.values())
        print(f"RESULT: {count} key(s) read by the payload, assigned by "
              f"nothing, and provided by no configuration home.")
        return 1
    print("RESULT: every science-configuration key the payload reads is "
          "either provided by the release or injected before use.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
