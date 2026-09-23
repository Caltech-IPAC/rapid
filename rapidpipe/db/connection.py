"""One connection path to the run-model tables.

Environment contract, in order, when a caller passes neither ``endpoint=``
nor ``credentials=`` (a caller holding either passes it explicitly instead
-- the CLI having just read a parameter tree, a launcher having just
resolved a secret under its own role -- and it always wins over both
steps below):

1. The standard ``PG*`` variables -- ``PGHOST``, ``PGPORT``, ``PGDATABASE``,
   ``PGUSER``, ``PGPASSWORD`` -- exactly as ``database/apply-migrations.sh``
   reads them, whenever they are set (``RAPID_DB_SECRET_ID`` still takes
   the credential from Secrets Manager instead of ``PGUSER``/``PGPASSWORD``
   when it is set).
2. The Batch estate's ``RAPID_PARAMETER_PATH`` SSM parameter tree, when the
   ``PG*`` endpoint variables are unset and this variable names a tree:
   its ``db/server``/``db/port``/``db/name``/``db/secret-id`` keys supply
   the endpoint and, via :func:`credentials_from_secret`, the credential.
   Designed in from the smdc branch's parameter-tree mechanism
   (``pipeline/entrypoints/job.py``'s ``database_connection_inputs``,
   ``submission/startup.py``'s ``fetch_parameters``) and unused by any
   caller in this repository today.
3. Neither of the above: the plain ``PG*`` read, which raises naming the
   missing variable, unchanged from before this fallback existed.

This module never writes the environment for a downstream reader to read
back, only reads it (or the tree) at the boundary; nothing here is an
in-process transport.

No connection pooling, no pooler-specific configuration: pgbouncer (or
any pooler) is server-side infrastructure that ``rapid_systems``
provisions and configures, and does not belong in this repository (see
the specification's "Repositories" table). This module only ever asks
the pooler's (or the database's) own listening port for one connection
at a time.

This module provides persistence only: it does not import ``rapidpipe.runs``
or any stage module, matching ``rapidpipe.db``'s package contract.

Ported, trimmed and adapted from the smdc branch's
``database/modules/utils/rapid_db_connect.py`` (kept: explicit
endpoint/credentials interface, Secrets Manager resolution, connect
timeout, ``application_name``, bounded retry with backoff and jitter,
TCP keepalives; dropped: the two named pooler "lanes", the SET ROLE
widening/reassert bookkeeping, ``ConnectionExecutor``, and the
STARTUP_* fleet-restart-horizon constants -- all pooler- or
operator-role-specific and out of scope for a repository that carries
no pooler code).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import random
import time
from dataclasses import dataclass
from typing import Any, Iterator

import psycopg2
import psycopg2.extensions

logger = logging.getLogger(__name__)

_REQUIRED_VARS = ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD")

# Starting values, replaceable by evidence without re-ratification: the
# stage contract and the specification state the shape (timeout, bounded
# retry, backoff) but not the numbers.
DEFAULT_CONNECT_TIMEOUT_S = 10
DEFAULT_CONNECT_ATTEMPTS = 5
DEFAULT_BACKOFF_INITIAL_S = 0.5
DEFAULT_BACKOFF_MULTIPLIER = 2.0
DEFAULT_BACKOFF_CAP_S = 8.0

# TCP keepalives. The incident this closes (condensed from the smdc
# branch's pooler_client_idle_timeout.rst / rapid_db_connect.py
# docstring): on 2026-09-12 a long-lived connection's peer was replaced
# by a host roll mid-poll. No RST or FIN arrives for a socket whose peer
# has simply vanished, so without keepalives the connection sat believed
# healthy for over two hours -- the kernel's own default idle time
# (7200s) plus nine probes 75s apart -- before psycopg2 finally reported
# a timeout. These settings bring detection down to about a minute:
# KEEPALIVES_IDLE_S before the first probe, then up to KEEPALIVES_COUNT
# probes KEEPALIVES_INTERVAL_S apart (30 + 3*10 = 60s), with
# TCP_USER_TIMEOUT_MS as a second, kernel-enforced backstop on the same
# budget. This is socket-level dead-peer detection only: it does not
# retry a statement that was in flight when the peer vanished, it only
# bounds how long a now-useless connection is believed healthy.
KEEPALIVES = 1
KEEPALIVES_IDLE_S = 30
KEEPALIVES_INTERVAL_S = 10
KEEPALIVES_COUNT = 3
TCP_USER_TIMEOUT_MS = 60000


class ConnectionConfigError(RuntimeError):
    """A required ``PG*`` environment variable is missing, or an explicit
    endpoint/credential was passed incomplete."""


class ConnectionUnavailable(RuntimeError):
    """Connecting failed on every attempt within the retry budget."""


@dataclass(frozen=True)
class Endpoint:
    """Where the database is: host, port, dbname -- passed explicitly.

    A dataclass rather than a bare tuple so a caller cannot half-populate
    one and have the missing field silently fall back to an environment
    read; :func:`connect` only reads the environment when ``endpoint`` is
    ``None`` altogether, never field-by-field.
    """

    host: str
    port: str
    dbname: str

    def __post_init__(self) -> None:
        missing = [name for name, value in (
            ("host", self.host), ("port", self.port), ("dbname", self.dbname),
        ) if value is None or str(value) == ""]
        if missing:
            raise ConnectionConfigError(
                "an explicit Endpoint is incomplete; missing: "
                + ", ".join(missing))


@dataclass(frozen=True)
class Credentials:
    """A resolved database credential, passed explicitly.

    ``__repr__`` is overridden so the password never prints into a log
    line, traceback frame, or the ``repr()`` of a containing structure.
    """

    user: str
    password: str

    def __post_init__(self) -> None:
        if not self.user or not self.password:
            raise ConnectionConfigError(
                "an explicit Credentials needs both a user and a password")

    def __repr__(self) -> str:
        return f"Credentials(user={self.user!r}, password=<redacted>)"


def _read_env(names: tuple[str, ...]) -> dict[str, str]:
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        raise ConnectionConfigError(
            "missing required environment variable(s): "
            f"{', '.join(missing)}; rapidpipe.db.connection reads only "
            f"{', '.join(_REQUIRED_VARS)}")
    return {name: os.environ[name] for name in names}


def _endpoint_from_environment() -> Endpoint:
    env = _read_env(("PGHOST", "PGPORT", "PGDATABASE"))
    return Endpoint(host=env["PGHOST"], port=env["PGPORT"], dbname=env["PGDATABASE"])


def _credentials_from_environment() -> Credentials:
    env = _read_env(("PGUSER", "PGPASSWORD"))
    return Credentials(user=env["PGUSER"], password=env["PGPASSWORD"])


#: The parameter tree's four database keys, read relative to
#: ``RAPID_PARAMETER_PATH`` -- following the smdc branch's
#: ``pipeline/entrypoints/job.py`` (``DB_PARAMETER_KEYS``) and
#: ``database_connection_inputs``.
_PARAMETER_TREE_DB_KEYS = ("db/server", "db/port", "db/name", "db/secret-id")


def _fetch_parameter_tree_db_values(path: str, ssm_client: Any = None) -> dict[str, str]:
    """Read the four ``db/*`` keys under ``path`` from the SSM parameter tree.

    One paginated ``get_parameters_by_path`` walk, exactly as the smdc
    branch's ``submission/startup.py.fetch_parameters`` reads the wider
    pipeline tree -- names come back relative to ``path``, decrypted
    (``WithDecryption=True``), and pagination follows ``NextToken``.
    ``boto3`` is imported lazily, here, matching
    :func:`credentials_from_secret`, so this module -- and every caller
    that never falls back to the tree -- imports without boto3 installed.

    Raises :class:`ConnectionConfigError` naming ``path`` and every one of
    :data:`_PARAMETER_TREE_DB_KEYS` that the tree does not carry.
    """
    if ssm_client is None:
        try:
            import boto3
        except ImportError as exc:
            raise ConnectionConfigError(
                "RAPID_PARAMETER_PATH is set but boto3 is not installed "
                "in this environment") from exc
        ssm_client = boto3.client("ssm")

    prefix = path.rstrip("/") + "/"
    values: dict[str, str] = {}
    kwargs: dict[str, Any] = {"Path": path, "Recursive": True, "WithDecryption": True}
    try:
        while True:
            response = ssm_client.get_parameters_by_path(**kwargs)
            for parameter in response.get("Parameters", []):
                name = parameter["Name"]
                relative = name[len(prefix):] if name.startswith(prefix) else name
                values[relative] = parameter["Value"]
            token = response.get("NextToken")
            if not token:
                break
            kwargs["NextToken"] = token
    except ConnectionConfigError:
        raise
    except Exception as exc:
        raise ConnectionConfigError(
            f"could not read the database parameters from the SSM "
            f"parameter tree at {path!r}: {exc}") from exc

    missing = [key for key in _PARAMETER_TREE_DB_KEYS if key not in values]
    if missing:
        raise ConnectionConfigError(
            f"the SSM parameter tree at {path!r} does not carry the "
            f"database endpoint; missing: {', '.join(missing)}")

    return values


def _cached_parameter_tree_db_values(
    path: str, ssm_client: Any, cache: dict[str, dict[str, str]],
) -> dict[str, str]:
    """As :func:`_fetch_parameter_tree_db_values`, but fetched at most once
    per :func:`connect` call: the endpoint and credential resolvers each
    need the tree, and ``cache`` (one dict, created fresh per ``connect``
    call and passed to both) makes that one read, not two."""
    if path not in cache:
        cache[path] = _fetch_parameter_tree_db_values(path, ssm_client=ssm_client)
    return cache[path]


def _endpoint_from_parameter_tree(
    path: str, ssm_client: Any, cache: dict[str, dict[str, str]],
) -> Endpoint:
    values = _cached_parameter_tree_db_values(path, ssm_client, cache)
    return Endpoint(host=values["db/server"], port=values["db/port"],
                     dbname=values["db/name"])


def _credentials_from_parameter_tree(
    path: str, ssm_client: Any, cache: dict[str, dict[str, str]],
) -> Credentials:
    values = _cached_parameter_tree_db_values(path, ssm_client, cache)
    return credentials_from_secret(values["db/secret-id"])


def credentials_from_secret(secret_id: str) -> Credentials:
    """Resolve a database credential from an AWS Secrets Manager secret.

    Reads a JSON secret with ``username``/``password`` keys via boto3.
    boto3 is imported lazily, here, so this module -- and every caller
    that never resolves a secret -- imports without boto3 installed;
    boto3 is not a dependency of this repository (see requirements.txt)
    and belongs to whatever deployment environment actually calls this
    function.

    Raises :class:`ConnectionConfigError` if boto3 is unavailable, the
    secret cannot be fetched, or its JSON body is missing either key.
    Never returns a partial credential and never prints or logs the
    password.
    """
    try:
        import boto3
    except ImportError as exc:
        raise ConnectionConfigError(
            "credentials_from_secret requires boto3, which is not "
            "installed in this environment") from exc

    try:
        client = boto3.client("secretsmanager")
        response = client.get_secret_value(SecretId=secret_id)
        secret = json.loads(response["SecretString"])
        user = secret["username"]
        password = secret["password"]
    except ConnectionConfigError:
        raise
    except Exception as exc:
        raise ConnectionConfigError(
            f"could not resolve database credentials from Secrets Manager "
            f"secret {secret_id!r}: {exc}") from exc

    return Credentials(user=user, password=password)


def _resolve_endpoint(
    endpoint: Endpoint | None, ssm_client: Any, tree_cache: dict[str, dict[str, str]],
) -> Endpoint:
    if endpoint is None:
        if os.environ.get("PGHOST"):
            return _endpoint_from_environment()
        parameter_path = os.environ.get("RAPID_PARAMETER_PATH")
        if parameter_path:
            return _endpoint_from_parameter_tree(parameter_path, ssm_client, tree_cache)
        return _endpoint_from_environment()
    if isinstance(endpoint, Endpoint):
        return endpoint
    return Endpoint(**endpoint) if hasattr(endpoint, "keys") else Endpoint(*endpoint)


def _resolve_credentials(
    credentials: Credentials | None, ssm_client: Any, tree_cache: dict[str, dict[str, str]],
) -> Credentials:
    if credentials is not None:
        if isinstance(credentials, Credentials):
            return credentials
        return Credentials(*credentials)

    secret_id = os.environ.get("RAPID_DB_SECRET_ID")
    if secret_id:
        return credentials_from_secret(secret_id)
    if os.environ.get("PGUSER"):
        return _credentials_from_environment()

    parameter_path = os.environ.get("RAPID_PARAMETER_PATH")
    if parameter_path:
        return _credentials_from_parameter_tree(parameter_path, ssm_client, tree_cache)
    return _credentials_from_environment()


def _connect_with_retry(
    *,
    endpoint: Endpoint,
    credentials: Credentials,
    application_name: str,
    connect_timeout: int,
    attempts: int,
    backoff_initial: float,
    backoff_multiplier: float,
    backoff_cap: float,
    keepalives: int,
    keepalives_idle: int,
    keepalives_interval: int,
    keepalives_count: int,
    tcp_user_timeout: int,
    jitter: bool,
    sleep,
    connect_fn,
    random_func,
) -> psycopg2.extensions.connection:
    if attempts < 1:
        raise ValueError(f"attempts must be >= 1; got {attempts}")

    # PostgreSQL truncates application_name at NAMEDATALEN-1 (63 bytes)
    # and would silently lose the tail of a long component name; trimmed
    # here where the truncation is visible rather than server-side where
    # it is not.
    composed_name = application_name[:63]

    delay = backoff_initial
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return connect_fn(
                host=endpoint.host,
                port=endpoint.port,
                dbname=endpoint.dbname,
                user=credentials.user,
                password=credentials.password,
                connect_timeout=connect_timeout,
                application_name=composed_name,
                keepalives=keepalives,
                keepalives_idle=keepalives_idle,
                keepalives_interval=keepalives_interval,
                keepalives_count=keepalives_count,
                tcp_user_timeout=tcp_user_timeout,
            )
        except psycopg2.OperationalError as exc:
            last_exc = exc
            if attempt == attempts:
                break
            wait = random_func(0, delay) if jitter else delay
            logger.warning(
                "database connect attempt %d/%d failed (%s); retrying in %.1fs",
                attempt, attempts, exc, wait)
            sleep(wait)
            delay = min(delay * backoff_multiplier, backoff_cap)
            continue

    raise ConnectionUnavailable(
        f"could not connect to {endpoint.host}:{endpoint.port}/{endpoint.dbname} "
        f"as {credentials.user} after {attempts} attempt(s): {last_exc}"
    ) from last_exc


@contextlib.contextmanager
def connect(
    *,
    endpoint: Endpoint | None = None,
    credentials: Credentials | None = None,
    application_name: str = "rapidpipe",
    connect_timeout: int = DEFAULT_CONNECT_TIMEOUT_S,
    attempts: int = DEFAULT_CONNECT_ATTEMPTS,
    backoff_initial: float = DEFAULT_BACKOFF_INITIAL_S,
    backoff_multiplier: float = DEFAULT_BACKOFF_MULTIPLIER,
    backoff_cap: float = DEFAULT_BACKOFF_CAP_S,
    keepalives: int = KEEPALIVES,
    keepalives_idle: int = KEEPALIVES_IDLE_S,
    keepalives_interval: int = KEEPALIVES_INTERVAL_S,
    keepalives_count: int = KEEPALIVES_COUNT,
    tcp_user_timeout: int = TCP_USER_TIMEOUT_MS,
    jitter: bool = False,
    sleep=time.sleep,
    connect_fn=None,
    random_func=random.uniform,
    ssm_client=None,
) -> Iterator[psycopg2.extensions.connection]:
    """Yield one connection with autocommit off.

    A plain context manager, not a pool: opens one connection, yields
    it, and closes it on exit. The connection's autocommit is left at
    psycopg2's default (off), so every statement runs inside an implicit
    transaction that the caller must commit or roll back --
    ``transaction()`` below is the common case of "one transaction per
    call", which every ``rapidpipe.runs.repository`` function needs
    (each is documented as a single transaction).

    ``endpoint`` and ``credentials`` are the explicit parameter
    interface: a caller holding either passes it here, and an explicit
    argument always wins over both the environment and the parameter
    tree below. What is not passed falls back, in order:

    1. The ``PG*`` environment -- ``PGHOST``, ``PGPORT``, ``PGDATABASE``
       for the endpoint, ``PGUSER``/``PGPASSWORD`` for credentials --
       whenever those variables are set.
    2. Unless ``RAPID_DB_SECRET_ID`` is set, in which case the credential
       is resolved from that Secrets Manager secret instead (see
       :func:`credentials_from_secret`), still ahead of the tree below.
    3. The ``RAPID_PARAMETER_PATH`` SSM parameter tree, when the ``PG*``
       endpoint variables are unset and this variable names a tree: its
       ``db/server``/``db/port``/``db/name`` keys supply the endpoint,
       and its ``db/secret-id`` key names the Secrets Manager secret used
       for credentials (again via :func:`credentials_from_secret`) when
       neither ``RAPID_DB_SECRET_ID`` nor ``PGUSER`` is set. Endpoint and
       credential fall back to the tree independently -- an operator can
       set ``PGUSER``/``PGPASSWORD`` while still resolving the endpoint
       from the tree, for instance.
    4. The plain ``PG*`` environment read, unchanged, if neither
       ``RAPID_PARAMETER_PATH`` nor the values above apply -- so a
       deployment that never sets ``RAPID_PARAMETER_PATH`` gets exactly
       the errors it always got.

    This is a boundary read, not an in-process transport: nothing in
    this module writes the environment for a downstream reader.
    ``ssm_client`` is a test injection point (:func:`credentials_from_secret`
    already covers Secrets Manager); nothing in production passes it.

    Connecting retries up to ``attempts`` times (default
    :data:`DEFAULT_CONNECT_ATTEMPTS`) on ``psycopg2.OperationalError``,
    with exponential backoff from ``backoff_initial`` up to
    ``backoff_cap``, doubling by ``backoff_multiplier`` each time.
    ``jitter=True`` applies full jitter (a random duration in
    ``[0, delay]`` in place of ``delay`` itself) so many callers retrying
    off the same event do not stay synchronized. All of these are
    ordinary keyword arguments a caller overrides the same way it
    overrides ``connect_timeout``. ``sleep``, ``connect_fn`` and
    ``random_func`` are injection points for tests; nothing in
    production passes them.

    ``keepalives``/``keepalives_idle``/``keepalives_interval``/
    ``keepalives_count``/``tcp_user_timeout`` set TCP-level dead-peer
    detection on every connection this opens (see the module docstring
    for the incident and the arithmetic behind the defaults).

    Does not swallow errors: connection failures and query errors raise;
    nothing here calls ``exit()`` or returns a sentinel in place of
    raising. Exhausting the retry budget raises
    :class:`ConnectionUnavailable`.
    """
    tree_cache: dict[str, dict[str, str]] = {}
    resolved_endpoint = _resolve_endpoint(endpoint, ssm_client, tree_cache)
    resolved_credentials = _resolve_credentials(credentials, ssm_client, tree_cache)
    conn = _connect_with_retry(
        endpoint=resolved_endpoint,
        credentials=resolved_credentials,
        application_name=application_name,
        connect_timeout=connect_timeout,
        attempts=attempts,
        backoff_initial=backoff_initial,
        backoff_multiplier=backoff_multiplier,
        backoff_cap=backoff_cap,
        keepalives=keepalives,
        keepalives_idle=keepalives_idle,
        keepalives_interval=keepalives_interval,
        keepalives_count=keepalives_count,
        tcp_user_timeout=tcp_user_timeout,
        jitter=jitter,
        sleep=sleep,
        connect_fn=connect_fn or psycopg2.connect,
        random_func=random_func,
    )
    try:
        conn.autocommit = False
        yield conn
    finally:
        conn.close()


@contextlib.contextmanager
def transaction(**kwargs: Any) -> Iterator[psycopg2.extensions.connection]:
    """Yield one connection inside one transaction: commit on success, rollback on error.

    Equivalent to ``with connect(**kwargs) as conn:`` followed by an
    explicit commit/rollback, spelled once here so every repository
    function opens with the same one-line pattern:

        with transaction() as conn:
            with conn.cursor() as cur:
                ...

    Accepts every keyword :func:`connect` does (``endpoint=``,
    ``credentials=``, ``application_name=``, retry and keepalive
    tuning) and passes them straight through.

    On an exception the transaction is rolled back and the exception
    re-raised unchanged; on normal exit it is committed.
    """
    with connect(**kwargs) as conn:
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()
