"""Which pitfalls (the case numbers in README's case list) are switched on.

Set with the environment variable PITFALLS, case numbers separated by commas
(e.g. PITFALLS=3). Each process reads its own environment: a pitfall on the
MCP server's side is switched on in the server's, one in the agent's loop in
the agent's. Unset or empty means the baseline, with every fix in place.

Only the cases that have been built are accepted, so a typo or a case that
does nothing yet fails at startup instead of producing a baseline trace that
looks like a reproduction.
"""
from __future__ import annotations
import os

ENV_VAR = "PITFALLS"
# Resource attribute recording which pitfalls a process ran with, so an
# exported trace says by itself what it is a trace of.
RESOURCE_ATTRIBUTE = "scenario.pitfalls"

# Case number -> the process that reads it.
#   1  agent: tool calls from one response are awaited one by one (system/agent.py)
#   2  MCP server: business tools wait with time.sleep inside async code (system/tools/mock_server.py)
#   3  MCP server: business tools answer without records (system/tools/mock_server.py)
#   10 agent: every LLM call starts with the current time to the second (system/agent.py)
AVAILABLE: frozenset[str] = frozenset({"1", "2", "3", "10"})


def enabled() -> frozenset[str]:
    value = os.environ.get(ENV_VAR, "")
    names = frozenset(n.strip() for n in value.split(",") if n.strip())
    unknown = names - AVAILABLE
    if unknown:
        raise ValueError(
            f"{ENV_VAR}: unknown or not yet built: {', '.join(sorted(unknown))}. "
            f"Available: {', '.join(sorted(AVAILABLE)) or 'none'}"
        )
    return names


def resource_attributes() -> dict[str, str]:
    """For build_tracer_provider: "none" for the baseline, otherwise the
    case numbers in sorted order (e.g. "3" or "1,3")."""
    return {RESOURCE_ATTRIBUTE: ",".join(sorted(enabled())) or "none"}
