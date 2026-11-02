# The MCP server on AgentCore Runtime (stage 5), plus delivery of the
# platform's own spans (InvokeAgentRuntime). The image has to be in ECR
# before this is created: apply the repository first, push, then the rest.

variable "mcp_server_image_tag" {
  type    = string
  default = "latest"
}

resource "aws_bedrockagentcore_agent_runtime" "mcp_server" {
  agent_runtime_name = "${local.runtime_prefix}_mcp_server"
  role_arn           = aws_iam_role.agentcore_runtime.arn

  agent_runtime_artifact {
    container_configuration {
      container_uri = "${aws_ecr_repository.mcp_server.repository_url}:${var.mcp_server_image_tag}"
    }
  }

  # Same AZ as RDS, so the subnet with the interface endpoints.
  network_configuration {
    network_mode = "VPC"
    network_mode_config {
      subnets         = [aws_subnet.private[0].id]
      security_groups = [aws_security_group.agentcore.id]
    }
  }

  protocol_configuration {
    server_protocol = "MCP"
  }

  environment_variables = {
    # Platform spans to the shared aws/spans group, where the agent's and the
    # server's spans already go (new runtimes default to a per-runtime group).
    UNIFIED_TRACES_DESTINATION_ENABLED = "false"
    # The runtime otherwise sets OTEL_* variables for ADOT, which the stock
    # OTLP exporter would also read (e.g. headers that redirect the spans).
    DISABLE_ADOT_OBSERVABILITY = "true"

    RETRIEVAL_BACKEND = "pgvector"
    PGHOST            = aws_db_instance.main.address
    PGPORT            = tostring(aws_db_instance.main.port)
    PGDATABASE        = aws_db_instance.main.db_name
    PGUSER            = local.mcp_server_db_user
    RDS_IAM_AUTH      = "1"
    # Check the server's certificate against the RDS CA bundle in the image.
    PGSSLMODE     = "verify-full"
    PGSSLROOTCERT = "/app/rds-ca.pem"
  }

  # Headers the runtime passes through to the container. The docs don't say
  # whether trace context reaches it without being listed here.
  request_header_configuration {
    request_header_allowlist = ["traceparent", "tracestate"]
  }

  # The role's policies have to exist before the runtime pulls the image
  # and the server logs in to RDS.
  depends_on = [aws_iam_role_policy.agentcore_runtime, aws_iam_role_policy.agentcore_runtime_rds]
}

locals {
  # Where an MCP client connects (the runtime ARN goes in URL-encoded).
  mcp_server_url = "https://bedrock-agentcore.${var.region}.amazonaws.com/runtimes/${urlencode(aws_bedrockagentcore_agent_runtime.mcp_server.agent_runtime_arn)}/invocations?qualifier=DEFAULT"
}

# Turning on the runtime's tracing (the console's "Tracing" switch): its
# TRACES delivered to X-Ray, which puts them in aws/spans.
resource "aws_cloudwatch_log_delivery_source" "runtime_traces" {
  name         = "${local.runtime_prefix}_mcp_server-traces"
  log_type     = "TRACES"
  resource_arn = aws_bedrockagentcore_agent_runtime.mcp_server.agent_runtime_arn
}

resource "aws_cloudwatch_log_delivery_destination" "xray" {
  name                      = "agent-latency-tracer-xray"
  delivery_destination_type = "XRAY"
}

resource "aws_cloudwatch_log_delivery" "runtime_traces" {
  delivery_source_name     = aws_cloudwatch_log_delivery_source.runtime_traces.name
  delivery_destination_arn = aws_cloudwatch_log_delivery_destination.xray.arn
}
