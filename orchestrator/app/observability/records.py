"""Telemetry record schema — one row per model call and per tool call.

Stored in the telemetry DynamoDB table (see store.py). PK = session_id,
SK = "ts#seq" so calls sort chronologically within a session. GSIs (by date /
model / user) power the aggregation views.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from decimal import Decimal

from app.common import clock


@dataclass
class CallRecord:
    session_id: str
    agent_id: str
    user: str
    kind: str                      # "llm" | "tool"
    # model (for llm) or provider label (for tool: knowledge / kb)
    label: str
    mode: str                      # "bedrock" | "gateway" | "simulated"
    input_tokens: int = 0          # exact model tokens (from Bedrock usage)
    output_tokens: int = 0         # exact model tokens
    system_tokens: int = 0         # system prompt slice of input (exact via CountTokens, else estimated)
    system_tokens_exact: bool = True   # False when CountTokens was unavailable and we estimated
    embed_tokens_est: int = 0      # estimate: KB query embedding tokens (tool rows)
    latency_ms: int = 0
    cost_usd: Decimal = Decimal("0")
    # Per-1M-token rates that priced this row (llm only), for UI display.
    in_rate: Decimal = Decimal("0")
    out_rate: Decimal = Decimal("0")
    # Eastern-Time 'YYYY-MM-DD HH:MM:SS'; sorts lexicographically = chronologically.
    ts: str = field(default_factory=clock.now_str)
    date: str = field(default_factory=clock.today_str)  # ET date, GSI hash key
    seq: int = 0

    def key(self) -> str:
        # unique, chronological sort key within the session (ts is fixed-width ET)
        return f"{self.ts}#{self.seq:04d}#{uuid.uuid4().hex[:6]}"

    def to_item(self) -> dict:
        item = asdict(self)
        item["sk"] = self.key()
        return item
