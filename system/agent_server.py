"""The agent as an HTTP service: `python -m system.agent_server` -> http://127.0.0.1:8080

For AgentCore Runtime's HTTP protocol: port 8080, GET /ping, POST /invocations.
Each invocation takes {"question": "..."} and runs the agent once. It joins the
caller's trace when the request carries a traceparent, so the platform's span
and the agent's sit in one trace.

Environment variables:
  LLM_BASE_URL       OpenAI-compatible server (default: the local llama.cpp)
  LLM_MODEL          model name to request
  LLM_PROVIDER       goes on the chat span as gen_ai.provider.name
  MCP_SERVER_URL     MCP server (default http://127.0.0.1:8000/mcp)
  TRACE_TO, TRACE_DB where the spans go (tracing.exporter.exporters_from_env)
  AGENT_SERVER_HOST  address to listen on (default "127.0.0.1"; the container sets "0.0.0.0")
  PITFALLS           cases switched on in the agent (system/pitfalls.py)
"""
from __future__ import annotations
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from opentelemetry import context, propagate
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from system import pitfalls
from system.agent import Agent
from system.llm.client import DEFAULT_BASE_URL, DEFAULT_MODEL, DEFAULT_PROVIDER_NAME, LlmClient
from system.mcp_client import McpToolClient
from tracing.exporter import exporters_from_env
from tracing.instrumentation import build_tracer_provider, get_tracer, set_global_tracer_provider

HOST = os.environ.get("AGENT_SERVER_HOST", "127.0.0.1")
PORT = 8080  # AgentCore Runtime expects HTTP agents on 8080
SESSION_HEADER = "x-amzn-bedrock-agentcore-runtime-session-id"


async def ping(request: Request) -> JSONResponse:
    return JSONResponse({"status": "Healthy"})


async def invocations(request: Request) -> JSONResponse:
    try:
        question = (await request.json())["question"]
    except (ValueError, KeyError, TypeError):
        return JSONResponse({"error": 'expected a JSON body with "question"'}, status_code=400)
    token = context.attach(propagate.extract(request.headers))
    try:
        result = await request.app.state.agent.run(question, conversation_id=request.headers.get(SESSION_HEADER))
    finally:
        context.detach(token)
    return JSONResponse({
        "answer": result.answer,
        "turns": result.turns,
        "stop_reason": result.stop_reason,
        "trace_id": result.trace_id,
    })


@asynccontextmanager
async def lifespan(app: Starlette) -> AsyncIterator[None]:
    provider = build_tracer_provider(
        *exporters_from_env(), service_name="agent", batch=True, resource_attributes=pitfalls.resource_attributes()
    )
    set_global_tracer_provider(provider)
    tracer = get_tracer(provider)
    llm = LlmClient(
        tracer,
        base_url=os.environ.get("LLM_BASE_URL", DEFAULT_BASE_URL),
        model=os.environ.get("LLM_MODEL", DEFAULT_MODEL),
        provider_name=os.environ.get("LLM_PROVIDER", DEFAULT_PROVIDER_NAME),
    )
    server_url = os.environ.get("MCP_SERVER_URL", "http://127.0.0.1:8000/mcp")
    # One connection for the life of the process, as in the baseline (system/agent.py).
    async with llm, McpToolClient(server_url, tracer, agent_name="agent") as mcp:
        app.state.agent = Agent(llm, mcp, tracer, sequential_tools="1" in pitfalls.enabled())
        yield
    provider.shutdown()  # write out spans still queued in the batch processor


app = Starlette(
    routes=[Route("/ping", ping), Route("/invocations", invocations, methods=["POST"])],
    lifespan=lifespan,
)


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)
