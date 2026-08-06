W8: current state, as the next worker inherits it
=================================================

An inventory, not a narrative: what is true of the live system on
2026-08-06 after W8's battery, image rebuild and reconciler enablement.
Everything below was observed, not inferred. Where something is NOT done, it
says so and says what blocks it. Supersedes ``w6b_state_summary.rst``, whose
"What is still owed" list is closed except where noted.

The image and the pins
----------------------

=========================  ===================================================
Deployed digest            ``sha256:8c10d1e3…``, tag ``9be19d4-20260806``,
                           built from smdc ``9be19d4``
Tessellation               **retired from the image.** The 1.4 GiB COPY layer,
                           build.sh's staging step and
                           ``ROMANTESSELLATIONDBNAME`` in both job definitions
                           are gone, per W7's ``tessellation_bake_retirement``
Size                       2,382,072,461 B, down 328,903,392 B compressed from
                           the predecessor (the 1.4 GiB uncompressed layer)
Scan gate                  HIGH 3 / MEDIUM 5 / LOW 1 — **identical to the
                           baseline by CVE identity**, not merely by count
                           (CVE-2026-15308, -54369, -58016). Zero CRITICAL
Job definitions            ``rapid-pipeline-science`` and
                           ``rapid-pipeline-bulk``, **both revision 12**, both
                           pinned to that digest, with ``RAPID_IMAGE_DIGEST``
                           agreeing with the ``Image`` line
Quiesce                    proven before every revision: zero children in any
                           of SUBMITTED/PENDING/RUNNABLE/STARTING/RUNNING on
                           both queues
=========================  ===================================================

**The image does NOT carry the tessellation-import fix** (see below). smdc
is at ``df214ff``; the image is two commits behind it.

Batch's retry surface, found live
---------------------------------

Registering the definitions surfaced two undocumented Batch constraints, both
discovered by a refused deploy rather than by reading:

* **"Up to 5 evaluateOnExit condition can be specified."** The committed
  template had grown to six when the ``DockerTimeoutError`` rule landed, and
  had never been deployed since. The three ``Cannot*Error`` rows are now one.
* **"Evaluate on exit condition contains restricted characters."**
  ``Cannot*Error*`` is refused — the wildcard is valid only as a TRAILING
  character. The pattern is a bare ``Cannot*`` prefix, which admits exactly
  the three reasons the three rows named.

Both definitions now carry four conditions: ``Host EC2*`` retry, ``Cannot*``
retry, ``DockerTimeoutError*`` retry, ``*`` exit.

The reconciler
--------------

**Running as a supervised systemd service on rapid-admin** — the deliverable
W6b left blocked. ``active (running)``, polling every 60 s, zero errors.

W6b's blocker is closed. It named one IAM grant and said no single host could
take it; both halves are now deployed:

* ``rapid-db-instance-role`` joins ``DbServiceOrchestratorReadPolicy``, the
  dual-grant shape ``DbServicePipelineReadPolicy`` already documents;
* ``rapid-db-config.yaml`` gained **step 12**, flipping ``rapid_orchestrator``
  LOGIN from its secret, mirroring step 11 exactly.

Proven the way W6b's own readiness test asks for — not by the role existing
but by an authenticated connection: ``psql`` through the pooler on 6432
returns ``POOLER-LOGIN-OK as rapid_orchestrator``, and the service's own log
reads ``connected to …:6432/rapid as rapid_orchestrator
(application_name=rapid-reconciler[transaction])``.

**Three defects stood between "enabled" and "running"**, none of which a
one-shot under a human's credentials could have surfaced, all now fixed:

1. ``authentication required`` pulling the image — the unit ran ``podman
   run`` against a private ECR repo with no credential. The archive sink it
   copied its hosting pattern from never hit this because it runs the host's
   own interpreter. Fixed with an ``ExecStartPre`` ECR login.
2. ``DBSERVER is not set`` — ``rapid_db_connect`` requires the endpoint in
   the environment and refuses to compile in a default; a Batch job
   definition carries it and a systemd unit has no equivalent. Both the unit
   (from the tree, at install) and ``main.py`` (from the tree it already
   fetches) now bind it.
3. ``could not resolve database credentials`` — ``get_db_credentials``
   fetches the secret through boto3's DEFAULT chain, which in the container
   is the host instance role, which is deliberately not granted the
   orchestrator secret. ``main.py`` resolves it through the assumed service
   role instead.

**Restart survival proven both ways.** An orderly ``systemctl restart`` (new
MainPID, active) and a hard ``podman rm -f`` of the container out from under
systemd — ``Restart=always`` brought it back, ``NRestarts=1``, polling again
within 25 s.

**Health behaviour proven.** Three consecutive poll failures (injected by
revoking the scheduler client) flip ``healthy`` to false:
``[True, True, False, False]`` against a threshold of 3.

The battery
-----------

``pipeline/reconciler/test/live_w8_battery.py``, **33 of 33 proven**,
``W8-BATTERY-OK``, against live rapid-db, live Batch and the real records and
diagnostics buckets. Full case → mechanism → evidence → verdict table in
``w8_battery.rst``.

Two defects it found:

* **the application could author a reconciler-only error category** —
  ``mark_application_closed`` validated against the UNION of the vocabulary,
  so an application could write ``scheduler_reclaimed``. Fixed with an
  application-side allowlist. The test that should have caught it asserted
  the defect instead: it looped over the union and required the writer to
  accept every member;
* **every job of every type died on a tessellation class that does not
  exist** — see below.

Suites
------

All in-image on the deployed digest, with the working tree staged over
``/code``: **W8-TESTS-OK, 808 tests.**

=========================  ==========================================
``pipeline/runtime``       296 passed, 7 subtests
``pipeline/reconciler``    103 passed
``pipeline/entrypoints``   22 passed
``pipeline/stages``        74 passed, 40 subtests
``pipeline``               16 passed
``submission``             151 passed
``pipeline/registration``  18 passed
``observability``          128 passed, 30 subtests
=========================  ==========================================

Two suites could not previously be COLLECTED in the image they test.
``_install_third_party_stubs`` stubbed a module whenever it was absent from
``sys.modules`` — but not-yet-imported is not the same as not-installed, and
in the image every name in its list except ``injectionLightCurveModels`` is
real. Shadowing a real package with a bare ``ModuleType`` broke imports two
ways: ``from astropy.wcs import WCS`` found a stub with no ``WCS``, and a
stub at ``numpy.ma`` beneath the real numpy sent numpy 2.x's lazy
``__getattr__`` into unbounded recursion. Fixed in all four copies of the
helper, which now judge by importability.

What is still owed
------------------

**The tessellation-import fix is not in the image.** W8's rebuild budget was
two iterations and both were spent. ``tessellation_provenance`` imported
``RomanTessellation``, which does not exist — W7's class is
``RomanTessellationClosedForm`` — so every job of every type failed before
running a stage. Fixed and pushed (smdc ``df214ff``), with a regression test
that reaches past the stubs to the real module, and proven in-image by
staging: the class resolves, and ``get_rtid(11.1, -43.8)`` returns
**5321355**, the value the 2024 conversion note works through by hand.

*The next rebuild picks up ``df214ff``.* Until then a submitted job will die
at exit 70 exactly as the live proof's did.

**The per-type live proof is blocked on DATA, not on this layer.** g0001 is
fully registered — 5,166 ``l2files`` with matching ``l2filemeta``, fid 8, 109
distinct fields, ~48 frames per field — but ``PSFs`` and ``RefImages`` are
both **empty**, and no PSF artifact exists in the products bucket or the
staged-input bucket. ``reference_image`` needs ``psf_uri`` and
``coadd_inputs_uri``; ``science`` needs both a PSF and a reference image. So
the first stage of the first job type has no input. **Registration is the one
type this database can support today**, and it is the one that ran.

The blocking item is a PSF set and a first reference image for g0001.
Producing them is science work.

**The pooler RPM is still unpublished.** W6b's dnf-transaction hypothesis is
applied to ``rpms/smoke-test.sh`` but **untested**: the one authorized
promoter run failed in 75 seconds because a newer ``build-rpms.yml`` run was
in flight — the push carrying the fix triggered CI fifteen seconds after the
promoter started, and the promoter's guard correctly deferred rather than
racing. rapid-db still runs 1.0-2, ``rpm -V`` still reports ``S.5....T.``,
and the three ``.bak`` files stay.

One thing W8 established that changes the urgency: the missing pooler line
for ``rapid_orchestrator`` is **not** a blocker. The reconciler authenticates
through the pooler today with 1.0-2 installed and no per-user line at all,
because ``auth_query`` resolves it.

**A scheduler-retry child is still unforced.** No pull failure has been
provoked, so the attempt-index derivation remains proven by unit test and by
single-attempt live behaviour, never against a real ≥2-attempt job. W8
proved the surrounding machinery — the resolver, the claim, the
reconciler-first record with its category and binding — but not the ordinal
derivation against real ``AttemptDetail`` data.

**A successful registration is still owed.** Every live attempt remains an
application failure or an unrecordable one, so the refusal path is proven and
the register path is not. It is gated on the same rebuild.
