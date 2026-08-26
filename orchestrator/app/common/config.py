"""Central configuration.

The workflow (agents + topology + HITL gates) is defined once in workflow.json
and loaded here. Everything else (graph, UI, BFF) derives from it, so adding an
agent is a config change plus a new agent module — no changes to the framework.

Load order for the workflow definition:
  1. WORKFLOW_JSON env var (inline JSON) — used by the Lambda BFF.
  2. app/workflow.json on disk — used by the runtime and local dev.
"""

import json
import os
from pathlib import Path

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "workflow.json"


def load_workflow() -> dict:
    inline = os.getenv("WORKFLOW_JSON")
    if inline:
        return json.loads(inline)
    return json.loads(_CONFIG_PATH.read_text())


WORKFLOW = load_workflow()
AGENTS: dict = WORKFLOW["agents"]
STEPS: list = WORKFLOW["steps"]
NODE_IDS: list = list(AGENTS.keys())

# The final agent in the pipeline (used to surface the run's result). The last
# step may be a single agent, a parallel group, or a sequence group.
_LAST_STEP = STEPS[-1]
LAST_AGENT_ID = _LAST_STEP.get("agent") or (
    _LAST_STEP.get("parallel") or _LAST_STEP.get("sequence"))[-1]

# Engine-level settings that describe the orchestrator itself (not an agent).
# The app consumes 'defaultModel' and 'longStepSeconds'; the rest is
# documentation. Environment variables still take precedence at deploy time.
ORCHESTRATOR: dict = WORKFLOW.get("orchestrator", {})

# --- runtime / infra settings (env-driven, then orchestrator block) -------

MODEL_ID = (os.getenv("BEDROCK_MODEL_ID")
            or ORCHESTRATOR.get("defaultModel")
            or "us.anthropic.claude-haiku-4-5-20251001-v1:0")
REGION = os.getenv("AWS_REGION", "us-east-1")
MEMORY_ID = os.getenv("MEMORY_ID")
STATUS_TABLE = os.getenv("STATUS_TABLE")
EVENTS_TABLE = os.getenv("EVENTS_TABLE")

MCP_TIMEOUT = float(os.getenv("MCP_TIMEOUT", "25"))

# AgentCore Gateway (MCP tool plane). Agents reach all MCP tools through one
# Cognito-authed Gateway endpoint; the runtime fetches a client-credentials token.
# Agents reference a Gateway-backed tool by a label in workflow.json ("mcp" key,
# e.g. "knowledge" or "kb"); the Gateway routes to the matching target. Swapping
# a target (e.g. to a different MCP server or REST API) is a Terraform change —
# no app change needed.
GATEWAY_URL = os.getenv("GATEWAY_URL", "")
GATEWAY_TOKEN_URL = os.getenv("GATEWAY_TOKEN_URL", "")
GATEWAY_CLIENT_ID = os.getenv("GATEWAY_CLIENT_ID", "")
GATEWAY_CLIENT_SECRET = os.getenv("GATEWAY_CLIENT_SECRET", "")
GATEWAY_AUDIENCE = os.getenv("GATEWAY_AUDIENCE", "")

# Per-step delay for long-running agents. Small by default for demos; raise to
# simulate genuinely long jobs (the async runtime keeps the session alive).
LONG_STEP_SECONDS = float(os.getenv("LONG_STEP_SECONDS")
                          or ORCHESTRATOR.get("longStepSeconds", 1.2))
