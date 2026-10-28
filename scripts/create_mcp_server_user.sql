-- The MCP server's DB user on RDS: logs in with an IAM token (rds_iam, no
-- password) and may only read the documents table. Run as the master user
-- after scripts/load_pgvector.py (it recreates the table, dropping grants):
--   psql -f scripts/create_mcp_server_user.sql
-- The name matches local.mcp_server_db_user in infra/agentcore.tf.
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mcp_server') THEN
    CREATE ROLE mcp_server LOGIN;
  END IF;
END
$$;
GRANT rds_iam TO mcp_server;
GRANT SELECT ON documents TO mcp_server;
