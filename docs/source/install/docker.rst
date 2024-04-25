Install Docker on Ubuntu EC2 instance
====================


1. Ssh into Ubuntu EC2 instance


.. code-block::

   ssh -i ~/.ssh/MyKey.pem ubuntu@ubuntu@ec2-34-219-130-182.us-west-2.compute.amazonaws.com


2. Update package instance:

.. code-block::

   sudo apt-get update

3. Install docker:

.. code-block::

   sudo apt-get install docker.io -y

4. Start docker service:

.. code-block::

   sudo apt-get install docker.io -y

5. Verify installation

.. code-block::

   sudo docker run hello-world


Install Docker on Centos EC2 instance
====================

TBW
