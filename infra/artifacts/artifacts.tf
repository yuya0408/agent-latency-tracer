# What the GPU server takes from inside the VPC, which has no way out to the
# internet: the weights (S3) and the images (ECR). Kept apart from ../ because
# that is destroyed after each stage, and moving these again takes a GPU server.

data "aws_caller_identity" "current" {}

resource "aws_s3_bucket" "artifacts" {
  # Bucket names are global. The account ID is read at run time, so it stays
  # out of the repository.
  bucket        = "agent-latency-tracer-artifacts-${data.aws_caller_identity.current.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_ecr_repository" "vllm" {
  name         = "agent-latency-tracer/vllm"
  force_delete = true # destroy removes pushed images too
}

resource "aws_ecr_repository" "otel_collector" {
  name         = "agent-latency-tracer/otel-collector"
  force_delete = true
}

output "artifacts_bucket" {
  value = aws_s3_bucket.artifacts.bucket
}

output "vllm_repository_url" {
  value = aws_ecr_repository.vllm.repository_url
}

output "otel_collector_repository_url" {
  value = aws_ecr_repository.otel_collector.repository_url
}
