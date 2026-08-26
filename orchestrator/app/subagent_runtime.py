"""Per-agent AgentCore Runtime entrypoint.

A dedicated agent (workflow.json `runtime: "dedicated"`) runs in its OWN
AgentCore Runtime container. That container runs THIS app, with the agent to
run selected by the AGENT_ID environment variable. It runs the agent's real
in-process implementation (build_agent_module) — the "dedicated" placement only
moves where the code executes, not what it does.

Invoke contract (called by the orchestrator's AgentCoreRuntimeAgent):
  {"agent_id","topic","outputs":{id:text,...},"feedback","session_id"}
Returns: {"agent_id","output"} (or {"error"} on failure).
"""

import os

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from app.common.context import AgentContext
from app.orchestrator.registry import build_agent_module

AGENT_ID = os.environ["AGENT_ID"]
_agent = build_agent_module(AGENT_ID)  # this runtime hosts exactly one agent

app = BedrockAgentCoreApp()


@app.entrypoint
async def invoke(payload, context=None):
    try:
        state = {
            "topic": payload.get("topic", ""),
            "outputs": payload.get("outputs", {}) or {},
            # AgentContext reads feedback[self.id]; scope it to this agent.
            "feedback": {AGENT_ID: payload.get("feedback", "") or ""},
        }
        config = {"configurable": {"thread_id": payload.get("session_id", "subagent")}}
        ctx = AgentContext(_agent, state, config)
        out = await _agent.run(ctx)
        return {"agent_id": AGENT_ID, "output": out}
    except Exception as e:  # noqa: BLE001
        return {"agent_id": AGENT_ID, "error": f"{type(e).__name__}: {e}"}


if __name__ == "__main__":
    app.run()
