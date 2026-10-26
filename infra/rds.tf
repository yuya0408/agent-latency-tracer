# RDS for PostgreSQL with pgvector, reachable only from inside the VPC. For
# now only the bastion may connect; the MCP server on AgentCore is added later.

resource "aws_db_subnet_group" "main" {
  name       = "agent-latency-tracer"
  subnet_ids = aws_subnet.private[*].id # RDS requires subnets in two AZs
}

resource "aws_security_group" "rds" {
  name   = "agent-latency-tracer-rds"
  vpc_id = aws_vpc.main.id
}

resource "aws_vpc_security_group_ingress_rule" "rds_from_bastion" {
  security_group_id            = aws_security_group.rds.id
  referenced_security_group_id = aws_security_group.bastion.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_db_instance" "main" {
  identifier     = "agent-latency-tracer"
  engine         = "postgres"
  engine_version = "18"            # the default minor; the actual version is recorded with the measurements
  instance_class = "db.t4g.medium" # the table and HNSW index at 100,000 x 1024 (533MB + 781MB) fit in memory

  allocated_storage = 20
  storage_type      = "gp3"
  storage_encrypted = true

  db_name  = "rag"
  username = "postgres"
  # RDS generates the password and keeps it in Secrets Manager, so it never
  # appears in the Terraform state.
  manage_master_user_password = true

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  publicly_accessible    = false
  # Same AZ as the bastion (and later AgentCore), so no cross-AZ hop is mixed
  # into the search latency.
  availability_zone = aws_subnet.private[0].availability_zone

  # Created for a session and destroyed after it.
  backup_retention_period = 0
  skip_final_snapshot     = true
  apply_immediately       = true
}
