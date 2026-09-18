"""MCP Documentation Research — a remote MCP server (step 2, parallel).

This agent exists to demonstrate the BRING-YOUR-OWN-MCP-SERVER path. It is bound
by workflow.json to a tool of type="mcp"; to point it at your own server, change
that tool's `endpoint` and nothing else.

The agent itself is deliberately identical in shape to the other two research
agents — proof that swapping a data source is config, not code.
"""
from app.common.base import Agent
from app.common.context import AgentContext
from app.subagents._shared import research

from .prompts import SYSTEM_PROMPT


class DocumentationSearchAgent(Agent):
    system_prompt = SYSTEM_PROMPT

    async def run(self, ctx: AgentContext) -> str:
        return await research.synthesize(ctx, system_prompt=SYSTEM_PROMPT)


agent = DocumentationSearchAgent()
