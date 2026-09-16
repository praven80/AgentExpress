"""Bedrock Guardrails client — ApplyGuardrail API wrapper."""

import asyncio
import os

import boto3

from app.common.config import REGION

_GUARDRAIL_ID = os.getenv("GUARDRAIL_ID", "")
_VERSION = os.getenv("GUARDRAIL_VERSION", "DRAFT")
_client = None


def _bedrock():
    global _client
    if _client is None:
        _client = boto3.client("bedrock-runtime", region_name=REGION)
    return _client


class GuardrailBlocked(Exception):
    """Raised when a guardrail blocks the content."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


async def check(text: str, source: str = "INPUT",
                guardrail_id: str = "", version: str = "") -> str:
    """Apply a guardrail to text. Returns the text unchanged if allowed, raises
    GuardrailBlocked if filtered. Uses the per-agent `guardrail_id` when given
    (from workflow.json), else the GUARDRAIL_ID env default. No-op if neither
    is set, so the feature is genuinely optional."""
    gid = guardrail_id or _GUARDRAIL_ID
    if not gid or not text:
        return text

    resp = await asyncio.to_thread(
        _bedrock().apply_guardrail,
        guardrailIdentifier=gid,
        guardrailVersion=version or _VERSION,
        source=source,
        content=[{"text": {"text": text}}],
    )

    if resp.get("action") == "GUARDRAIL_INTERVENED":
        outputs = resp.get("outputs", [])
        msg = outputs[0].get("text", "Blocked by guardrail") if outputs else "Blocked"
        raise GuardrailBlocked(msg)

    return text
