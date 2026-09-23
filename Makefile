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
