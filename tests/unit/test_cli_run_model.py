"""``rapidpipe run promote/rollback/delete/finish/pin/unpin`` wiring, with
the repository and cleanup calls monkeypatched and no database."""

from __future__ import annotations

import contextlib

import pytest

from rapidpipe.cli import main as cli
from rapidpipe.runs import cleanup, repository


class _Conn:
    def __init__(self):
        self.committed = 0
        self.rolled_back = 0

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1


@pytest.fixture()
def fake_conn(monkeypatch):
    conn = _Conn()

    @contextlib.contextmanager
    def _connect(**_kwargs):
        yield conn

    monkeypatch.setattr(cli, "connect", _connect)
    return conn


def test_run_promote_prints_the_promotion_id_and_commits(monkeypatch, fake_conn, capsys):
    seen = {}

    def _promote_run(conn, run_id, who, reason, *, kinds=None, check_policy_version=None):
        seen.update(run_id=run_id, who=who, reason=reason, kinds=kinds)
        return "PROMOTION01"

    monkeypatch.setattr(repository, "promote_run", _promote_run)
    rc = cli.main(["run", "promote", "RUN01", "--reason", "nightly", "--who", "ops",
                   "--kinds", "difference-image, psf"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "PROMOTION01"
    assert seen == {"run_id": "RUN01", "who": "ops", "reason": "nightly",
                    "kinds": ["difference-image", "psf"]}
    assert fake_conn.committed == 1


def test_run_promote_defaults_who_to_the_current_user(monkeypatch, fake_conn):
    seen = {}
    monkeypatch.setattr(cli.getpass, "getuser", lambda: "someone")
    monkeypatch.setattr(
        repository, "promote_run",
        lambda conn, run_id, who, reason, **kw: seen.setdefault("who", who) and "P")
    assert cli.main(["run", "promote", "RUN01", "--reason", "r"]) == 0
    assert seen["who"] == "someone"


def test_run_promote_refusal_exits_64_with_the_message(monkeypatch, fake_conn, capsys):
    def _refuse(*_a, **_k):
        raise repository.PromotionRefused("scratch never leaves scratch")

    monkeypatch.setattr(repository, "promote_run", _refuse)
    rc = cli.main(["run", "promote", "RUN01", "--reason", "r"])
    assert rc == 64
    assert "scratch never leaves scratch" in capsys.readouterr().err
    assert fake_conn.rolled_back == 1
    assert fake_conn.committed == 0


def test_run_rollback_prints_the_reversing_promotion_id(monkeypatch, fake_conn, capsys):
    monkeypatch.setattr(
        repository, "rollback_promotion",
        lambda conn, promotion_id, who, reason: f"UNDO-{promotion_id}")
    assert cli.main(["run", "rollback", "P1", "--reason", "bad", "--who", "ops"]) == 0
    assert capsys.readouterr().out.strip() == "UNDO-P1"


def test_run_delete_prints_the_report(monkeypatch, fake_conn, capsys):
    monkeypatch.setattr(cli.getpass, "getuser", lambda: "owner1")

    def _delete_run(conn, run_id, requested_by, **_kw):
        assert requested_by == "owner1"
        return cleanup.DeletionReport(
            run_id=run_id, objects_deleted=2, versions_deleted=3,
            rows_deleted={"sources": 5, "psfs": 1}, instances_marked=4)

    monkeypatch.setattr(cleanup, "delete_run", _delete_run)
    assert cli.main(["run", "delete", "RUN01"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "run_id: RUN01" in lines
    assert "objects_deleted: 2" in lines
    assert "versions_deleted: 3" in lines
    assert "rows_deleted.sources: 5" in lines
    assert "instances_marked: 4" in lines


def test_run_finish_and_pin_unpin(monkeypatch, fake_conn):
    calls = []
    monkeypatch.setattr(repository, "finish_run", lambda conn, run_id: calls.append(("finish", run_id)))
    monkeypatch.setattr(cleanup, "pin_run",
                        lambda conn, run_id, pinned: calls.append(("pin", run_id, pinned)))
    assert cli.main(["run", "finish", "R"]) == 0
    assert cli.main(["run", "pin", "R"]) == 0
    assert cli.main(["run", "unpin", "R"]) == 0
    assert calls == [("finish", "R"), ("pin", "R", True), ("pin", "R", False)]
    assert fake_conn.committed == 3


def test_run_finish_refusal_exits_64(monkeypatch, fake_conn, capsys):
    def _refuse(conn, run_id):
        raise repository.RunNotFinishable("run 'R' has 1 of 2 unit(s) not yet complete")

    monkeypatch.setattr(repository, "finish_run", _refuse)
    assert cli.main(["run", "finish", "R"]) == 64
    assert "not yet complete" in capsys.readouterr().err
