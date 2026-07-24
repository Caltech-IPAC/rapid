"""Throwaway prototypes: three ways to use pydantic for the alert schema.

  v1_annotated.py      -- vanilla pydantic generates the .avsc (no check)
  v2_registry_style.py -- registry-layout DSL generates the .avsc
                          (python -m test_pydantic.v2_check)
  v3_avsc_first.py     -- committed .avsc is the truth; plain pydantic
                          models are drift-checked against it
                          (python -m test_pydantic.v3_check)

See README.md for the trade-offs. Not wired into rapid_alerts.
"""
