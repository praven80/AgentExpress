# Multi-Agent Orchestrator (LangGraph + Amazon Bedrock AgentCore)

A production-style, **configuration-driven** multi-agent orchestration built with
**LangGraph** and deployed on **Amazon Bedrock AgentCore Runtime**. It runs a
small, generic **research → analysis → report** pipeline that demonstrates the
core patterns you need to build your own multi-agent system — sequential and
parallel execution, RAG, MCP tool access, human-in-the-loop review, durable
state — and wires up the **full AgentCore feature surface** (Runtime, Gateway,
Memory, Identity, Observability, Guardrails, Evaluations, Policy, Optimization),
all declared in one `workflow.json`.

This is a **reference pattern**, not a finished product for any one domain. The
sample topic (`"Design a serverless data pipeline on AWS"`) and the eight agents
are intentionally generic — swap the prompts, contracts, and Knowledge Base corpus
for your use case.

> ### ▶ Adopting this for your own use case?
> **Start with [`GETTING_STARTED.md`](GETTING_STARTED.md).** The surface you edit is:
>
> | You edit | For |
> |---|---|
> | `orchestrator/app/workflow.json` | agents, topology, tools, HITL gates, RBAC, guardrail policy, UI strings |
> | `orchestrator/app/subagents/<id>/` | one folder per agent — its prompt, its contract, its `run()` |
> | `orchestrator/kb_docs/` | your documents; each top-level folder becomes a corpus |
> | `terraform/terraform.tfvars` | deploy-time only: region, login, model, Gateway on/off (copy from `.example`) |
>
> Nothing else. No Terraform edits, no CDK edits, no Cedar policy to write, and no
> test to fix: both IaC paths generate the Gateway targets, the authorization rules
> and the Guardrail from your config, and the test suite derives its expectations
> from your `workflow.json` rather than naming this sample's agents. A mistake in
> `workflow.json` fails at `terraform plan` / `cdk synth` with a message naming the
> offending entry, rather than at deploy or at run time.
>
> Verified rather than asserted. Two checks are run against this repo: swapping in a
> different 4-agent workflow in another domain — renamed agents, a different topology,
> its own tool, guardrail and branding — and separately renaming a shipped agent by
> touching only `workflow.json` and its own folder. Both compile the graph and pass
> the full suite (214 Python + 127 TypeScript), `terraform validate` and `cdk synth`
> with no other change.
>
> Four couplings remain, none of which blocks a typical use case: the five tool
> *types* (`kb`/`websearch`/`mcp`/`openapi`/`lambda`) are code, all dedicated agents
> share one IAM role, KB retrieval parameters are fixed to `query` + `filter`, and the
> embedding model is Titan v2 / 1024 dims. The `lambda` type is the escape hatch for
> the rest: anything the Gateway cannot reach directly — a warehouse, an RDBMS, an
> internal service, a VPC resource — is reachable by pointing at a function.
>
> Your own **vocabulary** is not a coupling either. `assetType`, `sourceType` and
> `sectionType` are open strings, so a "claim-file" or a "policy-doc" survives into
> the UI instead of being coerced to `"other"`; the shared runners take your
> `instructions`; and which sections your terminal asset has lives with the agent that
> produces it, not in the shared contract.

## Patterns demonstrated

- **Sequential + parallel** execution — a **parallel** group (four agents run
  concurrently, join at one gate) and a **sequential** group (two agents run in
  order, one gate after both), side by side so the difference is explicit.
- **Five tool patterns, one config block** — every data source an agent can reach
  is declared in the `tools` block of `workflow.json`, and both IaC paths generate
  the Gateway target **and** the Cedar permit from it. The sample ships **four live
  side by side**, one per agent in the parallel research stage:
  **`kb`** (Bedrock Knowledge Base on **S3 Vectors**, corpus-scoped via metadata
  filtering), **`websearch`** (the AWS-managed **AgentCore Web Search** connector),
  **`mcp`** (**any** remote MCP server — swap the endpoint; the default is AWS's fully
  managed **AWS Knowledge MCP Server**, so there is nothing to deploy and no key to
  supply), and **`lambda`** (one of **your own functions**, which is how an agent
  reaches anything the Gateway cannot: a warehouse, an RDBMS, an internal service,
  a VPC resource). The fifth, **`openapi`**, fronts a REST API from a schema in S3.
  Adding a data source is a JSON edit: no HCL, no TypeScript, no policy to write.
- **Human-in-the-loop (HITL)** — approval gates with **approve / revise / deny**
  after a single agent, after the **parallel group** (revise re-runs only the
  agents you flag), and after the **sequential group** (revise re-runs the whole
  chain from the start).
- **Rewind & re-run** — after a run settles, re-run **any single agent** (plus
  everything downstream) or a **subset of one parallel stage**, with your note
  injected as that agent's feedback. Downstream gates re-pause, and every run is
  kept as a numbered **version** with the comment that triggered it.
- **Structured, validated output** — every agent emits a Pydantic contract asset
  with an evidence classification and claim tracing, so a reviewer can trust,
  exclude, or challenge each input.
- **Per-agent runtime placement** — each agent runs **in-process** in the
  orchestrator (`runtime: "main"`) or, by flipping one config field, in its **own
  dedicated AgentCore Runtime** (`runtime: "dedicated"`), invoked cross-runtime
  via `InvokeAgentRuntime` with the **trace context propagated** so a run is one
  distributed trace. In this sample the two research agents run on their own
  dedicated runtimes; the rest run in-process.
- **Configuration-driven identity** — one variable (`idp`) selects **Cognito**,
  **Auth0**, or **no login** for both auth boundaries: end-user **SPA login** for the
  UI (API Gateway JWT authorizer) and **machine-to-machine** (client-credentials)
  auth for agent → Gateway calls. Provider differences are isolated to
  `terraform/identity.tf`, one branch in the Gateway client, and one login strategy
  in the SPA.
- **RBAC on the human actions** — the `authorization` block maps JWT **groups** to the
  six mutating actions (approve/revise/deny, re-run, cancel, evaluate, insights,
  delete), so "logged in" and "may approve" are different things. Both IaC paths
  create the Cognito groups the block names, the UI disables what you can't use, and
  the in-app assistant is held to the same rules rather than becoming a way around
  them. An action you don't list stays open, so it is opt-in per action.
- **Run management** — stop/cancel a running pipeline (a durable cancel flag honored
  at every node boundary and heartbeat) and delete a previous run from the UI.
- **Durable state** — LangGraph checkpointing persisted in **AgentCore Memory**
  (HITL pause/resume across gates).
- **Live progress** — status mirrored to **DynamoDB**; the UI polls it for real-time per-agent status.
- **In-app assistant** — a chat widget that answers questions about your runs
  (status, cost, latency, outputs, guardrails, evals) and **takes actions**
  (approve a gate, re-run an agent, run an evaluation) through the same APIs the
  buttons use. A Bedrock Converse tool-use loop in the BFF; each capability is a
  config flag.
- **Consistent time** — all stored and displayed timestamps are US Eastern Time in
  `YYYY-MM-DD HH:MM:SS`, via a single dependency-free helper (`app/common/clock.py`).

## The AgentCore feature surface

Every capability is **declared per agent** in `workflow.json` under an
`agentcore` block and applied by the framework — so turning one on for an agent
is a config change, never a code change. Each lives in its own folder under
[`orchestrator/app/features/`](orchestrator/app/features/) with a README-style
module docstring, so you can read, keep, or delete them one at a time.

| Feature | What it does here | Config key | Code |
|---|---|---|---|
| **Runtime** | Hosts the orchestrator and each `dedicated` agent; async tasks keep the session alive across HITL pauses (8h limit) | `runtime` | `app/orchestrator/runtime.py`, `app/subagent_runtime.py` |
| **Gateway** | One OAuth-authed MCP endpoint fronting all tools (remote MCP server + KB retrieve Lambda) | the agent's `tool` + the generated Cedar permit | `app/features/gateway/` |
| **Memory** | Short-term = the LangGraph checkpointer. **Long-term** = semantic + summary strategies; the framework recalls an agent's past insights before it runs (auto-injected into its prompt) and stores its output after, namespaced per agent **and per subject** | `memory.longTerm` (short-term is deployment-wide) | `app/features/memory/` |
| **Identity** | Inbound identity (the configured IdP's JWT at the API/Gateway) and **outbound** Workload Identity tokens for calling an external API directly | `identity.inbound`, `identity.outbound` | `app/features/identity/` |
| **Observability** | OTEL session grouping, a readable AGENT span per agent with real token counts, per-tool spans, cross-runtime trace propagation, plus full metering of every model/tool/memory/guardrail/policy/eval event | always on | `app/features/observability/` |
| **Guardrails** | Bedrock `ApplyGuardrail` on agent **input** and/or **output**; a block halts the run with the guardrail's message and is recorded | `guardrails.input`, `guardrails.output` | `app/features/guardrails/` |
| **Evaluations** | LLM-as-judge over the agent's **real** run, scored **per named prompt per version**. `auto` scores at run completion; the UI's Evaluate button re-runs on demand | `evaluations.{enabled,auto,evaluators}` | `app/features/evaluations/` |
| **Policy** | Cedar authorization **enforced server-side at the Gateway** (default-deny in `ENFORCE`, or `LOG_ONLY`). The sample rule governs which KB corpora may be retrieved | `orchestrator.policy`, `policy.enabled` | `app/features/policy/`, `terraform/policy.tf` |
| **Optimization** | Cross-run **Insights**: a batch evaluation over recent traces surfacing failure patterns, user intents and execution summaries, in an Insights tab | (app-wide) | `app/features/optimization/` |

**Observability, in detail.** Every model / tool / memory / guardrail / policy /
eval / compute event is metered (exact Bedrock tokens via `CountTokens`, latency,
estimated USD) to a telemetry table. The in-app **Observability** tab gives you:

- **Overview** — aggregate by date / model / user over a range, with cost
  projection and CSV/JSON export.
- **Run detail** — per-agent cost/latency/tokens, expandable to individual calls,
  plus a **Prompts & I/O inspector** per agent showing the exact system prompt,
  input and output of every model call, every tool query and its retrieved
  context, every memory recall/store (with its namespace), every guardrail check
  and policy decision — **grouped by run version**, with the reviewer feedback
  that triggered each version, and the **evaluation scores** for each prompt.
- **Insights** — the cross-run batch analysis described above.

It stays isolated in `app/features/observability/` +
`terraform/observability.tf` + `web/observability.js`, so it can be understood or
removed as a unit.

## The workflow

A 4-stage, 6-agent pipeline, defined entirely in
[`orchestrator/app/workflow.json`](orchestrator/app/workflow.json). It
deliberately contrasts a **parallel** group with a **sequential** group:

```
1 Intake ─(HITL)─▶ 2 [ knowledge_research ‖ web_search ‖ documentation_search ]
                     ─(HITL)─▶ 3 [ analysis → recommendation ] ─(HITL)─▶ 4 Report
```

Agents are keyed by **semantic ids** in `workflow.json` (e.g. `intake`,
`analysis`); each has a self-contained package under `app/subagents/<id>/`.

| # | Stage | Agent id(s) | Pattern | Notes |
|---|-------|-------------|---------|-------|
| 1 | Request Intake | `intake` | single | LLM turns the request into a structured brief; HITL gate after |
| 2 | Research | `knowledge_research`, `web_search`, `documentation_search` | **parallel** (**RAG** ‖ **Web Search** ‖ **MCP**), each on a **dedicated runtime** | run concurrently in their own AgentCore runtimes — one per tool pattern; one HITL group gate after all three |
| 3 | Analysis → Recommendation | `analysis`, `recommendation` | **sequential** | run one after another; one HITL gate after both complete (revise re-runs the whole chain) |
| 4 | Report | `report` | terminal | assembles the final sectioned report (no gate) |

Each agent entry binds to a data source with one field — `tool` (a key in the
`tools` block) plus `corpus` for a Knowledge Base tool — and carries declarative
`sources` / `access` / `produces` metadata surfaced as chips in the UI, plus the
`agentcore` block that switches its features on. The eight agents deliberately use
**different** combinations so one deployment exercises the whole surface:

| Agent | Runtime | Tool | Long-term memory | Guardrails | Evaluations | Policy |
|---|---|---|---|---|---|---|
| `intake` | main | — | — | input | auto | — |
| `knowledge_research` | dedicated | `kb` (corpus `reference`) | — | — | on demand | **enabled** |
| `web_search` | dedicated | `websearch` | — | output | on demand | **enabled** |
| `documentation_search` | dedicated | `docs` (MCP) | — | output | on demand | **enabled** |
| `history_research` | main | `runs` (**your own Lambda**) | — | — | on demand | **enabled** |
| `analysis` | main | — | semantic | output | auto | — |
| `recommendation` | main | — | semantic + summary | output | auto | — |
| `report` | main | — | — | input + output | auto | — |

## Architecture

![Architecture diagram](architecture.png)

> **Editing the diagram.** [`architecture.drawio`](architecture.drawio) is the
> editable source of truth — open it at [diagrams.net](https://app.diagrams.net)
> (or the VS Code Draw.io Integration extension) and re-export to
> `architecture.png` after any change. Keep the two in sync: the `.png` is only a
> render. The `.drawio` reflects the full feature surface (both Memory resources,
> Guardrails, the Cedar policy engine on the Gateway, the telemetry/insights
> tables, and the Evaluations/Insights quality loop).

```
                    ┌── IdP ─────┐  (SPA login for users; M2M tokens for the runtime)
                    │  Cognito / │  selected by `idp`; Auth0 and "no login"
                    │  Auth0     │  are drop-in alternatives
                    ▼            ▼
Browser ─▶ CloudFront ─┬─▶ S3 (static UI)
   (Bearer JWT)        └─▶ API Gateway (JWT authorizer) ─▶ Lambda (BFF)
                             │                               (reads DynamoDB status)
                             │ InvokeAgentRuntime
                             ▼
                     AgentCore Runtime (orchestrator)  ──▶ AgentCore Memory
                     LangGraph: 1→[2‖2‖2]→[3→3]→4          ├─ checkpointer (HITL pause/resume)
                       │                                   └─ long-term semantic + summary
                       │  └─ InvokeAgentRuntime ─▶ Dedicated runtimes (one per research agent)
                       │       (+ trace context)
                       │ M2M client-credentials token          │
                       ▼                                       ▼
                     AgentCore Gateway (CUSTOM_JWT / the IdP)  DynamoDB (status + events
                       │  └─ Cedar Policy Engine (ENFORCE)      + telemetry + insights)
                       │     (permits generated from the `tools` block)
                       ├─ target: KB retrieve Lambda ▶ Bedrock KB (S3 Vectors)   type=kb
                       ├─ target: AgentCore Web Search (managed connector)       type=websearch
                       └─ target: your MCP server / REST API                     type=mcp | openapi
                       │
                       ├─▶ Bedrock Guardrails (ApplyGuardrail, per agent in/out)
                       ├─▶ AgentCore Evaluations (LLM-as-judge over the real run)
                       └─▶ CloudWatch (OTEL spans, Transaction Search) ─▶ AgentCore Insights

   UI polls status ◀── DynamoDB ◀── runtime writes progress
```

- **Orchestrator runtime** runs the LangGraph pipeline. On invoke it registers an
  async task, runs the workflow in the background, and returns immediately — so
  the session stays alive across HITL pauses (up to the 8-hour session limit).
- **Gateway** is a single OAuth-authed MCP endpoint fronting the whole tool plane:
  a Bedrock KB retrieve Lambda, the managed AgentCore Web Search connector, and a
  remote MCP server (the AWS-managed **AWS Knowledge MCP Server** by default —
  point it at your own by changing one field).
- **BFF (Lambda)** invokes the runtime, and serves DynamoDB-backed status + the
  workflow definition so the UI renders dynamically. `/api/*` is protected by the
  JWT authorizer for the configured IdP.
- **UI** is static (S3/CloudFront): logs in via the configured IdP, renders the DAG from
  `/api/workflow`, drives the HITL gates, and polls for progress.

## Repository layout

```
orchestrator/
├── app/                        # application package
│   ├── workflow.json           # SINGLE SOURCE OF TRUTH: orchestrator + ui + guardrail blocks,
│   │                           #   tools, agents (incl. their agentcore features),
│   │                           #   topology + HITL gates
│   ├── entry.py                # container entrypoint: AGENT_ID set -> a dedicated agent; unset -> orchestrator
│   ├── subagent_runtime.py     # per-agent AgentCore Runtime app (hosts one agent by AGENT_ID)
│   ├── common/                 # shared framework used by orchestrator + agents
│   │   ├── base.py             #   Agent base class (author contract)
│   │   ├── context.py          #   AgentContext: input(), llm(), mcp(), retrieve(), heartbeat(), log(),
│   │   │                       #     + the AgentCore feature helpers (guardrail, memory_*, identity, policy)
│   │   ├── config.py           #   loads workflow.json + env/infra settings
│   │   ├── state.py            #   graph state + reducers
│   │   ├── agentcore_agent.py  #   AgentCoreRuntimeAgent: node body for a dedicated agent (InvokeAgentRuntime)
│   │   ├── research.py         #   shared runner for agents that gather evidence from a tool
│   │   ├── synthesis.py        #   shared runner for agents that reason over upstream assets
│   │   ├── assets.py           #   asset plumbing BOTH runners use (brief, versioning, JSON repair, provenance)
│   │   ├── contracts/          #   Pydantic asset contracts (brief, research, analysis, recommendation, report)
│   │   ├── clock.py            #   Eastern-Time helper (all timestamps: YYYY-MM-DD HH:MM:SS ET)
│   │   └── llm.py, sink.py, bus.py           # infra helpers
│   ├── features/               # ONE FOLDER PER AGENTCORE CAPABILITY — config-driven, independently removable
│   │   ├── gateway/            #   MCP tool access (M2M token per IdP + Streamable HTTP)
│   │   ├── memory/             #   long-term semantic recall + store
│   │   ├── identity/           #   Workload Identity outbound OAuth tokens
│   │   ├── observability/      #   OTEL spans + metering + pricing (see terraform/observability.tf, web/observability.js)
│   │   ├── guardrails/         #   Bedrock ApplyGuardrail on input/output
│   │   ├── evaluations/        #   LLM-as-judge over real runs, per prompt per version
│   │   ├── policy/             #   Cedar authorization (enforced at the Gateway)
│   │   └── optimization/       #   cross-run Insights (batch evaluation)
│   ├── orchestrator/           # the orchestration engine + entrypoints
│   │   ├── graph_builder.py    #   builds the LangGraph (single / parallel / sequence steps + their HITL gates)
│   │   │                       #   + rerun_plan / group_rerun_plan (rewind planning)
│   │   ├── nodes.py            #   generic agent-node (spans, tokens, guardrails, memory) + HITL-gate wrappers
│   │   ├── registry.py         #   node factory: main (in-process module) vs dedicated (AgentCoreRuntimeAgent)
│   │   ├── runtime.py          #   AgentCore entrypoint (start / resume / rerun_from / evaluate / insights)
│   │   └── server.py           #   local dev server (same API + UI)
│   └── subagents/<name>/       # one self-contained package per agent (agent.py + prompts.py + __init__.py):
│                               #   intake, knowledge_research, web_search, documentation_search,
│                               #   analysis, recommendation, report
├── web/index.html              # config-driven UI: pluggable login, DAG, HITL gates, outputs, rerun, assistant
├── web/observability.js        # self-contained Observability tab (charts, drilldown, Prompts & I/O
│                               #   inspector, evaluation scores, Insights, export)
├── bff/handler.py              # Lambda BFF (sessions CRUD + decisions + cancel + rerun + evaluate
│                               #   + insights + telemetry reads + /api/me)
├── bff/authz.py                # RBAC: JWT groups -> which run actions a caller may take
│                               #   (from workflow.json `authorization`); gates the mutating routes
├── bff/chatbot.py              # in-app assistant: Bedrock Converse tool-use loop (config-driven tools,
│                               #   action tools withheld from callers authz denies)
├── kb_lambda/handler.py        # Gateway Lambda target: Bedrock KB retrieve
├── tool_lambda/handler.py      # the built-in `type: "lambda"` demo function (source: "tool_lambda"):
│                               #   answers from this deployment's own DynamoDB run history
├── format_workflow.py          # reformat app/workflow.json for reading (--check for CI)
├── docs/WORKFLOW_REFERENCE.md  # every workflow.json key, what reads it, what it does
├── kb_docs/reference/          # sample Knowledge Base corpus (replace with your own)
├── tests/                      # config-plane test suite (pytest; no AWS, no model, ~1s)
│                               #   topology, graph build, rewind plans, tool call shapes,
│                               #   citation verification, contracts, RBAC — see tests/README.md
├── pytest.ini, Dockerfile, requirements.txt, requirements-dev.txt
├── cdk/                        # CDK / TypeScript IaC (full parity; alternative to terraform/)
│   ├── bin/orchestrator.ts     #   app entrypoint (context: agentName, modelId, idp, …)
│   ├── lib/orchestrator-stack.ts  # the stack (runtimes, DynamoDB, BFF/API, S3+CloudFront UI)
│   ├── lib/tool-plane.ts       #   Gateway + Knowledge Base + Cedar policy subsystem
│   ├── test/                   #   config-plane tests (jest): projections, validators,
│   │                           #     Terraform↔CDK parity, synthesized template
│   └── package.json, cdk.json, tsconfig.json, jest.config.js, README.md
└── terraform/                  # full IaC (adds the Gateway, Knowledge Base and every AgentCore feature)
    ├── main.tf                 # orchestrator runtime, BOTH memories (checkpointer + long-term semantic),
    │                           #   ECR + image build/push, IAM (incl. Evaluations + Guardrails), OTEL env
    ├── subagent_runtimes.tf    # one AgentCore Runtime per dedicated agent (the 3 research agents)
    ├── identity.tf             # IdP abstraction: one place that knows how Cognito/Auth0/none differ
    ├── cognito.tf              # Cognito User Pool + domain + SPA/M2M clients (idp=cognito, create=true)
    ├── gateway.tf              # AgentCore Gateway + CUSTOM_JWT authorizer + policy attachment
    ├── tools.tf                # GENERATES every Gateway target + Cedar permit from workflow.json `tools`
    ├── kb.tf                   # S3 Vectors + Bedrock KB + retrieve Lambda (when a tool declares type=kb)
    ├── guardrail.tf            # Bedrock Guardrail (the GUARDRAIL_ID injected into every runtime)
    ├── policy.tf               # Cedar policy engine; the rules come from tools.tf (nothing hand-written)
    ├── optimization.tf         # Insights findings table + batch-evaluation IAM
    ├── transaction_search.tf   # CloudWatch Transaction Search (idempotent; required for spans/Insights)
    ├── observability.tf        # telemetry DynamoDB table (+ by_date GSI) + IAM + observability.js UI asset
    ├── bff.tf, ui.tf, dynamo.tf, variables.tf, outputs.tf, versions.tf
    └── deploy-role-policy.json # scoped IAM policy a deploy role needs
```

## Add or change an agent

An agent's `workflow.json` key IS its module name (the package under
`app/subagents/<id>/`); a `runtime` field decides *where* it runs. Both modes use
the same `Agent` interface, so the graph wiring is identical.

1. Create `orchestrator/app/subagents/<id>/agent.py` (and a `prompts.py`):
   ```python
   from app.common.base import Agent

   class MyAgent(Agent):
       system_prompt = "You are ..."
       async def run(self, ctx):
           return await ctx.llm(self.system_prompt, ctx.input("analysis") or ctx.topic)

   agent = MyAgent()
   ```
   and `orchestrator/app/subagents/<id>/__init__.py` with `from .agent import agent`.
2. Add it to `orchestrator/app/workflow.json` under `agents` and place it in `steps`
   (a single step, in a `parallel` group, in a `sequence` group, and/or with
   `"hitl": true`).
   - **In-process** (default): `"runtime": "main"`.
   - **Own runtime**: `"runtime": "dedicated"` — Terraform provisions a separate
     AgentCore Runtime for it automatically (it reads `workflow.json`).
3. Give it an `agentcore` block to switch on the capabilities it needs — nothing
   else to write, the framework applies them around your `run()`:
   ```json
   "agentcore": {
     "memory":      { "longTerm": ["semantic"] },
     "identity":    { "outbound": ["my-api-oauth"] },
     "guardrails":  { "input": true, "output": true },
     "evaluations": { "enabled": true, "auto": true,
                      "evaluators": ["Builtin.Faithfulness"] },
     "policy":      { "enabled": true }
   }
   ```
   Omit the block entirely and the agent simply runs with no features attached.
4. Redeploy. The graph, dedicated runtimes, status tracking, features, and UI pick
   it up automatically.

Inside `run()` you may also call the feature helpers directly —
`ctx.guardrail(text, "INPUT")`, `ctx.memory_recall(q)`, `ctx.memory_store(text)`,
`ctx.get_identity_token(provider)`, `ctx.policy_check(action)`. Each is a **no-op
when disabled** for that agent, so they are always safe to call. Guardrails and
long-term memory are already applied automatically by the node wrapper, so you
only need the explicit calls for extra checks (see
`app/subagents/knowledge_research/agent.py` for the `policy_check` example).

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r orchestrator/requirements-dev.txt

cd orchestrator
uvicorn app.orchestrator.server:app --port 8090
# open http://127.0.0.1:8090
```

> **There is no offline mode, by design.** This framework never fabricates data: a
> failed model call raises `ModelUnavailable` and an unreachable tool raises
> `ToolUnavailable`, so the run fails with the reason on the agent that failed. A
> simulated answer is indistinguishable from real evidence once it reaches the
> report, which is a far worse failure than an error. See
> [`app/common/errors.py`](orchestrator/app/common/errors.py).

Notes:
- Local dev needs **working AWS credentials** with Bedrock model access. Without
  them the run stops at the first agent with a precise reason.
- Agents bound to a `tool` need a deployed Gateway. Remove the `tool` binding to run
  a purely-reasoning subset locally.
- `dedicated` agents return a "not configured" placeholder locally — there are no
  dedicated runtimes to invoke off-cloud; they run for real once deployed.

## Deploy

Two infrastructure-as-code options are provided. They can target **different
accounts or regions** simultaneously (resource names include the account ID, so
there are no global collisions):

- **Terraform** (`orchestrator/terraform/`) — see **[`DEPLOYMENT.md`](DEPLOYMENT.md)**.
- **CDK / TypeScript** (`orchestrator/cdk/`) — see
  **[`orchestrator/cdk/README.md`](orchestrator/cdk/README.md)**.

Both provision the **same** resources and all nine AgentCore capabilities, including
the **AgentCore Gateway + Bedrock Knowledge Base** for live MCP + RAG and the Cedar
policy engine. `enable_gateway` / `-c enableGateway` controls the tool plane: with it
off, no Gateway is created and any agent bound to a `tool` fails fast rather than
inventing evidence.

Both take the same **`idp`** switch — `cognito`, `auth0`, or `none` — and both can
**auto-create Cognito** (User Pool + domain + SPA client), so no pre-existing auth
infrastructure is required.

### Terraform

> To point the framework at your own documents, MCP servers or REST APIs, edit the
> `tools` block in `orchestrator/app/workflow.json` — both IaC paths read it. See
> **[`GETTING_STARTED.md`](GETTING_STARTED.md)**.

Requires: Terraform, a container engine (Finch/Docker/Podman), and AWS credentials.

```bash
cd orchestrator/terraform
cp terraform.tfvars.example terraform.tfvars   # then edit
# Set: region, idp = "cognito", cognito = { create = true }, enable_gateway = false
terraform init                                 # first time only
terraform apply                                # builds the image, provisions everything
```

| Login mode | Configuration in `terraform.tfvars` |
|---|---|
| **Cognito, created for you** (recommended) | `idp = "cognito"`, `cognito = { create = true }` |
| **Cognito, bring your own** | `idp = "cognito"`, `cognito = { create = false, user_pool_id = …, client_id = …, domain_prefix = … }` |
| **Auth0** | `idp = "auth0"`, `auth0 = { domain = …, client_id = … }` |
| **No login** (sandbox only) | `idp = "none"`, `enable_gateway = false` |

Outputs include `ui_url` (CloudFront), `api_endpoint`, `cognito_user_pool_id`, and
`cognito_client_id`. After deploy, add `ui_url` to the Cognito App Client's Allowed
Callback/Sign-out URLs. Tear down with `terraform destroy`.

### CDK (TypeScript)

```bash
cd orchestrator/cdk
npm install
export CDK_DOCKER=finch          # only if you don't have Docker
cdk bootstrap                    # first time per account/region
# Core footprint (no Gateway; agents bound to a tool will fail fast):
cdk deploy -c idp=cognito -c createCognito=true

# Full surface — adds Gateway + Knowledge Base + Cedar policy:
cdk deploy -c idp=cognito -c createCognito=true -c enableGateway=true
```

| Login mode | Command |
|---|---|
| **Cognito, created for you** (recommended) | `cdk deploy -c idp=cognito -c createCognito=true` |
| **Cognito, bring your own** | `cdk deploy -c idp=cognito -c cognitoUserPoolId=… -c cognitoClientId=… -c cognitoDomainPrefix=…` |
| **Auth0** | `cdk deploy -c idp=auth0 -c auth0Domain=… -c auth0ClientId=…` |
| **No login** (sandbox only) | `cdk deploy -c idp=none` |

After deploy, add the `uiUrl` output to Cognito's Allowed Callback/Sign-out URLs.
Tear down with `cdk destroy`. Full details in
[`orchestrator/cdk/README.md`](orchestrator/cdk/README.md).

## Configuration

| Variable / env var             | Default                                          | Purpose                                       |
|--------------------------------|--------------------------------------------------|-----------------------------------------------|
| `model_id` / `BEDROCK_MODEL_ID`| `us.anthropic.claude-haiku-4-5-20251001-v1:0`    | Default model (env > `orchestrator.defaultModel` > this) |
| `region` / `AWS_REGION`        | `us-east-1`                                      | Region                                        |
| `idp`                          | `cognito`                                        | Identity provider: `cognito`, `auth0`, or `none` (no login) |
| `enable_gateway`               | `true`                                           | Create the Gateway + KB (OAuth-authed)        |
| `cognito`                      | `{ create = true }`                              | Cognito settings: `create`, `user_pool_id`, `client_id`, `domain_prefix` |
| `auth0`                        | `{}`                                             | Auth0 settings: `domain`, `client_id` (SPA application) |
| `gateway_identity`             | `{}`                                             | M2M client for agent → Gateway: `client_id`, `audience` (OAuth2 scope for Cognito / API identifier for Auth0) |
| `gateway_client_secret`        | — (via `TF_VAR_…`)                               | M2M client secret (never committed); unneeded when Terraform creates the Cognito client |
| `tool_api_keys`                | `{}` (via `TF_VAR_…`)                            | API keys for tools that need one, keyed by the `tools` name (never committed) |
| `transaction_search_indexing_percentage` | `100`                                  | Span indexing % for CloudWatch Transaction Search (1% is free; required for Insights) |

The **workflow itself** lives in `workflow.json`, not here: the `tools` block (your
data sources), the `agents` block (including each agent's `agentcore` feature
flags), the `steps` topology, `authorization` (which groups may approve, re-run,
cancel, evaluate or delete), `ui` (branding strings), `guardrail` (the content-safety
policy), `orchestrator.policy` (Cedar on/off + `ENFORCE`/`LOG_ONLY`) and
`orchestrator.chatbot`. **Both** Terraform and CDK read that file, so anything needing
infrastructure — a Gateway target, a Cedar permit, a dedicated runtime, a Cognito group
— is provisioned from the same source of truth either way.

The table above is for the **Terraform** path. The **CDK** app exposes the same
settings as context keys (`agentName`, `modelId`, `idp`, `createCognito`,
`cognitoUserPoolId`, `cognitoClientId`, `cognitoDomainPrefix`, `auth0Domain`,
`auth0ClientId`, `memoryEventExpiryDays`, `enableGateway`, `gatewayClientId`,
`gatewayAudience`, `transactionSearchIndexingPercentage`) — pass them via `cdk.json`
or `-c key=value`, with secrets in `$GATEWAY_CLIENT_SECRET` and `$TOOL_API_KEYS`. See
[`orchestrator/cdk/README.md`](orchestrator/cdk/README.md).

> **CDK covers all nine capabilities**, the same as Terraform: Runtime, Gateway,
> Memory (both the checkpointer and the long-term store), Identity, Observability,
> Guardrails, Policy, Evaluations and Insights — plus Transaction Search, which
> Insights needs to find traces. Two deliberate implementation differences: the
> container image is a CDK `DockerImageAsset` rather than a per-stack ECR repository,
> and Transaction Search is enabled through an idempotent custom resource rather than
> `AWS::XRay::TransactionSearchConfig` (an account-wide singleton that fails with
> `AlreadyExists` where it is already on).

## Security, cost & scalability

This is a **reference sample**, not a production-hardened product. It demonstrates
sound patterns and sensible defaults, but you must review and harden it for your
own environment before any non-sandbox use.

**Security — what's built in**
- **SPA login** on the UI (Cognito or Auth0) and an **API Gateway JWT authorizer** on `/api/*`.
- **Machine-to-machine** (client-credentials) tokens for agent → Gateway
  calls; the Gateway uses a **CUSTOM_JWT** authorizer that pins the client via `client_id`.
- **Cedar policy** enforced server-side at the Gateway (default-deny in `ENFORCE`),
  so a prompt-injected attempt to widen tool access is refused by infrastructure
  rather than by prompt wording.
- **RBAC on run actions** (`authorization` in `workflow.json`, enforced in
  `bff/authz.py`): approve/revise/deny, re-run, cancel, evaluate, insights and delete
  each require a group in the caller's JWT. The in-app assistant is held to the same
  rules — the action tools it isn't allowed to use are withheld from the model, so it
  cannot be asked to approve something on your behalf.
- **Bedrock Guardrails** on agent input and output (content filters, denied
  topics/words, PII block/anonymize), applied by the framework per agent.
- **SigV4/IAM** for internal service-to-service calls (BFF → runtime).
- **Scoped IAM roles** per component (orchestrator, dedicated agents, BFF, KB Lambda, Gateway).
- No secrets in the repo; the M2M secret is passed only via `TF_VAR_…` at apply time.

**Security — harden before production**
- **Auth can be turned off.** `idp = "none"` deploys the UI and `/api/*` **open** to
  anyone with the URL. Do not run that mode anywhere shared.
- **RBAC covers actions, not run ownership.** The `authorization` block decides who may
  approve, re-run, cancel, evaluate or delete. It does **not** partition runs: every
  authenticated user can start a run and read every other run's inputs, outputs and
  telemetry. Add a per-owner/per-tenant filter if that matters.
- No **WAF**, request throttling, or usage plans on API Gateway.
- CloudFront uses the **default certificate** (no custom domain/TLS policy hardening).
- The Gateway client secret is delivered as a **runtime environment variable**;
  prefer **AWS Secrets Manager** with rotation for production.
- Buckets use `force_destroy = true` (convenient for teardown; enable versioning +
  retention for real data). Set **log retention** and **CloudWatch alarms** as needed.
- The shipped **guardrail and Cedar policy are samples.** Replace the filters,
  denied topics and the permitted-corpus list in `terraform/guardrail.tf` /
  `terraform/policy.tf` with your own domain's rules before real use, and enable
  guardrails on every agent that handles untrusted text.
- **Telemetry captures prompt and output content** (that's what makes the Prompts
  & I/O inspector useful). If your inputs contain sensitive data, shorten
  `OBS_MAX_CAPTURE_CHARS`, enable the table's TTL, and restrict who can read the
  telemetry table and the Observability tab.
- The **in-app assistant can take actions** (approve a gate, re-run an agent). It is
  scoped to this app, gated by the same JWT as the rest of `/api/*`, and now held to the
  same `authorization` rules as the buttons. If you would rather it never acted at all,
  turn off the action tools in `workflow.json` (`orchestrator.chatbot.tools`).

**Cost — optimized for a sample**
- Serverless and **scale-to-zero** throughout: AgentCore runtimes, Lambda (BFF/KB),
  DynamoDB (**PAY_PER_REQUEST**), and **S3 Vectors** (no vector-store floor).
- **Claude Haiku** is the default model (cheap); per-agent model is configurable.
- Telemetry rows carry a **TTL**; the in-app Observability tab shows real per-run cost.
- Cost knobs: CloudFront `PriceClass_All` (narrow it), the two dedicated runtimes add
  a little vs. all-in-process, and KB ingestion + embeddings when the Gateway is enabled.
- **The optional features cost money too.** Evaluations run an LLM judge per
  evaluator per prompt (set `auto: false` to make it on-demand), Insights runs a
  batch evaluation over a whole window, guardrails are billed per text unit, and
  Transaction Search charges for indexed spans above the free 1%. All of them are
  metered into the same telemetry table, so the Observability tab shows what they
  actually cost you.

**Scalability**
- LangGraph on AgentCore runs stages **sequentially/parallel** per config, with the
  two research agents on **independent dedicated runtimes**; the orchestrator runs the
  workflow as a background task and persists across HITL pauses (up to the 8-hour limit).
- Known limits (documented below): the UI uses **polling** (not push), and the
  cross-runtime call to a dedicated agent is **synchronous** request/response.

## Notes / current limitations

- **Per-agent runtimes are supported** — flip an agent to `runtime: "dedicated"`
  in `workflow.json` to give it its own AgentCore Runtime (Terraform provisions it
  automatically). The cross-runtime call is synchronous request/response.
- **Two authorization layers, two subjects.** Cedar at the Gateway authorizes *agents
  calling tools*; `authorization` in `workflow.json` authorizes *humans acting on runs*.
  Neither partitions run *visibility* — that is still a future enhancement.
- **UI transport is polling** (DynamoDB-backed). Swap to DynamoDB Streams →
  WebSocket/AppSync for instant push without changing the runtime.
- **Parallel-group HITL** — the gate after the parallel group carries a per-agent
  decision; "revise" re-runs only the agents you flag.
- **Multi-agent re-run is scoped to one parallel stage.** Re-running several
  agents at once requires them to be in the same gated `parallel` step (so the
  group gate can re-review them together); any single agent can be re-run on its
  own, including one in the middle of a `sequence`.
- **Evaluations and Insights need a deployed runtime.** Both read the AgentCore
  Evaluate / BatchEvaluation APIs and CloudWatch traces, so locally they return an
  explicit "not available" note rather than scores. Insights additionally needs
  Transaction Search enabled (Terraform does this) and at least one completed run.
- **Evaluation scores are LLM-as-judge**, i.e. indicative rather than ground
  truth. Treat them as a regression signal across versions, not an absolute grade.
- **Costs shown are estimates** from a hand-maintained price book
  (`app/features/observability/pricing.py`); they will not match your bill to the
  cent. Update the constants there when prices change or you have negotiated rates.
