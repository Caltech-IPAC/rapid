W6b: current state, as W8 inherits it
=====================================

An inventory, not a narrative: what is true of the live system on
2026-08-06 after W6's three tails were closed. Everything below was
observed, not inferred — the commands and their exit codes are in the W6b
ledger. Where something is NOT done, it says so and says what blocks it.

The database
------------

======================  ================================================
Migrations applied      **000 through 016.** 016 is the last row in
                        ``schema_migrations``; it was the only pending
                        file and applied on the first attempt
``rapid_orchestrator``  exists, ``NOLOGIN``, not superuser, not
                        createrole, member of ``rapid_pipeline_write``
                        (which carries SELECT/INSERT/UPDATE on
                        ``attempts``). **Cannot authenticate yet** — see
                        "What is still owed"
``tessellation``        **6,291,458 rows**, version ``nside512-v2``,
                        1928 MB. Loaded 2026-08-06, having been empty
                        since 015 created it
Tessellation digest     ``ee5b7a61…8767cd9``, verified by dumping the
                        stored rows back out and re-digesting them
                        through the builder's own serializer — not a row
                        count, which would pass on 6.3M wrong rows
======================  ================================================

The tessellation's point predicate at catalog scale, against the live
table: **Index Scan** on ``tessellation_bbox_idx``, 6.4 ms, 2433 shared
buffer hits, returning **rtid 5321355** for (11.1, -43.8) — the value the
2024 conversion note works through by hand, so the loaded rows are
semantically right and not merely digest-identical. The batched-lookup
shape recorded for comparison takes **12.4 s** for 5,000 points against
25M buffer hits, which is the arithmetic case for the closed form.

A duplicate ``(version, rtid)`` insert is refused, live. So is a whole
re-load: running ``load.sh --target rapid-db`` a second time fails with
``tessellation version nside512-v2 is already installed`` and exits 1,
rather than silently doubling 6.3M rows.

Two staging prefixes are left in the build-artifacts bucket under
``tessellation-staging/``: one from W6's stub run (11:26Z, 451 MB —
the run that staged and exited 0 without loading) and one from the
immutability re-run above (14:06Z, 79 MB). The second is the script's
designed behaviour — a failed load leaves its prefix for diagnosis — and
both are safe to delete once read. Not deleted here: object deletion was
outside W6b's authorization.

The pooler
----------

The ``client_idle_timeout`` defect is fixed **on the live host** (W6) and
fixed **in the config source** (W6b, ``rapid-pgbouncer`` 1.0-4, committed
and pushed), which also carries the ``rapid_orchestrator`` line 016 needs.

**The drift is not yet closed on the host.** rapid-db still runs
``rapid-pgbouncer`` 1.0-2 and ``rpm -V`` still reports ``S.5....T.`` on
``/etc/pgbouncer/pgbouncer.rapid.ini``, because 1.0-4 never published —
the promoter's smoke gate refused the whole candidate set (see below). So
a package reinstall or a rebuilt host would **still** restore the defect
today. What changed is that the fix is now in git and in a built, signed
RPM rather than existing only as a hand edit on one host; what remains is
a green promoter run and ``dnf update rapid-pgbouncer``.

Three ``.bak`` files sit in ``/etc/pgbouncer`` from the hand fixes
(``pre-idle-fix``, ``pre-rapid-ops-drop``, ``pre-two-lanes``). They are
ephemeral state, removable once the package matches the live file — which
is exactly what has not happened yet, so they were deliberately left in
place.

The upstream question stands and is labelled as such: that per-user
``client_idle_timeout`` on five *human* logins reached ``rapid_pipeline``,
which has no per-user line at all, is **unverified against the pgbouncer
issue tracker**. The config says so where someone would re-add it. See
``pooler_client_idle_timeout.rst``.

The reconciler
--------------

Unchanged from W6: proven as a one-shot under ``rapid_pipeline``, with a
systemd unit deployed **disabled**. It is not running as a service, and
enabling it is not merely a systemctl call away — the service starts as
``rapid_orchestrator``, which cannot log in yet.

What is still owed
------------------

**One IAM grant blocks the reconciler service**, and everything else in
that chain is done. ``rapid_orchestrator`` needs its password from
``rapid/db/service/orchestrator`` to become ``LOGIN``, and the account's
identity split means no single host can perform that step today:

- ``rapid-migration-runner-role`` (the admin credential, the only identity
  that may run DDL) trusts ``rapid-db-instance-role`` **only**;
- ``rapid-orchestrator-role`` (the orchestrator secret) trusts
  ``rapid-admin-instance-role`` **only**.

Both verified live: rapid-admin got ``AccessDenied`` assuming the
migration runner, and ``rapid-db-instance-role``'s attached policies
include ``rapid-db-service-pipeline-read`` but no orchestrator
equivalent. ``DbServiceOrchestratorReadPolicy`` attaches to
``OrchestratorRole`` alone, where the pipeline equivalent
(``DbServicePipelineReadPolicy``) also lists ``rapid-db-instance-role``.

So the association step that would do this cannot be written either — it
runs on rapid-db under the instance role and would fail ``AccessDenied``
on every convergence pass. The fix is one line (add
``rapid-db-instance-role`` to that managed policy's ``Roles``) plus a
deploy of the live IAM stack, and it is left **proposed** rather than
taken, being outside W6b's authorization.

``pgbouncer.get_auth('rapid_orchestrator')`` reads ``NOT-RESOLVABLE``
until it lands. That is correct, not a fault.

Also still open, carried forward unchanged:

- **A scheduler-retry child.** No pull failure has been forced, so the
  attempt-index derivation is proven by unit test and by single-attempt
  live behaviour, never against a real ≥2-attempt job.
- **A successful registration.** Every live attempt is an application
  failure, so the refusal path is proven and the register path is not.
- **The master ``.ini`` and the baked tessellation constructors**, whose
  deletions the W6 fence's own conditions refused: 23 and 11 surviving
  readers respectively. See ``config_homes.rst`` and
  ``tessellation_bake_retirement.rst``.

Suites, as of this run
----------------------

All in-image on rapid-admin, against the pipeline image pinned by digest
``sha256:8f42e92e…`` — the claim being that they pass where the code
runs, not merely somewhere:

=====================  =========================================
W1 (``run-on-``)       ``W1-TESTS-OK`` — units plus a live
                       round-trip against rapid-db
W3 (``run-w3-``)       ``W3-TESTS-OK`` — 61 + 82 + 6 + 293 + 10
                       tests, all OK
W7 (``run-w7-``)       ``W7-UNITS-OK`` — 16 tests, 2 declared
                       skips (cross-repo parity and the SQLite
                       comparison, neither available in-image and
                       both saying so rather than passing quietly)
=====================  =========================================

W3 needed a fix to get there, and it is worth knowing why: the runner
staged ``pipeline/runtime`` but not the ``cdf/`` configuration files that
W4B's science-config suite reads. Those tests assert **the shipped files**
load, deliberately, and they arrived after the runner did — so five tests
errored with "the release's science configuration is missing", an error
correctly naming a staging gap that reads as a wrong image.

The promoter's smoke gate
-------------------------

Worth knowing before the next release attempt, because it is the last gate
between a built package and a published one, and it is where W6b's RPM
publish stopped.

The promoter builds, signs, and then installs the whole signed candidate
set on a **disposable canary** launched from the golden AMI. That smoke run
failed on::

    [Errno 2] No such file or directory:
    '/var/cache/dnf/rapid-.../packages/rapid-release-1.1-2.el10.noarch.rpm'

    FAIL: integration smoke failed on the signed candidate set — no publish

Two things this is NOT: it is not a rapid-pgbouncer problem (the gate's own
inventory line reports ``rapid-pgbouncer=true`` — the package built, signed,
and is present in the candidate repo), and it is not the baseline-duplicate
defect below, which was fixed earlier in the same run and did not recur.

It is a dnf cache miss on the canary, on ``rapid-release`` — the repo
definition package — immediately after a GPG key import. The refusal to
publish on it is correct behaviour: an unproven candidate set must not
reach the repo.

**It is reproducible, not a flake.** Two full promoter runs (46 and 50
minutes, fresh canary each time) failed at the same step with the
byte-identical message. Both downloaded all 28 packages successfully —
964 MB, including ``rapid-release`` itself — and then failed the
transaction.

The mechanism, from the log ordering and **labelled a hypothesis: not
proven by a probe**. ``rpms/smoke-test.sh`` installs everything in ONE
dnf transaction, and that list contains both ``rapid-release`` and the
third-party repo-definition packages (``rapid-pgadmin-repo``,
``rapid-vscode-repo``, ``rapid-rpmfusion-repo``). Installing a repo
definition mid-transaction lands a new ``.repo`` file, dnf then imports
that repo's GPG key — the log shows the pgAdmin key import on the line
immediately before the error — and the cache directory the pending
packages were downloaded into is invalidated underneath the running
transaction. ``rapid-release`` is simply the first file it then cannot
find.

If that reading is right the fix is to install the repo-definition
packages in their own transaction, after the rest, the same way
``rapid-fleet-config`` is already ordered last for a related resolution
reason. **Not attempted here** — each verification cycle is a ~50-minute
promoter run, and W6b had no room for one after the second failure.
Whoever picks it up should confirm the cache-invalidation reading on a
canary first rather than trusting this paragraph.

One CI defect, found and fixed
------------------------------

``build-rpms.yml``'s ``sync-baseline`` job verified the baseline package
set **before** pruning it, where every other merge point prunes first. The
overlay it builds is a version-bumped duplicate factory by construction,
so any release bump to an already-baselined package broke main on the next
scoped run — and the failure is self-sustaining, because the promoter
refuses to publish while a newer main run is red, so a red main blocks the
release that would clear it.

Found live: ``expected exactly one rapid-pgbouncer RPM (x86_64/noarch) in
baseline; found 2``, two consecutive red runs on main, the first of them
at 02:06Z on the rapid-ops drop — before W6b started. Fixed by pruning
before the gate, which is what ``repo-and-smoke`` already does.
