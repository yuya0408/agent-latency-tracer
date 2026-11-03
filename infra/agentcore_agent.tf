# The agent on its own AgentCore Runtime (stage 5b), separate from the MCP
# server's: it calls the vLLM server (gpu.tf) inside the VPC and the MCP server
# through the AgentCore API. Like the MCP server, the image has to be in ECR
# before the runtime is created: apply the repository first, push, then the rest.

variable "agent_image_tag" {
  type    = string
  default = "latest"
}

resource "aws_ecr_repository" "agent" {
  name         = "agent-latency-tracer/agent"
  force_delete = true # destroy removes pushed images too
}

resource "aws_iam_role" "agent_runtime" {
  name               = "agent-latency-tracer-agent-runtime"
  assume_role_policy = local.runtime_assume_role_policy
}

# No RDS: the agent reaches the documents only through the MCP server.
resource "aws_iam_role_policy" "agent_runtime" {
  role = aws_iam_role.agent_runtime.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Effect   = "Allow"
        Action   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
        Resource = [aws_ecr_repository.agent.arn]
      },
      {
        Effect = "Allow"
        Action = "bedrock-agentcore:InvokeAgentRuntime"
        Resource = [
          aws_bedrockagentcore_agent_runtime.mcp_server.agent_runtime_arn,
          "${aws_bedrockagentcore_agent_runtime.mcp_server.agent_runtime_arn}/*",
        ]
      },
    ], local.runtime_common_statements)
  })
}

resource "aws_security_group" "agent" {
  name   = "agent-latency-tracer-agent"
  vpc_id = aws_vpc.main.id
}

# No ingress rules, as for the MCP server's runtime.
resource "aws_vpc_security_group_egress_rule" "agent_https" {
  security_group_id = aws_security_group.agent.id
  cidr_ipv4         = var.vpc_cidr
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

# Image layers through the S3 gateway endpoint (see agentcore.tf).
resource "aws_vpc_security_group_egress_rule" "agent_s3" {
  security_group_id = aws_security_group.agent.id
  prefix_list_id    = aws_vpc_endpoint.s3.prefix_list_id
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_egress_rule" "agent_to_vllm" {
  security_group_id            = aws_security_group.agent.id
  referenced_security_group_id = aws_security_group.gpu.id
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
}

resource "aws_vpc_security_group_ingress_rule" "vllm_from_agent" {
  security_group_id            = aws_security_group.gpu.id
  referenced_security_group_id = aws_security_group.agent.id
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
}

resource "aws_vpc_security_group_ingress_rule" "agentcore_endpoints_from_agent" {
  security_group_id            = aws_security_group.agentcore_endpoints.id
  referenced_security_group_id = aws_security_group.agent.id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

resource "aws_bedrockagentcore_agent_runtime" "agent" {
  agent_runtime_name = "${local.runtime_prefix}_agent"
  role_arn           = aws_iam_role.agent_runtime.arn

  agent_runtime_artifact {
    container_configuration {
      container_uri = "${aws_ecr_repository.agent.repository_url}:${var.agent_image_tag}"
    }
  }

  # Same subnet as the GPU server and the endpoints.
  network_configuration {
    network_mode = "VPC"
    network_mode_config {
      subnets         = [aws_subnet.private[0].id]
      security_groups = [aws_security_group.agent.id]
    }
  }

  protocol_configuration {
    server_protocol = "HTTP"
  }

  environment_variables = {
    # As for the MCP server (agentcore_runtime.tf).
    UNIFIED_TRACES_DESTINATION_ENABLED = "false"
    DISABLE_ADOT_OBSERVABILITY         = "true"

    LLM_BASE_URL   = "http://${aws_instance.gpu.private_ip}:8000/v1"
    LLM_MODEL      = "Qwen/Qwen3-8B" # vLLM's --served-model-name (gpu_boot.sh.tftpl)
    LLM_PROVIDER   = "vllm"
    MCP_SERVER_URL = local.mcp_server_url
  }

  request_header_configuration {
    request_header_allowlist = ["traceparent", "tracestate"]
  }

  depends_on = [
    aws_iam_role_policy.agent_runtime,
    aws_vpc_endpoint.agentcore,
    aws_vpc_security_group_ingress_rule.agentcore_endpoints_from_agent,
    aws_vpc_security_group_ingress_rule.vllm_from_agent,
  ]
}

# The runtime's own spans to X-Ray, as for the MCP server.
resource "aws_cloudwatch_log_delivery_source" "agent_runtime_traces" {
  name         = "${local.runtime_prefix}_agent-traces"
  log_type     = "TRACES"
  resource_arn = aws_bedrockagentcore_agent_runtime.agent.agent_runtime_arn
}

resource "aws_cloudwatch_log_delivery" "agent_runtime_traces" {
  delivery_source_name     = aws_cloudwatch_log_delivery_source.agent_runtime_traces.name
  delivery_destination_arn = aws_cloudwatch_log_delivery_destination.xray.arn
}

output "agent_ecr_repository_url" {
  value = aws_ecr_repository.agent.repository_url
}

output "agent_runtime_arn" {
  value = aws_bedrockagentcore_agent_runtime.agent.agent_runtime_arn
}
