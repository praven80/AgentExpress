"""Price book + cost math — the ONE place to edit rates.

All observability cost figures flow through here. Prices are AWS public list
prices for us-east-1 as of 2026-08-20 (sources: the Amazon Bedrock pricing page
and the Amazon Bedrock AgentCore pricing page). They are ESTIMATES for planning —
they will not match your AWS bill to the cent (region, private pricing, batch/
prompt-caching discounts, and reconciliation all differ). Update the constants
below when prices change or when you have negotiated rates.

Everything returns a Decimal USD amount so the sums are exact.
"""

from __future__ import annotations

from decimal import Decimal

PRICES_AS_OF = "2026-08-20"
PRICES_REGION = "us-east-1"

# --- Bedrock foundation models: USD per 1,000,000 tokens (input, output) ------
# Keyed by a substring matched against the model id. First match wins, so keep
# more specific ids above generic ones.
_MODEL_PER_MTOK: list[tuple[str, Decimal, Decimal]] = [
    ("claude-haiku-4-5",   Decimal("1.00"),  Decimal("5.00")),
    ("claude-sonnet-4-5",  Decimal("3.00"),  Decimal("15.00")),
    ("claude-sonnet-5",    Decimal("2.00"),  Decimal("10.00")),
    ("claude-opus-4",      Decimal("15.00"), Decimal("75.00")),
    ("claude-3-5-haiku",   Decimal("0.80"),  Decimal("4.00")),
    ("claude-3-5-sonnet",  Decimal("3.00"),  Decimal("15.00")),
]
_MODEL_FALLBACK = (Decimal("1.00"), Decimal("5.00"))  # unknown model -> haiku-ish

# Titan Text Embeddings V2 — used by the KB retrieve to embed each query.
_EMBED_PER_MTOK = Decimal("0.02")

# --- AgentCore (from the AgentCore pricing page) ------------------------------
RUNTIME_VCPU_HOUR = Decimal("0.0895")     # per vCPU-hour
RUNTIME_GB_HOUR = Decimal("0.00945")      # per GB-hour
GATEWAY_PER_1K_INVOCATIONS = Decimal("0.005")   # InvokeTool / ListTools / Ping

# A single ctx.mcp()/ctx.retrieve() call goes through the Gateway. We count it as
# one ListTools + one InvokeTool for a conservative estimate.
_GATEWAY_INVOCATIONS_PER_TOOL_CALL = 2

# --- AgentCore Runtime compute footprint (for the per-session estimate) -------
# Billing is by actual vCPU/GB-hours consumed (the vended usage logs). Until those
# logs are enabled, we estimate compute cost from ACTIVE runtime seconds (not
# wall-clock — AgentCore doesn't bill while paused at a HITL gate) times this
# assumed microVM footprint. Edit to match your observed allocation.
RUNTIME_ASSUMED_VCPU = Decimal("1.0")
RUNTIME_ASSUMED_GB = Decimal("2.0")

_MTOK = Decimal("1000000")


def _model_rates(model_id: str) -> tuple[Decimal, Decimal]:
    mid = (model_id or "").lower()
    for key, in_rate, out_rate in _MODEL_PER_MTOK:
        if key in mid:
            return in_rate, out_rate
    return _MODEL_FALLBACK


def model_rates(model_id: str) -> tuple[Decimal, Decimal]:
    """(input, output) USD per 1,000,000 tokens for a model — for display."""
    return _model_rates(model_id)


def model_cost(model_id: str, input_tokens: int, output_tokens: int) -> Decimal:
    """USD for one model call from its token usage."""
    in_rate, out_rate = _model_rates(model_id)
    return (Decimal(max(0, input_tokens)) / _MTOK * in_rate
            + Decimal(max(0, output_tokens)) / _MTOK * out_rate)


def embedding_cost(tokens: int) -> Decimal:
    return Decimal(max(0, tokens)) / _MTOK * _EMBED_PER_MTOK


def gateway_tool_cost() -> Decimal:
    """USD for the Gateway invocations behind one tool/KB call."""
    return (Decimal(_GATEWAY_INVOCATIONS_PER_TOOL_CALL) / Decimal(1000)
            * GATEWAY_PER_1K_INVOCATIONS)


def runtime_compute_cost(vcpu_hours: Decimal | float, gb_hours: Decimal | float) -> Decimal:
    """USD for AgentCore Runtime compute consumed by a session (from the vended
    CPU/GB-hour usage logs)."""
    return (Decimal(str(vcpu_hours)) * RUNTIME_VCPU_HOUR
            + Decimal(str(gb_hours)) * RUNTIME_GB_HOUR)


def compute_cost_for_seconds(active_seconds: float | int) -> Decimal:
    """Estimated AgentCore Runtime compute cost for a burst of active seconds,
    using the assumed microVM footprint above."""
    hours = Decimal(str(max(0, active_seconds))) / Decimal(3600)
    return runtime_compute_cost(hours * RUNTIME_ASSUMED_VCPU, hours * RUNTIME_ASSUMED_GB)
