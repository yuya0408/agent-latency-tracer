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

output "mcp_server_url" {
  value = local.mcp_server_url
}

output "rds_address" {
  value = aws_db_instance.main.address
}

output "rds_secret_arn" {
  value = aws_db_instance.main.master_user_secret[0].secret_arn
}
