"""RAPID pipeline package.

``rapidpipe`` is the import package of the ``rapid-pipeline`` distribution:
Roman Alerts Promptly from Image Differencing, rebuilt to the stage contract
in ``rapid_docs/system``. It has eight subpackages -- ``stages`` (one module
per stage, each directly runnable), ``products`` (identifiers, kinds and
manifest types), ``db`` (persistence: typed repositories, connection and
migrations), ``runs`` (runs, units of work, attempts, the three output
states, promotion), ``launch`` (turning a run into Batch jobs), ``cli`` (the
command-line tool), ``science`` (the algorithms stages call) and ``release``
(cutting, recording and verifying releases). Dependency
direction is fixed: ``products`` imports none of ``runs``, ``db`` or
``stages``; ``db`` imports neither ``runs`` nor ``stages``; ``runs`` composes
``products`` and ``db``; ``science`` imports none of ``stages``, ``launch``
or ``cli``; stage modules never import each other, ``launch`` or ``cli``;
``release`` may import ``db`` and ``runs``, and only ``cli`` imports it.
"""

__version__ = "0.1.0"
