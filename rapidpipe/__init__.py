"""RAPID pipeline package.

``rapidpipe`` is the import package of the ``rapid-pipeline`` distribution:
Roman Alerts Promptly from Image Differencing, rebuilt to the stage contract
in ``rapid_docs/system``. It has ten subpackages -- ``stages`` (one module
per stage, each directly runnable), ``products`` (identifiers, kinds,
manifest types and the spatial derivations registration needs), ``db``
(persistence: typed repositories, connection and migrations), ``science``
(the algorithms stages call), ``checks`` (candidate checks and policies),
``runs`` (runs, units of work, attempts, the three output states, checking
and promotion), ``launch`` (turning a run into Batch jobs), ``selftest``
(stage fixtures), ``cli`` (the command-line tool) and ``release`` (cutting,
recording and verifying releases) -- and four leaf modules, ``exitcodes``,
``log``, ``revision`` and ``seams``, which import no other part of the
package.

Dependency direction is a fixed layer order, and a unit imports only units
strictly below it: the leaves < ``products`` < ``db`` and ``science``
(which do not import each other) < ``checks`` < ``runs`` < ``stages`` <
``launch`` < ``selftest`` < ``cli``. ``release`` imports only the leaves
and ``db``, and only ``cli`` imports it. Stage modules never import each
other. ``tests/unit/test_dependency_direction.py`` enforces all of it.
"""

__version__ = "0.1.0"
