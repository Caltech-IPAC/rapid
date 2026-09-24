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
