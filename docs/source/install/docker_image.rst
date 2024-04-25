Create Docker image
####################################################


Handy commands to free disk space
**********************************************************
.. code-block::

   sudo docker image ls
   sudo docker image rm -f redhat/ubi9
   sudo docker image rm -f rapid:1.0
   sudo docker system prune -a -f       # This command removes all Docker images.


Create Docker image on Ubuntu EC2 instance
**********************************************************
.. code-block::

   sudo docker build --file /source/code/location/docker/Dockerfile_ubuntu --tag rapid:1.0 .

Create Docker image on Centos EC2 instance
**********************************************************

TBW
