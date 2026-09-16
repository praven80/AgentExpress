"""Ask the model for structured JSON, check it, and repair it once.

This is the ONE seam every agent's model output crosses. There are five places
where a payload becomes a validated asset (four contracts plus the shared
research runner), but only three where a payload is *obtained*:

    app/common/research.py    -> the four research agents
    app/common/synthesis.py   -> analysis, recommendation, report
    app/subagents/intake/     -> intake

All three now call `ask_json`, so a rule added in `app/common/rules.py` takes
effect for all eight agents at once. That matters more than it sounds: three of
the eight run in their own AgentCore runtimes (`runtime: "dedicated"` in
workflow.json), where the orchestrator process never sees their prompt or their
evidence. A check bolted onto the node wrapper could not re-ask those three. This
runs inside whichever container the agent runs in, so it covers all of them.

THE REPAIR IS BOUNDED AT ONE RETRY
----------------------------------
Deliberately. An unbounded loop against a sampler can spend a whole token budget
converging on nothing, and the framework's existing convention for "I could not
establish this" is to emit a VALID asset that names its own shortfall rather than
to fail or to retry forever. So: check, re-ask once with the concrete violations,
keep whichever answer is better, and hand back what is still wrong so the caller
can record it on the asset (`AssetEnvelope.ruleViolations`). A reviewer reading a
report with a visible "this broke rule X" note is better served than one reading
a silently-wrong report, and far better served than one reading no report.

Nothing here fabricates or drops content. If the model is unavailable, `ctx.llm`
raises `ModelUnavailable` exactly as before.
"""

from __future__ import annotations

from app.common import assets, rules


async def ask_json(ctx, system_prompt: str, user: str, *,
                   rule_set=rules.SYNTHESIS, upstream: str = "",
                   extra_counts: dict[str, int] | None = None,
                   max_tokens: int | None = None,
                   name: str | None = None) -> tuple[dict, list[str]]:
    """Return (payload, unrepaired) for one structured model call.

    `upstream` is the text the model was grounded in — the evidence block for a
    research agent, the upstream assets for a synthesis agent, the raw request
    for intake. The grounding rules test the output against it, so passing the
    wrong thing here makes those rules meaningless; pass exactly what the model
    was shown.

    `unrepaired` is a list of human-readable violation strings, empty on the
    happy path. `payload` is `{}` only when the model returned nothing parseable,
    which each caller already degrades on.
    """
    text = await ctx.llm(system_prompt, user, max_tokens=max_tokens, name=name)
    payload = assets.extract_json(text) or {}
    if not payload:
        return {}, []

    found = rules.check(payload, upstream=upstream, rules=rule_set,
                        extra_counts=extra_counts)
    if not found:
        return payload, []

    await _log(ctx, f"output rules: {len(found)} violation(s); re-asking once "
                    f"({', '.join(sorted({v.rule for v in found}))})")

    repaired_text = await ctx.llm(
        system_prompt, user + rules.as_repair_instructions(found),
        max_tokens=max_tokens, name=f"{name or ctx.agent_id}-repair")
    repaired = assets.extract_json(repaired_text) or {}
    if not repaired:
        # The retry produced nothing usable. Keep the first answer, which at least
        # has substance, and report what is wrong with it.
        return payload, [str(v) for v in found]

    still = rules.check(repaired, upstream=upstream, rules=rule_set,
                        extra_counts=extra_counts)
    if len(still) >= len(found):
        # No improvement. Prefer the original rather than a rewrite that lost
        # content without fixing anything.
        await _log(ctx, "output rules: re-ask did not improve; keeping the "
                        "first answer and recording the violations")
        return payload, [str(v) for v in found]

    if still:
        await _log(ctx, f"output rules: {len(found) - len(still)} fixed, "
                        f"{len(still)} recorded on the asset")
    return repaired, [str(v) for v in still]


async def _log(ctx, message: str) -> None:
    """Timeline line, best-effort — a rule check must never break a run."""
    try:
        await ctx.log(message)
    except Exception:  # noqa: BLE001,S110 - observability is not the job here
        pass
