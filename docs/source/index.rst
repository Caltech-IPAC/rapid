.. Caltech-IPAC-RAPID documentation master file, created by
   sphinx-quickstart on Thu Mar 28 06:50:35 2024.
   You can adapt this file completely to your liking, but it should at least
   contain the root `toctree` directive.

RAPID Image-Difference Pipeline Documentation
#############################################

.. note::
   The RAPID Image-Difference Pipeline source code and documentation are
   under development at IPAC/Caltech.

   This Sphinx site documents the pipeline as it exists on the ``dev``
   branch. On ``rebuild``, the pipeline lives under ``rapidpipe/``;
   its design and operations pages are on the rapid_docs site instead.
   Paths named on the pages below, such as
   ``pipeline/``, ``alerts/``, ``database/schema/`` and
   ``database/scripts/``, exist on ``dev`` only.


Getting the Source Code
***********************

The source code is in the `RAPID GitHub Repository <https://github.com/Caltech-IPAC/rapid>`_.


Running the Latest RAPID Pipeline
*********************************

A Docker image pre-built from a recent git-clone of the RAPID GitHub
repository (8/21/26) has the pipeline installed and ready to run.
It is publicly available at:

.. code-block::

   public.ecr.aws/<ecr-public-alias>/rapid_science_pipeline:latest

The image is approximately 8.6 GB; the target machine needs sufficient
disk space. Use it to ``docker-run`` a container and execute code for
image-differencing and other tasks inside it. Interactive use requires a
bash entry point, which also inhibits the automated pipeline. For example,
use a ``docker-run`` command like:

.. code-block::

   docker run -it --entrypoint bash --name my_test -v /home/ubuntu/work/test_20241206:/work public.ecr.aws/<ecr-public-alias>/rapid_science_pipeline:latest


The image was built using this Docker file in the RAPID git repo:

.. code-block::

   rapid/docker/Dockerfile_ubuntu_runSingleSciencePipeline

It contains a RAPID git-clone in /code, so no volume binding to an external
filesystem containing the RAPID git repo is needed. It also contains a
C-code build of the RAPID software stack with this run-time environment:

.. code-block::

   export PATH=/code/c/bin:/root/.local/bin:/root/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
   export LD_LIBRARY_PATH=/code/c/lib

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
   dev/database_connections.rst
   analyses/analyses.rst

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
