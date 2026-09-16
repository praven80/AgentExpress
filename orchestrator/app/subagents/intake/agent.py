"""Intake — turns the user's request into a structured brief (step 1).

The first stage of the pipeline. It takes the free-text request (ctx.topic) and
asks the model to distil it into a RequestBrief: a clear objective, scope, the
key questions to answer, constraints, and open questions for the human to
confirm at the HITL gate.

On a "revise" gate decision the reviewer's feedback arrives as ctx.feedback and
the agent re-derives the brief taking that guidance into account; the version
bumps so prior versions are preserved.
"""

from __future__ import annotations

import json

from app.common import clock, rules, structured, synthesis
from app.common.base import Agent
from app.common.context import AgentContext
from app.common.contracts import RequestBrief
from app.common.contracts.base import AssetStatus

from .prompts import SCHEMA, SYSTEM_PROMPT


class IntakeAgent(Agent):
    system_prompt = SYSTEM_PROMPT

    async def run(self, ctx: AgentContext) -> str:
        user = f"=== USER REQUEST ===\n{ctx.topic}\n"
        if ctx.feedback:
            user += f"\n=== REVIEWER GUIDANCE ===\n{ctx.feedback}\n"
        user += f"\nReturn ONLY JSON matching this schema:\n{SCHEMA}"

        # Same checked path as every other agent (app/common/structured.py). The
        # brief has no upstream assets, so the raw request IS its grounding: a
        # figure or a numbered plan in the brief that the user never mentioned is
        # invented, and this is the earliest point it can be caught — everything
        # downstream treats the brief as authoritative.
        payload, unrepaired = await structured.ask_json(
            ctx, SYSTEM_PROMPT, user,
            rule_set=rules.BRIEF, upstream=ctx.topic,
        )
        version = synthesis.prior_version(ctx)

        title = str(payload.get("title") or ctx.topic or "Untitled request").strip()
        objective = str(payload.get("objective") or "").strip()
        if not objective:
            objective = ctx.topic

        asset = RequestBrief(
            assetId=f"asset-request-brief-{synthesis.slug(title)}-v{version}",
            version=version,
            status=AssetStatus.IN_REVIEW,
            createdAt=clock.now_et(),
            createdByAgent=ctx.agent_id,
            executiveSummary=str(payload.get("executiveSummary") or objective).strip() or None,
            title=title,
            objective=objective,
            # Passed through, not str()-ed: the contract's Scope model accepts
            # either the {inScope, outOfScope} object the model returns or a
            # plain string. str() on a dict produced a Python repr.
            scope=payload.get("scope") or {},
            keyQuestions=synthesis.str_list(payload, "keyQuestions"),
            constraints=synthesis.str_list(payload, "constraints"),
            assumptions=synthesis.str_list(payload, "assumptions"),
            openQuestions=synthesis.str_list(payload, "openQuestions"),
            ruleViolations=unrepaired,
        )
        return json.dumps(asset.model_dump(by_alias=True, mode="json"), indent=2)


agent = IntakeAgent()
