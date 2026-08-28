.. Caltech-IPAC-RAPID documentation master file, created by
   sphinx-quickstart on Thu Mar 28 06:50:35 2024.
   You can adapt this file completely to your liking, but it should at least
   contain the root `toctree` directive.

RAPID Image-Difference Pipeline Documentation
####################################################

Welcome! This is the documentation for the RAPID Image-Difference
Pipeline, under development at IPAC/Caltech.


.. note::
   Development of source code and documentation is currently ongoing.


Running the RAPID Pipeline
*************************************

The pipeline is run by a supervised service — the Virtual Pipeline Operator —
which polls the operations database for ready work and submits AWS Batch
array jobs. It is not launched by running a script per phase. The full
procedure is :doc:`RAPID Pipeline Execution </ops/bulk_run>`; the design
record for the operator is :doc:`/dev/vpo_service`.

The operational container image is built and deployed from the RAPID
**infrastructure repository**, not from this one: a unified application
image, a base layer plus a filtered source layer, which the AWS Batch job
definitions and the service units are pinned to by digest. The legacy
single-pipeline container recipe that used to live at
``docker/Dockerfile_ubuntu_runSingleSciencePipeline`` was removed from this
repository when that image superseded it.

The public science-stack image
=============================================

A separate, publicly readable image carries the RAPID science stack — the
C-code build, the Python dependencies and a git-clone of the repository at
``/code`` — for interactive use: running image differencing, ``awaicgen``,
catalog generation and similar by hand.

.. code-block::

   public.ecr.aws/y9b1s7h8/rapid_science_pipeline:latest

.. note::

   This image is built from the ``dev`` branch — see
   ``docker/Dockerfile_ubuntu_runSingleSciencePipeline.sh``, which passes
   ``--build-arg RAPID_BRANCH=dev``. It therefore does **not** carry the
   operator, reconciler, publisher or ``rapidctl`` entry points, and it is
   not the image the deployed pipeline runs. Use it to exercise the science
   code interactively, not to run or reproduce an operational pipeline.

   That build script cannot be run from this branch as it stands: the
   Dockerfile it names was removed here along with the rest of the legacy
   container recipe, so rebuilding the public image is done from ``dev``.

It is approximately 8.4 GB and requires sufficient disk space on the target
machine. The image sets an ``ENTRYPOINT``, so an interactive shell must
override it with ``--entrypoint bash`` (and ``bash`` must not be appended at
the end of the command):

.. code-block::

   docker run -it --entrypoint bash --name my_test -v /home/ubuntu/work/test_20241206:/work public.ecr.aws/y9b1s7h8/rapid_science_pipeline:latest

No volume binding is needed to supply the source: the clone at ``/code`` is
self-contained. The run-time environment for the C-code build is

.. code-block::

   export PATH=/code/c/bin:/root/.local/bin:/root/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
   export LD_LIBRARY_PATH=/code/c/lib



Getting the Source Code
*****************************

Please refer to the `RAPID GitHub Repository <https://github.com/Caltech-IPAC/rapid>`_ for the source code.

..
   Separate file for installation of the pipeline and building the C code.


Installing RAPID Pipeline
************************************

.. toctree::
   :maxdepth: 2

   install/install.rst


RAPID Operations Database
************************************

.. toctree::
   :maxdepth: 2

   db/db.rst

RAPID Pipeline Design
************************************

.. toctree::
   :maxdepth: 2

   pl/pl.rst

RAPID Computing Architecture
************************************

.. toctree::
   :maxdepth: 2

   sysarch/comp_arch.rst

RAPID Pipeline Execution
************************************

.. toctree::
   :maxdepth: 2

   ops/bulk_run.rst

RAPID Pipeline Products
************************************

.. toctree::
   :maxdepth: 1

   prod/products.rst

RAPID Pipeline Development
************************************

.. toctree::
   :maxdepth: 2

   dev/notes.rst
   dev/tests.rst
   dev/vpo_service.rst
   dev/post_db_chain.rst
   dev/tessellation_bake_retirement.rst
   analyses/analyses.rst

Development history and evidence
************************************

These record how the pipeline reached its current shape: review
dispositions, hardening rounds, per-worker state summaries, and the live
evidence behind specific rulings. They are the durable home for that kind
of narration — history belongs here, while the code's own docstrings state
what is true now.

Every file below existed already and was reachable only by direct link:
22 of the 27 files under ``dev/`` were absent from every toctree, so Sphinx
built them without a route in and warned about each one. Adding to that
collection would have made the problem worse, so it is fixed here instead.

.. toctree::
   :maxdepth: 1
   :caption: Reviews and audits

   dev/review_disposition.rst
   dev/attempt_writer_review.rst
   dev/execute_command_exit_code_audit.rst
   dev/config_homes.rst
   dev/o1_env_policy.rst

.. toctree::
   :maxdepth: 1
   :caption: Hardening and fix rounds

   dev/fix_round.rst
   dev/preq8_hardening.rst
   dev/smoke_run.rst
   dev/sfft_environment.rst
   dev/pooler_client_idle_timeout.rst

.. toctree::
   :maxdepth: 1
   :caption: Simulated-data test campaigns

   dev/openuniv_tests.rst
   dev/rimtimsim_tests.rst
   dev/socsim_tests.rst
   dev/gbtds_sim_g0001_registration.rst

.. toctree::
   :maxdepth: 1
   :caption: Worker evidence and state summaries

   dev/w5_canary_evidence.rst
   dev/w6_completion_evidence.rst
   dev/w6b_state_summary.rst
   dev/w8_battery.rst
   dev/w8_state_summary.rst
   dev/w9_ramp.rst
   dev/w9_state_summary.rst
   dev/w9prep_state_summary.rst

RAPID Archive Deliveries
************************************

.. toctree::
   :maxdepth: 1

   archive/archive.rst

RAPID Forced Photometry
************************************

.. toctree::
   :maxdepth: 1

   fp/fp_backend.rst

Acronyms
************************************

.. toctree::
   :maxdepth: 2

   acronyms.rst

Indices and Tables
==================

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
