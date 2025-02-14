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


Running the latest RAPID Pipeline
*************************************

A docker image has been pre-built from a recent git-clone of the RAPID Github
repository (2/14/25).
This docker image offers the convenience of having the RAPID
pipeline already installed and ready to run.  It is publicly available from

.. code-block::

   public.ecr.aws/y9b1s7h8/rapid_science_pipeline:latest

You can use it to docker-run a container and from within execute
code for image-differencing, etc., using a docker-run command like the
following (note that an entry point to bash is required for interactive use
and to inhibit running the automated pipeline):

.. code-block::

   docker run -it --entrypoint bash --name my_test -v /home/ubuntu/work/test_20241206:/work public.ecr.aws/y9b1s7h8/rapid_science_pipeline:latest


The Docker file used to generate this docker image is

.. code-block::

   rapid/docker/Dockerfile_ubuntu_runSingleSciencePipeline

in the RAPID git repo.  The docker image self-contains a
RAPID git-clone in the /code directory (no volume binding to an
external filesystem containing the RAPID git repo is necessary).  The
docker image also contains a
C-code build of the RAPID software stack with the following run-time environment:

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

RAPID Pipeline Development Notes
************************************

.. toctree::
   :maxdepth: 2

   dev/notes.rst

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
