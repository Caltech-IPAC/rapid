# ported-from: none
"""Stage entrypoints: one module per stage, each directly runnable.

Holds one module per stage. The foundation's stages (``import``, ``associate``,
``packet``, ``reference``, ``difference``) export ``run(job)``
(:class:`rapidpipe.foundation.local.JobContext`); the stages still on the
older contract, the fixed list ``admit``, ``finalize``,
export ``main(argv)`` built on ``rapidpipe.stages.contract``. A stage module may import
``rapidpipe.products``, ``rapidpipe.db``, ``rapidpipe.foundation`` and
``rapidpipe.science``, but never another stage module, ``rapidpipe.launch``
or ``rapidpipe.cli``. The database stages of the run-model rebuild
(``register``, ``load``, ``maintain``, ``crossmatch``, ``statistics``,
``prune``, ``alerts``, ``export``) were retired with that schema; the
foundation plan's Part 5 items 2 and 3 port their science to the
publish-last shape.
"""
