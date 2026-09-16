"""AgentCoreRuntimeAgent — the orchestrator-side node body for an agent that runs
in its OWN AgentCore Runtime (workflow.json `runtime: "dedicated"`).

Instead of running the agent in-process, it calls bedrock-agentcore
InvokeAgentRuntime against that agent's dedicated runtime ARN, passing the same
inputs an in-process agent would read (topic, subject, upstream outputs,
reviewer feedback) and returning the runtime's output text. Same Agent
interface and node wrapper as an in-process agent, so the graph wiring is
identical — only where the compute happens differs.

The per-agent runtime ARNs are injected by Terraform as AGENT_RUNTIME_ARNS
(a JSON map of agent_id -> runtime ARN).
"""

from __future__ import annotations

import asyncio
import json
import os

import boto3

from app.common.base import Agent

_RUNTIME_ARNS: dict = json.loads(os.getenv("AGENT_RUNTIME_ARNS", "{}"))
_REGION = os.getenv("AWS_REGION", "us-east-1")
_client = None


def _agentcore():
    global _client
    if _client is None:
        _client = boto3.client("bedrock-agentcore", region_name=_REGION)
    return _client


class AgentCoreRuntimeAgent(Agent):
    """Runs this agent by invoking its dedicated AgentCore Runtime."""

    async def run(self, ctx) -> str:
        arn = _RUNTIME_ARNS.get(self.id)
        if not arn:
            # No dedicated runtime wired (e.g. local dev). Be explicit rather
            # than silently producing nothing.
            await ctx.log(f"No dedicated runtime ARN for '{self.id}' (AGENT_RUNTIME_ARNS)")
            return f"[dedicated runtime for {self.id} not configured]"

        from app.features.observability import otel
        payload = {
            "agent_id": self.id,
            "topic": ctx.topic,
            "subject_id": (ctx.state or {}).get("subject_id", ""),
            "outputs": (ctx.state or {}).get("outputs", {}) or {},
            "feedback": getattr(ctx, "feedback", "") or "",
            "session_id": ctx.session_id,
            # Propagate the trace context so this dedicated runtime's spans join
            # the SAME CloudWatch trace as the orchestrator (distributed trace).
            "otel_context": otel.carrier(),
        }
        # Session id must be >= 33 chars; make it deterministic per (session, agent).
        session = f"{ctx.session_id}-{self.id}".ljust(33, "0")[:33]

        await ctx.log(f"Invoking dedicated AgentCore Runtime for '{self.id}'")
        resp = await asyncio.to_thread(
            _agentcore().invoke_agent_runtime,
            agentRuntimeArn=arn,
            runtimeSessionId=session,
            payload=json.dumps(payload).encode(),
        )
        stream = resp.get("response")
        raw = stream.read() if hasattr(stream, "read") else stream
        data = json.loads(raw)
        if isinstance(data, str):  # tolerate a JSON-string-wrapped body
            data = json.loads(data)
        if isinstance(data, dict):
            if data.get("error"):
                return f"[dedicated runtime error for {self.id}: {data['error']}]"
            return data.get("output", "")
        return str(data)


agent = AgentCoreRuntimeAgent()
