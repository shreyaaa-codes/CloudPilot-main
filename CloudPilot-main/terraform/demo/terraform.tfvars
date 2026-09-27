# Baseline configuration: the "current state" of infrastructure.
# Change a value here in a PR to trigger a CloudPilot review.
#
#   ALLOW:  ec2_instance_type  = "t3.small"        (small, safe upgrade)
#   BLOCK:  rds_instance_class = "db.r6g.2xlarge"   (expensive, risky upgrade)

ec2_instance_type  = "t3.micro"
rds_instance_class = "db.t3.medium"
