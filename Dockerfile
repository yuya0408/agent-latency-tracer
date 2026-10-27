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

ENV MCP_SERVER_HOST=0.0.0.0
EXPOSE 8000
CMD ["python", "-m", "system.mcp_server"]
