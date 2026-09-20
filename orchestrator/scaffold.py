#!/usr/bin/env python3
"""Create a new agent: its `workflow.json` entry AND its folder, in one command.

    python3 scaffold.py agent triage
    python3 scaffold.py agent triage --produces claim-triage --tool claims_db
    python3 scaffold.py agent fraud_check --remote            # runtime "a2a", no folder
    python3 scaffold.py agent triage --dry-run                # show, write nothing

WHY THIS EXISTS
The framework's promise is that a customer touches two things: `workflow.json` and a
folder under `app/subagents/<id>/`. Both were learnable only by reading an existing
agent and copying it — which works, and means the first thing anyone does is inherit
whichever agent they happened to open. `cost_research` is 354 lines with a tool-args
model call and a rates table; `documentation_search` is 27 lines. Copying the wrong
one is a bad first hour.

So this writes the MINIMUM that is complete and runs: four required keys in
workflow.json and three files whose contract the registry actually enforces
(`check_agent_module`). Nothing is stubbed out with a TODO that would pass validation
and fail at run time — the generated `run()` really calls the model and really returns
its answer, so a customer can deploy immediately and then make it theirs.

WHAT IT DELIBERATELY DOES NOT DO
It does not put the agent in `steps`. Where a step belongs in the topology is the one
decision that is genuinely the customer's, it depends on what the agent consumes, and a
guess would either be wrong or teach that ordering does not matter. The command prints
the one line to add and where.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SUBAGENTS = ROOT / "app" / "subagents"
WORKFLOW = ROOT / "app" / "workflow.json"

#: Same rule the registry and the generated schema enforce: the id becomes part of an
#: AgentCore Runtime name, so no hyphens.
ID_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")

INIT_PY = '''from .agent import agent

__all__ = ["agent"]
'''

PROMPTS_PY = '''"""Prompts for the {title} agent.

EVERYTHING ABOUT WHAT THIS AGENT SAYS LIVES HERE, and nothing about how it runs. That
split is the only convention the framework asks of this folder, and it is worth keeping
for one practical reason: a prompt is the part you will edit fifty times, and you want
to open a file that is only prompt.

`SYSTEM_PROMPT` is the agent's instructions. `SCHEMA` is the shape you require back,
passed to the model as text — keep the two next to each other so a field you add to one
cannot be forgotten in the other.
"""

SYSTEM_PROMPT = (
    "You are the {title} agent. <Say what this agent is for, in one or two "
    "sentences.>\\n\\n"
    "RULES\\n"
    "1. Use ONLY the inputs you are given. Invent nothing.\\n"
    "2. No figure that is not in your inputs — no cost, threshold, percentage or "
    "duration. Not even as an illustration: a reader takes a number in a deliverable "
    "for one somebody agreed to.\\n"
    "3. Say plainly what you could not determine, rather than filling the gap.\\n"
    "4. Return ONLY valid JSON."
)

#: The shape the model must return. Sent as text, so describe each field in place —
#: that description is the most effective part of the whole prompt.
SCHEMA = (
    '{{"summary": "one or two sentences on what you found", '
    '"findings": ["each material point, as its own string"], '
    '"openQuestions": ["what you could not determine, or []"]}}'
)
'''

AGENT_PY = '''"""{title} — <one line on what this agent does and where it sits>.

THE CONTRACT THIS FILE HAS TO MEET is three lines long, and `registry.check_agent_module`
enforces every one of them: subclass `Agent`, override `async def run(self, ctx) -> str`,
and assign `agent = ...` at the bottom. Everything else here is yours to change.

WHAT `ctx` GIVES YOU (app/common/context.py), all of it config-driven — each helper is a
no-op when the corresponding block is absent from this agent's workflow.json entry, so
you can call them unconditionally:

    await ctx.llm(system, user)      the model call. ROUTE EVERY MODEL CALL THROUGH IT:
                                    guardrails, cost and token telemetry, long-term
                                    memory injection, truncation detection and
                                    cancellation all live there. A framework holding its
                                    own Bedrock client loses all of that silently while
                                    the run still reports success.
    ctx.input("<agent_id>")          an upstream agent's approved output
    await ctx.retrieve(query)        your `tool` when it is a Knowledge Base
    await ctx.call_tool(query)       your `tool` otherwise
    await ctx.call_tool_rows(query)  the same, as data rows (see `rowFields`)
    ctx.feedback                     a reviewer's comment when this is a revise
    await ctx.log(msg)               a line on the run timeline
    await ctx.heartbeat(pct)         progress, for a long step

Your token budget is `maxTokens` in workflow.json and `ctx.llm` applies it — never pass
a number here, or the budget moves back into code where whoever edits the config cannot
see it (there is a test for that).
"""

import json

from app.common.base import Agent
from app.common.context import AgentContext

from .prompts import SCHEMA, SYSTEM_PROMPT


class {cls}(Agent):
    # Used by the framework for evaluations and for the observability prompt inspector.
    system_prompt = SYSTEM_PROMPT

    async def run(self, ctx: AgentContext) -> str:
        user = (
            f"=== REQUEST ===\\n{{ctx.topic}}\\n"
            # Everything an earlier step approved. Derived from the topology, so adding
            # an agent before this one in `steps` feeds it here with no change to this
            # file.
            f"\\n=== APPROVED UPSTREAM OUTPUTS ===\\n{{self._upstream(ctx)}}\\n"
        )
        if ctx.feedback:
            user += f"\\n=== REVIEWER GUIDANCE (this is a revision) ===\\n{{ctx.feedback}}\\n"
        user += f"\\nReturn ONLY JSON matching this schema:\\n{{SCHEMA}}"

        # Returned as text, which is what a node's output is. To emit a VALIDATED asset
        # instead — with an assetId a later agent can cite — give this agent a pydantic
        # contract and use app/subagents/_shared/synthesis.py; `report` is the worked
        # example.
        return await ctx.llm(SYSTEM_PROMPT, user)

    @staticmethod
    def _upstream(ctx: AgentContext) -> str:
        from app.common.config import upstream_of

        blocks = []
        for agent_id in upstream_of("{agent_id}"):
            raw = ctx.input(agent_id)
            if raw:
                blocks.append(f"--- {{agent_id.replace('_', ' ').upper()}} ---\\n{{raw}}")
        return "\\n\\n".join(blocks) or json.dumps({{"note": "this is the first step"}})


agent = {cls}()
'''


def title_of(agent_id: str) -> str:
    return agent_id.replace("_", " ").title()


def class_of(agent_id: str) -> str:
    return "".join(p.capitalize() for p in agent_id.split("_")) + "Agent"


def entry(args) -> OrderedDict:
    """The workflow.json entry, in the canonical key order.

    Built here rather than copied from a template so it cannot drift from
    `app/keys.json` — the order comes from the same place `format_workflow.py` reads.
    """
    sys.path.insert(0, str(ROOT))
    try:
        from format_workflow import top_level_order
        order = top_level_order("agent")
    finally:
        sys.path.pop(0)

    spec: dict = {"name": title_of(args.agent_id), "produces": args.produces}
    if args.remote:
        spec |= {"runtime": "a2a", "source": "a2a_lambda", "skill": args.skill or "compliance",
                 "auth": "sigv4"}
    else:
        spec |= {"runtime": args.runtime, "maxTokens": args.max_tokens}
        if args.tool:
            spec["tool"] = args.tool
            tool = (args.workflow.get("tools") or {}).get(args.tool) or {}
            # A Knowledge Base tool is the one type where a second key is effectively
            # required: without `corpus` the agent retrieves across every corpus, which is
            # rarely what someone binding to a KB means and is invisible when it is wrong.
            # Defaulted to the tool's first declared corpus, which is at least a real one.
            if str(tool.get("type") or "").lower() == "kb" and tool.get("corpora"):
                spec["corpus"] = tool["corpora"][0]
        else:
            spec["access"] = ["Upstream assets (orchestrator graph state)"]
    return OrderedDict((k, spec[k]) for k in order if k in spec)


def files(agent_id: str) -> dict[str, str]:
    fmt = {"agent_id": agent_id, "title": title_of(agent_id), "cls": class_of(agent_id)}
    return {
        "__init__.py": INIT_PY,
        "prompts.py": PROMPTS_PY.format(**fmt),
        "agent.py": AGENT_PY.format(**fmt),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a new agent's workflow.json entry and its folder.")
    sub = parser.add_subparsers(dest="what", required=True)
    p = sub.add_parser("agent", help="a new agent")
    p.add_argument("agent_id", help="the agent id; also the folder name under app/subagents/")
    p.add_argument("--produces", default="", help="the deliverable name (default: <id>-output)")
    p.add_argument("--runtime", default="main", choices=("main", "dedicated"),
                   help="main = in-process, dedicated = its own AgentCore Runtime")
    p.add_argument("--max-tokens", type=int, default=4000, help="output token budget")
    p.add_argument("--tool", default="", help="a key in the workflow.json `tools` block")
    p.add_argument("--remote", action="store_true",
                   help='runtime "a2a": an agent you do NOT operate. Writes no folder.')
    p.add_argument("--skill", default="", help="with --remote: which stand-in skill")
    p.add_argument("--dry-run", action="store_true", help="print, write nothing")
    args = parser.parse_args()

    agent_id = args.agent_id
    if not ID_RE.match(agent_id):
        print(f"scaffold: {agent_id!r} is not a valid agent id. It becomes part of an "
              f"AgentCore Runtime name, so it must match {ID_RE.pattern} — no hyphens.",
              file=sys.stderr)
        return 2
    args.produces = args.produces or f"{agent_id.replace('_', '-')}-output"

    workflow = json.loads(WORKFLOW.read_text(), object_pairs_hook=OrderedDict)
    # ALREADY IN CONFIG -> write the folder only, and leave workflow.json alone.
    #
    # This used to be a hard error, which made the tool useless for the order the docs
    # actually recommend: "define the workflow.json configuration, then define the agent
    # logic in the subagents folder". Designing the pipeline first and implementing the
    # agents second is the natural direction, and it was the one direction the scaffold
    # refused — found while building a foreign workflow exactly that way.
    existing = agent_id in workflow["agents"]
    if existing:
        spec_in_config = workflow["agents"][agent_id]
        placement = str(spec_in_config.get("runtime") or "main")
        if placement == "a2a":
            print(f"scaffold: agent {agent_id!r} is runtime \"a2a\" in workflow.json, so its "
                  f"code is somebody else's — there is no folder to create.", file=sys.stderr)
            return 2
        if (SUBAGENTS / agent_id).exists():
            print(f"scaffold: app/subagents/{agent_id}/ already exists, and {agent_id!r} is "
                  f"already in workflow.json. Nothing to do.", file=sys.stderr)
            return 2
    if args.tool and args.tool not in (workflow.get("tools") or {}):
        print(f"scaffold: --tool {args.tool!r} is not a key in the `tools` block "
              f"({', '.join(workflow.get('tools') or {}) or 'none declared'}). Declare the "
              f"tool first, or leave it off.", file=sys.stderr)
        return 2

    args.workflow = workflow
    # When the entry already exists it is the CUSTOMER'S; generating one and overwriting
    # theirs would throw away the decisions they came here having already made.
    spec = spec_in_config if existing else entry(args)
    folder = SUBAGENTS / agent_id
    written = {} if args.remote else files(agent_id)

    if existing:
        print(f"agents.{agent_id} is already configured; writing the folder only:")
        print("  " + json.dumps(spec, indent=2, ensure_ascii=False).replace("\n", "\n  "))
    else:
        print(f"workflow.json  agents.{agent_id}:")
        print("  " + json.dumps(spec, indent=2, ensure_ascii=False).replace("\n", "\n  "))
    for name in written:
        print(f"app/subagents/{agent_id}/{name}")
    if args.remote:
        print("(no folder: runtime \"a2a\" means the code is somebody else's)")

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return 0

    if written:
        if folder.exists():
            print(f"scaffold: {folder} already exists.", file=sys.stderr)
            return 2
        folder.mkdir(parents=True)
        for name, body in written.items():
            (folder / name).write_text(body)

    if existing:
        # Their config, untouched. The whole point of this path.
        print(f"\nDone. app/subagents/{agent_id}/ now implements the entry you already "
              f"wrote; workflow.json was not modified.")
        return 0

    workflow["agents"][agent_id] = spec
    WORKFLOW.write_text(json.dumps(workflow, indent=2, ensure_ascii=False) + "\n")
    # Straight back into canonical order and readable form, so the file a customer opens
    # next never shows them a shape the framework would not have written.
    subprocess.run([sys.executable, str(ROOT / "format_workflow.py")],  # noqa: S603
                   cwd=ROOT, check=True)

    print(f"\nDone. One thing left, and it is the decision only you can make: put "
          f"{agent_id!r} in `steps`.")
    print(f'  {{ "agent": "{agent_id}", "hitl": true }}')
    print("Position matters — an agent reads whatever ran in an EARLIER step, so put it "
          "after the agents whose output it needs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
