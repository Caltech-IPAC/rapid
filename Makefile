# Stage fixtures (rapid_docs system/stage-contract.md, "Local execution"):
# `make stage-<name>` prepares an isolated fixture, runs the stage without
# account credentials, and checks its products and manifest.
#
# PYTHON is the interpreter that runs the stage; it needs rapidpipe's
# dependencies (numpy, astropy, healpy; photutils, pandas, pyarrow and
# sympy too for the real tools).

PYTHON ?= python3

# fake: every external tool replaced by tests/unit/fakedifftools.py's
# stand-ins; runs anywhere. real: SExtractor, SWarp, bkgest, ZOGY, SFFT,
# photutils and the SIP-to-PV converter for real -- the pipeline image.
DIFFERENCE_TOOLS ?= fake

.PHONY: stage-difference
stage-difference:
	$(PYTHON) tests/fixtures/difference/run_fixture.py --tools $(DIFFERENCE_TOOLS)

# The load stage's fixture: a difference attempt's catalogs into sources,
# against the fake database (tests/unit/fakeloaddb.py); runs anywhere. The
# PostgreSQL path is tests/db/test_load.py.
.PHONY: stage-load
stage-load:
	$(PYTHON) tests/fixtures/load/run_fixture.py

# The finalize stage's fixture: a synthetic difference attempt's products
# republished with the stamped header (rapidpipe/selftest/support/
# fakefinalize.py builds the inputs); no tools, no database, runs anywhere.
.PHONY: stage-finalize
stage-finalize:
	$(PYTHON) tests/fixtures/finalize/run_fixture.py

# The maintain stage's fixture: CLUSTER/ANALYZE of a sources child table,
# against the fake database (tests/unit/fakemaintaindb.py); runs anywhere.
# The PostgreSQL path is tests/db/test_maintain.py.
.PHONY: stage-maintain
stage-maintain:
	$(PYTHON) tests/fixtures/maintain/run_fixture.py

# The crossmatch stage's fixture: two source sets' sources into one field's
# astroobjects and merges, both passes, against the fake database
# (rapidpipe/selftest/support/fakecrossmatchdb.py); runs anywhere. The
# PostgreSQL path is tests/db/test_crossmatch.py.
.PHONY: stage-crossmatch
stage-crossmatch:
	$(PYTHON) tests/fixtures/crossmatch/run_fixture.py

# The alerts stage's fixture: a synthetic difference image, reference
# catalog and catalog result sets into an Avro container and outbox rows,
# against the fake database (rapidpipe/selftest/support/fakealertsdb.py);
# runs anywhere PYTHON has fastavro. The PostgreSQL path is
# tests/db/test_alerts.py.
.PHONY: stage-alerts
stage-alerts:
	$(PYTHON) tests/fixtures/alerts/run_fixture.py

# The statistics stage's fixture: per-object statistics over an association
# set's base-plus-delta membership, against the fake database
# (rapidpipe/selftest/support/fakestatisticsdb.py); runs anywhere. The
# PostgreSQL path is tests/db/test_statistics.py.
.PHONY: stage-statistics
stage-statistics:
	$(PYTHON) tests/fixtures/statistics/run_fixture.py

# The prune stage's fixture: the not-best merge exclusion into a pruned-set,
# against the fake database (tests/unit/fakeprunedb.py); runs anywhere. The
# PostgreSQL path is tests/db/test_prune.py.
.PHONY: stage-prune
stage-prune:
	$(PYTHON) tests/fixtures/prune/run_fixture.py

# The reference stage's fixture: three small gzipped L2-shaped frames
# coadded into a 128x128 reference with its SExtractor catalog and header
# stamp; no database. REFERENCE_TOOLS=fake (the default) replaces awaicgen
# and SExtractor with rapidpipe/selftest/support/fakereftools.py and runs
# anywhere; real runs the pipeline image's own tools.
REFERENCE_TOOLS ?= fake

.PHONY: stage-reference
stage-reference:
	$(PYTHON) tests/fixtures/reference/run_fixture.py --tools $(REFERENCE_TOOLS)

# The photometry stage's fixture: a declared stub (supervisor step 8,
# 2026-09-24, ruling R9) -- a structurally valid input-set manifest still
# exits 69 and publishes no manifest; no tools, no database, runs anywhere.
.PHONY: stage-photometry
stage-photometry:
	$(PYTHON) tests/fixtures/photometry/run_fixture.py

# The export stage's fixture (supervisor step 8, 2026-09-24, ruling R12):
# a fake database of ~200 sources in named source sets, and hats-import run
# for real (needs hats-import installed) into one catalog-export.
.PHONY: stage-export
stage-export:
	$(PYTHON) tests/fixtures/export/run_fixture.py
