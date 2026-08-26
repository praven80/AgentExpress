# Architecture

Generic multi-agent orchestrator (LangGraph + Amazon Bedrock AgentCore), with
**Cognito** identity at the front door and an **AgentCore Gateway** fronting the
MCP/tool plane. This describes what is actually deployed.

> **Infrastructure as code — two options.** The full architecture below is
> provisioned by the **Terraform** config (`orchestrator/terraform/`). A **CDK /
> TypeScript** app (`orchestrator/cdk/`) provisions the same **core** footprint
> minus the AgentCore Gateway + Bedrock Knowledge Base — i.e. everything except the
> shaded "Gateway / KB" path; with those absent, MCP and RAG run simulated while LLM
> inference stays real. Both build the same container image and create the
> orchestrator plus one dedicated runtime per `dedicated` agent.
>
> **Both options can auto-create Cognito** (`create_cognito = true` in Terraform,
> `-c createCognito=true` in CDK) so you don't need a pre-existing User Pool. They
> can safely target **different accounts or regions** simultaneously (e.g. CDK →
> us-east-1, Terraform → us-west-2) since resource names include the account ID.

- [1. Deployed architecture](#1-deployed-architecture)
- [2. Where each concern lives](#2-where-each-concern-lives)
- [3. Key decisions](#3-key-decisions)
- [4. Future enhancements](#4-future-enhancements)

---

## 1. Deployed architecture

```
                              ┌─────────────┐
                              │   Cognito   │  one User Pool, two app clients:
                              │ (OIDC/JWT)  │   • Public client → user login (UI)
                              └──┬───────┬──┘   • Confidential client → agent → Gateway
                    user login   │       │  client-credentials
                                 ▼       │
                        ┌────────────────┴───┐
                        │   Browser (SPA)     │  Cognito Hosted UI; sends Bearer ID token
                        └─────────┬───────────┘
                                  │  HTTPS, Authorization: Bearer <JWT>
                                  ▼
                        ┌──────────────────────┐
                        │      CloudFront       │  /      → S3 (static UI)
                        │                       │  /api/* → API Gateway
                        └───────────┬───────────┘
                                    ▼
                        ┌──────────────────────────┐
                        │      Lambda BFF           │  (reads the DynamoDB status store
                        │  sessions + status +      │   to serve live status to the UI)
                        │  decisions + telemetry    │
                        └───────────┬───────────────┘
              SigV4 InvokeAgentRuntime │
                                    ▼
                        ┌──────────────────────────┐  checkpoint      ┌───────────────────────────┐
                        │  AgentCore Runtime        │─────────────────▶│ AgentCore Memory          │
                        │  (orchestrator)           │  (graph state)   │ (LangGraph checkpointer;  │
                        │  LangGraph                │                  │  HITL pause / resume)     │
                        │  1→[2‖2]→[3→3]→4          │                  └───────────────────────────┘
                        │                           │  write progress  ┌───────────────────────────┐
                        │                           │─────────────────▶│ DynamoDB (status + events)│
                        └─┬─────────┬───────────┬─┘                  │ live progress; BFF reads  │
                          │         │           │                    └───────────────────────────┘
             InvokeModel  │         │ InvokeAgentRuntime (dedicated agents)
                          │         ▼
                          │  ┌───────────────────────────────────────┐
                          │  │ Dedicated AgentCore Runtimes (per agent)│
                          │  │  knowledge_research  ‖  web_research    │
                          │  └───────────────┬───────────────────────┘
                          │      (also reach the Gateway) │ Cognito M2M token
                          ▼                               ▼
                     ┌──────────┐ ┌─────────────────────────────────┐
                     │ Bedrock  │ │  AgentCore Gateway (MCP)         │
                     │ (Claude) │ │  CUSTOM_JWT authorizer (Cognito) │
                     └──────────┘ └───────────┬─────────────────────┘
                                              │
                                  ┌───────────┴───────────┐
                                  ▼                       ▼
                        ┌────────────────────┐  ┌─────────────────────────┐
                        │ target: knowledge  │  │ target: kb (Lambda)     │
                        │ AWS Knowledge MCP  │  │ retrieve ▶ Bedrock KB   │
                        │ (web_research)     │  │ (S3 Vectors)            │
                        └────────────────────┘  │ (knowledge_research)    │
                                                 └─────────────────────────┘
```

Workflow (4 stages, 6 agents) — a parallel group next to a sequential group:
`1 Intake ─(HITL)▶ 2 [knowledge_research ‖ web_research] ─(HITL)▶
3 [analysis → recommendation] ─(HITL)▶ 4 Report`.
The runtime writes to two independent stores (there is no Memory→DynamoDB flow):
durable graph state to **AgentCore Memory** (the LangGraph checkpointer, for HITL
pause/resume) and live per-session progress to **DynamoDB** (status + events),
which the BFF reads for the UI. Most agents run in-process in the orchestrator
runtime; the two research agents are marked `runtime: "dedicated"` and each runs
in its **own** AgentCore Runtime, invoked via `InvokeAgentRuntime`.

---

## 2. Where each concern lives

### Authentication (who you are) — Cognito + API Gateway
- The browser logs in via Cognito (Hosted UI) and sends the ID token as a Bearer on
  every `/api/*` call. The **API Gateway JWT authorizer** validates it (issuer =
  Cognito User Pool endpoint, audience = App Client ID); unauthenticated requests
  never reach the BFF. The BFF records the caller (`email`/`sub`) on the session for audit.

### The pipeline — LangGraph over a config-driven graph
- `workflow.json` defines the agents and the `steps` topology. `graph_builder.py`
  turns each step into graph nodes: a single agent gets a node (+ an optional HITL
  gate); a `parallel` step fans out to all its agents and joins at one group gate;
  a `sequence` step chains its agents in order with one gate after the last (revise
  loops back to the first). Each is a distinct, config-only pattern.
- Each agent is a small `Agent` subclass in `app/subagents/<id>/`. The research
  agents share `common/research.py` (RAG/MCP + evidence classification); the
  analysis/recommendation/report agents share `common/synthesis.py` (gather
  approved upstream assets → structured JSON → validated contract).

### Agent runtime placement — in-process vs dedicated
- `runtime: "main"` — the agent runs in-process as a LangGraph node inside the
  orchestrator runtime.
- `runtime: "dedicated"` — Terraform provisions a **separate AgentCore Runtime**
  for that agent (same image; an `AGENT_ID` env var selects which agent it hosts).
  The orchestrator's node body (`AgentCoreRuntimeAgent`) calls `InvokeAgentRuntime`
  with the same inputs an in-process agent would read, and returns the output. Same
  `Agent` interface either way, so the graph wiring is identical — placement is
  config only. This sample ships all agents as `main`.

### Tool access (which tools an agent may call) — AgentCore Gateway
- Agents reach tools through one **Gateway** MCP endpoint over Streamable HTTP,
  using a short-lived **Cognito client-credentials** access token. The Gateway's
  **CUSTOM_JWT** authorizer validates the token and pins the client via the
  `client_id` claim (Cognito client-credentials tokens carry `client_id`).
- Two targets: **knowledge** (public AWS Knowledge MCP server, used by
  `web_research`) and **kb** (a Lambda target running `bedrock:Retrieve`, used by
  `knowledge_research`).

### RAG — Bedrock Knowledge Base on S3 Vectors
- A single Bedrock Knowledge Base (Titan Text Embeddings v2) backed by **S3 Vectors**
  (serverless). The retrieve Lambda returns ranked chunks to ground answers.
- The RAG agent is scoped to its own corpus via a **`doc_type` metadata filter**
  (a sidecar `.metadata.json` on ingest tags each doc with its folder; the agent
  carries a `KB_FILTER`), so one KB can serve many agents while each retrieves only
  its relevant document set. Add a folder under `kb_docs/` to add a corpus.

### State & progress
- **AgentCore Memory** is the durable LangGraph checkpointer (pause/resume across
  the HITL gates). **DynamoDB** holds live status + a per-session event timeline
  the UI polls.

### Observability & cost — isolated capture + in-app dashboard
- Every model call, tool/KB call, and per-run compute burst is metered to a
  dedicated **telemetry DynamoDB table** (PK `session_id`, SK a chronological
  `ts#seq#uuid`, plus a `by_date` GSI). Capture is **best-effort and never affects
  a run** — the hooks swallow their own errors.
- **Token counts are exact** (from Bedrock `usage_metadata` + the CountTokens API);
  **cost is a dated list-price estimate** from an editable price book
  (`app/observability/pricing.py`), not the billed amount.
- The BFF exposes read-only endpoints (`/api/sessions/{id}/telemetry`,
  `/api/telemetry/aggregate?by=date|model|user`); the **Observability** UI tab
  (`web/observability.js`) renders per-run drilldown, aggregation, projection, and export.
- The whole feature is **isolated by design** — `app/observability/`,
  `terraform/observability.tf`, `web/observability.js`, and one-line hooks in
  `llm.py`/`mcp.py`/`context.py`/`runtime.py` — so it can be removed as a unit.

### Time — Eastern Time everywhere
- All stored and displayed timestamps use **US Eastern Time** in `YYYY-MM-DD HH:MM:SS`,
  produced by one dependency-free helper (`app/common/clock.py`, mirrored in
  `bff/clock.py` since the BFF ships as its own zip).

### Config (`workflow.json`)
```json
"orchestrator": { "name": "multiagent_orchestrator", "engine": "langgraph",
                  "defaultModel": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
                  "longStepSeconds": 1.2 },
"agents": {
  "knowledge_research": { "name": "Knowledge Base Research", "runtime": "dedicated",
                          "rag": "reference", "model": "...haiku...",
                          "sources": [...], "access": [...], "produces": "research-finding" },
  "web_research": { "name": "External Research", "runtime": "dedicated", "mcp": "knowledge", ... },
  "analysis":  { "name": "Analysis", "runtime": "main", "model": "...haiku..." }
},
"steps": [
  { "agent": "intake", "hitl": true },
  { "parallel": ["knowledge_research", "web_research"], "hitl": true, "gateId": "research" },
  { "sequence": ["analysis", "recommendation"], "hitl": true, "gateId": "analysis_reco" },
  { "agent": "report" }
]
```
Agents are keyed by **semantic id** (the key IS the module/package name). Each
entry also carries declarative `sources` / `access` / `produces` metadata (and a
`rag` corpus for the KB agent). The `orchestrator` block documents the engine
(not an agent); the app consumes `defaultModel` and `longStepSeconds` (env overrides).

---

## 3. Key decisions

- **Config-driven runtime placement.** `runtime: "dedicated"` in `workflow.json`
  provisions a real per-agent AgentCore Runtime and the orchestrator invokes it via
  `InvokeAgentRuntime`. One container image serves both roles; `AGENT_ID` selects a
  single agent. Flipping an agent between `main` and `dedicated` is a config edit —
  both IaC apps read `workflow.json` (Terraform `for_each`, CDK a loop) and create
  the runtimes automatically.
- **Two IaC options, one source of truth.** Terraform (full, incl. Gateway + KB) and
  CDK/TypeScript (core footprint) both derive agents + topology from `workflow.json`
  and build the same image. The AgentCore resources are created via the same
  CloudFormation resource types (`AWS::BedrockAgentCore::*`) — Terraform through its
  Cloud Control provider, CDK through L1 `CfnResource` escape hatches (no stable L2 yet).
- **Cognito auto-creation.** Both IaC options can provision a Cognito User Pool,
  Hosted UI domain, and public SPA App Client from scratch (`create_cognito = true` /
  `-c createCognito=true`). This eliminates the need for pre-existing Cognito
  resources and is the recommended path for demos and sandboxes. The domain prefix
  includes the account ID to avoid global collisions.
- **Multi-account / multi-region safe.** CDK and Terraform can target different
  accounts and/or regions simultaneously. Resource names incorporate the AWS account
  ID (S3 bucket, Cognito domain), so there are no global naming collisions.
- **HITL after parallel groups.** The graph builder supports a join gate after a
  parallel step: all agents in the group fan in, one decision gates the group, and
  "revise" re-runs only the agents the reviewer flags.
- **Cognito for both boundaries.** End-user login (SPA) and the agent→Gateway machine
  identity (M2M) use the same Cognito User Pool.
- **Client pinning via `client_id`** (Cognito client-credentials tokens carry `client_id`).
- **Classic Bedrock KB + Lambda target** — a standard Bedrock KB (S3 Vectors)
  fronted via a Gateway Lambda target; fully declarative.
- **Swappable Gateway targets.** Pointing at a different remote MCP is a
  `gateway_mcp_endpoint` change; a REST provider attaches as an OpenAPI target.
- **Structured, validated assets.** Every agent emits a Pydantic contract asset
  (evidence classification + claim tracing), so review and downstream consumption
  are machine-checkable rather than prose.
- **BFF → Runtime stays IAM/SigV4** (service-to-service). Cognito governs the end-user
  and agent→Gateway boundaries, not internal calls.
- **Observability is an isolated, additive layer** behind one metering surface, so
  it can be maintained or removed without affecting the workflow.

---

## 3a. Security posture (sample vs production)

This is a **reference sample**. It ships sound patterns — Cognito SPA login + API
Gateway JWT authorizer, Cognito client-credentials with a CUSTOM_JWT Gateway
authorizer pinned on `client_id`, SigV4 for internal calls, and per-component scoped
IAM roles — and keeps no secrets in the repo. Before any non-sandbox use, harden it:

- **Auth can be disabled.** `enable_gateway = false` + empty `cognito_*` deploys an
  **open** UI/API (intended only for a personal sandbox).
- **Authentication only — no RBAC.** Add role-based authorization at the BFF, the
  node/gate wrappers, and the HITL gate if you need it.
- **Edge/API hardening.** No WAF, throttling, or usage plans; CloudFront uses the
  default certificate. Add these + a custom domain for production.
- **Secrets.** The Gateway M2M secret is delivered as a runtime env var; prefer
  **AWS Secrets Manager** with rotation.
- **Data protection.** Buckets use `force_destroy = true`; enable versioning,
  retention, log-retention, and alarms for real data.
- **Untrusted content.** Treat all model/MCP/RAG output as untrusted; add
  guardrails/output filtering for your domain.

See the README's "Security, cost & scalability" section for the same list plus
cost and scaling notes.

## 4. Future enhancements

- **Long-running dedicated agents.** The cross-runtime call is synchronous
  request/response today; a poll/resume protocol would support dedicated agents that
  run for many minutes and independent per-agent scaling.
- **Per-agent authorization (RBAC).** Today auth is authentication-only. Roles could
  be carried in the token and enforced at the BFF, the node/gate wrappers, and the HITL gate.
- **Push UI transport.** Replace polling with DynamoDB Streams → WebSocket/AppSync.
- **AgentCore Managed KB.** Migrate the KB retrieval to the Managed KB connector
  target once Terraform provider support lands.
