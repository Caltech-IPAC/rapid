"""The finalize stage's science: stamping a difference image's primary header.

Source: `origin/dev`'s post-processing pipeline (ppid 17,
``pipeline/awsBatchSubmitJobs_runSinglePostProcPipeline.py``) and the
helper it calls, ``modules/utils/rapid_pipeline_subs.py``
``addKeywordsToFITSHeader``. :mod:`rapidpipe.science.finalize.headers`
holds the keyword table as a pure function and the one FITS write.

Imports nothing from ``rapidpipe`` outside ``rapidpipe.science`` (stage
contract, dependency direction).
"""
