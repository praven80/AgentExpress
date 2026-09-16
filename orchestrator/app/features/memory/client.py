"""AgentCore Memory client — recall and store long-term semantic memory.

No-ops when SEMANTIC_MEMORY_ID is unset (local dev / memory disabled), so an
agent that calls these always runs.
"""

import asyncio
import datetime
import os

from app.common.config import REGION

_MEMORY_ID = os.getenv("SEMANTIC_MEMORY_ID", "")
_mgr = None


def _manager():
    global _mgr
    if _mgr is None:
        from bedrock_agentcore.memory import MemorySessionManager
        _mgr = MemorySessionManager(memory_id=_MEMORY_ID, region_name=REGION)
    return _mgr


def _record_text(record) -> str:
    """Pull the text out of one long-term memory record. AgentCore returns the
    content as {"text": "..."} (dict); be tolerant of a plain string or an object
    so a shape change never crashes the agent."""
    content = record.get("content") if isinstance(record, dict) else getattr(record, "content", None)
    if isinstance(content, dict):
        return content.get("text") or content.get("Text") or ""
    if isinstance(content, str):
        return content
    text = getattr(content, "text", None)
    return text if isinstance(text, str) else ""


async def recall(query: str, namespace: str = "") -> list[str]:
    """Search long-term memory for relevant past insights.
    Returns a list of content strings (empty if nothing found or disabled).

    No session id: long-term memory is scoped by NAMESPACE (which encodes agent +
    subject), deliberately so — the point of a long-term recall is to reach across
    sessions, and passing one in would suggest it narrowed the search.
    """
    if not _MEMORY_ID or not query:
        return []

    records = await asyncio.to_thread(
        _manager().search_long_term_memories,
        query=query,
        namespace=namespace or None,
        top_k=5,
    )
    return [t for t in (_record_text(r) for r in (records or [])) if t]


_data_client = None


def _data_plane():
    global _data_client
    if _data_client is None:
        import boto3
        _data_client = boto3.client("bedrock-agentcore", region_name=REGION)
    return _data_client


async def store(session_id: str, actor_id: str, content: str) -> None:
    """Store an insight in long-term memory for future recall. No-op if
    SEMANTIC_MEMORY_ID is not configured.

    Writes ONE short-term event (a USER prompt + ASSISTANT insight) via the
    CreateEvent data-plane API; AgentCore's strategies then asynchronously
    extract long-term records into their namespaces (insights/{actorId}, …).

    Takes no namespace, and cannot: the actor id encodes agent + subject, and
    AgentCore derives the namespace from it SERVER-SIDE. A namespace argument here
    would be silently ignored.
    """
    if not _MEMORY_ID or not content:
        return

    await asyncio.to_thread(
        lambda: _data_plane().create_event(
            memoryId=_MEMORY_ID, actorId=actor_id, sessionId=session_id,
            eventTimestamp=datetime.datetime.now(datetime.timezone.utc),
            payload=[
                {"conversational": {"role": "USER",
                                    "content": {"text": f"Reusable insight for {actor_id}:"}}},
                {"conversational": {"role": "ASSISTANT", "content": {"text": content}}},
            ],
        )
    )
