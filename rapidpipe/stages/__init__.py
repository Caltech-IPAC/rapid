# ported-from: none
"""Stage entrypoints: one module per stage, each directly runnable.

Holds one module per stage name in the fixed list (``admit``, ``reference``,
``difference``, ``finalize``), each exporting ``main(argv)`` built on
``rapidpipe.stages.contract``. A stage module may import
``rapidpipe.products``, ``rapidpipe.db``, ``rapidpipe.foundation`` and
``rapidpipe.science``, but never another stage module, ``rapidpipe.launch``
or ``rapidpipe.cli``. The database stages of the run-model rebuild
(``register``, ``load``, ``maintain``, ``crossmatch``, ``statistics``,
``prune``, ``alerts``, ``export``) were retired with that schema; the
foundation plan's Part 5 items 2 and 3 port their science to the
publish-last shape.
"""
