"""Per-run telemetry scope.

The metering chokepoints (llm.py, mcp.py) need to know which session / agent /
user a call belongs to, without every call site passing it explicitly. We stash
that on a contextvar that AgentContext sets when an agent starts running; because
each agent runs in its own async task, the value propagates correctly to the
LLM/tool calls made inside that task.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass


@dataclass(frozen=True)
class Scope:
    session_id: str = ""
    agent_id: str = ""
    user: str = ""


_current: contextvars.ContextVar[Scope] = contextvars.ContextVar("obs_scope", default=Scope())


def set_scope(session_id: str, agent_id: str, user: str = "") -> None:
    _current.set(Scope(session_id=session_id or "", agent_id=agent_id or "", user=user or ""))


def get_scope() -> Scope:
    return _current.get()
