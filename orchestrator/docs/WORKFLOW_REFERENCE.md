# `workflow.json` reference

Every key, what reads it, and what happens if you change it. This file holds the
explanation so `workflow.json` can stay short enough to read as configuration.

**Rule of the file:** if a key isn't listed here, nothing reads it — and
`tests/test_config_keys.py` will fail. That guard exists because the config had
accumulated keys that looked like switches and did nothing, which is worse than no
key at all, since a reader believes them.

After editing, run `python3 format_workflow.py` (add `--check` in CI).

---

## Top level

| Key | Purpose |
|---|---|
| `$comment` | Orientation for whoever opens the file. Not read by anything. |
| `orchestrator` | Engine-wide settings. Not an agent — it has no model or prompt of its own. |
| `ui` | Presentation strings, so re-branding is a config edit rather than an `index.html` edit. |
| `guardrail` | The Bedrock Guardrail *policy* — what is enforced. Agents opt in per agent. |
| `authorization` | Which JWT groups may approve, re-run, cancel, evaluate, run insights, delete. |
| `tools` | Every data source an agent may call. |
| `agents` | One entry per folder under `app/subagents/<id>/`. |
| `steps` | The topology and the human-review gates. |

Account-level values — region, IdP, whether the Gateway is on — live in
`terraform.tfvars` or CDK context, **not** here, so this file stays portable
between accounts.

### What else you may edit

This file plus `app/subagents/<id>/` and `kb_docs/` is the whole surface. Nothing
outside it should need changing, and three things that used to live in framework
code have been moved so they don't:

| Yours to change | Where it lives |
|---|---|
| An agent's identity and behaviour | `app/subagents/<id>/prompts.py` and `agent.py` |
| How an evidence-gathering agent should use its inputs | pass `instructions=` to `research.synthesize` from your `agent.py` |
| Your terminal asset's sections | a `SECTIONS` tuple in that agent's `prompts.py` — it builds the prompt, the schema and the completeness check |
| Your own asset shape | a Pydantic model in your agent's folder. `assetType`, `sourceType` and `sectionType` are open strings, so your vocabulary survives into the UI |

Renaming an agent means: its key in `agents`, its ids in `steps`, its folder under
`app/subagents/`, and the id it passes to `upstream_of()` in its own `agent.py`. If
you miss the last one the container fails at start with a message naming the agent
and listing the ids in the topology — it does not silently synthesize from its own
previous output. The test suite derives its expectations from this file, so a rename
does not turn tests red.

## `orchestrator`

| Key | Read by | Notes |
|---|---|---|
| `defaultModel` | `config.py` | Model for any agent that doesn't name its own. `BEDROCK_MODEL_ID` overrides it. |
| `policy.enabled` | `policy.tf`, `tool-plane.ts` | Creates the Cedar policy engine and attaches it to the Gateway. |
| `policy.mode` | same | `ENFORCE` obeys a DENY and blocks the call; `LOG_ONLY` evaluates and logs without blocking — the safe way to roll out. |
| `chatbot.enabled` | `bff/chatbot.py`, UI | Shows the chat icon. |
| `chatbot.model` | `bff/chatbot.py` | Model for the assistant's tool-use loop. |
| `chatbot.greeting` / `placeholder` | UI | Optional; the page's own text is the fallback. |
| `chatbot.tools.<name>` | `bff/chatbot.py` | One flag per assistant capability: `status`, `sessions`, `outputs`, `costs`, `latency`, `guardrails`, `evals`, `runEval`, `rerun`, `review`. Anything unlisted defaults to **on**. |

You never write Cedar by hand — the rules are generated from the `tools` block.
Every tool you declare is permitted; anything not declared is refused by Cedar's
default-deny, including a tool name a prompt-injected instruction invents.

## `agents.<id>`

The key **is** the agent id **and** the folder name under `app/subagents/`. It must
match `^[a-zA-Z][a-zA-Z0-9_]*$` — no hyphens, because the id becomes part of an
AgentCore Runtime name.

| Key | Read by | Notes |
|---|---|---|
| `name` | `registry.py`, UI | Display name. |
| `runtime` | `registry.py`, `subagent_runtimes.tf` | `main` runs in-process in the orchestrator; `dedicated` provisions the agent its **own** AgentCore Runtime and the orchestrator calls it via `InvokeAgentRuntime`. Same `run()` code either way — placement is config. |
| `model` | `registry.py` | **Optional.** Omit to use `orchestrator.defaultModel`. No shipped agent sets it; the mechanism is there when you want a bigger model for one step. |
| `maxTokens` | `registry.py` → `agent.max_tokens` | Output budget in tokens for this agent's model calls. |
| `temperature` | `registry.py` | **Optional**, defaults to 0. |
| `tool` | `registry.py` | A key in the `tools` block. Validated — a name that doesn't resolve fails at plan/synth. Omit and the agent reasons purely over upstream outputs. |
| `corpus` | `registry.py` | For a `type: "kb"` tool: which corpus to retrieve from. Must be one of that tool's declared `corpora`. |
| `produces` | `nodes.py` | The deliverable name, injected into the agent's task prompt. |
| `access` | UI chip | A short human label for the data source. **Only read when the agent has no `tool`** — with a tool, the chip is derived from the tool's type. Don't set both. |

### `maxTokens` — why it is per agent

Different agents emit very differently sized payloads. A research agent returns a
full structured JSON — summary, several classified findings, sources, limitations —
and the report agent returns a whole sectioned document.

Set it too low and the model's output is **truncated mid-JSON**, which does not
surface as "budget exceeded". It surfaces as *"the model returned no parseable
research JSON"*, because the framework refuses to emit a half-parsed asset that
downstream agents would treat as real findings. If you see that error, this is the
first thing to raise.

Current values: `intake` 3000, the four research agents 4000, `analysis` and
`recommendation` 6000, `report` 8000.

These were literals buried at each call site until they were moved here. Nothing in
`app/subagents/` passes `max_tokens` any more, and a test enforces that — otherwise
the number in this file would be quietly overridden by code.

### `agents.<id>.agentcore` — the AgentCore feature switches

The framework applies these around the agent's `run()`, so turning a capability on
or off is a config change. Omit a block to leave the capability off.

| Key | Read by | Notes |
|---|---|---|
| `memory.longTerm` | `context.py` | List of strategies: `semantic` extracts discrete insights into `insights/{actor}` (cross-run), `summary` maintains a running summary in `summary/{actor}/{session}` (session-scoped). Before `run()` the framework recalls past insights into the system prompt; after, it stores new ones. Namespaced per agent **and** per subject, so insights don't leak between topics. |
| `identity.outbound` | `context.py` | Credential providers this agent may fetch an OAuth token from, to call an external API **directly** via `ctx.get_identity_token`. Not needed for anything reached through the Gateway. |
| `guardrails.input` | `context.py` | Run the Bedrock guardrail on the agent's input, before the model sees it. |
| `guardrails.output` | `context.py` | Run it on the agent's output. Use this on any agent handling untrusted text. |
| `evaluations.enabled` | `evaluations/service.py` | Enables AgentCore Evaluations (LLM-as-judge) and shows the Evaluate button. |
| `evaluations.auto` | same | `true` scores the agent automatically at run completion; `false` means on demand only. Each evaluator is a billed model call, so `false` is the cheaper default. |
| `evaluations.evaluators` | same | Built-ins, `Builtin.<Name>`: `Coherence`, `Conciseness`, `Correctness`, `Faithfulness`, `GoalSuccessRate`, `Harmfulness`, `Helpfulness`, `InstructionFollowing`, `Refusal`, `ResponseRelevance`, `Stereotyping`, `ToolParameterAccuracy`, `ToolSelectionAccuracy`. |
| `policy.enabled` | `context.py` | An **app-level** Cedar check via `ctx.policy_check`. Secondary: tool calls through the Gateway are already authorized server-side by the attached engine. |

**Deliberately not per-agent**, though it might look like it should be:

- **Short-term memory / the checkpointer.** One LangGraph checkpointer per
  deployment, in AgentCore Memory. There is no per-agent switch.
- **Inbound authentication.** One API Gateway JWT authorizer per deployment.
- **Observability.** Telemetry is captured for every agent, always. There is no
  off switch, per agent or otherwise.
- **Which Gateway targets an agent may reach.** Controlled by the agent's `tool`
  binding plus the generated Cedar permit — enforced server-side, not by a list in
  this file.

Each of those had a key here at one point. They were removed because they implied
control that didn't exist.

## `tools.<name>`

The key is **both** the Gateway target name **and** the label an agent binds to via
`tool`, so the two can never drift. Five types:

| `type` | Backend | Required keys |
|---|---|---|
| `kb` | Bedrock Knowledge Base over `kb_docs/`, on S3 Vectors | `corpora` |
| `websearch` | The AWS-managed AgentCore Web Search connector | — |
| `mcp` | Any remote MCP server over Streamable HTTP | `endpoint` |
| `openapi` | Any REST API, from an OpenAPI schema in S3 | `schemaS3Uri` |
| `lambda` | Any Lambda you own | `lambdaArn` **or** `source`, plus `toolSchema` |

Shared keys:

| Key | Notes |
|---|---|
| `description` | Becomes the Gateway target description. Truncated to 190 characters on deploy. |
| `call` | Which tool on the target to invoke. **Required** when the target publishes more than one — the app refuses to guess, because calling the wrong tool returns real-looking data for a question nobody asked. |
| `arg` | The parameter the agent's query goes into. Default `query`. This is what makes an arbitrary MCP server reachable from config: every server names its parameters differently. |
| `args` | Fixed extra arguments sent on every call. |
| `auth` | Outbound auth to the endpoint: `none`, `apikey` (vaulted, sent as `X-API-Key`), `sigv4` (the Gateway signs with its own role — no secret). |
| `policy.tool` | Narrow the Cedar permit to one tool name. Omit for a target-level permit, which is what a remote MCP server needs since its tool names aren't known at deploy time. |
| `policy.restrictTo` | Argument allow-lists, e.g. `{ "filter": ["reference"] }`. Enforced at the Gateway, so a prompt-injected attempt to widen access is refused by infrastructure rather than by prompt wording. |
| `policy.permit` | `false` registers the target but deliberately does not permit it — useful for demonstrating default-deny. |

**Secrets never live here.** An API key goes in `tool_api_keys` in
`terraform.tfvars`, or `$TOOL_API_KEYS` for CDK, keyed by the tool name.

### `type: "kb"`

One per deployment. `corpora` lists the top-level folders under `kb_docs/`; each
becomes a `doc_type` tag, so different agents can be scoped to different document
sets. A corpus naming a folder that doesn't exist is rejected at plan time — before
that check existed, it produced a Cedar filter on a `doc_type` no chunk carried and
retrieval quietly returned nothing.

### `type: "websearch"`

One per deployment, and only in `us-east-1`, `eu-west-1`, `ap-northeast-1`.

Domain filtering has **two layers that compose on every request**:

- **Target level** — `targetIncludeDomains` / `targetExcludeDomains`. Set on the
  target, hidden from the agent, applied to every request. The enforceable layer.
- **Request level** — `includeDomains` / `excludeDomains`, plus `publishedFrom` /
  `publishedTo` as inclusive ISO-8601 UTC bounds. Sent per call by the app and
  supplied by the caller, so treat it as scoping, not a boundary.

A domain is dropped if it appears on **either** exclude list, and returned only if
it appears on **every** include list that is set — so a request-level filter can
never override a target-level exclude or widen past a target-level include.

`maxResults` is 1–25, default 10. The query is capped at 200 characters and the app
clamps it. Request-level filters need connector v1.2.0+; `connectorVersion` pins it,
but **only on the Terraform path** — CloudFormation's connector source accepts only
`connectorId`, so CDK rejects the field rather than accepting and ignoring it.

**Acceptable use:** you must retain and display the source citations returned with
each result. The framework enforces the display half — the connector's
`title`/`url`/`publishedDate` are preserved into the evidence, every citation URL is
verified against that evidence before it is kept, and the UI renders each source as
a link. An unverifiable URL is dropped and a `sourced-fact` resting on it is
downgraded to `agent-interpretation`.

### `type: "mcp"`

To use your own MCP server, change `endpoint` (and `call`/`arg` to match one of its
tools). That is the entire change — no code, no IaC, no policy edit.

`listingMode`: `DEFAULT` pre-indexes and caches the server's tool catalogue (needed
for semantic tool search); `DYNAMIC` forwards `tools/list` at invocation time, which
avoids a stale catalogue when the server's tool set changes. Either works.

**On tool names:** the Gateway composes every published name as
`<targetName>___<toolName>`, and AWS *also* prefixes tools on its managed servers
with `aws___`. So the shipped `docs` target publishes
`docs___aws___search_documentation`, and `call` is `aws___search_documentation` —
the published name minus the `<targetName>___` part. Doubly prefixed names work
fine; nothing splits on `___`.

**If you verify with a `tools/list` call, follow `nextCursor`.** The response is
paginated, and a target whose tools land on page two looks empty if you only read
page one.

### `type: "lambda"`

The escape hatch: a Lambda reaches what the Gateway cannot — a warehouse (Redshift,
Snowflake), any RDBMS, an internal service, anything inside a VPC.

Supply the function **one** of two ways:

- `lambdaArn` — a function **you** own. The framework only registers it: it grants
  the Gateway `lambda:InvokeFunction` on exactly that ARN and, for a same-account
  function, adds the resource-policy statement. It never touches your code or your
  execution role. A cross-account function works, but its owning account must add
  that statement itself.
- `source` — a function the **framework** ships and deploys, which keeps this file
  account-neutral (a real ARN would pin it to one AWS account). The only accepted
  value is `tool_lambda`, the run-history demo under `orchestrator/tool_lambda/`.
  It is not a general "deploy any directory" feature: a framework-deployed function
  needs an execution role config cannot express, so that role is fixed at logs plus
  read-only on this deployment's own DynamoDB tables.
  The demo also shows a trap worth copying: a tool that reads this deployment's own
  state can see the **caller's own run**, which is always still `running` and has no
  outcome to learn from. Left in, the agent cited the very run it was part of as prior
  art. `tool_lambda` filters in-flight runs (`running`, `cancelling`) out of its scan
  and reports how many it excluded, so the count is honest rather than silently short.
  `waiting_human` stays — that is a real state and cannot be the caller.

`toolSchema` **declares** the tools, because unlike an MCP server there is no
`tools/list` for the Gateway to call. Each entry is `{ name, description,
properties }`, and each property is `{ type, required, description }` where `type`
is one of `string`, `number`, `integer`, `boolean`, `array`, `object`.

One function may publish several tools — the Gateway passes the tool name through so
your handler can dispatch on it. When it does, `call` is required and `arg` must
name a property of the called tool. Both are checked at plan/synth time, along with
the ARN format, an empty schema, a tool with no properties, and an unsupported
property type. All of those would otherwise be accepted by the API and then publish
a tool the agent cannot call — which shows up as an empty answer, not an error.

## `authorization`

Answers "may *this user* do this?", which the JWT authorizer does not. Six actions:
`decision` (approve / revise / deny a gate), `rerun`, `cancel`, `evaluate`,
`insights`, `delete`.

- An action **not listed** is unrestricted, so deleting the block restores the
  pre-RBAC behaviour where any authenticated user could do anything.
- An action listed **with groups** needs at least one of them.
- An action listed with an **empty list** is denied to everyone — that is how you
  switch a capability off.

`groupsClaim` is the JWT claim holding the caller's groups: `cognito:groups` for
Cognito, or a namespaced custom claim for Auth0 (e.g. `https://your-app/roles`,
added by a post-login Action) — Auth0 will not emit an unnamespaced one.

Both IaC paths create a Cognito group for every group named here. *Membership* is
not managed in IaC: it is per-person and changes far more often than a deploy.

Enforced in `bff/authz.py` on the mutating routes **and** applied to the in-app
assistant's action tools, so "ask the chatbot to approve it" is not a way around it.
Read-only endpoints are not gated. A non-empty `actions` map with `idp = "none"` is
rejected at plan/synth time — with no authorizer there are no claims, so every rule
would deny everyone.

## `guardrail`

The Bedrock Guardrail policy. Agents opt in per agent via
`agentcore.guardrails.{input,output}`; this block defines *what* is enforced.

| Key | Notes |
|---|---|
| `contentFilters` | Filter type → strength (`NONE`/`LOW`/`MEDIUM`/`HIGH`), applied to input and output. `PROMPT_ATTACK` is input-only, so its output strength is forced to `NONE`. |
| `deniedWords` | Exact custom terms. `BLOCKED_DEMO_TERM` is a deterministic string for testing that blocking works end to end — **replace it**. |
| `managedWordLists` | e.g. `PROFANITY`. |
| `deniedTopics` | Each needs a `name` and a `definition`; `examples` optional. |
| `piiEntities` | Bedrock PII entity type → `BLOCK` or `ANONYMIZE`. |
| `blockedInputMessage` / `blockedOutputMessage` | What the user sees when something is blocked. |

Omit any key to drop that policy; omit the whole block for an empty guardrail.

## `steps`

Each entry is one of:

- `{ "agent": "id" }` — a single agent
- `{ "parallel": ["a", "b"] }` — fan out, join at one gate
- `{ "sequence": ["a", "b"] }` — chain in order

Add `"hitl": true` for a human-review gate after the step, and `gateId` / `gateName`
to name it (the id is how the BFF and UI address the gate).

Revise behaviour differs by shape, and the difference is the point: a **parallel**
gate re-runs only the agents the reviewer flags; a **sequence** gate re-runs the
whole chain from its first agent, because a later member's output depends on the
earlier ones.

Reorder, add or remove steps freely — the graph, the UI diagram and the IaC all
follow. The parallel stage renders two agents per row.

## What is checked before anything deploys

Both IaC paths validate this file at plan/synth time and name the exact problem.
See §7 of [`GETTING_STARTED.md`](../../GETTING_STARTED.md) for the full list. The
corpora and `lambda` schema checks matter most, because those are the failures that
used to be silent.
