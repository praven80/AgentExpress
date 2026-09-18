"""Web Search Research — the live public web (step 2, parallel).

Bound by workflow.json to a tool of type="websearch", which the IaC provisions as
the managed AgentCore Web Search connector on the Gateway. No API keys, no
endpoint, no schema: the connector is AWS-operated and queries never leave AWS.

Nothing about the search is coded here — `maxResults` and any domain filters come
from the tool's entry in workflow.json.
"""
from app.common.base import Agent
from app.common.context import AgentContext
from app.subagents._shared import research

from .prompts import SYSTEM_PROMPT


class WebSearchAgent(Agent):
    system_prompt = SYSTEM_PROMPT

    async def run(self, ctx: AgentContext) -> str:
        return await research.synthesize(ctx, system_prompt=SYSTEM_PROMPT)


agent = WebSearchAgent()
