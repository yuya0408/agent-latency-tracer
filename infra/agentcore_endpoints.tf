# Interface endpoints AgentCore Runtime needs in a VPC without internet access:
# ECR (image pulls) and CloudWatch Logs, plus X-Ray for the traces,
# bedrock-agentcore for the agent's calls to the MCP server's runtime and
# bedrock-runtime for the MCP server's query embeddings. Image layers
# come through the free S3 gateway endpoint in vpc.tf. One subnet only: the
# runtime sits in the same AZ as RDS (docs/DESIGN.md §1), and these are paid per AZ.

resource "aws_security_group" "agentcore_endpoints" {
  name   = "agent-latency-tracer-agentcore-endpoints"
  vpc_id = aws_vpc.main.id
}

resource "aws_vpc_security_group_ingress_rule" "agentcore_endpoints_https" {
  security_group_id            = aws_security_group.agentcore_endpoints.id
  referenced_security_group_id = aws_security_group.agentcore.id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

resource "aws_vpc_endpoint" "agentcore" {
  for_each            = toset(["ecr.api", "ecr.dkr", "logs", "xray", "bedrock-agentcore", "bedrock-runtime"])
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.private[0].id]
  security_group_ids  = [aws_security_group.agentcore_endpoints.id]
  private_dns_enabled = true
}
