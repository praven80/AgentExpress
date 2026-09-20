# Config-plane tests

```bash
cd orchestrator
pip install -r requirements-dev.txt
pytest
```

827 tests in about 12 seconds. No AWS credentials, no model calls, no network — so this
belongs in a pre-commit hook or a CI step, not a nightly job.

## What "config plane" means here

- **Data plane** — an agent's prompt and the model's answer. Non-deterministic; testing it
  means judging output quality, which is what AgentCore Evaluations and the HITL gates are
  for. Deliberately not tested here.
- **Config plane** — the code that turns `workflow.json` into a running system: which agent
  reads whose output, how a step becomes graph nodes and edges, what a tool call's
  arguments look like, which citations and figures survive, and who may approve. Entirely
  deterministic, and the part a customer breaks when adapting the framework.

The promise these tests keep honest: *edit `workflow.json`, `app/subagents/` and — only for
a tool that asks the framework to deploy a function — `app/tools/`, and nothing else.*

## What each file covers

| File | Covers |
|---|---|
| `test_topology.py` | `step_agents`, `FIRST_AGENT_ID` / `LAST_AGENT_ID`, `upstream_of` across all three step shapes |
| `test_graph_build.py` | `build_graph` compiles for 14 topologies; the three HITL gate routers |
| `test_rerun_plan.py` | `rerun_plan` / `group_rerun_plan` — which node a rewind is attributed to |
| `test_subagents.py` | That the code a customer writes stays in step with the config declaring it. Both directions for `app/subagents/` (a folder for every local agent, and no orphan folder) and for every tool declaring `source`; the three-line agent contract checked through the framework's own `check_agent_module`; that each way a folder can be wrong names the file and the fix; that `scaffold.py` output really loads and configures; and that `.dockerignore` keeps `app/tools/` out of the orchestrator image |
| `test_workflow_schema.py` | The config surface as a customer meets it: `app/workflow.schema.json` is valid, is in sync with `app/keys.json`, accepts the shipped workflow and a foreign one, and **rejects each of the 29 mistakes the three deploy-time validators reject**. Also that the canonical key order is real and the formatter's guard catches a reorder `==` cannot see |
| `test_config_keys.py` | Every key in `workflow.json` is one something reads — a closed allow-list, so an unwired key fails here |
| `test_foreign_use_case.py` | The framework names no shipped agent id (AST-checked), and a workflow from another domain still builds |
| `test_a2a.py` | `runtime: "a2a"` — Agent Card discovery, the JSON-RPC shape against the published form, all four auth modes including SigV4, task polling and every terminal state, the asset envelope stamped on a structured reply, and the shipped stand-in driven by the real client with only Bedrock faked |
| `test_branching.py` | `branch` — every operator and near-miss, spec and topology validation, and a graph that is actually **invoked** to prove the chosen path ran and the other was marked skipped |
| `test_tool_calls.py` | `_tool_arguments` (argument shape per tool type), `_select_tool` (Gateway naming), `_extract_chunks` (evidence + citations) |
| `test_citations.py` | URL verification against the evidence; downgrading a `sourced-fact` that rested on an invented link |
| `test_grounding.py` | The same problem for NUMBERS: a figure in no upstream asset is flagged for the reviewer. Asserts the matcher on every near-miss that would make it noisy (prose counts, `(1)`/`(2)` markers, asset ids, versions, timestamps, thousands separators, trailing zeros) and on both ends of a range, since the unit sits only on the far end. Plus the placement — an agent with a `tool` is exempt, one without is checked, and a hit warns rather than failing the run |
| `test_contracts.py` | Open `assetType` / `sectionType`, the strict envelope, report-section ordering |
| `test_asset_versioning.py` | An asset's own `version` agrees with the run count the timeline shows |
| `test_truncation.py` | An answer cut off at `maxTokens` is surfaced, not silently accepted |
| `test_authz.py` | `bff/authz.py` semantics and claim shapes; the assistant's action-tool filtering |
| `test_bff_routes.py` | Every gated mutating BFF route returns 403 for a caller without the group |
| `test_bff_projection.py` | What the browser may see — an allow-list, so deploy-time detail (tool endpoints, schemas, Cedar policy, denied words) cannot leak by omission. Plus that a large workflow is no longer a deploy failure: the projection used to ship in a `WORKFLOW_JSON` env var against Lambda's unraisable 4 KB environment cap, and now travels in the deployment package |
| `test_asset_rendering.py` | `web/index.html`'s own functions under node: a contract the renderer has never seen gets first-class layout, chosen by shape rather than by this sample's field names |
| `test_frameworks.py` | An agent authored with an agentic framework cannot dodge governance — every model call arrives at `ctx.llm`, named, with the agent's prompt intact; the truncation flag survives; a model failure is not turned into placeholder text; offering the framework its own tools raises rather than being dropped; and Strands, a nested LangGraph and no-framework emit the same contract |
| `test_cost_research.py` | The deterministic agent: unit rates read as data rows via `rowFields`, never paraphrased, never totalled |
| `test_tool_lambda.py` | The shipped `type: "lambda"` demo — Price List query shaping and unresolved-service reporting |
| `test_kb_retrieval.py` | The KB retrieve Lambda, and the asymmetry between its two filters, which is a security boundary. The filter an AGENT sends is one scalar (its corpus), because the generated Cedar permit is a scalar value match evaluated at the Gateway before this function runs; the rich multi-condition filter is set on the TARGET, invisible to the caller, and ANDed with the agent's so it can only narrow. Asserts the target filter applies when the agent sends nothing, that the two intersect rather than replace, that a caller sending a whole filter document has it treated as the corpus name it claims to be, and that a malformed target filter refuses to retrieve rather than dropping the scope |
| `test_lifecycle_research.py` | The shipped `type: "openapi"` demo, and its two date-reasoning traps: the catalogue returns releases newest-first, and `isMaintained` marks the maintenance track rather than current support. Neither produces a type error or an empty result — the asset is well-formed and every date in it real, while the sentences built from them are false. Fixtures are the real response shape for that reason |
| `test_memory_insight.py` | What long-term memory stores and recalls, and the on-topic filter that stops cross-subject bleed |
| `test_evaluations_gate.py` | `agentcore.evaluations.enabled` is enforced server-side on every path (REST route, assistant, auto-evaluate), not just by the UI hiding a button — a client-side gate is not enforcement, and the API is billed |
| `test_observability_honesty.py` | The observability plane must not present a guess as a measurement: an unrecognised model is marked `rates_known=False` rather than priced at a fallback, and `orchestrator.modelRates` lets a customer supply real rates |
| `test_runtime_invoke.py` | `InvokeAgentRuntime` is not retried, because it is not idempotent |
| `test_clock_parity.py` | `app/common/clock.py` and `bff/clock.py` agree, since the BFF ships as its own zip |

## Why these assertions

Most are regressions: each shipped at some point and none raised an error — they produced
a working deployment that was quietly wrong. A literal `"intake"` as `FIRST_AGENT_ID` that
dropped the brief when the first agent was renamed. `_extract_chunks` discarding the `url`
on every web result, so any link in the report was invented. `sectionType` as a closed
`Literal` that silently dropped an unexpected section. `assetType` and `sourceType` closed,
so a customer could not name their own kind of evidence without editing framework code.

The RBAC files are the other kind — not a past regression but a guard whose failure mode is
silent, because a rule that is correct and applied to five routes out of six looks exactly
like a rule that works. Every guard here was verified by removing it and confirming the
suite goes red.

## The import harness

`conftest.py` provides `workflow(defn)`, which sets `WORKFLOW_JSON` and re-imports the `app`
tree from scratch:

```python
with workflow(wf([{"agent": "triage"}, {"agent": "decide"}])) as imp:
    assert imp("app.common.config").FIRST_AGENT_ID == "triage"
```

`app/common/config.py` reads the workflow once at import, and other modules bind those
values (`graph_builder` imports `STEPS`, `registry` imports `AGENTS`). That is correct for a
container serving one workflow for its whole life, but it means a test cannot mutate config
in place. Re-importing is also what a customer does when they swap the file.

Synthetic agents in these tests use `runtime: "dedicated"` — a real placement that makes the
registry build an `AgentCoreRuntimeAgent` instead of importing `app.subagents.<id>`, so a
topology test can use any agent ids it likes without inventing packages on disk.

## Adding a test

Add it to the file matching the concern. For a new `tools` type, step shape or
`authorization` action, the corresponding table (`TOPOLOGIES`, `MUTATING_ROUTES`, the
`_REGISTRY` checks) is usually the only place needing a line.
