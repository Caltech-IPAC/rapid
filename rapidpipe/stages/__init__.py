# ported-from: none
"""Stage entrypoints: one module per stage, each exporting ``run(job)``.

The foundation's stages (``import``, ``intake``, ``psf``, ``packet``,
``reference``, ``difference``) export ``run(job)``
(:class:`rapidpipe.foundation.local.JobContext`), run by ``job local`` and
``job run``. A stage module may import
``rapidpipe.products``, ``rapidpipe.db``, ``rapidpipe.foundation`` and
``rapidpipe.science``, but never another stage module, ``rapidpipe.launch``
or ``rapidpipe.cli``. The database stages of the run-model rebuild
(``register``, ``load``, ``maintain``, ``crossmatch``, ``statistics``,
``prune``, ``alerts``, ``export``) were retired with that schema; the
foundation plan's Part 5 items 2 and 3 port their science to the
publish-last shape. ``associate``, their port, was retired on 2026-10-24:
the difference job associates its own sources (the plan's amendment).
The older ``main(argv)`` contract's ``admit`` and ``finalize`` were retired
on 2026-10-25; git keeps them.
"""
