# The receiving side for AgentCore Runtime: the role the runtime runs as and
# its security group (the image repository is in ../artifacts/). The runtime
# itself is created with the MCP server's image in stage 5 (docs/DESIGN.md §6).

data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  # Runtime names allow letters, digits and underscores only.
  runtime_prefix = "agent_latency_tracer"
  # Created in the database by scripts/create_mcp_server_user.sql.
  mcp_server_db_user = "mcp_server"
}

data "aws_ecr_repository" "mcp_server" {
  name = "agent-latency-tracer/mcp-server"
}

locals {
  runtime_assume_role_policy = jsonencode({
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

  # The execution role in the AgentCore docs, narrowed to this project and with
  # no Bedrock model access (the LLM is self-hosted). Each runtime adds ECR
  # access to its own repository.
  runtime_common_statements = [
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
      # The container's own spans, sent over OTLP (tracing/aws_exporter.py).
      Effect   = "Allow"
      Action   = "xray:PutSpans"
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
}

resource "aws_iam_role" "agentcore_runtime" {
  name               = "agent-latency-tracer-agentcore-runtime"
  assume_role_policy = local.runtime_assume_role_policy
}

resource "aws_iam_role_policy" "agentcore_runtime" {
  role = aws_iam_role.agentcore_runtime.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([{
      Effect   = "Allow"
      Action   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
      Resource = [data.aws_ecr_repository.mcp_server.arn]
    }], local.runtime_common_statements)
  })
}

# Logging in to RDS as the MCP server's DB user, with an IAM token.
resource "aws_iam_role_policy" "agentcore_runtime_rds" {
  role = aws_iam_role.agentcore_runtime.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "rds-db:connect"
      Resource = "arn:aws:rds-db:${var.region}:${local.account_id}:dbuser:${aws_db_instance.main.resource_id}/${local.mcp_server_db_user}"
    }]
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

# Image layers come from S3 through the gateway endpoint, which keeps S3's
# public addresses: the VPC CIDR rule above doesn't cover them.
resource "aws_vpc_security_group_egress_rule" "agentcore_s3" {
  security_group_id = aws_security_group.agentcore.id
  prefix_list_id    = aws_vpc_endpoint.s3.prefix_list_id
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
