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

CHECKING IS FREE. REPAIRING IS NOT. THE CUSTOMER DECIDES.
---------------------------------------------------------
`rules.check` is pure functions over a parsed payload — no model call, no IO, no
measurable cost. The REPAIR is a second model call for every agent that failed,
and on the first live run seven of eight agents failed, so it very nearly doubled
the price and the wall-clock of a run.

That was shipped as an unconditional framework behaviour, which was wrong: it is a
cost decision, and cost decisions belong to whoever pays. Both are now config
(`orchestrator.outputRules`, overridable per agent — see
`config.output_rules_for`), and repair is OFF by default:

  enabled false           no checks at all. One call.
  enabled, repair false   check, record every violation on the asset, do not
    (the default)         re-ask. ONE call, same cost as no enforcement at all.
                          The reviewer still sees exactly what failed, in the UI,
                          above the summary — they just decide what to do about it
                          instead of the framework spending their money guessing.
  enabled, repair true    check, re-ask once with the concrete violations quoted.
                          Two calls for an agent that failed, one for one that
                          passed. Turn this on per agent, for the agents whose
                          output a reader actually consumes.

WHY THE REPAIR IS BOUNDED AT ONE RETRY WHEN IT IS ON
An unbounded loop against a sampler can spend a whole token budget converging on
nothing, and this framework's convention for "I could not establish this" is to
emit a VALID asset that names its own shortfall rather than to fail or to retry
forever. So: check, re-ask once, keep whichever answer is genuinely better, and
hand back what is still wrong so the caller records it on the asset
(`AssetEnvelope.ruleViolations`). A reviewer reading a report with a visible
"this broke rule X" note is better served than one reading a silently-wrong
report, and far better served than one reading no report.

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
    settings = dict(getattr(ctx, "output_rules", None) or {})
    text = await ctx.llm(system_prompt, user, max_tokens=max_tokens, name=name)
    payload = assets.extract_json(text) or {}
    if not payload or not settings.get("enabled", True):
        return payload, []

    max_words = int(settings.get("maxWords") or 0)
    found = rules.check(payload, upstream=upstream, rules=rule_set,
                        extra_counts=extra_counts, max_words=max_words)
    if not found:
        return payload, []

    kinds = ", ".join(sorted({v.rule for v in found}))
    if not settings.get("repair", False):
        # CHECK-ONLY, the default. The violations go on the asset and into the UI
        # for the reviewer; the framework does not spend a second call on the
        # customer's behalf. Turn on `outputRules.repair` for this agent to trade
        # that call for a chance at a cleaner deliverable.
        await _log(ctx, f"output rules: {len(found)} violation(s) recorded on the "
                        f"asset ({kinds}); repair is off for this agent")
        return payload, [str(v) for v in found]

    await _log(ctx, f"output rules: {len(found)} violation(s); re-asking once "
                    f"({kinds})")

    repaired_text = await ctx.llm(
        system_prompt, user + rules.as_repair_instructions(found),
        max_tokens=max_tokens, name=f"{name or ctx.agent_id}-repair")
    repaired = assets.extract_json(repaired_text) or {}
    if not repaired:
        # The retry produced nothing usable. Keep the first answer, which at least
        # has substance, and report what is wrong with it.
        return payload, [str(v) for v in found]

    still = rules.check(repaired, upstream=upstream, rules=rule_set,
                        extra_counts=extra_counts, max_words=max_words)
    # A rewrite that fixes three ungrounded figures while introducing an invented
    # plan has a LOWER count and is not better — it traded a defect the reviewer was
    # told about for one nobody asked for. So a NEW KIND of violation always loses,
    # whatever the count does, and so does a higher count.
    #
    # A TIE IS BROKEN BY MAGNITUDE, not accepted blindly. Counting cannot see degree,
    # and `over-budget` is a degree: a re-ask measured on a real report cut it from
    # 2453 words to 1704 and was thrown away because the count still said one
    # violation either way. But accepting every tie is wrong too — a tie can hide a
    # rewrite that dropped a `limitations` entry and added another invented label. So
    # a tie is kept only when the prose actually got shorter, and otherwise the first
    # answer wins, which is the content-safe default.
    new_kinds = {v.rule for v in still} - {v.rule for v in found}
    tie_not_shorter = (len(still) == len(found)
                       and rules.prose_words(repaired) >= rules.prose_words(payload))
    if new_kinds or len(still) > len(found) or tie_not_shorter:
        reason = ("introduced " + ", ".join(sorted(new_kinds)) if new_kinds
                  else "did not reduce the violations or the length")
        await _log(ctx, f"output rules: re-ask {reason}; keeping the first answer "
                        f"and recording its violations")
        return payload, [str(v) for v in found]

    if still:
        fixed = len(found) - len(still)
        await _log(ctx, f"output rules: kept the re-ask "
                        f"({fixed} fixed, {len(still)} recorded on the asset)"
                        if fixed else
                        f"output rules: kept the re-ask (same {len(still)} kind(s), "
                        f"no new ones)")
    return repaired, [str(v) for v in still]


async def _log(ctx, message: str) -> None:
    """Timeline line, best-effort — a rule check must never break a run."""
    try:
        await ctx.log(message)
    except Exception:  # noqa: BLE001,S110 - observability is not the job here
        pass
