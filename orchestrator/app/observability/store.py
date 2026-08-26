"""DynamoDB writer for telemetry records (best-effort).

Writes never raise — a telemetry failure must never break a workflow run. When
TELEMETRY_TABLE is unset (local dev), writes are no-ops so the flow still runs.
Reads and aggregation live in the BFF (bff/handler.py), which only sums the
pre-computed cost per row.
"""

from __future__ import annotations

import os
from decimal import Decimal

from app.observability.records import CallRecord

TELEMETRY_TABLE = os.getenv("TELEMETRY_TABLE", "")

_table = None


def _tbl():
    global _table
    if _table is None:
        import boto3
        _table = boto3.resource("dynamodb").Table(TELEMETRY_TABLE)
    return _table


def _clean(item: dict) -> dict:
    # DynamoDB rejects float; Decimal is fine. Drop empty strings on GSI keys.
    out = {}
    for k, v in item.items():
        if isinstance(v, float):
            v = Decimal(str(v))
        out[k] = v
    return out


def put(record: CallRecord) -> None:
    if not TELEMETRY_TABLE:
        return
    try:
        _tbl().put_item(Item=_clean(record.to_item()))
    except Exception as e:  # noqa: BLE001 - telemetry must never break a run
        print(f"[observability] telemetry write failed: {type(e).__name__}: {e}")
