.. admonition:: Retired
   :class: warning

   This page is retired and is kept for history only. It installs Docker on
   an Ubuntu EC2 instance. The deployed environment runs RHEL 10 with
   rootless podman, not Ubuntu with Docker, and container hosts are built
   from a golden AMI rather than configured by hand.

Install Docker on EC2 instance
####################################################

Install Docker on Ubuntu EC2 instance
**********************************************************

1. Ssh into Ubuntu EC2 instance


.. code-block::

   ssh -i ~/.ssh/MyKey.pem ubuntu@ubuntu@ec2-34-219-130-182.us-west-2.compute.amazonaws.com


2. Update package instance:

.. code-block::

   sudo apt-get update

3. Install Docker service:

.. code-block::

   sudo apt-get install docker.io -y

4. Start Docker service:

.. code-block::

   sudo systemctl start docker

5. Configure to automatically start Docker service on system boot (usually already configured by AWS when EC2 instance is launched):

.. code-block::

   sudo systemctl enable docker.service

6. Verify installation

.. code-block::

   sudo docker run hello-world


Install Docker on Centos EC2 instance
**********************************************************

TBW
