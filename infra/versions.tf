terraform {
  required_version = ">= 1.10" # use_lockfile (S3-native state locking) needs 1.10

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.68"
    }
  }

  # The bucket is not written here; it comes from backend.hcl (not committed):
  #   terraform init -backend-config=backend.hcl
  backend "s3" {
    key          = "agent-latency-tracer/terraform.tfstate"
    region       = "ap-northeast-1"
    use_lockfile = true
  }
}

provider "aws" {
  region = var.region

  # Every resource gets this tag, so anything left running can be found by it.
  default_tags {
    tags = { Project = "agent-latency-tracer" }
  }
}
