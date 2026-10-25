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


Running the Latest RAPID Pipeline
*************************************

A Docker image has been pre-built from a recent git-clone of the RAPID Github
repository (10/21/26).
This Docker image offers the convenience of having the RAPID
pipeline already installed and ready to run.  It is publicly available from

.. code-block::

   public.ecr.aws/y9b1s7h8/rapid_science_pipeline:latest

It is currently approximately 15.3 GB in size, and requires sufficient disk space on the target machine.
It can be used to ``docker-run`` a container and from within execute
code for image-differencing, etc., using a ``docker-run`` command like the
following (note that an entry point to bash is required for interactive use
and to inhibit running the automated pipeline):

.. code-block::

   docker run -it --entrypoint bash --name my_test --shm-size=64g -e TMPDIR=/dev/shm \
       -v /home/ubuntu/work/test_20241206:/work \
       -e OMP_NUM_THREADS=1 -e OPENBLAS_NUM_THREADS=1 -e MKL_NUM_THREADS=1 -e NUMEXPR_NUM_THREADS=1 \
       public.ecr.aws/y9b1s7h8/rapid_science_pipeline:latest

The options after the container name were adopted for the 10/21/2026 socsims test (see
:doc:`dev/socsim_tests`):

* ``--shm-size=64g`` and ``-e TMPDIR=/dev/shm`` put the container's temporary files in memory
  (``/dev/shm``, 64 MB by default in Docker) rather than on the instance's root volume.  Forced
  photometry in alert production stages each previous image there; on the root volume (gp3, 125
  MiB/s) that was disk-bound at 33.9 s per image, in memory it took 2.1 s.  The 64 GB come out of
  the instance's memory, so lower the size on an instance with less memory.
* ``OMP_NUM_THREADS``, ``OPENBLAS_NUM_THREADS``, ``MKL_NUM_THREADS`` and ``NUMEXPR_NUM_THREADS``
  set to 1 keep each process to one thread.  The pipeline stages already run one process per core,
  and without these, numpy, scipy and numexpr would each start as many threads as there are cores
  in every process, overloading the machine.


The Docker file used to generate this Docker image is

.. code-block::

   rapid/docker/Dockerfile_ubuntu_runSingleSciencePipeline

in the RAPID git repo.  The Docker image self-contains a
RAPID git-clone in the /code directory (no volume binding to an
external filesystem containing the RAPID git repo is necessary).  The
Docker image also contains a
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
   pl/l2_ingest.rst

RAPID Computing Architecture
************************************

.. toctree::
   :maxdepth: 2

   sysarch/comp_arch.rst

RAPID Pipeline Execution
************************************

.. toctree::
   :maxdepth: 2

   ops/vpo.rst
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
