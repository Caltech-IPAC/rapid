#!/bin/bash -x


#####################################################################################
# This script is used to build the RAPID-pipeline Docker image,
# and propagate it to the AWS Elastic Container Registry (ECR).
#
# Instructions:
# 1. Log into EC2 instance.
# 2. Sudo to root with the following command:
#        sudo su
# 3. Execute the following command in the background:
#        docker/Dockerfile_ubuntu_runSingleSciencePipeline.sh &
# The build output is captured automatically in the build.out local file:
#        tail -f build.out
#####################################################################################

cd /home/ubuntu/rapid
git pull
docker system prune -a -f
cd /home/ubuntu/rapid

# The RAPID git repo branch dev is specified as an argument to the following docker build command.

docker build --build-arg RAPID_BRANCH=dev --no-cache --file /home/ubuntu/rapid/docker/Dockerfile_ubuntu_runSingleSciencePipeline --tag rapid_science_pipeline:1.0 . >& build.out

cksum=$(tail -n 2 build.out | grep "Successfully built" | sed 's/Successfully built //')
echo $cksum

# ECR_PUBLIC_ALIAS is account-specific; set it in the environment, never
# committed here.
: "${ECR_PUBLIC_ALIAS:?Set ECR_PUBLIC_ALIAS to the account public ECR alias}"

aws ecr-public get-login-password --region us-east-1 | docker login --username AWS --password-stdin public.ecr.aws/$ECR_PUBLIC_ALIAS

docker tag $cksum public.ecr.aws/$ECR_PUBLIC_ALIAS/rapid_science_pipeline:latest
docker push public.ecr.aws/$ECR_PUBLIC_ALIAS/rapid_science_pipeline:latest
