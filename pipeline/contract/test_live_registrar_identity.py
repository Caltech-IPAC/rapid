"""The LIVE registrar wires the identity repository (package R, item R1).

**THE DEFECT THIS CLOSES.** Package D's product/artifact identity machinery
was code-complete and DORMANT. Two call sites build a registrar:

  * `pipeline/entrypoints/job.py:registrar_for` — passes an
    `identity_repository`, and is on the `JOB_TYPE_REGISTRATION` route;
  * `pipeline/operator/registrar.py:production_registrar` — the operator
    service's own path, which did NOT.

The registration consumer never takes the first route in production, so the
only branch that ever ran was `pipeline/registration/products.py`'s
`identity_repository is None` — the branch its own comment calls "the
pre-rollout path". `products` and `artifacts` were therefore never populated
by a live pass. `pipeline/gc/references.py` records what that cost: an
anti-join keyed on `artifacts.uri` would have classified EVERY REAL PRODUCT
as unreferenced garbage, so the GC candidate rule had to be written around
the absence.

**WHY THE ASSERTION IS ON THE LIVE PATH SPECIFICALLY.** A test that built a
registrar the way `job.py` does would have passed for the whole time the
defect existed — that path was always wired. The distinction between the two
construction paths IS the bug, so these tests go through
`production_registrar()` and nothing else.

**AND WHY DEGRADATION IS TESTED, NOT ASSUMED.** DRAFT 048 is not in the
authoritative migration stream, so the live registrar runs against databases
without `products`/`artifacts` until that change request lands. Wiring the
repository unconditionally would not degrade there: `register_identity`
calls `upsert_product` first, `ProductRepository._query` re-raises the
UndefinedTable as `RepositoryQueryFailed`, nothing catches it, and the
per-attempt transaction rolls back — every registration failing permanently
for want of a table the legacy path does not need. That is a durable
rejection, which D's P8 forbids ("legacy-only, log why, never invent a key,
never durably reject"). The catalog probe in `identity_repository_for` is
what prevents it, and `test_absent_048_degrades_to_legacy_only` is what
proves the probe works rather than trusting it.
"""

import os

import pytest

from pipeline.contract import fixture
from pipeline.operator import registrar as opregistrar
from pipeline.repositories.products import ProductRepository

#: The tables DRAFT 048 adds. All three are probed by the wiring, so all
#: three are named here — a test that probed only `products` could pass
#: against a half-applied schema the wiring itself would refuse.
IDENTITY_TABLES = ("products", "artifacts", "product_artifacts")


@pytest.fixture
def records_bucket_env():
    """`production_registrar` REQUIRES a records bucket, and exits without one.

    Set for the duration of a test rather than at import: this module's tests
    are about the identity wiring, and a bucket name leaking into the rest of
    the session would change what other tests' `production_registrar` calls
    resolve. Restored exactly, including "it was unset".
    """
    saved = {name: os.environ.get(name)
             for name in ("RAPID_VPO_DRY_RUN", "RAPID_RECORDS_BUCKET")}
    os.environ.pop("RAPID_VPO_DRY_RUN", None)
    os.environ["RAPID_RECORDS_BUCKET"] = "roman-rapid-records"
    yield
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


class _CapturingRegistrar:
    """Stands in for `products.registrar`, recording what it was handed.

    **IT TRACKS THE REAL SIGNATURE AND CAN REFUSE A WRONG CALL** — the
    standard `test_registrar.py` already sets for this seam. A double
    absorbing everything through `**kwargs` would let the wiring pass
    `identity_repository` under a misspelled keyword, or stop passing it at
    all, and still report success.
    """

    def __init__(self):
        self.calls = []

    def __call__(self, dbh, store, fallback_roles=None,
                 identity_repository=None):
        self.calls.append({"dbh": dbh, "store": store,
                           "fallback_roles": fallback_roles,
                           "identity_repository": identity_repository})
        return lambda *a, **k: None


@pytest.fixture
def capture(monkeypatch):
    """Patch `products.registrar` at the module the wiring imports it FROM.

    `production_registrar` does `from pipeline.registration.products import
    registrar` inside the function body, so the name is looked up on the
    module at call time — patching the source module is what the interception
    needs, and patching a stale local binding is the way this kind of test
    silently stops intercepting anything.
    """
    import pipeline.registration.products as products_mod

    captured = _CapturingRegistrar()
    monkeypatch.setattr(products_mod, "registrar", captured)
    return captured


def _s3_client():
    """A client object the store can hold without any AWS call being made.

    `production_registrar` builds `S3ObjectStore(bucket, client=...)`, which
    stores the client; nothing in these tests reads an object. Passing one
    explicitly is what stops the factory constructing an ambient boto3 client
    — the instance-role AccessDenied class its own comment records.
    """
    return object()


@pytest.mark.contract
def test_the_live_registrar_passes_an_identity_repository(conn, capture,
                                                          records_bucket_env):
    """048 present: the LIVE path wires the repository, on the pass's own conn.

    **THE MUTATION CHECK FOR THIS ASSERTION IS WRITTEN BUT NOT YET RUN.**
    Removing the `identity_repository=` argument from
    `pipeline/operator/registrar.py:for_connection` should fail this test on
    `assert repository is not None`, and
    `scripts/mutation-brief-r-on-rapid-admin.sh` performs exactly that
    mutation — but the acceptance run carrying it lost its AWS credentials
    before its results could be read, so nothing has yet OBSERVED this test
    go red. Stated as pending rather than claimed: an unrun check is not
    evidence, and H round 2's standard is that a green must be earned.
    Re-run that script on rapid-admin to close it. See `notes-r-evidence.md`.
    """
    for table in IDENTITY_TABLES:
        if not fixture.has_table(conn, table):
            pytest.skip(f"DRAFT migration 048 is not applied ({table} absent)")

    factory = opregistrar.production_registrar(s3_client=_s3_client())
    assert factory is not None, "production defaults to production, not dry-run"

    factory(conn)

    assert len(capture.calls) == 1
    repository = capture.calls[0]["identity_repository"]
    assert repository is not None, (
        "the LIVE registrar did not pass an identity repository, so no "
        "product or artifact rows would be written by an operator pass — "
        "the dormancy R1 exists to end")
    assert isinstance(repository, ProductRepository)

    # THE SAME CONNECTION AS THE PASS, which is what makes the identity rows
    # atomic with the legacy rows and the watermark. A repository over any
    # other connection would be round-3 finding #8 through the new tables.
    assert repository._conn is conn


@pytest.mark.contract
def test_each_phase_gets_a_repository_on_its_own_connection(conn, second_conn,
                                                            capture,
                                                            records_bucket_env):
    """The operator's three phases each open their own registration connection.

    One repository built once in the enclosing factory could only ever belong
    to one of them — the same reasoning that makes this layer a factory at
    all (round-4 finding #2), now applying to the repository too. A regression
    to a shared instance shows up here as the same connection twice.
    """
    for table in IDENTITY_TABLES:
        if not fixture.has_table(conn, table):
            pytest.skip(f"DRAFT migration 048 is not applied ({table} absent)")

    factory = opregistrar.production_registrar(s3_client=_s3_client())

    factory(conn)
    factory(second_conn)

    first, second = capture.calls[0], capture.calls[1]
    assert first["identity_repository"] is not second["identity_repository"]
    assert first["identity_repository"]._conn is conn
    assert second["identity_repository"]._conn is second_conn


@pytest.mark.contract
def test_absent_048_degrades_to_legacy_only(conn, capture, records_bucket_env):
    """No 048: no repository, no exception, no rejection (D's P8).

    The absence is SIMULATED by pointing the probe at a table name that does
    not exist, rather than by dropping the real tables — this suite runs
    against a shared scratch database and a DROP would break every other
    test in the session. What is exercised is the wiring's own probe-and-
    degrade branch, on a real connection issuing a real catalog query.

    The assertion is deliberately BOTH halves: the repository is None (so
    `products.py` takes its documented pre-rollout branch and registration
    proceeds legacy-only) AND no exception escaped (so an attempt is never
    durably rejected for want of the new tables).
    """
    monkey = pytest.MonkeyPatch()
    monkey.setattr(
        opregistrar, "_IDENTITY_TABLES_PROBE",
        "SELECT to_regclass('public.products_absent_r1_probe') IS NOT NULL")
    try:
        factory = opregistrar.production_registrar(s3_client=_s3_client())
        factory(conn)
    finally:
        monkey.undo()

    assert capture.calls[0]["identity_repository"] is None, (
        "with 048 absent the wiring must degrade to legacy-only rather than "
        "passing a repository whose first statement would abort the "
        "registration transaction")

    # THE LEGACY WIRING IS UNTOUCHED BY THE DEGRADATION. A degradation that
    # also dropped the legacy handle or the fallback roles would be a second
    # defect wearing the first one's clothes.
    assert capture.calls[0]["dbh"] is not None
    assert capture.calls[0]["fallback_roles"] is None
