# RDS for PostgreSQL with pgvector, reachable only from inside the VPC: from
# the bastion (loading, through the tunnel) and the MCP server on AgentCore.

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

# Log each connection and disconnection, to see from the database's side when
# and why the MCP server's pooled connections end (on 2026-11-05 they were
# found dead after 4 minutes unused). Read with the RDS console or
# `aws rds download-db-log-file-portion`; not exported to CloudWatch.
resource "aws_db_parameter_group" "main" {
  name   = "agent-latency-tracer"
  family = "postgres18"

  # PostgreSQL 18 takes a list here; "all" also logs how long each connection
  # took to set up (setup_durations).
  parameter {
    name  = "log_connections"
    value = "all"
  }
  parameter {
    name  = "log_disconnections"
    value = "1"
  }
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
  # The MCP server logs in with a token signed by its role instead of a
  # password (a DB user granted rds_iam; scripts/create_mcp_server_user.sql).
  iam_database_authentication_enabled = true

  parameter_group_name   = aws_db_parameter_group.main.name
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
