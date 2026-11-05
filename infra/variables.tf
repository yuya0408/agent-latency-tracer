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

variable "pitfalls" {
  description = <<-EOT
    Cases to switch on (system/pitfalls.py), e.g. "1,2". Both runtimes get
    the same value: each process acts only on its own cases, and both record
    it on their spans, so a trace says which run it came from. Empty is the
    baseline.
  EOT
  type        = string
  default     = ""

  validation {
    condition     = can(regex("^([0-9]+(,[0-9]+)*)?$", var.pitfalls))
    error_message = "pitfalls: case numbers separated by commas, without spaces (e.g. \"1,2\")."
  }
}
