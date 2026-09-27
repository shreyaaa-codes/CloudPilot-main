import boto3

ec2 = boto3.client('ec2', region_name = 'eu-north-1')
rds_client = boto3.client('rds', region_name = 'eu-north-1')

def discover_all(ec2_response,volume_response,rds_response):
    instances = []
    for reservation in ec2_response['Reservations']:
        for instance in reservation['Instances']:
            instance_id = instance['InstanceId']
            state = instance['State']['Name']
            instance_type = instance['InstanceType']
            launch_time = instance['LaunchTime']
            instances += [(instance_id,state,instance_type,launch_time)]

    volumes = []
    for volume in volume_response['Volumes']:
        volume_id = volume['VolumeId']
        size = volume['Size']
        state = volume['State']
        attachments = volume['Attachments']
        is_attached = len(attachments)>0
        volumes += [(volume_id, size, state, is_attached)]

    dbs = []
    for instance in rds_response['DBInstances']:
        instance_id = instance['DBInstanceIdentifier']
        engine = instance['Engine']
        status = instance['DBInstanceStatus']
        dbs += [(instance_id, engine, status)]

    return instances,volumes,dbs


def main():
    ec2_response = ec2.describe_instances()
    volume_response = ec2.describe_volumes()
    rds_response = rds_client.describe_db_instances()
    [instances,volumes,dbs]  = discover_all(ec2_response,volume_response,rds_response)
    for i in instances:
        print(f"ID: {i[0]} | State: {i[1]} | Type: {i[2]} | Launch Time: {i[3]}")

    for v in volumes:
        flag = "UNATTACHED - candidate for cleanup" if not v[3] else "attached"
        print(f"Volume: {v[0]} | Size: {v[1]} | State: {v[2]} | {flag}")

    for d in dbs:
        print(f"ID: {d[0]} | Engine: {d[1]} | Status: {d[2]}")


if __name__ == "__main__":
    main()