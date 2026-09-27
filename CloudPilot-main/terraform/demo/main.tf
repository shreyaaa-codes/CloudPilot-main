terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
    }
  }
}

provider "aws" {
  region                      = "eu-north-1"
  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_requesting_account_id  = true
  access_key                  = "mock"
  secret_key                  = "mock"
}

# ── Variables ─────────────────────────────────────────────────────────────────
# Change these in terraform.tfvars to produce different CloudPilot decisions.
#
#   ALLOW scenario:  ec2_instance_type = "t3.small"   (~$7.59/mo delta)
#   BLOCK scenario:  rds_instance_class = "db.r6g.2xlarge"  (~$493.48/mo delta)

variable "ec2_instance_type" {
  description = "EC2 instance type for the demo workload"
  type        = string
  default     = "t3.micro"
}

variable "rds_instance_class" {
  description = "RDS instance class for the checkout database"
  type        = string
  default     = "db.t3.medium"
}

# ── Resources ─────────────────────────────────────────────────────────────────

resource "aws_instance" "cloudpilot_demo" {
  ami           = "ami-0abcdef1234567890"
  instance_type = var.ec2_instance_type

  tags = {
    Name        = "CloudPilot-Demo"
    Environment = "development"
    Owner       = "CloudPilot"
  }
}

resource "aws_db_instance" "checkout" {
  identifier          = "checkout"
  instance_class      = var.rds_instance_class
  engine              = "postgres"
  allocated_storage   = 20
  username            = "demo"
  password            = "demo-password-not-real"
  skip_final_snapshot = true

  tags = {
    Environment = "production"
    Owner       = "Payments"
  }
}