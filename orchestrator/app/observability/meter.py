"""Metering API — the only surface the rest of the app calls.

Two chokepoints call in:
  * app/common/llm.py  -> record_llm(...)   after each Bedrock call
  * app/common/mcp.py  -> record_tool(...)  after each Gateway/KB call

Everything is best-effort and swallows its own errors: capturing telemetry must
never change the behaviour or success of a workflow run.
"""

from __future__ import annotations

import math
import time
from contextlib import contextmanager

from app.observability import pricing, store
from app.observability.records import CallRecord
from app.observability.scope import get_scope

# monotonically increasing per-process sequence for stable ordering within a ms
_seq = 0


def _next_seq() -> int:
    global _seq
    _seq = (_seq + 1) % 10000
    return _seq


@contextmanager
def timed():
    """Yield a callable returning elapsed milliseconds."""
    start = time.perf_counter()
    yield lambda: int((time.perf_counter() - start) * 1000)


def est_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token) for the KB query embedding, which
    Bedrock does not report separately, and as a fallback for the system-prompt
    slice when the exact CountTokens call is unavailable."""
    return math.ceil(len(text or "") / 4)


def _system_tokens(model: str, system_text: str, mode: str) -> tuple[int, bool]:
    """Exact system-prompt tokens via Bedrock CountTokens (cached), falling back
    to the ~4-chars/token estimate. Returns (tokens, is_exact)."""
    if mode == "simulated" or not system_text:
        return est_tokens(system_text), False
    from app.observability import tokens as _tok
    exact = _tok.exact_system_tokens(model, system_text)
    if exact is None:
        return est_tokens(system_text), False
    return exact, True


def record_llm(*, model: str, input_tokens: int, output_tokens: int,
               system_text: str = "", latency_ms: int = 0, mode: str = "bedrock") -> None:
    try:
        s = get_scope()
        in_rate, out_rate = pricing.model_rates(model)
        cost = pricing.model_cost(model, input_tokens, output_tokens) if mode != "simulated" else pricing.Decimal("0")
        sys_tokens, sys_exact = _system_tokens(model, system_text, mode)
        store.put(CallRecord(
            session_id=s.session_id, agent_id=s.agent_id, user=s.user,
            kind="llm", label=model, mode=mode,
            input_tokens=int(input_tokens or 0), output_tokens=int(output_tokens or 0),
            system_tokens=sys_tokens, system_tokens_exact=sys_exact,
            latency_ms=int(latency_ms or 0), cost_usd=cost,
            in_rate=in_rate, out_rate=out_rate, seq=_next_seq(),
        ))
    except Exception as e:  # noqa: BLE001
        print(f"[observability] record_llm skipped: {type(e).__name__}: {e}")


def record_session_compute(*, session_id: str, user: str = "", active_seconds: float = 0.0) -> None:
    """Write the AgentCore Runtime compute cost for one active burst (one runtime
    invocation) of a session. Called at the end of each start/resume run."""
    try:
        from app.observability import costs
        store.put(CallRecord(
            session_id=session_id, agent_id="__session__", user=user or "",
            kind="agentcore", label="runtime-compute", mode="agentcore",
            latency_ms=int(active_seconds * 1000),
            cost_usd=costs.session_compute_cost(active_seconds), seq=_next_seq(),
        ))
    except Exception as e:  # noqa: BLE001
        print(f"[observability] record_session_compute skipped: {type(e).__name__}: {e}")


def record_tool(*, provider: str, query: str = "", latency_ms: int = 0,
                mode: str = "gateway") -> None:
    """A tool/KB call. Its Gateway invocation cost is charged here; the KB path
    also embeds the query (Titan). The RESULT tokens are billed as input tokens
    on the next model call, so they are captured by record_llm, not here."""
    try:
        s = get_scope()
        if mode == "simulated":
            cost = pricing.Decimal("0")
            embed = 0
        else:
            cost = pricing.gateway_tool_cost()
            embed = est_tokens(query) if provider == "kb" else 0
            if embed:
                cost += pricing.embedding_cost(embed)
        # embed tokens are an estimate and are NOT model tokens, so keep them out
        # of input_tokens (which stays exact) — store them in their own field.
        store.put(CallRecord(
            session_id=s.session_id, agent_id=s.agent_id, user=s.user,
            kind="tool", label=provider, mode=mode,
            input_tokens=0, embed_tokens_est=embed, latency_ms=int(latency_ms or 0),
            cost_usd=cost, seq=_next_seq(),
        ))
    except Exception as e:  # noqa: BLE001
        print(f"[observability] record_tool skipped: {type(e).__name__}: {e}")
