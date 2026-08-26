"""Knowledge Base Research — the RAG research agent (step 2, parallel).

Retrieves grounding evidence from the Bedrock Knowledge Base (RAG) and produces
a ResearchOutput with every finding classified by evidence type. Runs in
parallel with web_research; both are reviewed together at the "research" group
HITL gate.

Retrieval is scoped to this agent's corpus via KB_FILTER (the KB is one shared
index; each document is tagged with a doc_type equal to its top-level folder).
"""

from app.common import research
from app.common.base import Agent
from app.common.context import AgentContext

from .prompts import SYSTEM_PROMPT

# Scopes RAG retrieval to this agent's corpus (kb_docs/reference/).
KB_FILTER = "reference"


class KnowledgeResearchAgent(Agent):
    system_prompt = SYSTEM_PROMPT

    async def run(self, ctx: AgentContext) -> str:
        return await research.synthesize(
            ctx, system_prompt=SYSTEM_PROMPT, use_rag=True, kb_filter=KB_FILTER,
        )


agent = KnowledgeResearchAgent()
