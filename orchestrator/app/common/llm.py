"""LLM helper. Calls Claude on Amazon Bedrock.

A failed model call RAISES ModelUnavailable — it is never substituted with
placeholder text. See app/common/errors.py for why: fabricated output is
indistinguishable from real evidence once it reaches the report.

The model, temperature, and max_tokens are per-call, so each agent can use a
different model (configured in workflow.json).

`name` names the model call. An agent may issue several distinct prompts; the
name is carried onto the telemetry row and the captured prompt so observability
and AgentCore Evaluations can scope to ONE prompt at a time.
"""

import contextlib

from app.common.config import MODEL_ID, REGION
from app.common.errors import ModelUnavailable


def _text_of(content) -> str:
    """Normalise an AIMessage.content to plain text. ChatBedrockConverse (the
    Bedrock Converse API) returns a LIST of typed content blocks
    (e.g. [{"type": "text", "text": "..."}], plus optional reasoning/tool blocks),
    not a string. Concatenate the text blocks so downstream JSON parsing works;
    a naive str(list) would yield a Python repr that can't be parsed."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            # only real text blocks (skip reasoning/tool_use/etc.)
            elif (isinstance(block, dict) and isinstance(block.get("text"), str)
                  and block.get("type", "text") == "text"):
                parts.append(block["text"])
        if parts:
            return "".join(parts)
    return content if isinstance(content, str) else str(content)


async def run_llm(name: str, system: str, user: str,
                  model: str | None = None, temperature: float = 0,
                  max_tokens: int = 300) -> tuple[str, bool]:
    """Call the model and return (text, hit_token_ceiling).

    The second value is the one that is easy to lose and expensive to lose. When a
    response stops because it reached `max_tokens` rather than because the model
    finished, the text is a PREFIX — for a structured agent that means JSON cut off
    mid-object. `assets.extract_json` deliberately repairs that into a usable partial
    asset rather than failing the run, which is the right trade, but it means the only
    remaining evidence that anything was lost is this flag. Observed live: an
    `analysis` call stopped at exactly its 6000-token budget, mid-way through writing
    a `sources` entry; the asset validated, the timeline said "Analysis complete", and
    a reviewer approved it at the gate with no way to know the tail was missing.
    """
    model = model or MODEL_ID
    # Capture this call's prompt (system + the real source inputs) so the agent's
    # AGENT span carries it as gen_ai.task.input for AgentCore Evaluations, and so
    # per-prompt evaluation can find it. No-op outside an agent run.
    with contextlib.suppress(Exception):
        from app.features.observability import otel as _otel
        _otel.capture_prompt(name, system, user)
    try:
        from langchain_aws import ChatBedrockConverse

        llm = ChatBedrockConverse(model=model, region_name=REGION,
                                  temperature=temperature, max_tokens=max_tokens)
        import time as _t
        _start = _t.perf_counter()
        msg = await llm.ainvoke([("system", system), ("human", user)])
        _latency_ms = int((_t.perf_counter() - _start) * 1000)
        out_text = _text_of(msg.content)
        _meter_llm(model, msg, system, user, out_text, _latency_ms, mode="bedrock",
                   temperature=temperature, max_tokens=max_tokens, name=name)
        with contextlib.suppress(Exception):
            from app.features.observability import otel as _otel
            _otel.capture_output(out_text)  # pair this call's response with its prompt
        return out_text, _finish_reason_of(msg).lower() in _TRUNCATED_REASONS
    except Exception as e:
        # Record the failure, then fail the run — see the module docstring: a model
        # call that did not happen must never look like one that returned nothing.
        _meter_llm(model, None, system, user, "", 0, mode="error",
                   temperature=temperature, max_tokens=max_tokens, name=name)
        raise ModelUnavailable(
            f"Bedrock model call '{name}' failed on {model}: {type(e).__name__}: {e}. "
            f"Check that the region has model access enabled for this model id and that "
            f"the runtime's credentials permit bedrock:InvokeModel."
        ) from e


# Stop reasons that mean "I ran out of room", not "I finished". Bedrock Converse says
# `max_tokens`; the two spellings cover the other providers langchain-aws fronts, so a
# model swap in workflow.json does not quietly turn this check off.
_TRUNCATED_REASONS = frozenset({"max_tokens", "max_token", "length"})


def _finish_reason_of(msg) -> str:
    """Best-effort stop reason from a ChatBedrockConverse response."""
    if msg is None:
        return ""
    meta = getattr(msg, "response_metadata", None) or {}
    return str(meta.get("stopReason") or meta.get("finish_reason") or "")


def _meter_llm(model, msg, system, user, output_text, latency_ms, mode,
               temperature: float = 0.0, max_tokens: int = 0, name: str = "") -> None:
    """Best-effort observability hook (isolated in app/features/observability)."""
    with contextlib.suppress(Exception):  # metering must never break a call
        usage = getattr(msg, "usage_metadata", None) or {} if msg is not None else {}
        input_tokens = int(usage.get("input_tokens", 0) or 0)
        output_tokens = int(usage.get("output_tokens", 0) or 0)
        # Stamp real token usage onto the OTEL agent span (ADOT's auto "chat"
        # span reports 0 for Converse as of 0.19.0). Accumulates across the
        # agent's calls; surfaces as gen_ai.usage.* in GenAI Observability.
        from app.features.observability import otel
        otel.add_tokens(input_tokens, output_tokens)
        from app.features.observability import meter
        meter.record_llm(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            system_text=system, latency_ms=latency_ms, mode=mode,
            user_input=user, output_text=output_text,
            temperature=temperature, max_tokens=max_tokens,
            finish_reason=_finish_reason_of(msg), prompt=name,
        )
