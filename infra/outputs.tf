output "vpc_id" {
  value = aws_vpc.main.id
}

output "private_subnet_ids" {
  value = aws_subnet.private[*].id
}

output "bastion_instance_id" {
  value = aws_instance.bastion.id
}

output "ecr_repository_url" {
  value = aws_ecr_repository.mcp_server.repository_url
}

# Where the MCP client connects (the runtime ARN goes in URL-encoded).
output "mcp_server_url" {
  value = "https://bedrock-agentcore.${var.region}.amazonaws.com/runtimes/${urlencode(aws_bedrockagentcore_agent_runtime.mcp_server.agent_runtime_arn)}/invocations?qualifier=DEFAULT"
}

output "rds_address" {
  value = aws_db_instance.main.address
}

output "rds_secret_arn" {
  value = aws_db_instance.main.master_user_secret[0].secret_arn
}
