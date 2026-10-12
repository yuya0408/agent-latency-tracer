variable "region" {
  type    = string
  default = "ap-northeast-1"
}

variable "az_ids" {
  description = <<-EOT
    AZ IDs (not names: names map to different AZs in each account) for the
    private subnets. AgentCore Runtime reaches a VPC only from apne1-az1, az2
    and az4, and RDS needs subnets in at least two AZs.
  EOT
  type        = list(string)
  default     = ["apne1-az1", "apne1-az2"]
}

variable "vpc_cidr" {
  type    = string
  default = "10.0.0.0/16"
}
