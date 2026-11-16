# What the VPC, which has no way out to the internet, takes from inside: the
# weights (S3) and the images (ECR). Kept apart from ../ because that is
# destroyed after each stage. Moving vLLM and the weights again takes a GPU
# server, and the MCP server and agent images take a build and push each.

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

resource "aws_ecr_repository" "mcp_server" {
  name         = "agent-latency-tracer/mcp-server"
  force_delete = true
}

resource "aws_ecr_repository" "agent" {
  name         = "agent-latency-tracer/agent"
  force_delete = true
}

# Each build is tagged with its commit and pushed by buildx as three images: an
# index (tagged), the image and a provenance attestation (both untagged). Keep
# the last three builds. ECR does not expire an image an index still refers
# to, so the untagged rule only removes what an expired index left behind.
resource "aws_ecr_lifecycle_policy" "keep_recent" {
  for_each = {
    vllm           = aws_ecr_repository.vllm.name
    otel_collector = aws_ecr_repository.otel_collector.name
    mcp_server     = aws_ecr_repository.mcp_server.name
    agent          = aws_ecr_repository.agent.name
  }
  repository = each.value

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep the last three tagged builds"
        selection = {
          tagStatus      = "tagged"
          tagPatternList = ["*"]
          countType      = "imageCountMoreThan"
          countNumber    = 3
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Remove untagged images no index refers to"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 1
        }
        action = { type = "expire" }
      },
    ]
  })
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

output "mcp_server_repository_url" {
  value = aws_ecr_repository.mcp_server.repository_url
}

output "agent_repository_url" {
  value = aws_ecr_repository.agent.repository_url
}
