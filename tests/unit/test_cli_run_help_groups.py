"""``rapidpipe run --help``'s five titled subcommand groups (tool.md,
"The run commands"): lifecycle, recovery, promotion, housekeeping,
inspection. Database-free: only argparse's own parser tree and its
formatted help text."""

from __future__ import annotations

import argparse
import re

import pytest

from rapidpipe.cli.main import RUN_COMMAND_GROUPS, _build_parser, main


def _find_subparsers_action(parser: argparse.ArgumentParser) -> argparse._SubParsersAction:
    for action in parser._subparsers._group_actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    raise AssertionError(f"{parser.prog}: no subparsers action")


def test_run_command_groups_cover_every_run_subcommand_exactly_once():
    parser = _build_parser()
    run_parser = _find_subparsers_action(parser).choices["run"]
    run_subparsers = _find_subparsers_action(run_parser)

    every_name = list(run_subparsers.choices)
    grouped_names = [name for _, names in RUN_COMMAND_GROUPS for name in names]

    # Exactly one group each: no name missing, none doubled.
    assert sorted(grouped_names) == sorted(every_name)
    assert len(grouped_names) == len(set(grouped_names))


def test_run_help_shows_the_five_groups_in_order_with_their_subcommands(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["run", "--help"])
    assert exc_info.value.code == 0
    out = re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)

    # The five titles appear, each once, in the declared order.
    titles = [title for title, _ in RUN_COMMAND_GROUPS]
    positions = [out.index(f"{title}:") for title in titles]
    assert positions == sorted(positions), (titles, out)

    # Every subcommand is listed as its own indented entry, after its
    # group's title and before the next group's (or end of output, for
    # the last group) -- not merely as a word somewhere in that span
    # (the run parser's own description contains "list", "run",
    # "promote" and "delete" as prose).
    boundaries = positions + [len(out)]
    for index, (title, names) in enumerate(RUN_COMMAND_GROUPS):
        section = out[boundaries[index]:boundaries[index + 1]]
        for name in names:
            assert re.search(rf"^\s+{re.escape(name)}\b", section, re.MULTILINE), (
                f"{name!r} not listed under {title!r}:\n{section}")

    # The flat, ungrouped listing (argparse's default: every choice name
    # indented under a "{create,list,...}" heading) is gone: no
    # subcommand appears as its own indented entry before the first
    # titled group.
    preamble = out[:positions[0]]
    for _, names in RUN_COMMAND_GROUPS:
        for name in names:
            assert not re.search(rf"^\s+{re.escape(name)}\b", preamble, re.MULTILINE), (
                f"{name!r} still in the flat listing:\n{preamble}")
