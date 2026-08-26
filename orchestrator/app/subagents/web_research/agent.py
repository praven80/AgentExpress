"""External Research — the MCP research agent (step 2, parallel).

Gathers grounding evidence from a remote MCP server reached through the
AgentCore Gateway (the default target is the public AWS Knowledge MCP server,
which needs no credentials), and produces a ResearchOutput with every finding
classified by evidence type. MCP-only — it does not use the Knowledge Base.

Runs in parallel with knowledge_research; both are reviewed together at the
"research" group HITL gate. The MCP call degrades to a noted data limitation
when the Gateway is not wired (e.g. local dev).
"""

from app.common import research
from app.common.base import Agent
from app.common.context import AgentContext

from .prompts import SYSTEM_PROMPT

# The Gateway target label this agent calls (see workflow.json "mcp" and the
# Gateway target name in terraform/gateway.tf — they must match).
MCP_LABEL = "knowledge"


class WebResearchAgent(Agent):
    system_prompt = SYSTEM_PROMPT

    async def run(self, ctx: AgentContext) -> str:
        return await research.synthesize(
            ctx, system_prompt=SYSTEM_PROMPT, mcp_label=MCP_LABEL, use_rag=False,
        )


agent = WebResearchAgent()
