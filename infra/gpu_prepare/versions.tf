terraform {
  required_version = ">= 1.10" # use_lockfile (S3-native state locking) needs 1.10

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.68"
    }
  }

  # Same bucket as ../, a separate key:
  #   terraform init -backend-config=../backend.hcl
  backend "s3" {
    key          = "agent-latency-tracer/gpu_prepare.tfstate"
    region       = "ap-northeast-1"
    use_lockfile = true
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = { Project = "agent-latency-tracer" }
  }
}

variable "region" {
  type    = string
  default = "ap-northeast-1"
}
