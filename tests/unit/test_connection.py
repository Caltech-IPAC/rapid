"""Unit tests for rapidpipe.db.connection: psycopg2 mocked, no network, no database.

Covers the explicit endpoint/credentials interface, environment fallback,
the Secrets Manager resolver, bounded retry with backoff, and that
application_name/connect_timeout/keepalive parameters reach the driver.
"""

from __future__ import annotations

import json

import pytest

from rapidpipe.db import connection as conn_mod

ENV_VARS = ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD")


def _clear_pg_env(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("RAPID_DB_SECRET_ID", raising=False)


def _set_pg_env(monkeypatch, **overrides):
    values = {
        "PGHOST": "env-host",
        "PGPORT": "5432",
        "PGDATABASE": "env-db",
        "PGUSER": "env-user",
        "PGPASSWORD": "env-pass",
    }
    values.update(overrides)
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return values


class _FakeConnection:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.autocommit = None
        self.closed = False
        self.committed = False
        self.rolled_back = False

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def _recording_connect_fn(calls, conn_to_return=None, fail_times=0, exc_cls=None):
    """Return a connect_fn that records each call's kwargs.

    Raises exc_cls() the first `fail_times` calls, then returns a
    _FakeConnection (or `conn_to_return` if given).
    """
    state = {"n": 0}
    exc_cls = exc_cls or conn_mod.psycopg2.OperationalError

    def _connect(**kwargs):
        calls.append(kwargs)
        state["n"] += 1
        if state["n"] <= fail_times:
            raise exc_cls(f"simulated failure {state['n']}")
        return conn_to_return if conn_to_return is not None else _FakeConnection(**kwargs)

    return _connect


# ======================================================================
# explicit endpoint/credentials win over environment
# ======================================================================

def test_explicit_endpoint_and_credentials_win_over_environment(monkeypatch):
    _set_pg_env(monkeypatch)  # environment is populated but must be ignored
    calls = []
    endpoint = conn_mod.Endpoint(host="explicit-host", port="6543", dbname="explicit-db")
    credentials = conn_mod.Credentials(user="explicit-user", password="explicit-pass")

    with conn_mod.connect(
        endpoint=endpoint,
        credentials=credentials,
        connect_fn=_recording_connect_fn(calls),
        sleep=lambda _s: None,
    ):
        pass

    assert len(calls) == 1
    assert calls[0]["host"] == "explicit-host"
    assert calls[0]["port"] == "6543"
    assert calls[0]["dbname"] == "explicit-db"
    assert calls[0]["user"] == "explicit-user"
    assert calls[0]["password"] == "explicit-pass"


def test_explicit_endpoint_accepts_a_plain_mapping(monkeypatch):
    _clear_pg_env(monkeypatch)
    calls = []
    with conn_mod.connect(
        endpoint={"host": "h", "port": "5432", "dbname": "d"},
        credentials=("u", "p"),
        connect_fn=_recording_connect_fn(calls),
        sleep=lambda _s: None,
    ):
        pass
    assert calls[0]["host"] == "h"
    assert calls[0]["user"] == "u"


# ======================================================================
# environment fallback
# ======================================================================

def test_environment_fallback_when_nothing_explicit_is_passed(monkeypatch):
    values = _set_pg_env(monkeypatch)
    calls = []
    with conn_mod.connect(connect_fn=_recording_connect_fn(calls), sleep=lambda _s: None):
        pass
    assert calls[0]["host"] == values["PGHOST"]
    assert calls[0]["port"] == values["PGPORT"]
    assert calls[0]["dbname"] == values["PGDATABASE"]
    assert calls[0]["user"] == values["PGUSER"]
    assert calls[0]["password"] == values["PGPASSWORD"]


def test_missing_environment_variable_raises_a_clear_error_naming_it(monkeypatch):
    _set_pg_env(monkeypatch)
    monkeypatch.delenv("PGHOST", raising=False)
    with pytest.raises(conn_mod.ConnectionConfigError, match="PGHOST"):
        with conn_mod.connect(sleep=lambda _s: None):
            pass


def test_missing_environment_variable_for_credentials_names_it(monkeypatch):
    _set_pg_env(monkeypatch)
    monkeypatch.delenv("PGPASSWORD", raising=False)
    with pytest.raises(conn_mod.ConnectionConfigError, match="PGPASSWORD"):
        with conn_mod.connect(sleep=lambda _s: None):
            pass


def test_endpoint_construction_names_every_missing_field():
    with pytest.raises(conn_mod.ConnectionConfigError, match="host"):
        conn_mod.Endpoint(host="", port="5432", dbname="d")


def test_credentials_construction_requires_both_fields():
    with pytest.raises(conn_mod.ConnectionConfigError):
        conn_mod.Credentials(user="u", password="")
    with pytest.raises(conn_mod.ConnectionConfigError):
        conn_mod.Credentials(user="", password="p")


def test_credentials_repr_never_prints_the_password():
    creds = conn_mod.Credentials(user="u", password="super-secret")
    assert "super-secret" not in repr(creds)
    assert "redacted" in repr(creds)


# ======================================================================
# retry on OperationalError, bounded, backoff sequence
# ======================================================================

def test_retry_happens_on_operational_error_and_succeeds_within_the_limit(monkeypatch):
    _clear_pg_env(monkeypatch)
    calls = []
    sleeps = []
    connect_fn = _recording_connect_fn(calls, fail_times=2)

    with conn_mod.connect(
        endpoint=conn_mod.Endpoint("h", "5432", "d"),
        credentials=conn_mod.Credentials("u", "p"),
        attempts=5,
        backoff_initial=1.0,
        backoff_multiplier=2.0,
        backoff_cap=100.0,
        connect_fn=connect_fn,
        sleep=sleeps.append,
    ):
        pass

    assert len(calls) == 3  # two failures, then success
    assert sleeps == [1.0, 2.0]


def test_retry_stops_at_the_limit_and_raises_the_last_error(monkeypatch):
    _clear_pg_env(monkeypatch)
    calls = []
    connect_fn = _recording_connect_fn(calls, fail_times=10)  # always fails

    with pytest.raises(conn_mod.ConnectionUnavailable) as excinfo:
        with conn_mod.connect(
            endpoint=conn_mod.Endpoint("h", "5432", "d"),
            credentials=conn_mod.Credentials("u", "p"),
            attempts=3,
            backoff_initial=0.1,
            backoff_cap=1.0,
            connect_fn=connect_fn,
            sleep=lambda _s: None,
        ):
            pass

    assert len(calls) == 3  # bounded at `attempts`, never a fourth try
    assert "simulated failure 3" in str(excinfo.value)
    assert excinfo.value.__cause__ is not None


def test_backoff_is_bounded_by_the_cap(monkeypatch):
    _clear_pg_env(monkeypatch)
    calls = []
    sleeps = []
    connect_fn = _recording_connect_fn(calls, fail_times=5)

    with conn_mod.connect(
        endpoint=conn_mod.Endpoint("h", "5432", "d"),
        credentials=conn_mod.Credentials("u", "p"),
        attempts=6,
        backoff_initial=1.0,
        backoff_multiplier=3.0,
        backoff_cap=5.0,
        connect_fn=connect_fn,
        sleep=sleeps.append,
    ):
        pass

    # 1.0, 3.0, 5.0 (capped from 9.0), 5.0 (capped), 5.0 (capped)
    assert sleeps == [1.0, 3.0, 5.0, 5.0, 5.0]
    assert max(sleeps) <= 5.0


def test_jitter_true_samples_within_zero_and_delay(monkeypatch):
    _clear_pg_env(monkeypatch)
    calls = []
    sleeps = []
    connect_fn = _recording_connect_fn(calls, fail_times=1)

    with conn_mod.connect(
        endpoint=conn_mod.Endpoint("h", "5432", "d"),
        credentials=conn_mod.Credentials("u", "p"),
        attempts=2,
        backoff_initial=4.0,
        jitter=True,
        connect_fn=connect_fn,
        sleep=sleeps.append,
        random_func=lambda lo, hi: lo + hi,  # deterministic stand-in: lo + hi
    ):
        pass

    assert sleeps == [0 + 4.0]


def test_non_operational_error_is_not_retried(monkeypatch):
    _clear_pg_env(monkeypatch)
    calls = []

    def _connect(**kwargs):
        calls.append(kwargs)
        raise conn_mod.psycopg2.ProgrammingError("bad query")

    with pytest.raises(conn_mod.psycopg2.ProgrammingError):
        with conn_mod.connect(
            endpoint=conn_mod.Endpoint("h", "5432", "d"),
            credentials=conn_mod.Credentials("u", "p"),
            attempts=5,
            connect_fn=_connect,
            sleep=lambda _s: None,
        ):
            pass

    assert len(calls) == 1  # never retried


def test_attempts_less_than_one_is_rejected(monkeypatch):
    _clear_pg_env(monkeypatch)
    with pytest.raises(ValueError):
        with conn_mod.connect(
            endpoint=conn_mod.Endpoint("h", "5432", "d"),
            credentials=conn_mod.Credentials("u", "p"),
            attempts=0,
            connect_fn=_recording_connect_fn([]),
            sleep=lambda _s: None,
        ):
            pass


# ======================================================================
# application_name, connect_timeout, keepalives passed through
# ======================================================================

def test_application_name_connect_timeout_and_keepalives_are_passed_through(monkeypatch):
    _clear_pg_env(monkeypatch)
    calls = []
    with conn_mod.connect(
        endpoint=conn_mod.Endpoint("h", "5432", "d"),
        credentials=conn_mod.Credentials("u", "p"),
        application_name="my-component",
        connect_timeout=42,
        keepalives=1,
        keepalives_idle=11,
        keepalives_interval=22,
        keepalives_count=33,
        tcp_user_timeout=44000,
        connect_fn=_recording_connect_fn(calls),
        sleep=lambda _s: None,
    ):
        pass

    kwargs = calls[0]
    assert kwargs["application_name"] == "my-component"
    assert kwargs["connect_timeout"] == 42
    assert kwargs["keepalives"] == 1
    assert kwargs["keepalives_idle"] == 11
    assert kwargs["keepalives_interval"] == 22
    assert kwargs["keepalives_count"] == 33
    assert kwargs["tcp_user_timeout"] == 44000


def test_application_name_is_truncated_at_63_bytes(monkeypatch):
    _clear_pg_env(monkeypatch)
    calls = []
    long_name = "x" * 100
    with conn_mod.connect(
        endpoint=conn_mod.Endpoint("h", "5432", "d"),
        credentials=conn_mod.Credentials("u", "p"),
        application_name=long_name,
        connect_fn=_recording_connect_fn(calls),
        sleep=lambda _s: None,
    ):
        pass
    assert len(calls[0]["application_name"]) == 63


# ======================================================================
# connect() / transaction() still work with no arguments, matching the
# existing caller (rapidpipe.runs.repository) that calls transaction()
# ======================================================================

def test_connect_and_transaction_accept_no_arguments(monkeypatch):
    _set_pg_env(monkeypatch)
    calls = []
    monkeypatch.setattr(
        conn_mod.psycopg2, "connect", _recording_connect_fn(calls))
    with conn_mod.connect(sleep=lambda _s: None):
        pass
    assert len(calls) == 1


def test_transaction_commits_on_success_and_rolls_back_on_error(monkeypatch):
    _clear_pg_env(monkeypatch)
    fake = _FakeConnection()

    def _connect(**kwargs):
        return fake

    with conn_mod.transaction(
        endpoint=conn_mod.Endpoint("h", "5432", "d"),
        credentials=conn_mod.Credentials("u", "p"),
        connect_fn=_connect,
        sleep=lambda _s: None,
    ):
        pass
    assert fake.committed
    assert not fake.rolled_back
    assert fake.closed

    fake2 = _FakeConnection()

    class _Boom(Exception):
        pass

    with pytest.raises(_Boom):
        with conn_mod.transaction(
            endpoint=conn_mod.Endpoint("h", "5432", "d"),
            credentials=conn_mod.Credentials("u", "p"),
            connect_fn=lambda **kwargs: fake2,
            sleep=lambda _s: None,
        ):
            raise _Boom()
    assert fake2.rolled_back
    assert not fake2.committed
    assert fake2.closed


# ======================================================================
# Secrets Manager resolver
# ======================================================================

def test_credentials_from_secret_parses_the_json_shape(monkeypatch):
    boto3 = pytest.importorskip("boto3")

    class _FakeClient:
        def get_secret_value(self, SecretId):  # noqa: N803 - matches boto3's kwarg
            assert SecretId == "my-secret"
            return {"SecretString": json.dumps({"username": "u", "password": "p"})}

    monkeypatch.setattr(boto3, "client", lambda service: _FakeClient())

    creds = conn_mod.credentials_from_secret("my-secret")
    assert creds.user == "u"
    assert creds.password == "p"


def test_credentials_from_secret_raises_on_a_missing_key(monkeypatch):
    boto3 = pytest.importorskip("boto3")

    class _FakeClient:
        def get_secret_value(self, SecretId):  # noqa: N803
            return {"SecretString": json.dumps({"username": "u"})}  # no password

    monkeypatch.setattr(boto3, "client", lambda service: _FakeClient())

    with pytest.raises(conn_mod.ConnectionConfigError):
        conn_mod.credentials_from_secret("my-secret")


def test_credentials_from_secret_without_boto3_raises_clearly(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "boto3":
            raise ImportError("no module named boto3")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    with pytest.raises(conn_mod.ConnectionConfigError, match="boto3"):
        conn_mod.credentials_from_secret("my-secret")


def test_rapid_db_secret_id_env_var_routes_to_the_secret_resolver(monkeypatch):
    _clear_pg_env(monkeypatch)
    monkeypatch.setenv("PGHOST", "h")
    monkeypatch.setenv("PGPORT", "5432")
    monkeypatch.setenv("PGDATABASE", "d")
    monkeypatch.setenv("RAPID_DB_SECRET_ID", "the-secret")

    calls = []

    def _fake_credentials_from_secret(secret_id):
        calls.append(secret_id)
        return conn_mod.Credentials(user="secret-user", password="secret-pass")

    monkeypatch.setattr(conn_mod, "credentials_from_secret", _fake_credentials_from_secret)

    connect_calls = []
    with conn_mod.connect(connect_fn=_recording_connect_fn(connect_calls), sleep=lambda _s: None):
        pass

    assert calls == ["the-secret"]
    assert connect_calls[0]["user"] == "secret-user"
    assert connect_calls[0]["password"] == "secret-pass"
