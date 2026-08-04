"""
Array-job submission and job-startup configuration.

The submission side of design/compute.md: ready per-SCA work is batched
into array jobs, each batch carries a manifest binding array index to SCA
identity, and each job reads its configuration from the pipeline parameter
tree at startup and hashes it into a configuration digest.

Modules
-------
manifest
    The index -> SCA binding: build it, size the array from it, resolve a
    child's own entry, serialize it for the job to read back.
batching
    Accumulate ready work and cut it into submittable batches.
submit
    Turn one batch into one SubmitJob call.
startup
    What a job does when it starts: read the parameter tree, compute the
    configuration digest, resolve its own manifest entry.

Attempt records — the observability rows written at submission time — are
deliberately not here; they are their own workstream.
"""
