"""Stage entrypoints: one module per stage, each directly runnable.

Holds one module per stage name in the fixed list (``admit``, ``reference``,
``difference``, ``finalize``, ``register``, ``load``, ``crossmatch``,
``statistics``, ``prune``, ``alerts``, ``photometry``, ``export``), each
exporting ``main(argv)`` built on ``rapidpipe.stages.contract``. A stage
module may import ``rapidpipe.products``, ``rapidpipe.db``, ``rapidpipe.runs``
and ``rapidpipe.science``, but never another stage module, ``rapidpipe.launch``
or ``rapidpipe.cli``.
"""
