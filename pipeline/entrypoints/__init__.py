"""
The container's entrypoint.

One module, invoked as the job definition's command with a fixed workload-class
discriminator. `pipeline/entrypoints/job.py` is the whole of it; this package
exists so that the command line is a stable, importable path
(`python -m pipeline.entrypoints.job --class prompt`) rather than a script
location that moves when the tree does.
"""
