#!/bin/bash -x


# This script is used to build the RAPID-pipeline Docker image,
# and propagate it to the AWS Elatstic Container Registry (ECR).
#
# Instructions:
# 1. Log into EC2 instance.
# 2. Sudo to root with the following command:
#        sudo su
# 3. Execute the following command:
#        docker/Dockerfile_ubuntu_runSingleSciencePipeline.sh
# The build output is captured automatically in the build.out local file.

cd /home/ubuntu/rapid
git pull
docker system prune -a -f
cd /home/ubuntu/rapid

docker build --no-cache --file /home/ubuntu/rapid/docker/Dockerfile_ubuntu_runSingleSciencePipeline --tag rapid_science_pipeline:1.0 . >& build.out

cksum=$(tail -n 2 build.out | grep "Successfully built" | sed 's/Successfully built //')
echo $cksum

aws ecr-public get-login-password --region us-east-1 | docker login --username AWS --password-stdin public.ecr.aws/y9b1s7h8

docker tag $cksum public.ecr.aws/y9b1s7h8/rapid_science_pipeline:latest
docker push public.ecr.aws/y9b1s7h8/rapid_science_pipeline:latest
