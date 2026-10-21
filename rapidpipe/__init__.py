"""RAPID pipeline package.

``rapidpipe`` is the import package of the ``rapid-pipeline`` distribution:
Roman Alerts Promptly from Image Differencing, rebuilt to the stage contract
in ``rapid_docs/system`` and to the foundation plan (v5, 2026-10-20). It
has eight subpackages -- ``stages`` (one module per stage, each directly
runnable), ``products`` (identifiers, kinds, manifest types and spatial
derivations), ``db`` (the connection and identifiers), ``science`` (the
algorithms stages call), ``foundation`` (runs, jobs, the publisher and the
official bindings over ``database/schema.sql``), ``launch`` (submitting a
job to Batch or running it locally), ``selftest`` (stage fixtures) and
``cli`` (the command-line tool) -- and three leaf modules, ``exitcodes``,
``log`` and ``revision``, which import no other part of the
package.

Dependency direction is a fixed layer order, and a unit imports only units
strictly below it: the leaves < ``products`` < ``db``, ``science`` and
``foundation`` (which do not import each other) < ``stages`` < ``launch``
< ``selftest`` < ``cli``. Stage modules never import each other. ``tests/unit/test_dependency_direction.py`` enforces all of it.
"""

__version__ = "0.1.0"
