"""LLM helper. Calls Claude on Amazon Bedrock when credentials are available,
falling back to a simulated response so the workflow always runs locally.

The model, temperature, and max_tokens are per-call, so each agent can use a
different model (configured in workflow.json).
"""

from app.common.config import MODEL_ID, REGION


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
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                # only real text blocks (skip reasoning/tool_use/etc.)
                if block.get("type", "text") == "text":
                    parts.append(block["text"])
        if parts:
            return "".join(parts)
    return content if isinstance(content, str) else str(content)


async def run_llm(name: str, system: str, user: str,
                  model: str | None = None, temperature: float = 0,
                  max_tokens: int = 300) -> str:
    model = model or MODEL_ID
    try:
        from langchain_aws import ChatBedrockConverse

        llm = ChatBedrockConverse(model=model, region_name=REGION,
                                  temperature=temperature, max_tokens=max_tokens)
        import time as _t
        _start = _t.perf_counter()
        msg = await llm.ainvoke([("system", system), ("human", user)])
        _latency_ms = int((_t.perf_counter() - _start) * 1000)
        _meter_llm(model, msg, system, _latency_ms, mode="bedrock")
        return _text_of(msg.content)
    except Exception as e:  # noqa: BLE001 - demo fallback
        _meter_llm(model, None, system, 0, mode="simulated")
        return f"[simulated {name} via {model}] ({type(e).__name__}) {system.split('.')[0]}. Input: {user[:80]}"


def _meter_llm(model, msg, system, latency_ms, mode) -> None:
    """Best-effort observability hook (isolated in app/observability)."""
    try:
        usage = getattr(msg, "usage_metadata", None) or {} if msg is not None else {}
        from app.observability import meter
        meter.record_llm(
            model=model,
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            system_text=system, latency_ms=latency_ms, mode=mode,
        )
    except Exception:  # noqa: BLE001 - metering must never break a call
        pass
