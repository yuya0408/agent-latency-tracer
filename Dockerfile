# The MCP server's image for AgentCore Runtime, which runs linux/arm64
# containers and sends MCP requests to port 8000, path /mcp.
#   docker buildx build --platform linux/arm64 -t agent-latency-tracer/mcp-server .
FROM python:3.14-slim

WORKDIR /app
COPY pyproject.toml ./
COPY system/ system/
COPY tracing/ tracing/
# rds: pgvector search (stage 5). aws: sending spans to CloudWatch.
RUN pip install --no-cache-dir ".[rds,aws]"
# RDS's CA certificates, for PGSSLMODE=verify-full.
ADD https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem /app/rds-ca.pem

ENV MCP_SERVER_HOST=0.0.0.0 \
    TRACE_TO=cloudwatch
EXPOSE 8000
# Run as an unprivileged user, not root. Checked with local Docker; the runs
# on AgentCore Runtime used the earlier image, which ran as root.
RUN useradd --create-home app
USER app
CMD ["python", "-m", "system.mcp_server"]
