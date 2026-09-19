# Architecture

Generic multi-agent orchestrator (LangGraph + Amazon Bedrock AgentCore), with a
**pluggable identity provider** at the front door and an **AgentCore Gateway**
fronting the MCP/tool plane. This describes what is actually deployed.

> **Infrastructure as code — two options, same architecture.** Everything below is
> provisioned by **either** the **Terraform** config (`orchestrator/terraform/`) or
> the **CDK / TypeScript** app (`orchestrator/cdk/`). Both read `app/workflow.json`
> as the single source of truth, build the same container image, and create the same
> resources including the AgentCore Gateway, the Bedrock Knowledge Base, the
> Guardrail, the Cedar policy engine and Transaction Search.
>
> One switch changes the feature surface in both: `enable_gateway` /
> `-c enableGateway`. Left off, no Gateway/KB/policy engine is created, and any agent
> bound to a `tool` **fails fast** — the framework does not fabricate evidence (see
> `app/common/errors.py`). Turned on, MCP, RAG and Cedar authorization are all live.
>
> **Both options take the same `idp` switch** — `cognito`, `auth0`, or `none` — and
> both can auto-create Cognito (`cognito = { create = true }` in Terraform,
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
                              │  IdP (OIDC) │  `idp` = cognito | auth0 | none
                              │  Cognito or │   • public/SPA client  → user login (UI)
                              │  Auth0      │   • confidential client → agent → Gateway
                              └──┬───────┬──┘     (client-credentials)
                    user login   │       │
                                 ▼       │
                        ┌────────────────┴───┐
                        │   Browser (SPA)     │  provider's hosted login;
                        └─────────┬───────────┘  sends Bearer ID token
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
                        │  decisions + rerun +      │
                        │  evaluate + insights +    │  + in-app assistant: Bedrock
                        │  telemetry                │    Converse tool-use loop
                        └───────────┬───────────────┘
              SigV4 InvokeAgentRuntime │
                                    ▼
                        ┌──────────────────────────┐  checkpoint      ┌───────────────────────────┐
                        │  AgentCore Runtime        │─────────────────▶│ AgentCore Memory ×2       │
                        │  (orchestrator)           │  (graph state)   │ • checkpointer (HITL)     │
                        │  LangGraph                │  recall / store  │ • long-term semantic +    │
                        │  1→[2‖2‖2‖2]→[3→3]→4          │◀────────────────▶│   summary (per agent +    │
                        │                           │                  │   per subject)            │
                        │                           │                  └───────────────────────────┘
                        │                           │  write progress  ┌───────────────────────────┐
                        │                           │─────────────────▶│ DynamoDB                  │
                        │                           │                  │ status + events (UI)      │
                        │                           │                  │ telemetry (cost/quality)  │
                        └─┬─────────┬───────────┬─┘                  │ insights (findings)       │
                          │         │           │                    └───────────────────────────┘
             InvokeModel  │         │ InvokeAgentRuntime (dedicated agents; trace ctx propagated)
                          │         ▼
                          │  ┌───────────────────────────────────────┐
                          │  │ Dedicated AgentCore Runtimes (per agent)│
                          │  │  knowledge_research ‖ web_search        │
                          │  │                     ‖ documentation_search│
                          │  └───────────────┬───────────────────────┘
                          │      (also reach the Gateway) │ M2M token (per IdP)
                          ▼                               ▼
                     ┌──────────┐ ┌─────────────────────────────────┐
                     │ Bedrock  │ │  AgentCore Gateway (MCP)         │
                     │ (Claude) │ │  CUSTOM_JWT authorizer (the IdP) │
                     │ + Guard- │ │  + Cedar Policy Engine           │
                     │   rails  │ │    (ENFORCE / LOG_ONLY)          │
                     └──────────┘ └───────────┬─────────────────────┘
                                              │  (authorized per tool call)
            Targets + Cedar permits are GENERATED from the `tools` block
            in app/workflow.json (terraform/tools.tf, cdk/lib/tool-plane.ts)
                  ┌───────────────────────┼───────────────────────┐
                  ▼                       ▼                       ▼
        ┌───────────────────┐ ┌──────────────────┐ ┌────────────────────────┐
        │ type=kb (Lambda)  │ │ type=websearch   │ │ type=mcp | openapi     │
        │ retrieve ▶ Bedrock│ │ AgentCore Web    │ │ your MCP server or     │
        │ KB (S3 Vectors)   │ │ Search connector │ │ REST API (+ vaulted key)│
        └───────────────────┘ └──────────────────┘ └────────────────────────┘

   Quality loop (out-of-band, reading the same run):
     OTEL spans ─▶ CloudWatch (Transaction Search)
        ├─▶ AgentCore Evaluations  — LLM-as-judge per prompt per version (auto at
        │     run completion, or on demand from the UI)
        └─▶ AgentCore Insights     — batch evaluation across ALL recent runs
              (failure patterns / user intents / execution summaries)
```

Workflow (4 stages, 8 agents) — a parallel group next to a sequential group:
`1 Intake ─(HITL)▶ 2 [knowledge_research ‖ web_search ‖ documentation_search ‖
cost_research] ─(HITL)▶ 3 [analysis → recommendation] ─(HITL)▶ 4 Report`, with a
`branch` on step 1 that can skip step 2 or end the run (see §Pipeline). The four
research agents exist to show four different tool patterns behind one Gateway — a
Knowledge Base, a managed connector, a remote MCP server and a Lambda. Three share
one runner and one contract, a line of config apart; `cost_research` deliberately
does not — it runs `main` rather than `dedicated`, on a smaller token budget, and
reads the tool's DATA rows through `ctx.call_tool_rows` instead of treating the
result as evidence to paraphrase.
Stage 3 is the other deliberate contrast: `analysis` and `recommendation` are
`runtime: "a2a"` — agents this deployment does not operate, reached over the
Agent2Agent protocol. They sit in a `sequence` behind a single gate like any other
step, which is the point: the trust boundary changes what the framework can enforce
about them, not how they are wired.
The runtime writes to two independent stores (there is no Memory→DynamoDB flow):
durable graph state to **AgentCore Memory** (the LangGraph checkpointer, for HITL
pause/resume) and live per-session progress to **DynamoDB** (status + events),
which the BFF reads for the UI. Most agents run in-process in the orchestrator
runtime; three of the research agents are marked `runtime: "dedicated"` and each runs
in its **own** AgentCore Runtime, invoked via `InvokeAgentRuntime`.

---

## 2. Where each concern lives

### Authentication (who you are) — configurable IdP + API Gateway
- **One variable, `idp`, selects the provider** for both auth boundaries: end-user
  login and the agent→Gateway machine token. `cognito`, `auth0` and `none` (no login)
  ship in the box. Everything provider-specific is derived in
  **`terraform/identity.tf`**; the rest of the stack consumes only neutral values
  (`jwt_issuer`, `jwt_audience`, `gateway_token_url`, …).
- The browser logs in via the provider's hosted login page and sends the ID token as a
  Bearer on every `/api/*` call. The **API Gateway JWT authorizer** validates it —
  issuer is the Cognito User Pool endpoint or the Auth0 tenant domain, audience is the
  SPA client id — so unauthenticated requests never reach the BFF. The BFF records the
  caller (`email`/`sub`) on the session for audit.
- The SPA implements each provider as a small strategy (`AUTH_PROVIDERS` in
  `web/index.html`) with the same `init`/`login`/`logout` contract: Cognito uses the
  Hosted UI code flow directly, Auth0 loads `auth0-spa-js` on demand, and `none` is a
  no-op. Terraform renders the chosen provider into `auth-config.js`, so the same UI
  bundle serves all three.
- **Adding a provider** means one branch in `identity.tf`, one in the Gateway client's
  token request, and one strategy in the SPA. Nothing else changes.

### Authorization (what you may do) — RBAC on run actions
The JWT authorizer answers "is this a valid user?". It does **not** answer "may *this*
user approve a review gate?" — and for a human-in-the-loop product, "any authenticated
user can approve" is the wrong default. That second question is answered by
**`bff/authz.py`**, driven by the **`authorization`** block in `workflow.json`:

```json
"authorization": {
  "groupsClaim": "cognito:groups",
  "actions": {
    "decision": ["approvers"],
    "rerun":    ["approvers"],
    "cancel":   ["approvers", "operators"],
    "evaluate": ["operators"],
    "insights": ["operators"],
    "delete":   ["operators"]
  }
}
```

- **Six mutating actions** are recognised: `decision` (approve/revise/deny a gate),
  `rerun`, `cancel`, `evaluate`, `insights`, `delete`. Read endpoints are not gated —
  they are already behind the authorizer, and hiding a run from someone who can see the
  UI buys nothing.
- **Semantics chosen so the default stays backwards compatible.** An action *not
  listed* is unrestricted; listed *with groups* requires at least one of them; listed
  with an **empty list** is denied to everyone, which is how you switch a capability
  off. Delete the block and behaviour is exactly what it was before RBAC existed.
- **Enforced on the routes, and on the assistant.** The in-app assistant can approve
  gates, re-run agents and start evaluations, so it is a second path to the same
  actions. The BFF passes the caller's permitted actions into `chatbot.handle_chat`,
  which **withholds the tools** they may not use. A tool the model was never given
  cannot be talked into being called — "ask the chatbot to approve it" is not a way
  around the gate.
- **`groupsClaim` is configurable because providers differ.** Cognito issues
  `cognito:groups`; Auth0 will not emit an unnamespaced custom claim, so it needs
  something like `https://your-app/roles` added by a post-login Action. The claim
  arrives as a real array, a JSON-encoded string, or Cognito's bracketed
  `[a b]` form depending on the path — `groups_of()` normalises all three rather than
  assuming one.
- **The groups are provisioned from the config.** Both IaC paths create a Cognito
  group for every group named in `actions` (`aws_cognito_user_group.authz` /
  `cognito.CfnUserPoolGroup`), because a group that does not exist cannot be joined and
  Cognito would simply omit the claim — leaving every gated action denied with nothing
  to point at. *Membership* is not managed in IaC: it is per-person and changes far more
  often than a deploy.
- **The UI layer is advisory.** `GET /api/me` returns `{user, groups, permittedActions}`
  and the SPA disables or hides the controls the caller cannot use, so nobody is offered
  a button that 403s. Every action is still checked independently server-side.
- **Restricting anything requires an IdP.** With `idp = "none"` there is no authorizer,
  so no claims, so no groups — every rule would evaluate against an empty group set and
  deny everyone, locking the UI out of the app it just deployed. Both IaC paths reject
  that combination at plan/synth time, and also reject an unrecognised action key (a
  typo looks like a restriction but gates nothing, leaving the real action wide open).

Note the division of labour: **Cedar at the Gateway** authorizes *agents calling tools*;
**`authz.py` at the BFF** authorizes *humans acting on runs*. Different subjects,
different boundaries, both config-driven.

### The pipeline — LangGraph over a config-driven graph
- `workflow.json` defines the agents and the `steps` topology. `graph_builder.py`
  turns each step into graph nodes: a single agent gets a node (+ an optional HITL
  gate); a `parallel` step fans out to all its agents and joins at one group gate;
  a `sequence` step chains its agents in order with one gate after the last (revise
  loops back to the first). Each is a distinct, config-only pattern.
- A step may also carry `branch`, which makes the **agent's own output** choose the
  next step (`app/common/branching.py` — a schema-agnostic rule language over a dot
  path into the output JSON). It compiles to one extra node after the step, which
  evaluates the rules, records the chosen step in the `branch` state channel, logs
  the rule that matched, and marks the bypassed agents `skipped`. The router itself
  is a pure read of that channel, so routing is asserted without running an agent.
  Targets name a *later* step, so the graph stays acyclic and a branch can only
  redirect a run, never strand one. With a `hitl` gate on the same step, the human
  approves first and the branch then reads the output they approved.
- Each agent whose code this deployment ships is a small `Agent` subclass in
  `app/subagents/<id>/`. The research agents share
  `app/subagents/_shared/research.py` (RAG/MCP + evidence classification); the
  `report` agent uses `app/subagents/_shared/synthesis.py` (gather approved upstream
  assets → structured JSON → validated contract). Both live under `subagents/` on
  purpose: they are this SAMPLE's editorial choices, not framework, and a customer
  replaces them.
  `analysis` and `recommendation` have **no package at all** — they are
  `runtime: "a2a"`, so their reasoning happens outside this deployment. They used to
  be local `synthesis.py` agents, and the two contracts they produce
  (`_shared/contracts/analysis.py`, `recommendation.py`) are still there: they are now
  the shapes the REMOTE agents are asked to return, and `tests/test_a2a.py` validates
  the shipped stand-in's replies against them. That is the one place the check can
  live, because the framework cannot validate a remote reply against a contract class
  for code it does not own.
- **The agentic framework inside an agent is per-agent and is the author's choice.**
  `run()` is a plain `async def`, so an agent may drive Strands, CrewAI, LlamaIndex,
  a graph of its own, or nothing. `research.synthesize` exposes this as a `think`
  hook — evidence gathering and contract assembly stay put and only the reasoning
  step changes. Shipped: `web_search` reasons inside a **Strands** agent
  (`app/subagents/_shared/strands_bridge.py`), `knowledge_research` inside a
  **nested LangGraph** with a conditional edge that gives an unparseable draft one
  bounded repair attempt, and `documentation_search`/`cost_research` inside no
  framework at all. All four emit the same contract, so the choice is invisible
  downstream.
  The invariant that makes this safe is that **the model call goes through
  `ctx.llm`**: guardrails, cost/token telemetry, memory injection, truncation
  detection and cancellation live there, and a framework holding its own Bedrock
  client would lose all of them while the run still reported success. The
  consequence is that a framework's *native* tool loop is unavailable (`ctx.llm`
  returns text, so it cannot carry a `toolUse` block) — the bridge raises rather
  than dropping tools, and data access stays declared in `workflow.json` and called
  through `ctx.call_tool`/`ctx.retrieve`. The other cost is the image: one container
  serves every agent, so every agent pays for every framework in it (measured:
  `+ strands-agents` 153 → 166 MB; `+ crewai` 804 MB, which is why CrewAI is
  documented and not shipped).

### Agent runtime placement — in-process, dedicated, or not yours at all
- `runtime: "main"` — the agent runs in-process as a LangGraph node inside the
  orchestrator runtime.
- `runtime: "dedicated"` — Terraform provisions a **separate AgentCore Runtime**
  for that agent (same image; an `AGENT_ID` env var selects which agent it hosts).
  The orchestrator's node body (`AgentCoreRuntimeAgent`) calls `InvokeAgentRuntime`
  with the same inputs an in-process agent would read, and returns the output. Same
  `Agent` interface either way, so the graph wiring is identical — placement is
  config only. This sample ships three of its eight agents as `dedicated`
  (`knowledge_research`, `web_search`, `documentation_search`), two as `a2a`
  (`analysis`, `recommendation`); the rest are `main`.
- `runtime: "a2a"` is the third value, and it is not a placement of your code — it is a
  **trust boundary**. The step is run by an agent you do not operate, reached over the
  **Agent2Agent protocol** at its Agent Card URL (`app/common/a2a_agent.py`): the card
  is fetched from `/.well-known/agent-card.json`, `supportedInterfaces` chooses the RPC
  endpoint in the card's preference order, the task goes out as JSON-RPC 2.0
  `message/send`, and a Task that is still working is polled via `tasks/get` under a
  bounded budget. No module under `app/subagents/`, because the code is somebody
  else's; `tool`/`corpus`/`model`/`maxTokens` are rejected on the AGENT entry, because a
  remote agent reaches its own data sources and makes its own model call — its output
  budget is its own business, and in the shipped stand-in each skill declares one
  (`a2a_lambda/handler.py`).
  A2A is transport and discovery only — it carries no opinion about WHICH agent to
  call, so routing stays this framework's job (`steps`, `branch`, a review gate) and an
  a2a agent slots into the topology with no special case.
- **A remote agent is still a first-class asset producer**, and that takes work on this
  side. The division is: the remote supplies the CONTENT, the framework supplies the
  ENVELOPE (`A2AAgent._as_asset`). `version` is how many times this step has run in
  this session, `createdByAgent` is the id this workflow gave it, `assetType` is its
  `produces`, `sourceAssetIds` are the upstream assets this graph handed over — none of
  which a remote agent can know, and one asked to invent `version` gets it wrong on
  every re-run, silently, because a wrong integer still validates. A JSON object reply
  is wrapped; a PROSE reply is left exactly as written, because a remote reviewer that
  answers in sentences is a legitimate remote agent. Before this, a remote reply had no
  `assetId`, so `synthesis.upstream_context` — which collects exactly that field — took
  it in as an unattributable block and a report could mention it but never cite it.
- **What degrades, and what does not.** Guardrails apply, because the framework wraps
  the call in *our* container. **Long-term memory works in both directions**: recall is
  carried to the remote agent inside the A2A task (`recalledContext`, with
  `RECALL_CAVEAT` attached so it cannot be read as evidence) and the reply is stored as
  a new insight. That is a deliberate difference from `dedicated`, where recall would be
  billed and discarded because `InvokeAgentRuntime` has a fixed payload that cannot
  carry it — A2A carries opaque text, so it can. Two things genuinely degrade:
  evaluations fall back to a role descriptor (there is no local model call to capture a
  prompt from, so the shipped a2a agents are `auto: false` with Faithfulness dropped),
  and the remote agent's own tool calls are outside this deployment's Cedar policy.
  Neither can the framework validate the reply against a pydantic contract, because
  there is no local class for somebody else's code — which is why the UI renders an
  asset by SHAPE rather than by field name.

### Tool access (which tools an agent may call) — AgentCore Gateway
- Agents reach tools through one **Gateway** MCP endpoint over Streamable HTTP, using
  a short-lived **client-credentials** access token from the configured IdP. The
  Gateway's **CUSTOM_JWT** authorizer validates it, pinning the caller in the way that
  provider's tokens allow: Cognito tokens carry `client_id` + `scope` and no `aud`, so
  the authorizer uses `allowed_clients`; Auth0 tokens carry `aud` + `azp` and no
  `client_id`, so it uses `allowed_audience` plus an `azp` custom claim. The runtime's
  token request differs to match (`GATEWAY_AUTH_FLOW`).
- **Targets are generated from config.** The `tools` block in `app/workflow.json`
  declares every data source; `terraform/tools.tf` and `cdk/lib/tool-plane.ts` turn
  each entry into a Gateway target and a Cedar permit. Five types are supported:

  | `type` | Target kind | Backend |
  |---|---|---|
  | `kb` | Lambda | `bedrock:Retrieve` against a Bedrock Knowledge Base on S3 Vectors |
  | `websearch` | **connector** (`connectorId: web-search`) | the AWS-managed AgentCore Web Search index |
  | `mcp` | MCP server | any remote MCP server over Streamable HTTP |
  | `openapi` | OpenAPI schema | any REST API, from a schema in S3 |
  | `lambda` | Lambda | **any function you own** — the escape hatch for a warehouse, an RDBMS, an internal service, or anything inside a VPC |

  A target's KEY is both its Gateway name and the label an agent binds to via its
  `tool` field, so the two can never drift — the IaC validates that every agent's
  `tool` resolves.
- **The remote MCP target is fully managed.** `docs` points at
  `https://knowledge-mcp.global.api.aws` — the **AWS Knowledge MCP Server**, hosted
  and operated by AWS. Nothing is deployed for it and no credential is needed: five
  real tools over live AWS content (`search_documentation`, `read_documentation`,
  `list_regions`, `get_regional_availability`, `retrieve_skill`). A customer swaps in
  their own server by changing `endpoint` (and `call`/`arg` to match one of its
  tools) — no code, no IaC, no policy edit.
- **Tool names are composed by the Gateway, and names can be doubly prefixed.**
  The Gateway publishes every tool as `<targetName>___<toolName>`, server-side.
  Nothing in this repo writes that prefix: `kb.tf` declares the tool as `retrieve`
  and the Gateway publishes `kb___retrieve`. AWS *also* uses `___` to namespace the
  tools on its managed servers, so this target's tools arrive as
  `docs___aws___search_documentation`. That composes and works fine — it is simply
  why `workflow.json` sets `"call": "aws___search_documentation"`, carrying AWS's
  prefix. `call` is matched against the published name; nothing splits on `___`.
- **`tools/list` is PAGINATED — follow `nextCursor`.** This is the one real trap.
  A client that reads only the first page sees a subset and concludes a target
  published nothing, when its tools are simply on page two. This deployment has four
  targets, and the remote MCP server alone publishes five tools, so the catalogue
  does not fit one page. The app's MCP client paginates correctly; hand-rolled
  verification scripts often do not.
- **`listingMode`** selects how the Gateway discovers a server's tools: `DEFAULT`
  synchronises and caches the catalogue when the target is created (needed for
  semantic tool search), `DYNAMIC` forwards `tools/list` to the server at invocation
  time. Either works; `DYNAMIC` avoids a stale cache when a server's tools change.
  If a target genuinely publishes nothing the agent raises `ToolUnavailable` and the
  run fails with that reason — no answer is ever invented.
- **A tool result arrives inside an MCP envelope, and unwrapping it is not optional.**
  `tools/call` returns `content: [{"type":"text","text":"<a JSON string>"}]` — a
  *list* of parts, each carrying the real payload re-encoded as text. Code that
  inspects the envelope for a `results` key finds nothing, falls through to a generic
  "stringify and clip" path, and hands the model a truncated blob of raw JSON with the
  `url` and `publishedDate` fields still buried in it. That is exactly what happened
  here: citations came back `null` from a connector that always returns them, and
  agents listed truncated evidence as a limitation of their own findings. The client
  now parses each text part, flattens result lists across parts, and pulls
  title/URL/date/text by trying the field names the different backends use.
- **Evidence has a budget, not a hard clip.** `MAX_EVIDENCE_CHARS` (20000, env-tunable)
  bounds the whole evidence block and `MAX_RESULT_CHARS` (4000) bounds any single
  result, so one verbose result cannot crowd out the rest. Results are numbered so a
  claim can name the one it rests on.

### Web Search — the managed AgentCore connector
- A built-in connector (`connectorId: "web-search"`), so there is no endpoint, no
  API key and no schema to maintain. Queries never leave AWS. The tool surfaces as
  `<target>___WebSearch`. Outbound auth is the Gateway's own execution role
  (`GATEWAY_IAM_ROLE`), granted `bedrock-agentcore:InvokeWebSearch` on the
  AWS-owned tool ARN plus `InvokeGateway`.
- **Citations are a contract, not a nicety.** The connector returns `title`, `url`
  and `publishedDate` with every result, and its acceptable-use terms require those
  to be retained and *displayed* wherever a result is surfaced. So the framework
  preserves them into the evidence the model sees, and then **verifies** every
  citation URL against that evidence: a link the model invented is dropped, and a
  `sourced-fact` resting on one is downgraded to `agent-interpretation`. The UI
  renders each source URL as a link. Without this, a fabricated URL is
  indistinguishable from a real citation to whoever reads the report.
- **Domain filtering is one key, on the target.** `domains: {include, exclude}` is set
  on the Gateway target, hidden from the agent and applied to every request, so it is a
  boundary rather than a preference. It replaced four keys — a request-level
  `includeDomains`/`excludeDomains` pair beside a target-level
  `targetIncludeDomains`/`targetExcludeDomains` pair — which expressed one intent, with
  the weaker layer holding the more obvious name. `publishedFrom` / `publishedTo` remain
  request-level because the target has no equivalent; being caller-supplied they are
  scoping, not a boundary.
  A domain is dropped if it is on `exclude`, and returned only if on `include` when
  `include` is set.

### RAG — Bedrock Knowledge Base on S3 Vectors
- A single Bedrock Knowledge Base (Titan Text Embeddings v2) backed by **S3 Vectors**
  (serverless). The retrieve Lambda returns ranked chunks to ground answers.
- The RAG agent is scoped to its own corpus via a **`doc_type` metadata filter**
  (a sidecar `.metadata.json` on ingest tags each doc with its folder; the agent
  carries a `KB_FILTER`), so one KB can serve many agents while each retrieves only
  its relevant document set. Add a folder under `kb_docs/` to add a corpus.
- **S3 Vectors caps *filterable* metadata at 2048 bytes per vector**, so both keys
  that grow with the document — `AMAZON_BEDROCK_TEXT` (the chunk) and
  `AMAZON_BEDROCK_METADATA` (a JSON blob carrying the chunk text again, the parent
  text, the source location and a doc id) — are declared non-filterable. Miss one and
  ingestion accepts small documents and then fails on a larger one
  (`numberOfDocumentsFailed: 1`, job `FAILED`), leaving the KB quietly missing that
  content. Nothing filters on either; the only filter here is `doc_type`.
- The index and the KB are **named from a digest of their immutable properties**
  (`kb-index-<sha8>`, `<agent>-kb-<sha8>` over dimension + the non-filterable key
  list). Neither property can change in place, so a change must *replace* the
  resource — and CloudFormation refuses to replace a resource with a fixed custom
  name. A derived name makes the replacement routine, and it cascades correctly: the
  KB's `storageConfiguration` holds the index ARN and is immutable too, so the KB
  carries the same digest. A parity test asserts both paths derive identical names.

### State & progress
- **AgentCore Memory** is the durable LangGraph checkpointer (pause/resume across
  the HITL gates). **DynamoDB** holds live status + a per-session event timeline
  the UI polls.
- Every agent run is appended to a `history` channel as a numbered **version** with
  the reviewer comment that triggered it, so the UI and the observability inspector
  can show v1 → "feedback" → v2.

### Rewind & re-run
- `graph_builder.rerun_plan(agent_id)` computes how to rewind so one agent re-runs
  and cascades downstream: which node to attribute a state write to (`as_node`) and
  which gate decision to force to `approve` so the router forwards instead of
  looping. `runtime._rewind` applies it with `aupdate_state`, then invokes the graph
  with `None` — the pending tasks run and every downstream gate re-pauses, because
  gate nodes always call `interrupt()`. It handles a target that is a single agent,
  any member of a parallel group (the stage re-runs as a unit), or one in the
  middle of a `sequence` (entered from the agent before it).
- `group_rerun_plan(agent_ids)` re-runs a **subset of one gated parallel stage** by
  reusing that gate's existing subset routing: seed its decision to `revise` with
  `group_rerun=<subset>`, so only those agents re-run and the gate re-reviews them.

### AgentCore features — one folder each, switched on per agent
- Every capability lives in its own folder under `app/features/` with a
  README-style module docstring: `gateway/`, `memory/`, `identity/`,
  `observability/`, `guardrails/`, `evaluations/`, `policy/`, `optimization/`.
  (Runtime hosting needs no module — it *is* `app/orchestrator/runtime.py` and
  `app/subagent_runtime.py`.)
- Each agent's `agentcore` block in `workflow.json` declares what it uses.
  `AgentContext` reads that config and exposes helpers (`ctx.guardrail`,
  `ctx.memory_recall`/`memory_store`, `ctx.get_identity_token`, `ctx.policy_check`)
  that are **no-ops when the feature is disabled** — so an agent can call them
  unconditionally, and enabling a capability is a config edit.
- The node wrapper (`nodes.py`) applies **guardrails** (input before `run()`,
  output after) and **long-term memory** (recall before, store after)
  automatically, so those two need no agent code at all.
- **Long-term memory** is a *second* AgentCore Memory resource, separate from the
  checkpointer, carrying `semantic` and `summary` strategies. The namespace is
  derived at run time from `actorId = "<agentId>-<subjectSlug>"`, so insights stay
  isolated per agent and per subject (`subjectId` on the run) with no Terraform
  change per agent.
- **Policy** is enforced server-side: a Cedar engine is attached to the Gateway, so
  a tool call is authorized (or denied) before it reaches the target. `ENFORCE` is
  default-deny; `LOG_ONLY` evaluates and logs without blocking. The app only *reads*
  the outcome (a denial surfaces as `mode="denied"`) to display it.
- **Evaluations** score an agent's real run with LLM-as-judge, **per named prompt
  per version**. The primary path replays each prompt's persisted input/output from
  the telemetry table into `bedrock-agentcore:Evaluate`; a fallback reads the
  agent's OTEL AGENT span from CloudWatch. `auto: true` scores at run completion;
  the UI button re-scores on demand.
- **Optimization** is used via cross-run **Insights** — a batch evaluation over a
  window of runtime traces producing failure-pattern, user-intent and
  execution-summary clusters, stored in a small findings table.

### Observability & cost — isolated capture + in-app dashboard
- Every model call, tool/KB call, memory op, guardrail check, policy decision,
  evaluation result, and per-run compute burst is metered to a dedicated
  **telemetry DynamoDB table** (PK `session_id`, SK a chronological `ts#seq#uuid`,
  plus a `by_date` GSI). Rows carry the **agent-run version** and, for model calls,
  the **named prompt** — which is what lets the UI separate re-runs and lets
  evaluation scope to one prompt. Capture is **best-effort and never affects a
  run**: the hooks swallow their own errors, and the writer trims oversized text
  fields so a row is never dropped for exceeding DynamoDB's 400 KB limit.
- **OTEL**: `otel.py` adds what ADOT can't — one `session.id` per run (padded to
  match AgentCore's `runtimeSessionId` so a run is *one* session, not a duplicate
  pair), a readable AGENT span per agent stamped with real token totals and the
  `gen_ai.task.*` attributes Evaluations reads, per-tool spans, trace-context
  propagation across `InvokeAgentRuntime`, and a **force-flush** at the end of each
  burst (without it, spans buffered when AgentCore freezes an idle container are
  lost).
- **Token counts are exact** (from Bedrock `usage_metadata` + the CountTokens API);
  **cost is a dated list-price estimate** from an editable price book
  (`app/features/observability/pricing.py`), not the billed amount.
- The BFF exposes read-only endpoints (`/api/sessions/{id}/telemetry`,
  `/api/telemetry/aggregate?by=date|model|user`, `/api/insights`); the
  **Observability** UI tab (`web/observability.js`) renders per-run drilldown, the
  **Prompts & I/O inspector** (per agent, per version: prompts, tool queries,
  memory ops, guardrail/policy decisions, evaluation scores), aggregation,
  projection, Insights, and export.
- The whole feature is **isolated by design** — `app/features/observability/`,
  `terraform/observability.tf`, `web/observability.js`, and thin hooks in
  `llm.py`/`gateway/client.py`/`context.py`/`nodes.py`/`runtime.py` — so it can be
  removed as a unit.

### Time — Eastern Time everywhere
- All stored and displayed timestamps use **US Eastern Time** in `YYYY-MM-DD HH:MM:SS`,
  produced by one dependency-free helper (`app/common/clock.py`, mirrored in
  `bff/clock.py` since the BFF ships as its own zip).

### Config (`workflow.json`)
```json
"orchestrator": { "defaultModel": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
                  "runtimeInvoke": {...}, "policy": {...}, "chatbot": {...} },
"ui":            { "title": "...", "heading": "...", "defaultTopic": "..." },
"guardrail":     { "contentFilters": {...}, "deniedTopics": [...], "piiEntities": {...} },
"authorization": { "groupsClaim": "cognito:groups",
                   "actions": { "decision": ["approvers"], "evaluate": ["operators"] } },
"tools": {
  "kb":        { "type": "kb", "corpora": ["reference"], "policy": {...} },
  "websearch": { "type": "websearch", "maxResults": 10 },
  "docs":      { "type": "mcp", "endpoint": "https://knowledge-mcp.global.api.aws",
                 "call": "aws___search_documentation", "arg": "search_phrase" },
  "pricing":   { "type": "lambda", "source": "tool_lambda", "call": "aws_prices",
                 "arg": "services", "rowFields": {...}, "toolSchema": [...] }
},
"agents": {
  "knowledge_research": { "name": "Knowledge Base Research", "runtime": "dedicated",
                          "tool": "kb", "corpus": "reference", "maxTokens": 4000,
                          "produces": "research-finding",
                          "agentcore": { "evaluations": {"enabled": true, "auto": false,
                                                         "evaluators": ["Builtin.Faithfulness"]},
                                         "policy": {"enabled": true} } },
  "web_search":           { "name": "Web Search Research",       "runtime": "dedicated", "tool": "websearch", ... },
  "documentation_search": { "name": "MCP Documentation Research", "runtime": "dedicated", "tool": "docs", ... },
  "cost_research":        { "name": "Cost Research",            "runtime": "main",      "tool": "pricing", "maxTokens": 1500, ... },
  "analysis":  { "name": "Analysis", "runtime": "a2a", "source": "a2a_lambda",
                 "skill": "analysis", "auth": "sigv4", "produces": "analysis",
                 "agentcore": { "memory": {"longTerm": ["semantic"]},
                                "guardrails": {"output": true}, ... } }
},
"steps": [
  { "agent": "intake", "hitl": true,
    "branch": { "when": [{ "field": "objective",    "exists": false, "goto": "END" },
                         { "field": "keyQuestions", "lt": 1, "goto": "analysis_reco" }] } },
  { "parallel": ["knowledge_research", "web_search", "documentation_search",
                 "cost_research"],
    "hitl": true, "gateId": "research", "gateName": "Research" },
  { "sequence": ["analysis", "recommendation"], "hitl": true, "gateId": "analysis_reco",
    "gateName": "Analysis & Recommendation (A2A)" },
  { "agent": "report" }
]
```
Agents are keyed by **semantic id** (the key IS the module/package name). Each
entry binds to a data source with `tool` (a key in the `tools` block) plus `corpus`
for a Knowledge Base tool, and carries declarative `access` / `produces`
metadata plus the `agentcore` block that switches its features on. The `orchestrator`
block describes the engine rather than an agent: the app consumes `defaultModel`,
and the IaC consumes `policy` (Cedar on/off + mode) and `chatbot` (assistant on/off
+ model + per-tool flags), with env overrides. The four
sibling top-level blocks are cross-cutting rather than per-agent: `ui` (presentation
strings), `guardrail` (the content-safety policy), `authorization` (who may act on a
run) and `tools` (the data sources agents may reach).
**Terraform reads the same file**, so a feature needing infrastructure (a dedicated
runtime, a policy engine) is provisioned from this one source of truth.

### Testing — the config plane, not the data plane
Two suites, one per language, both fast enough for a pre-commit hook and needing
neither AWS credentials nor a container builder:

- **`orchestrator/tests/`** (pytest, 652 tests, a few seconds) — the runtime side:
  topology derivation, graph compilation across 14 step shapes, branch rules and
  routing, rewind planning, tool argument shapes, Gateway tool-name resolution,
  citation verification, contract coercion, and the RBAC rules plus their wiring on
  every mutating route.
- **`orchestrator/cdk/test/`** (jest, 132 tests) — the IaC side: the projections
  and validators, the synthesized template (Cognito groups, route set + authorizer,
  BFF environment, Gateway targets, Cedar policies), and **Terraform ↔ CDK parity**.

**Deliberately not tested: prompt quality.** That is non-deterministic and has its own
machinery — the HITL gates and AgentCore Evaluations.

The split matters because the config plane is where a customer's edits land, and its
failures are *quiet*: a renamed first agent that drops the request brief, a section
type discarded from a report, a guard applied to five routes out of six, a projection
that drifts between the two IaC paths so one deployment loses features the other
keeps. Every one of those actually shipped at some point. Both suites were
mutation-checked — each bug was reintroduced and confirmed to turn the suite red —
rather than merely being green. See the two `README.md` files in those directories.

**The config surface is described once and consumed four ways.**
`orchestrator/app/keys.json` holds every key `workflow.json` may contain, which
placement or tool type each is legal for, whether it is required, and what reads it.
Four consumers: the allow-list tests, `format_workflow.py` for the **canonical key
order** every agent/tool/step is written in, `build_schema.py` which generates
`app/workflow.schema.json`, and the reference doc. `workflow.json` points at that
schema with `$schema`, so an editor completes the legal keys for the `runtime` you
chose, enumerates allowed values and explains each key on hover — the questions a
customer has *before* a deploy, answered where they are asking them. The schema is
generated rather than written because it would otherwise be a fourth encoding of rules
that already exist in Python, CDK and Terraform, and a schema that disagreed with the
validators is worse than none: a test asserts it rejects all 29 mistakes those
validators reject.

**The framework's closed value sets live in ONE file**, `orchestrator/app/vocabulary.json`,
read by all three planes: `app/common/vocabulary.py` via `json`, `cdk/lib/vocabulary.ts` via
`JSON.parse`, and `terraform/*.tf` via `jsondecode(file(...))`. Runtimes, tool types,
tool-schema property types, a2a auth modes and sources, memory strategies, the RBAC action
names, guardrail strengths and PII actions are each declared once, with a comment saying why
that set is closed.

They used to be written out two or three times — the tool types and the RBAC action names
existed in all three languages — and a copy that got missed rejected a config the other planes
accepted, so whether a workflow deployed depended on which IaC path you used. It is
framework-owned rather than part of `workflow.json` on purpose: a customer's file must not be
able to widen a set the framework enforces.

The parity suite exists because nothing structural keeps the two IaC paths in step:
they are independent implementations of the same infrastructure, and the one time they
drifted it was found by comparing two live deployments. It reads the HCL as text and
compares the API route list, the tool-plane shapes, and the constants duplicated across
HCL / TypeScript / Python (notably the seven RBAC action names, which
`tests/test_authz.py` additionally proves are the exact set the routes enforce).

It used to compare the BFF workflow projection too. That comparison is gone because
the thing it compared is gone: the projection was built twice, once in HCL and once in
TypeScript, and is now one Python function (`orchestrator/bff/workflow.py`). Deleting a
duplicate implementation is a better fix than testing that two copies agree — and it
removed a hard ceiling at the same time, because the projection no longer has to fit
in a 4 KB Lambda environment.

Not covered by either: the IaC's own resource semantics. `terraform validate` /
`cdk synth` plus the plan-time preconditions in both paths are the gate there.

---

## 3. Key decisions

- **Config-driven runtime placement.** `runtime: "dedicated"` in `workflow.json`
  provisions a real per-agent AgentCore Runtime and the orchestrator invokes it via
  `InvokeAgentRuntime`. One container image serves both roles; `AGENT_ID` selects a
  single agent. Flipping an agent between `main` and `dedicated` is a config edit —
  both IaC apps read `workflow.json` (Terraform `for_each`, CDK a loop) and create
  the runtimes automatically.
- **Two IaC options, one source of truth.** Terraform and CDK/TypeScript both derive
  agents + topology from `workflow.json`, build the same image, and provision the same
  resources. The AgentCore resources come from the same CloudFormation types
  (`AWS::BedrockAgentCore::*`) — Terraform through its Cloud Control provider, CDK
  through the generated L1 constructs (`aws-bedrockagentcore`) plus `CfnResource`
  escape hatches for Memory/Runtime, which have no L1 or stable L2 yet.
- **Configuration-driven identity.** A single `idp` variable (`cognito` / `auth0` /
  `none`) drives login, the API authorizer and the Gateway M2M token. Provider
  differences are confined to `terraform/identity.tf`, one branch in the Gateway
  client, and one login strategy in the SPA — so a new OIDC provider is a small,
  local change rather than a refactor. Misconfiguration fails at plan/synth time with
  a message naming the missing variable.
- **Cognito auto-creation.** Both IaC options can provision a Cognito User Pool,
  Hosted UI domain, and public SPA App Client from scratch (`cognito = { create = true }` /
  `-c createCognito=true`). This eliminates the need for pre-existing Cognito
  resources and is the recommended path for demos and sandboxes. The domain prefix
  includes the account ID to avoid global collisions.
- **Multi-account / multi-region safe.** CDK and Terraform can target different
  accounts and/or regions simultaneously. Resource names incorporate the AWS account
  ID (S3 bucket, Cognito domain), so there are no global naming collisions.
- **HITL after parallel groups.** The graph builder supports a join gate after a
  parallel step: all agents in the group fan in, one decision gates the group, and
  "revise" re-runs only the agents the reviewer flags.
- **One IdP for both boundaries.** End-user login (SPA) and the agent→Gateway machine
  identity (M2M) come from the same provider, so there is one place to configure trust.
- **Caller pinning matched to the token format** — `allowed_clients` on the `client_id`
  claim for Cognito, `allowed_audience` + an `azp` custom claim for Auth0. Setting both
  would never validate, since neither provider emits the other's claims.
- **Classic Bedrock KB + Lambda target** — a standard Bedrock KB (S3 Vectors)
  fronted via a Gateway Lambda target; fully declarative.
- **Swappable Gateway targets.** Pointing at a different remote MCP server is an
  `endpoint` change in the `tools` block; a REST API attaches as an OpenAPI target
  from a schema in S3. Both regenerate the Cedar permit automatically.
- **Structured, validated assets.** Every agent emits a Pydantic contract asset
  (evidence classification + claim tracing), so review and downstream consumption
  are machine-checkable rather than prose. Where a model naturally answers with an
  object, the contract models it as one — `RequestBrief.scope` is a `Scope`
  (`inScope` / `outOfScope` / `summary`), because as a bare `str` it captured the
  model's object as a **Python dict repr** and shipped `{'in_scope': [...]}` into the
  report for a human to read. A `mode="before"` validator still accepts a plain
  string, so an existing config or a terser model does not break.
- **BFF → Runtime stays IAM/SigV4** (service-to-service). The IdP governs the end-user
  and agent→Gateway boundaries, not internal calls.
- **Observability is an isolated, additive layer** behind one metering surface, so
  it can be maintained or removed without affecting the workflow.
- **Features are config, not code.** Each AgentCore capability is a folder under
  `app/features/` plus a key in an agent's `agentcore` block. Guardrails and
  long-term memory are applied by the node wrapper, so most agents need no
  feature-specific code at all — and any feature left out of the config, or
  un-provisioned (no env var), degrades to a no-op instead of an error.
- **Policy is enforced by infrastructure, not prompts.** Cedar rules attached to
  the Gateway decide whether a tool call proceeds, so a prompt-injected attempt to
  widen tool access fails server-side. The app-level `ctx.policy_check` helper is
  explicitly the *secondary*, fail-open path for in-process decisions.
- **Evaluation reads persisted prompt I/O, not just spans.** Scoring from the
  telemetry rows (with a CloudWatch span fallback) makes it deterministic per prompt
  per version, and avoids depending on spans reaching the `aws/spans` log group —
  which is why `Evaluate` is called with `sessionSpans` inline rather than using
  managed Online Evaluation.
- **The assistant lives in the BFF, not the graph.** It answers *about* runs and
  must stay responsive while a run is paused, so it is a plain Bedrock Converse
  tool-use loop in the Lambda rather than another agent in the pipeline.

---

## 3a. Security posture (sample vs production)

This is a **reference sample**. It ships sound patterns — SPA login (Cognito or Auth0)
+ an API Gateway JWT authorizer, client-credentials with a CUSTOM_JWT Gateway
authorizer pinned to the caller, SigV4 for internal calls, and per-component scoped
IAM roles — and keeps no secrets in the repo. Before any non-sandbox use, harden it:

- **Auth can be turned off.** `idp = "none"` deploys an **open** UI/API (intended only
  for a personal sandbox).
- **RBAC covers run actions, not agents or data.** `authorization` in `workflow.json`
  gates the seven mutating actions (start a run, approve/revise/deny, re-run, cancel, evaluate,
  insights, delete) on JWT group membership, and the same rules constrain the
  assistant. It does **not** partition *runs* between users: every authenticated user
  can start a run and read every other run's inputs, outputs and telemetry. If you need
  per-tenant or per-run isolation, that is still yours to add.
- **Edge/API hardening.** No WAF, throttling, or usage plans; CloudFront uses the
  default certificate. Add these + a custom domain for production.
- **Secrets.** The Gateway M2M secret is delivered as a runtime env var; prefer
  **AWS Secrets Manager** with rotation.
- **Data protection.** Buckets use `force_destroy = true`; enable versioning,
  retention, log-retention, and alarms for real data.
- **Untrusted content.** Treat all model/MCP/RAG output as untrusted. Guardrails and
  Cedar policy are wired in, but the **shipped rules are samples**. Both are
  generated, so replace them where they are declared — the `guardrail` block and the
  `kb` tool's `policy.restrictTo` in `app/workflow.json`, not `terraform/guardrail.tf`
  or `terraform/policy.tf` — and enable guardrails on every agent that handles
  untrusted text.
- **Telemetry stores prompt/output content** (that's what powers the Prompts & I/O
  inspector). With sensitive inputs, lower `OBS_MAX_CAPTURE_CHARS`, use the table's
  TTL, and restrict read access to the telemetry table and the Observability tab.
- **The assistant can act** (approve a gate, re-run an agent). It obeys the same
  `authorization` rules as the REST routes — the action tools are withheld from a caller
  who lacks the group — but if you would rather it never acted at all, turn the action
  tools off in `workflow.json` (`orchestrator.chatbot.tools`).

See the README's "Security, cost & scalability" section for the same list plus
cost and scaling notes.

## 4. Future enhancements

- **Long-running dedicated agents.** The cross-runtime call is synchronous
  request/response today; a poll/resume protocol would support dedicated agents that
  run for many minutes and independent per-agent scaling.
- **Run-scoped and tenant-scoped authorization.** RBAC on run *actions* now ships
  (`authorization` in `workflow.json`, enforced in `bff/authz.py`). What is still open is
  RBAC on the run *objects*: every authenticated user can list and read every run. A
  per-owner or per-tenant partition on the status/telemetry tables, plus a group check on
  a specific run rather than the action class, would close that.
- **Push UI transport.** Replace polling with DynamoDB Streams → WebSocket/AppSync.
- **AgentCore Managed KB.** Migrate the KB retrieval to the Managed KB connector
  target once Terraform provider support lands.
- **Per-prompt Recommendations.** AgentCore Optimization's `StartRecommendation`
  is not wired: its session reconstruction does not accept this multi-agent trace
  structure (it reports "no sessions identified from input agent traces" even with
  correctly-formatted, X-Ray-indexed traces). Cross-run **Insights** is used
  instead. Revisit when the API supports multi-agent orchestrator traces.
- **Transaction Search is enabled imperatively, in both IaC paths.** It is an
  account-wide singleton, so `AWS::XRay::TransactionSearchConfig` can only ever
  *create* it and fails with `AlreadyExists` on any account where it is already on.
  Both paths instead call the idempotent `UpdateTraceSegmentDestination`
  (Terraform: `null_resource`; CDK: a custom resource) so a re-deploy converges
  rather than failing. It is deliberately left enabled on teardown.
