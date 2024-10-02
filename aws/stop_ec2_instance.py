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


# Optionally detach volume from EC2 instance.

if aws_ec2_volume_id is not None:

    if aws_ec2_volume_device is not None:


        # Check if volume actually needs to be detached.

        response = ec2.describe_volumes(
            VolumeIds=[
                aws_ec2_volume_id,
            ],
            DryRun=False
        )

        print("After calling ec2.describe_volumes: response =",response)

        current_volume_state = None

        try:

            current_volume_state = response['Volumes'][0]['Attachments'][0]['State']

        except:
            pass

        print("current_volume_state =",current_volume_state)

        if current_volume_state == 'detached' or current_volume_state is None:

            print("Volume {} is already detached...".format(aws_ec2_volume_id))

        else:


            # Execute command to detach volume.

            response = ec2.detach_volume(
                Device=aws_ec2_volume_device,
                InstanceId=aws_ec2_instance_id,
                VolumeId=aws_ec2_volume_id,
                Force=False,
                DryRun=False
            )

            print("After executing command to detach volume: response =",response)


            # Ensure volume is detached before stopping EC2 instance.

            i = 0

            while i < 10:


                # Check whether volume has detached.

                print("Waiting for volume to be in detached state (will try 10 times): i =",i)

                response = ec2.describe_volumes(
                    VolumeIds=[
                        aws_ec2_volume_id,
                    ],
                    DryRun=False
                )

                print("After calling ec2.describe_volumes: response =",response)

                current_volume_state = None

                try:

                    current_volume_state = response['Volumes'][0]['Attachments'][0]['State']

                except:
                    pass

                print("current_volume_state =",current_volume_state)

                if current_volume_state == 'detached' or current_volume_state is None:

                    print("Volume successfully detached: response =",response)

                    break

                print("Sleeping for 30 seconds; then will check again...")
                time.sleep(30)

                i += 1


# Stop EC2 instance.

response = ec2.stop_instances(
    InstanceIds=[
        aws_ec2_instance_id,
    ],
    Hibernate=False,
    Force=False,
    DryRun=False
)

print("After calling ec2.stop_instances: response =",response)

print("Sleeping for 30 seconds; then will check again...")
time.sleep(30)


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
