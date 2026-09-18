"""Knowledge Base Research — RAG over your own documents (step 2, parallel).

Its data source is DECLARED, not coded: workflow.json binds this agent to the
tool named by its `tool` field (type="kb") and scopes retrieval to its `corpus`.
The shared runner in app/common/research.py does the rest.

To point this at your own documents, replace the contents of kb_docs/ — each
top-level folder is a corpus. No change here.
"""
from app.common.base import Agent
from app.common.context import AgentContext
from app.subagents._shared import research

from .prompts import SYSTEM_PROMPT


class KnowledgeResearchAgent(Agent):
    system_prompt = SYSTEM_PROMPT

    async def run(self, ctx: AgentContext) -> str:
        # Optional, app-level Cedar check — the ONE place this sample shows the
        # secondary policy path. It is NOT what protects the Knowledge Base: the
        # retrieval inside synthesize() goes through the Gateway, and the attached
        # policy engine authorizes that call server-side (app/features/policy/,
        # terraform/policy.tf). Keep this pattern for gating an in-process action
        # that never crosses the Gateway. Fail-open, and a no-op when policy is
        # disabled for this agent in workflow.json.
        if not await ctx.policy_check("retrieve_knowledge"):
            await ctx.log("Policy denied retrieve_knowledge for this agent")
            ctx.tool = None  # reason over upstream inputs only, with no retrieval
        return await research.synthesize(ctx, system_prompt=SYSTEM_PROMPT)


agent = KnowledgeResearchAgent()
