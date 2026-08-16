#!/usr/bin/env python3
"""Assert that between two git refs, only docstrings/comments changed.

Usage:
    check_docstring_only_diff.py <repo-path> <ref-old> <ref-new> [--files ...]

Exit 0 = docstring/comment-only; exit 1 = real code changed. Written for the
docstring-relocation review pass (2026-08-15), kept because "prove this diff
changed no behaviour" recurs whenever prose is moved out of or into code.

KNOWN BLIND SPOTS, and they matter when judging a clean exit:
  * A docstring consumed at RUNTIME as ``__doc__`` — argparse help, a CLI
    banner, or an assertion over the text — is invisible to this check. The
    contract suite's ``test_rapiddb_is_documented_as_frozen`` asserts the
    literal substrings "FROZEN" and "pipeline/repositories" appear in
    ``RAPIDDB.__doc__``: delete them and this tool still exits 0 while CI
    fails. Docstrings are not always inert.
  * The ``__doc__ = "..."`` assignment form is a plain statement, not a
    docstring node, so it is NOT stripped — a change there reads as real
    code, which is the safe direction.
  * A doctest inside a docstring (``alerts/providers.py`` has one) is
    stripped along with its docstring, so removing executable example code
    reads as a match.

For every .py file that differs between the two refs (or the given file list),
parses both versions with `ast`, strips docstrings, and compares the
resulting AST dumps (ast.dump, which does not include comments or the raw
docstring text itself... except a docstring IS visible in the dump as an
Expr(Constant(str)) node, which is why we explicitly strip it before
dumping). If the stripped dumps match, only docstring/whitespace/comment
content changed. If they differ, real code changed.

Comments are never part of the AST at all, so they are implicitly ignored
by construction (nothing to strip) as long as they are not accidentally
inside a string literal already excluded by the docstring-stripping logic.

Read-only: uses `git show <ref>:<path>` to fetch file content, never
checks out or mutates the working tree.
"""
import argparse
import ast
import subprocess
import sys


def git_show(repo, ref, path):
    result = subprocess.run(
        ["git", "-C", repo, "show", f"{ref}:{path}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None  # file did not exist at this ref (added/removed)
    return result.stdout


def changed_py_files(repo, ref_old, ref_new):
    result = subprocess.run(
        ["git", "-C", repo, "diff", "--name-only", "--diff-filter=ACMR",
         f"{ref_old}..{ref_new}", "--", "*.py"],
        capture_output=True, text=True, check=True,
    )
    return [l for l in result.stdout.splitlines() if l.strip()]


class DocstringStripper(ast.NodeTransformer):
    """Remove the docstring Expr node from module/class/function bodies."""

    def _strip_body(self, node):
        if (
            node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(getattr(node.body[0], "value", None), ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            node.body = node.body[1:]
        return node

    def visit_Module(self, node):
        self.generic_visit(node)
        return self._strip_body(node)

    def visit_ClassDef(self, node):
        self.generic_visit(node)
        return self._strip_body(node)

    def visit_FunctionDef(self, node):
        self.generic_visit(node)
        return self._strip_body(node)

    def visit_AsyncFunctionDef(self, node):
        self.generic_visit(node)
        return self._strip_body(node)


def normalized_dump(src, filename):
    tree = ast.parse(src, filename=filename)
    stripped = DocstringStripper().visit(tree)
    ast.fix_missing_locations(stripped)
    # annotate_fields=True, include_attributes=False: ignore line/col
    # numbers so pure line-shift from docstring-length changes doesn't
    # register as a difference.
    return ast.dump(stripped, annotate_fields=True, include_attributes=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("ref_old")
    ap.add_argument("ref_new")
    ap.add_argument("--files", nargs="*", default=None,
                     help="Restrict to these paths instead of auto-detecting changed .py files")
    args = ap.parse_args()

    files = args.files if args.files else changed_py_files(args.repo, args.ref_old, args.ref_new)

    if not files:
        print(f"No changed .py files between {args.ref_old} and {args.ref_new}.")
        return 0

    only_docstring_changes = []
    real_code_changes = []
    skipped = []

    for path in files:
        old_src = git_show(args.repo, args.ref_old, path)
        new_src = git_show(args.repo, args.ref_new, path)
        if old_src is None or new_src is None:
            skipped.append((path, "added or removed file"))
            continue
        try:
            old_dump = normalized_dump(old_src, path)
            new_dump = normalized_dump(new_src, path)
        except SyntaxError as e:
            skipped.append((path, f"SyntaxError: {e}"))
            continue

        if old_dump == new_dump:
            only_docstring_changes.append(path)
        else:
            real_code_changes.append(path)

    print(f"Compared {args.ref_old} -> {args.ref_new}: {len(files)} changed .py file(s)")
    print()
    print(f"Docstring/comment-only changes ({len(only_docstring_changes)}):")
    for p in only_docstring_changes:
        print(f"  OK    {p}")
    print()
    print(f"Real code changes ({len(real_code_changes)}):")
    for p in real_code_changes:
        print(f"  DIFF  {p}")
    if skipped:
        print()
        print(f"Skipped ({len(skipped)}):")
        for p, reason in skipped:
            print(f"  SKIP  {p}: {reason}")

    return 1 if real_code_changes else 0


if __name__ == "__main__":
    sys.exit(main())
