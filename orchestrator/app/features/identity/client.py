"""AgentCore Workload Identity client — fetch OAuth tokens for external APIs."""

import asyncio

from app.common.config import REGION

_client = None


def _identity_client():
    global _client
    if _client is None:
        from bedrock_agentcore.services.identity import IdentityClient
        _client = IdentityClient(REGION)
    return _client


async def get_token(provider_name: str, scopes: list[str] | None = None) -> str:
    """Fetch an OAuth access token for the named credential provider.
    Returns an empty string if provider_name is empty or the call fails, so a
    missing provider degrades to "no token" rather than breaking the agent."""
    if not provider_name:
        return ""

    try:
        token = await asyncio.to_thread(
            _identity_client().get_token_sync,
            provider_name=provider_name,
            scopes=scopes or [],
            auth_flow="M2M",
        )
        return token or ""
    except Exception:  # noqa: BLE001
        return ""
