"""Where a conversation is kept between questions, beyond the agent's process.

On AgentCore, a session is one agent process while it lives. After 15 minutes
unused the process is gone, and with it a conversation kept only in memory;
the next question in the same session lands on a new process. With MEMORY_ID
set, each message is also written to AgentCore Memory's short-term memory
(the events of one session) and read back when a process meets a session it
doesn't have. This is what Strands Agents' AgentCoreMemorySessionManager does:
read once when the agent for a session is made, write each message as it is
added (docs/DESIGN.md §1).

Events are keyed by actor and session. The session is the Runtime's session
ID, so a caller that keeps sending the same one gets the same conversation
whichever process answers.
"""
from __future__ import annotations
import asyncio, json
from datetime import datetime, timezone
from typing import Any

from opentelemetry import trace

from tracing.instrumentation import aws_sdk_span

# Who the conversation belongs to. A chat app passes the signed-in user here;
# this project has no users, and the session alone tells conversations apart.
ACTOR_ID = "user"
# Not part of any convention: how many events a ListEvents read returned.
MEMORY_EVENTS = "agent.memory.events"

_SERVICE = "BedrockAgentCore"
_ROLES = {"user": "USER", "assistant": "ASSISTANT", "tool": "TOOL"}
# The order ListEvents returns events in is not documented, and two messages
# can be written within the same timestamp, so each event carries its place.
_SEQ = "seq"


class AgentCoreMemory:
    """Short-term memory in AgentCore Memory: one event per message, its text
    the message as JSON (tool calls and results included, as in the agent's
    own history).

    The client is made at start-up and its calls run in a thread, as the MCP
    server's Titan client (system/mcp_server/server.py): making it reads
    botocore's service model, and its calls are synchronous."""

    def __init__(self, memory_id: str, tracer: trace.Tracer) -> None:
        import botocore.session  # needs the "aws" extra
        from botocore.config import Config

        self._client = botocore.session.get_session().create_client("bedrock-agentcore", config=Config(retries={"mode": "standard"}))
        self._memory_id = memory_id
        self._tracer = tracer
        self._next_seq: dict[str, int] = {}

    async def load(self, conversation_id: str) -> list[dict[str, Any]]:
        """Every message of the conversation, oldest first ([] for a new one)."""
        with aws_sdk_span(self._tracer, service=_SERVICE, operation="ListEvents", region=self._region) as span:
            events, retries = await asyncio.to_thread(self._list, conversation_id)
            span.set_attribute(MEMORY_EVENTS, len(events))
            # botocore retries throttling and dropped connections on its own.
            span.set_attribute("botocore.retry_attempts", retries)
        events.sort(key=lambda e: int(e["metadata"][_SEQ]["stringValue"]))
        self._next_seq[conversation_id] = len(events)
        return [json.loads(e["payload"][0]["conversational"]["content"]["text"]) for e in events]

    async def append(self, conversation_id: str, message: dict[str, Any]) -> None:
        seq = self._next_seq.get(conversation_id, 0)
        self._next_seq[conversation_id] = seq + 1
        with aws_sdk_span(self._tracer, service=_SERVICE, operation="CreateEvent", region=self._region) as span:
            retries = await asyncio.to_thread(self._create, conversation_id, message, seq)
            span.set_attribute("botocore.retry_attempts", retries)

    @property
    def _region(self) -> str:
        return self._client.meta.region_name

    def _list(self, conversation_id: str) -> tuple[list[dict[str, Any]], int]:
        events: list[dict[str, Any]] = []
        retries = 0
        kwargs: dict[str, Any] = {
            "memoryId": self._memory_id, "actorId": ACTOR_ID, "sessionId": conversation_id, "maxResults": 100,
        }
        while True:
            resp = self._client.list_events(**kwargs)
            events.extend(resp["events"])
            retries += resp["ResponseMetadata"]["RetryAttempts"]
            if not resp.get("nextToken"):
                return events, retries
            kwargs["nextToken"] = resp["nextToken"]

    def _create(self, conversation_id: str, message: dict[str, Any], seq: int) -> int:
        resp = self._client.create_event(
            memoryId=self._memory_id,
            actorId=ACTOR_ID,
            sessionId=conversation_id,
            eventTimestamp=datetime.now(timezone.utc),
            payload=[{
                "conversational": {
                    "role": _ROLES.get(message["role"], "OTHER"),
                    "content": {"text": json.dumps(message, ensure_ascii=False)},
                },
            }],
            metadata={_SEQ: {"stringValue": str(seq)}},
        )
        return resp["ResponseMetadata"]["RetryAttempts"]
