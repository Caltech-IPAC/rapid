# Stage fixtures (rapid_docs system/stage-contract.md, "Local execution"):
# `make stage-<name>` prepares an isolated fixture, runs the stage without
# account credentials, and checks its products and manifest.
#
# PYTHON is the interpreter that runs the stage; it needs rapidpipe's
# dependencies (numpy, astropy, healpy; photutils, pandas, pyarrow and
# sympy too for the real tools).

PYTHON ?= python3

# The finalize stage's fixture: a synthetic difference attempt's products
# republished with the stamped header (rapidpipe/selftest/support/
# fakefinalize.py builds the inputs); no tools, no database, runs anywhere.
.PHONY: stage-finalize
stage-finalize:
	$(PYTHON) tests/fixtures/finalize/run_fixture.py

