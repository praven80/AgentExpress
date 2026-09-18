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
`workflow.json` and `app/subagents/` and nothing else", and these tests are what
keep that promise honest.

## What each file covers

| File | Covers |
|---|---|
| `test_topology.py` | `step_agents`, `FIRST_AGENT_ID` / `LAST_AGENT_ID`, `upstream_of` across all three step shapes |
| `test_graph_build.py` | `build_graph` compiles for 14 topologies; the three HITL gate routers |
| `test_rerun_plan.py` | `rerun_plan` / `group_rerun_plan` — which node a rewind is attributed to |
| `test_branching.py` | `branch` — every operator and near-miss, the spec/topology validation, and a graph that is actually **invoked** to prove the chosen path ran and the other was marked skipped |
| `test_tool_calls.py` | `_tool_arguments` (the argument shape, per tool type), `_select_tool` (Gateway naming), `_extract_chunks` (evidence + citations) |
| `test_citations.py` | URL verification against the evidence; downgrading a `sourced-fact` that rested on an invented link |
| `test_contracts.py` | Open `assetType` / `sectionType`, the strict envelope, report-section ordering |
| `test_authz.py` | `bff/authz.py` semantics and claim shapes; the assistant's action-tool filtering |
| `test_bff_routes.py` | Each mutating BFF route actually returns 403 for a caller without the group |

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
like a rule that works. `test_bff_routes.py` drives all six routes, and each guard
was verified by removing it and confirming the suite goes red.

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
