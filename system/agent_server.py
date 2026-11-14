"""The agent as an HTTP service: `python -m system.agent_server` -> http://127.0.0.1:8080

For AgentCore Runtime's HTTP protocol: port 8080, GET /ping, POST /invocations.
Each invocation takes {"question": "..."} and runs the agent once. It joins the
caller's trace when the request carries a traceparent, so the platform's span
and the agent's sit in one trace.

With `Accept: text/event-stream` the answer comes back as server-sent events
while the LLM writes it, as a chat that streams shows it: {"text": "..."} for
each piece, then one {"answer", "turns", "stop_reason", "trace_id"} event (or
{"error": "..."}). Without it, the reply is one JSON object at the end.

Environment variables:
  LLM_BASE_URL       OpenAI-compatible server (default: the local llama.cpp)
  LLM_MODEL          model name to request
  LLM_PROVIDER       goes on the chat span as gen_ai.provider.name
  MCP_SERVER_URL     MCP server (default http://127.0.0.1:8000/mcp)
  MEMORY_ID          AgentCore Memory to keep conversations in (system/memory.py);
                     unset, a conversation lives in the process only
  TRACE_TO           where the spans go (tracing.exporter.exporters_from_env)
  AGENT_SERVER_HOST  address to listen on (default "127.0.0.1"; the container sets "0.0.0.0")
  PITFALLS           cases switched on in the agent (system/pitfalls.py)
"""
from __future__ import annotations
import asyncio, json, os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import uvicorn
from opentelemetry import context, propagate
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from system import pitfalls
from system.agent import Agent, AgentResult
from system.llm.client import DEFAULT_BASE_URL, DEFAULT_MODEL, DEFAULT_PROVIDER_NAME, LlmClient
from system.mcp_client import McpToolClient
from system.memory import AgentCoreMemory
from tracing.exporter import exporters_from_env
from tracing.instrumentation import build_tracer_provider, get_tracer, set_global_tracer_provider

HOST = os.environ.get("AGENT_SERVER_HOST", "127.0.0.1")
PORT = 8080  # AgentCore Runtime expects HTTP agents on 8080
SESSION_HEADER = "x-amzn-bedrock-agentcore-runtime-session-id"
# How long a streamed answer may go without sending anything. Common proxy
# idle timeouts are a minute or more (nginx's proxy_read_timeout: 60 s).
PING_INTERVAL_S = 15.0
# How long to let running requests finish once told to stop (SIGTERM). The
# runtime says so when a session is stopped (StopRuntimeSession, sent when the
# user leaves) or has gone unused for 15 minutes: either the caller is gone or
# nothing is running, so none is worth waiting for. Without a limit uvicorn
# waits for every request to end, and a stopped session's agent went on to
# the end of its answer (2026-11-11). Past the limit uvicorn cancels them,
# which stops the agent as a closed stream does (_events).
SHUTDOWN_GRACE_S = 0


async def ping(request: Request) -> JSONResponse:
    return JSONResponse({"status": "Healthy"})


def _summary(result: AgentResult) -> dict:
    return {
        "answer": result.answer,
        "turns": result.turns,
        "stop_reason": result.stop_reason,
        "trace_id": result.trace_id,
    }


def _event(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def invocations(request: Request) -> JSONResponse | StreamingResponse:
    try:
        question = (await request.json())["question"]
    except (ValueError, KeyError, TypeError):
        return JSONResponse({"error": 'expected a JSON body with "question"'}, status_code=400)
    agent = request.app.state.agent
    caller = propagate.extract(request.headers)
    conversation_id = request.headers.get(SESSION_HEADER)

    async def run(on_text=None):
        # Attached inside the task that runs the agent, so the spans join the
        # caller's trace however the response is sent.
        token = context.attach(caller)
        try:
            return await agent.run(question, conversation_id=conversation_id, on_text=on_text)
        finally:
            context.detach(token)

    if "text/event-stream" not in request.headers.get("accept", ""):
        return JSONResponse(_summary(await run()))
    return StreamingResponse(_events(run), media_type="text/event-stream")


async def _events(run: Callable[[Callable[[str], None]], Awaitable[AgentResult]]) -> AsyncIterator[str]:
    """The agent's text as it comes, then the summary (or the error). The
    status (200) is sent before the agent finishes, so a failure can only go in
    the stream; the agent's own span records it."""
    pieces: asyncio.Queue[str] = asyncio.Queue()
    task = asyncio.create_task(run(pieces.put_nowait))
    try:
        while not (task.done() and pieces.empty()):
            getter = asyncio.ensure_future(pieces.get())
            await asyncio.wait({getter, task}, timeout=PING_INTERVAL_S, return_when=asyncio.FIRST_COMPLETED)
            if getter.done():
                yield _event({"text": getter.result()})
            else:
                getter.cancel()
                if not task.done():
                    # Nothing to send while tools run (tens of seconds): a
                    # comment line keeps proxies from closing an idle stream.
                    yield ": ping\n\n"
        try:
            yield _event(_summary(task.result()))
        except Exception as e:
            yield _event({"error": f"{type(e).__name__}: {e}"})
    finally:
        # The caller went away: stop the agent rather than leave it using
        # the GPU for nobody (as the duplicated question did, docs/runbook.md).
        task.cancel()


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
    memory_id = os.environ.get("MEMORY_ID")
    memory = AgentCoreMemory(memory_id, tracer) if memory_id else None
    # One connection for the life of the process, as in the baseline (system/agent.py).
    async with llm, McpToolClient(server_url, tracer, agent_name="agent") as mcp:
        enabled = pitfalls.enabled()
        app.state.agent = Agent(
            llm, mcp, tracer, sequential_tools="1" in enabled, time_in_prompt="10" in enabled, memory=memory
        )
        yield
    provider.shutdown()  # write out spans still queued in the batch processor


app = Starlette(
    routes=[Route("/ping", ping), Route("/invocations", invocations, methods=["POST"])],
    lifespan=lifespan,
)


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, timeout_graceful_shutdown=SHUTDOWN_GRACE_S)
