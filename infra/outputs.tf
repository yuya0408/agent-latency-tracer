output "vpc_id" {
  value = aws_vpc.main.id
}

output "private_subnet_ids" {
  value = aws_subnet.private[*].id
}

output "bastion_instance_id" {
  value = aws_instance.bastion.id
}

output "rds_address" {
  value = aws_db_instance.main.address
}

output "rds_secret_arn" {
  value = aws_db_instance.main.master_user_secret[0].secret_arn
}
