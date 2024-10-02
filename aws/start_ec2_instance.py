import os
import time
import boto3


aws_access_key_id = os.getenv('AWS_ACCESS_KEY_ID')
aws_secret_access_key = os.getenv('AWS_SECRET_ACCESS_KEY')
aws_ec2_instance_id = os.getenv('AWS_EC2_INSTANCE_ID')
aws_ec2_volume_id = os.getenv('AWS_EC2_VOLUME_ID')
aws_ec2_volume_device = os.getenv('AWS_EC2_VOLUME_DEVICE')

if aws_access_key_id is None:

    print("*** Error: Env. var. AWS_ACCESS_KEY_ID not set; quitting...")
    exit(64)

if aws_secret_access_key is None:

    print("*** Error: Env. var. AWS_SECRET_ACCESS_KEY not set; quitting...")
    exit(64)

if aws_ec2_instance_id is None:

    print("*** Error: Env. var. AWS_EC2_INSTANCE_ID not set; quitting...")
    exit(64)

ec2 = boto3.client('ec2')


# Start EC2 instance.

response = ec2.start_instances(
    InstanceIds=[
        aws_ec2_instance_id,
    ],
    AdditionalInfo='string',
    DryRun=False
)

print("After starting EC2 instance: response =",response)


if (aws_ec2_volume_id is not None) and (aws_ec2_volume_device is not None):
    print("Sleeping for 30 seconds; then will attempt to attach volume {} to device {}...".format(aws_ec2_volume_id,aws_ec2_volume_device))
else:
    print("Sleeping for 30 seconds; then will call ec2.describe_instances to see if machine is running...")
time.sleep(30)


# Optionally attach volume to EC2 instance.

if aws_ec2_volume_id is not None:

    if aws_ec2_volume_device is not None:

        i = 0

        while i < 10:

            print("Waiting for EC2 instance to be in running state (will try 10 times): i =",i)

            response = ec2.describe_instance_status(
                InstanceIds=[
                    aws_ec2_instance_id,
                ],
                DryRun=False,
                IncludeAllInstances=True
            )

            print("After calling ec2.describe_instance_status: response =",response)

            current_ec2_instance_state = response['InstanceStatuses'][0]['InstanceState']['Name']

            print("current_ec2_instance_state =",current_ec2_instance_state)

            if current_ec2_instance_state == 'running':

                response = ec2.attach_volume(
                    Device=aws_ec2_volume_device,
                    InstanceId=aws_ec2_instance_id,
                    VolumeId=aws_ec2_volume_id,
                    DryRun=False
                 )

                print("After attaching volume: response =",response)

                break

            print("Sleeping for 30 seconds...")
            time.sleep(30)

            i += 1


# Describe EC2 instance and get public DNS name,corresponding IP address, and final state.

response = ec2.describe_instances(
    InstanceIds=[
        aws_ec2_instance_id,
    ],
    DryRun=False
)

print("After calling ec2.describe_instances: response =",response)

public_dns_name = response['Reservations'][0]['Instances'][0]['PublicDnsName']

print("public_dns_name =",public_dns_name)

public_ip_address = response['Reservations'][0]['Instances'][0]['PublicIpAddress']

print("public_ip_address =",public_ip_address)

current_state_of_ec2_instance = response['Reservations'][0]['Instances'][0]['State']

print("current_state_of_ec2_instance =",current_state_of_ec2_instance)


# Terminate.

print("Terminating normally...")

exit(0)
