"""
The pipeline's algorithmic stages, extracted from the payload monoliths.

W5 of the Batch payload co-design. The three
`awsBatchSubmitJobs_runSingle*Pipeline.py` scripts were each one flat
`if __name__ == '__main__':` block — 2,961 lines in the science case — in which
the algorithm and the operational skeleton were the same code. The standing
ruling was to rewrite them "to skeleton depth: algorithmic stage bodies
extracted as-is, operational skeleton fully replaced".

That is the division of labour here. **The stage bodies in this package are the
monoliths' bodies**, moved rather than rewritten: the same calls in the same
order against the same helpers, so that a reviewer holding the old file beside
the new one is reading the same science. What changed is only what extraction
forces:

- **Parameters instead of globals.** A monolith stage read `swarp_dict` from
  module scope; the extracted stage takes it as an argument. The values are
  identical — the mechanism by which they arrive is not.
- **Release content instead of the downloaded `.ini`.** Tool parameters come
  from `cdf/science/pipeline.toml` through W4's reader, not from a 749-line
  master file re-serialized onto S3 per job. The values were extracted
  mechanically and are round-trip tested against that `.ini` (W4), so this
  substitution does not change a science result.
- **Per-invocation facts instead of `.ini` sections.** Which exposure, which
  SCA, which reference image: from the manifest's `UnitFacts`.
- **`run_tool` instead of `execute_command`.** Already done at the helper level
  by W3 — the extracted stages inherit fail-loud tool invocation rather than
  implementing it.
- **The per-attempt working directory instead of the container cwd.** No
  `chdir` into a subdirectory, no cwd-relative filenames.

**What is deliberately absent.** No `.done` sentinel is written, no log is
grepped, no exit code carries application meaning, and nothing tests `>= 64`.
Those were the operational skeleton, and the runtime replaced them: a stage
signals failure by raising, `run_stage` records the span, and the termination
protocol authors the outcome.

Modules
-------
context
    `StageContext` — what every stage is handed. Replaces the ~40 module-level
    globals the monoliths shared implicitly.
science
    The science (prompt differencing) pipeline's stages.
reference_image
    The reference-image construction pipeline's stages.
post_process
    The post-process pipeline's stages.
sequences
    The per-job-type stage sequences the entrypoint dispatches to.
"""

from pipeline.stages.context import StageContext  # noqa: F401
from pipeline.stages.sequences import (  # noqa: F401
    SEQUENCES,
    sequence_for,
)

__all__ = [
    "SEQUENCES",
    "StageContext",
    "sequence_for",
]
