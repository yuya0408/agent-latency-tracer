# The receiving side for AgentCore Runtime: the image repository, the role the
# runtime runs as, and its security group. The runtime itself is created with
# the MCP server's image in stage 5 (docs/DESIGN.md §6).

data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  # Runtime names allow letters, digits and underscores only.
  runtime_prefix = "agent_latency_tracer"
}

resource "aws_ecr_repository" "mcp_server" {
  name         = "agent-latency-tracer/mcp-server"
  force_delete = true # destroy removes pushed images too
}

resource "aws_iam_role" "agentcore_runtime" {
  name = "agent-latency-tracer-agentcore-runtime"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "bedrock-agentcore.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account_id }
        ArnLike      = { "aws:SourceArn" = "arn:aws:bedrock-agentcore:${var.region}:${local.account_id}:*" }
      }
    }]
  })
}

# The execution role in the AgentCore docs, narrowed to this project: ECR to
# this repository only, and no Bedrock model access (the LLM is self-hosted).
resource "aws_iam_role_policy" "agentcore_runtime" {
  role = aws_iam_role.agentcore_runtime.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
        Resource = [aws_ecr_repository.mcp_server.arn]
      },
      {
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["logs:DescribeLogStreams", "logs:CreateLogGroup"]
        Resource = ["arn:aws:logs:${var.region}:${local.account_id}:log-group:/aws/bedrock-agentcore/runtimes/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["logs:DescribeLogGroups"]
        Resource = ["arn:aws:logs:${var.region}:${local.account_id}:log-group:*"]
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = ["arn:aws:logs:${var.region}:${local.account_id}:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*"]
      },
      {
        Effect   = "Allow"
        Action   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets"]
        Resource = "*"
      },
      {
        Effect    = "Allow"
        Action    = "cloudwatch:PutMetricData"
        Resource  = "*"
        Condition = { StringEquals = { "cloudwatch:namespace" = "bedrock-agentcore" } }
      },
      {
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:GetWorkloadAccessToken",
          "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
          "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
        ]
        Resource = [
          "arn:aws:bedrock-agentcore:${var.region}:${local.account_id}:workload-identity-directory/default",
          "arn:aws:bedrock-agentcore:${var.region}:${local.account_id}:workload-identity-directory/default/workload-identity/${local.runtime_prefix}*",
        ]
      },
    ]
  })
}

resource "aws_security_group" "agentcore" {
  name   = "agent-latency-tracer-agentcore"
  vpc_id = aws_vpc.main.id
}

# No ingress rules: callers reach the runtime through the AgentCore API, not its ENI.
resource "aws_vpc_security_group_egress_rule" "agentcore_https" {
  security_group_id = aws_security_group.agentcore.id
  cidr_ipv4         = var.vpc_cidr
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_egress_rule" "agentcore_postgres" {
  security_group_id            = aws_security_group.agentcore.id
  referenced_security_group_id = aws_security_group.rds.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_vpc_security_group_ingress_rule" "rds_from_agentcore" {
  security_group_id            = aws_security_group.rds.id
  referenced_security_group_id = aws_security_group.agentcore.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}
