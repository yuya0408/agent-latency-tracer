# Interface endpoints that let the SSM Agent on the bastion reach Systems
# Manager without internet access. Paid per hour per subnet, so they sit in
# the bastion's subnet only and go away with it (AgentCore has its own, in
# agentcore_endpoints.tf).

resource "aws_security_group" "ssm_endpoints" {
  name   = "agent-latency-tracer-ssm-endpoints"
  vpc_id = aws_vpc.main.id
}

resource "aws_vpc_security_group_ingress_rule" "ssm_endpoints_https" {
  security_group_id            = aws_security_group.ssm_endpoints.id
  referenced_security_group_id = aws_security_group.bastion.id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

# The list in the Systems Manager docs. Recent agents (3.3.40.0+) use
# ssmmessages instead of ec2messages, but the docs still list both.
resource "aws_vpc_endpoint" "ssm" {
  for_each            = toset(["ssm", "ssmmessages", "ec2messages"])
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.private[0].id]
  security_group_ids  = [aws_security_group.ssm_endpoints.id]
  private_dns_enabled = true # the usual SSM hostnames resolve to these inside the VPC
}
