SFFT environment: the venv investigation and its answer
=======================================================

**Answer: there is no conflict, and there is no venv. SFFT folds into the
main image environment, and the shell activation path is deleted.**

Resolved 2026-08-06 (W5) inside the build container on ``rapid-admin``,
against ``rapid-pipeline@sha256:87fe2651...b1c2`` — the co-design's
instruction was to resolve this in the build container rather than by
reading pins, because the resolver is the authority on whether two
dependency sets coexist.

The question
------------

``pipeline/awsBatchSubmitJobs_runSingleSciencePipeline.py`` invoked SFFT
through a shell, activating a virtual environment first (lines 1895-1908
of that file, before its deletion)::

    activate_cmd = "source /sfft_env/bin/activate"
    deactivate_cmd = "deactivate"
    cmd = activate_cmd + " && " + sfft_cmd_str + " && " + deactivate_cmd
    exitcode_from_sfft,_ = util.execute_command_in_shell(cmd)

This was the pipeline's only genuine shell invocation, and the reason
``pipeline/runtime/process.py`` carries a checked shell variant
(``run_shell``) at all. The co-design left the question open for
implementation: *"the SFFT venv — today's only shell case — is
investigated at implementation: it folds into the main environment unless
a real dependency conflict forces it to stay, as a recorded fact."*

What the probes found
---------------------

Three probes, run in the image over SSM. Full output is in the W5 ledger;
the findings:

**1. The image's scientific stack.**

===========  ==========
numpy        2.4.6
scipy        1.18.0
astropy      8.0.1
photutils    3.0.0
pandas       2.3.3
cupy         absent
===========  ==========

**2. SFFT already imports in the main environment.** ``sfft`` 1.7.3 is
installed at ``/opt/rapid/conda/envs/rapid/lib/python3.14/site-packages``,
and all four imports ``modules/sfft/sfft_rapid_rimtimsim.py`` makes —
``sfft.CustomizedPacket.Customized_Packet``,
``sfft.utils.pyAstroMatic.PYSEx.PY_SEx``,
``sfft.utils.SFFTSolutionReader.Realize_MatchingKernel``,
``sfft.utils.DeCorrelationCalculator.DeCorrelation_Calculator`` — resolve
there.

**3. There is no /sfft_env in the image.** The directory does not exist.
The venv belonged to the legacy Ubuntu Dockerfile
(``docker/Dockerfile_ubuntu_runSingleSciencePipeline``), which the
rapid_systems build superseded on 2026-08-02. **The monolith's activation
line would therefore have failed outright in the current image** — one
more way the SFFT stage could not have run, alongside its call to
``util.execute_command_in_shell``, which W3 had already removed.

**4. Nothing needs installing.** ``pip install --dry-run sfft`` reports
every requirement already satisfied: scipy>=1.5.2, astropy>=3.2.3,
fastremap>=1.7.0, sep>=1.0.3, numba>=0.53.1, llvmlite>=0.36.0,
pyfftw>=0.12.0. No version conflict, no resolution work, nothing to pin.

The decision
------------

**One interpreter.** ``pipeline/stages/science.py``'s ``run_sfft`` invokes
SFFT through ``run_tool`` — argv list, ``shell=False``, checked — like
every other tool in the pipeline.

**The shell variant is kept.** ``run_shell`` may now have zero callers in
the payload, and that is fine: it is API, it is tested, and the runtime's
"no unchecked variant exists" property is stated in terms of it. Deleting
a checked path because it is momentarily unused would only mean the next
genuine shell case gets hand-rolled.

**The escape hatch is the image's to set, not the code's.** ``run_sfft``
reads ``RAPID_SFFT_VENV``; when it is set, SFFT is invoked through
``run_shell`` with the venv activated and every argument ``shlex.quote``-d.
It is not set in the Containerfile, so the folded-in path is what runs. If
a future SFFT release genuinely conflicts, the venv returns as an image
change and one ENV line — not as a code change.

One defect fixed in passing
---------------------------

The monolith built its shell command with ``' '.join(sfft_cmd)`` — no
quoting. Two of the arguments are gain-match catalogue filenames derived
from image filenames, so a space or shell metacharacter anywhere in an
input name would have become syntax. The surviving venv path quotes every
argument.
