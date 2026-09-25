"""``rapidpipe stage run|list|describe``, the legacy ``rapidpipe stage
<name> ...`` form the Batch launcher submits, and a help walk over every
subcommand of ``rapidpipe``."""

from __future__ import annotations

import argparse

import pytest

from rapidpipe.cli import main as cli
from rapidpipe.cli import stagectl

#: What ``rapidpipe.launch.batch.submit_unit`` puts after the entrypoint.
LAUNCHER_ARGV = [
    "stage", "difference",
    "--run", "RUN01", "--unit", "r0034/SCA01", "--attempt", "ATT01",
    "--inputs", "s3://bucket/in", "--outputs", "s3://bucket/out",
    "--settings", "s3://bucket/settings.toml",
]


@pytest.fixture()
def forwarded(monkeypatch):
    calls = []

    def _run_stage(name, argv):
        calls.append((name, list(argv)))
        return 0

    monkeypatch.setattr(cli, "_run_stage_command", _run_stage)
    return calls


def test_rewrite_legacy_argv():
    assert stagectl.rewrite_legacy_argv(LAUNCHER_ARGV) == ["stage", "run", *LAUNCHER_ARGV[1:]]
    for unchanged in (["stage", "run", "admit"], ["stage", "list"], ["stage", "describe", "admit"],
                      ["stage", "--help"], ["stage", "-h"], ["stage"], ["run", "admit"]):
        assert stagectl.rewrite_legacy_argv(unchanged) == unchanged
    # An unknown word is rewritten too, so it gets the exit-64 message.
    assert stagectl.rewrite_legacy_argv(["stage", "bogus"]) == ["stage", "run", "bogus"]


def test_launcher_form_forwards_byte_for_byte(forwarded):
    assert cli.main(LAUNCHER_ARGV) == 0
    assert forwarded == [("difference", LAUNCHER_ARGV[2:])]


def test_stage_run_forwards_byte_for_byte(forwarded):
    assert cli.main(["stage", "run", *LAUNCHER_ARGV[1:]]) == 0
    assert forwarded == [("difference", LAUNCHER_ARGV[2:])]


@pytest.mark.parametrize("prefix", [["stage"], ["stage", "run"]])
def test_stage_name_help_reaches_the_stage_parser(forwarded, prefix):
    assert cli.main([*prefix, "admit", "-h"]) == 0
    assert forwarded == [("admit", ["-h"])]


def test_stage_admit_help_is_the_stage_own_help(capsys):
    try:
        rc = cli.main(["stage", "admit", "--help"])
    except SystemExit as exc:
        rc = exc.code
    assert rc == 0
    assert "rapidpipe stage admit" in capsys.readouterr().out


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_stage_group_help_exits_0(flag, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["stage", flag])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for sub in ("run", "list", "describe"):
        assert sub in out


def test_stage_without_subcommand_exits_64(capsys):
    assert cli.main(["stage"]) == 64
    assert "a subcommand or stage name is required" in capsys.readouterr().err


def test_unknown_stage_exits_64(capsys):
    assert cli.main(["stage", "bogus", "--run", "R"]) == 64
    assert "unknown stage 'bogus'" in capsys.readouterr().err


def test_stage_list(capsys):
    assert cli.main(["stage", "list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    from rapidpipe.stages.contract import STAGE_NAMES

    assert [line.split("\t")[0] for line in lines] == list(STAGE_NAMES)
    assert "admit\tdetector-image" in lines


def test_stage_describe_admit(capsys):
    assert cli.main(["stage", "describe", "admit"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "name: admit" in lines
    assert "unit: detector-image" in lines
    assert "outputs: l2-image" in lines
    assert "inputs: (none)" in lines
    assert any(line.startswith("arguments.description: rapidpipe stage admit") for line in lines)
    assert any(line.startswith("exit_codes: 0 SUCCESS, 64 USAGE") for line in lines)
    assert any(line.startswith("database_access: ") for line in lines)


def test_stage_describe_unknown_is_a_usage_error():
    with pytest.raises(SystemExit) as exc:
        cli.main(["stage", "describe", "bogus"])
    assert exc.value.code == 2


def _subcommand_paths(parser: argparse.ArgumentParser, prefix: tuple[str, ...] = ()):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                path = (*prefix, name)
                yield path, sub
                yield from _subcommand_paths(sub, path)


def test_every_subcommand_has_help_and_description():
    parser = cli._build_parser()
    missing = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            helps = {a.dest: a.help for a in action._choices_actions}
            for name, sub in action.choices.items():
                if not helps.get(name) or not sub.description:
                    missing.append(name)
    run_parser = dict(_subcommand_paths(parser))[("run",)]
    stage_parser = dict(_subcommand_paths(parser))[("stage",)]
    for group in (run_parser, stage_parser):
        for action in group._actions:
            if isinstance(action, argparse._SubParsersAction):
                helps = {a.dest: a.help for a in action._choices_actions}
                for name, sub in action.choices.items():
                    if not helps.get(name) or not sub.description:
                        missing.append(f"{group.prog} {name}")
    assert missing == []


def test_help_walk_every_subparser_exits_0(capsys):
    paths = [path for path, _ in _subcommand_paths(cli._build_parser())]
    assert ("run", "start") in paths and ("stage", "describe") in paths
    for path in [(), *paths]:
        with pytest.raises(SystemExit) as exc:
            cli.main([*path, "--help"])
        assert exc.value.code == 0, path
    capsys.readouterr()


def test_run_without_subcommand_prints_help_and_exits_64(capsys):
    assert cli.main(["run"]) == 64
    err = capsys.readouterr().err
    assert "usage:" in err
    assert "start, status, inputs, compare, expire" in err
