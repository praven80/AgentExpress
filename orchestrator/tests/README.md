# Config-plane tests

```bash
cd orchestrator
pip install -r requirements-dev.txt
pytest
```

No AWS credentials, no model calls, no network. The whole suite runs in about a
second, so it belongs in a pre-commit hook or a CI step rather than a nightly job.

## What "config plane" means here

This framework has two planes:

- **Data plane** — an agent's prompt and the model's answer. Non-deterministic;
  testing it means judging output quality, which is what AgentCore Evaluations and
  the HITL gates are for.
- **Config plane** — the code that turns `workflow.json` into a running system:
  which agent reads whose output, how a step becomes graph nodes and edges, what a
  tool call's arguments look like, which citations survive, and who is allowed to
  approve. Entirely deterministic, and the part a customer breaks when they adapt
  the framework to their own workflow.

Only the config plane is tested here. The promise this project makes is "edit
`workflow.json`, `app/subagents/` and — only if a tool asks the framework to deploy a
function for you — `app/tools/`, and nothing else", and these tests are what keep that
promise honest.

## What each file covers

| File | Covers |
|---|---|
| `test_topology.py` | `step_agents`, `FIRST_AGENT_ID` / `LAST_AGENT_ID`, `upstream_of` across all three step shapes |
| `test_graph_build.py` | `build_graph` compiles for 14 topologies; the three HITL gate routers |
| `test_rerun_plan.py` | `rerun_plan` / `group_rerun_plan` — which node a rewind is attributed to |
| `test_subagents.py` | The code a customer writes, and that it stays in step with the config that declares it. For `app/subagents/`: that there is a folder for every local agent **and no folder without one** (a leftover folder is code a reader assumes is live); that each meets the three-line contract, checked through the framework's own `check_agent_module` rather than a re-implementation; that each way a folder can be wrong produces a message naming the file and the fix; and that `scaffold.py` generates an agent which really loads, configures and satisfies the same checks — because handing every new customer a broken first agent is a defect nothing else would notice. For `app/tools/`: the same both-directions rule against every tool that declares `source` (a declared folder exists, and no folder is orphaned), that `.dockerignore` keeps it out of the orchestrator image since the function runs as a Lambda, and that a tool folder cannot pass for an agent |
| `test_workflow_schema.py` | The config surface as a CUSTOMER meets it: that `app/workflow.schema.json` is a valid schema, is in sync with `app/keys.json`, accepts both the shipped workflow and a foreign one, and **rejects each of the 29 mistakes the three deploy-time validators reject** — because an editor that says a config is fine while a deploy rejects it teaches people to ignore the editor. Also that the canonical key order is real: every agent, tool and step in the shipped file is in it, and the formatter's guard catches a reorder that `==` on two dicts cannot see |
| `test_a2a.py` | `runtime: "a2a"` — Agent Card discovery, the JSON-RPC request shape asserted against the published form, all four auth modes (including SigV4 signing), task polling and every terminal state, each place the protocol allows an answer to hide, the asset envelope the framework stamps on a structured reply (and the prose reply it leaves alone), and the shipped stand-in driven by the real client with only Bedrock faked — including that its replies satisfy the pydantic contracts they claim to produce |
| `test_branching.py` | `branch` — every operator and near-miss, the spec/topology validation, and a graph that is actually **invoked** to prove the chosen path ran and the other was marked skipped |
| `test_tool_calls.py` | `_tool_arguments` (the argument shape, per tool type), `_select_tool` (Gateway naming), `_extract_chunks` (evidence + citations) |
| `test_citations.py` | URL verification against the evidence; downgrading a `sourced-fact` that rested on an invented link |
| `test_contracts.py` | Open `assetType` / `sectionType`, the strict envelope, report-section ordering |
| `test_asset_versioning.py` | An asset's own `version` agrees with the run count the timeline shows (a first run said v1 in one place and 2 in the other) |
| `test_authz.py` | `bff/authz.py` semantics and claim shapes; the assistant's action-tool filtering |
| `test_bff_routes.py` | Each gated mutating BFF route actually returns 403 for a caller without the group |
| `test_config_keys.py` | Every key in `workflow.json` is one something reads — a CLOSED allow-list, so an unwired key fails here |
| `test_foreign_use_case.py` | The framework names no shipped agent id (AST-checked), and a workflow from another domain entirely still builds |
| `test_cost_research.py` | The deterministic agent: unit rates read as DATA rows via `rowFields`, never paraphrased, and never totalled |
| `test_bff_projection.py` | What the browser is allowed to see — the projection is an allow-list, so deploy-time detail (tool endpoints, schemas, Cedar policy, the guardrail's denied words) cannot leak by omission — plus the proof that a 40-agent workflow is no longer a deploy failure |
| `test_asset_rendering.py` | `web/index.html`'s own functions, run under node: a contract the renderer has never seen gets first-class layout, chosen by SHAPE rather than by this sample's field names |
| `test_frameworks.py` | An agent authored with an agentic framework still cannot dodge governance — every framework's model call arrives at `ctx.llm`, named, with the agent's own prompt intact; the truncation flag survives the round trip; a model failure is not turned into placeholder text; offering the framework its own tools raises rather than being dropped; and Strands, a nested LangGraph and no-framework all emit the same contract |
| `test_tool_lambda.py` | The shipped `type: "lambda"` demo — the Price List query shaping and its unresolved-service reporting |
| `test_memory_insight.py` | What long-term memory stores and recalls, and the on-topic filter that stops cross-subject bleed |
| `test_truncation.py` | An answer cut off at `maxTokens` is SURFACED, not silently accepted |
| `test_runtime_invoke.py` | `InvokeAgentRuntime` is not retried, because it is not idempotent (measured: one blip billed the agent twice) |
| `test_clock_parity.py` | `app/common/clock.py` and `bff/clock.py` agree, since the BFF ships as its own zip |

## Why these particular assertions

Most of them are regressions. Each of the following shipped at some point, and
none of them raised an error — they produced a working deployment that was quietly
wrong:

- `FIRST_AGENT_ID` was the literal string `"intake"`, so renaming the first agent
  dropped the request brief from every downstream prompt.
- `upstream_of` returned *every* agent for an id in no step, so a renamed synthesis
  agent would have synthesized from its own previous output.
- `_extract_chunks` kept only `text`, discarding the `url` on every web result — so
  any URL in the report was one the model invented, and the acceptable-use terms
  requiring citations to be displayed were being broken.
- `sectionType` was a closed `Literal`, so a report section outside the expected
  list was silently dropped from the report.
- `assetType` was a closed enum, and `sourceType` a closed `Literal` that coerced
  anything else to `"other"` — so a customer could not define their own contract, or
  even name their own kind of evidence, without editing shared framework code.
- `slug` and `prior_version` were byte-identical copies in the two agent runners,
  and their `extract_json` had DIVERGED: only the synthesis runner could salvage a
  JSON object the model truncated at its token budget, though the research runner
  emits the larger payload and is the more likely to be cut off.

The RBAC files are the other kind: not a past regression, but a guard whose failure
mode is silent. A rule that is correct but not applied to one route looks exactly
like a rule that works. `test_bff_routes.py` drives every route in its
`MUTATING_ROUTES` table, and each guard was verified by removing it and confirming the
suite goes red.

## The import harness

`conftest.py` provides `workflow(defn)`, which sets `WORKFLOW_JSON` and re-imports
the `app` tree from scratch:

```python
with workflow(wf([{"agent": "triage"}, {"agent": "decide"}])) as imp:
    assert imp("app.common.config").FIRST_AGENT_ID == "triage"
```

This is needed because `app/common/config.py` reads the workflow once, at import,
and other modules bind those values (`graph_builder` imports `STEPS`, `registry`
imports `AGENTS`). That is correct for a container serving one workflow for its
whole life, but it means a test cannot mutate config in place. Re-importing is also
literally what a customer does when they swap the file, which is the behaviour under
test.

Synthetic agents in these tests use `runtime: "dedicated"`. That is a real,
supported placement, and it makes the registry build an `AgentCoreRuntimeAgent`
rather than importing `app.subagents.<id>` — so a topology test can use any agent
ids it likes without inventing agent packages on disk.

## Adding a test

Add it to the file that matches the concern. If you are adding a new `tools` type,
a new step shape, or a new `authorization` action, the corresponding table
(`TOPOLOGIES`, `MUTATING_ROUTES`, the `_REGISTRY` checks) is usually the only place
that needs a line.
