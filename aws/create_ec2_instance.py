"""
1. Allocate an Elastic IP address.
2. Create a new EC2 instance.
3. Associate the Elastic IP address with the EC2 instance.
"""

import os
import time
import boto3
from datetime import datetime

dt_object = datetime.now()
date_int = int(dt_object.strftime("%Y%m%d%H%M%S"))

print(date_int)

user = os.getenv('USER')

print(user)


# Default settings.

unique_machine_name = user . str(date_int)
ami_id = "ami-04dd23e62ed049936"                       # This is Streetfighter's AMI ID
instance_type = "t3.2xlarge"                           # This is Streetfighter's instance type
subnet_id = "subnet-018a469dbf588b7cd"                 # This is Streetfighter's subnet ID
security_group_id = "sg-02d51a3f98ee0a4f7"             # This is Streetfighter's security group ID
key_pair_name = "RussBuildMachine"                     # This is Russ's key-pair name
boot_disk_volume_size = 128                            # Size in GB


# Overrides from environment variables.

aws_access_key_id = os.getenv('AWS_ACCESS_KEY_ID')
aws_secret_access_key = os.getenv('AWS_SECRET_ACCESS_KEY')
aws_unique_machine_name = os.getenv('AWS_UNIQUE_MACHINE_NAME)
aws_ami_id = os.getenv('AWS_AMI_ID')
aws_instance_type = os.getenv('AWS_INSTANCE_TYPE')
aws_subnet_id = os.getenv('AWS_SUBNET_ID')
aws_security_group_id = os.getenv('AWS_SECURITY_GROUP_ID')
aws_key_pair_name = os.getenv('AWS_KEY_PAIR_NAME')
aws_boot_disk_volume_size = os.getenv('AWS_AMI_ID')

if aws_access_key_id is None:

    print("*** Error: Env. var. AWS_ACCESS_KEY_ID not set; quitting...")
    exit(64)

if aws_secret_access_key is None:

    print("*** Error: Env. var. AWS_SECRET_ACCESS_KEY not set; quitting...")
    exit(64)

if aws_unique_machine_name is not None:
    unique_machine_name = aws_unique_machine_name

if aws_ami_id is not None:
    ami_id = aws_ami_id

if aws_instance_type is not None:
    instance_type = aws_instance_type

if aws_subnet_id is not None:
    subnet_id = aws_subnet_id

if aws_security_group_id is not None:
    security_group_id = aws_security_group_id

if aws_key_pair_name is not None:
    key_pair_name = aws_key_pair_name

if aws_boot_disk_volume_size is not None:
    boot_disk_volume_size = aws_boot_disk_volume_size


# Method to create EC2 instance with above specifications.

def create_instance_with_eip(ec2_client,
                             unique_machine_name,
                             image_id,
                             instance_type,
                             subnet_id,
                             security_group_id,
                             eip_allocation_id,
                             key_pair_name,
                             boot_disk_volume_size):


    """
    Creates an AWS EC2 instance and associates an Elastic IP with it.

    Args:
        image_id (str): The ID of the AMI to use.
        instance_type (str): The instance type (e.g., "t2.micro").
        subnet_id (str): The ID of the subnet to launch the instance in.
        security_group_id (str): The ID of the security group to apply.
        eip_allocation_id (str): The allocation ID of the Elastic IP.

    Returns:
        str: The instance ID of the created instance.
    """

    tag_specifications=[
            {
                'ResourceType': 'instance',
                'Tags': [
                    {
                        'Key': 'Name',
                        'Value': unique_machine_name
                    },
                ]
            },
        ]


    # BlockDeviceMappings configuration
    block_device_mappings = [
        {
            'DeviceName': '/dev/sda1',                    # Root device
            'Ebs': {
                'VolumeSize': boot_disk_volume_size,      # Desired volume size in GiB
                'DeleteOnTermination': True,
                'VolumeType': 'gp3'                       # Desired volume type
            }
        }
    ]


    # Create and start an EC2 instance.
    instance_response = ec2_client.run_instances(
        ImageId=image_id,
        InstanceType=instance_type,
        MinCount=1,
        MaxCount=1,
        KeyName=key_pair_name,
        SubnetId=subnet_id,
        SecurityGroupIds=[security_group_id],
        BlockDeviceMappings=block_device_mappings,
        TagSpecifications = tag_specifications,
        InstanceInitiatedShutdownBehavior='stop'
    )
    instance_id = instance_response['Instances'][0]['InstanceId']
    print(f"Launched instance with ID: {instance_id}")


    # Wait for the instance to be running.

    running_flag = False

    while True:

        print("Check whether EC2 instance is in running state...")

        response = ec2.describe_instance_status(
            InstanceIds=[
                instance_id,
            ],
            DryRun=False,
            IncludeAllInstances=True
        )

        print("After calling ec2.describe_instance_status: response =",response)

        current_ec2_instance_state = response['InstanceStatuses'][0]['InstanceState']['Name']

        print("current_ec2_instance_state =",current_ec2_instance_state)

        if current_ec2_instance_state == 'running':

            running_flag = True

            print("EC2 instance is running...")

            break

        print("Sleeping for 30 seconds...")
        time.sleep(30)


    if running_flag:


        # Associate the Elastic IP with the EC2 instance.
        ec2_client.associate_address(
            AllocationId=eip_allocation_id,
            InstanceId=instance_id
        )

        print("Elastic IP associated with the instance.")


    return instance_id


#-------------------------------------------------------------------------------------------------------------
# Main program.
#-------------------------------------------------------------------------------------------------------------

if __name__ == '__main__':


    # Get EC2 client.
    ec2_client = boto3.client('ec2')


    # Allocate the Elastic IP.
    response = ec2_client.allocate_address(Domain='vpc')
    print(f"From allocate_address: response={response}")
    allocation_id = response['AllocationId']
    public_ip = response['PublicIp']


    # Print inputs.
    print("unique_machine_name =",unique_machine_name)
    print("ami_id =",ami_id)
    print("instance_type =",instance_type)
    print("subnet_id =",subnet_id)
    print("security_group_id =",security_group_id)
    print("allocation_id =",allocation_id)
    print("key_pair_name =",key_pair_name)
    print("boot_disk_volume_size =",boot_disk_volume_size)


    # Create a new EC2 instance and associate the Elastic IP address.
    instance_id = create_instance_with_eip(ec2_client,
                                           unique_machine_name,
                                           ami_id,
                                           instance_type,
                                           subnet_id,
                                           security_group_id,
                                           allocation_id,
                                           key_pair_name,
                                           boot_disk_volume_size)

    print(f"Instance ID: {instance_id}")
    print(f"Elastic IP: {public_ip}")

    print("The new EC2 instance will remaining running, so do not forget later to stop it when no longer needed to save money!")


    # Termination.

    exit(0)
