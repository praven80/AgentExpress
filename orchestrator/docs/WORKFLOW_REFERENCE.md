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
| Which agentic framework an agent reasons with | pass `think=` to `research.synthesize`, or just write it in `run()`. Per agent — `web_search` uses Strands, `knowledge_research` a nested LangGraph, the others none. Route the model call through `ctx.llm` or you lose guardrails, cost telemetry, memory and truncation detection silently; see the README |
| Your terminal asset's sections | a `SECTIONS` tuple in that agent's `prompts.py` — it builds the prompt, the schema and the completeness check |
| Framework value sets (runtimes, tool types, memory strategies, RBAC action names, …) | `app/vocabulary.json` — framework-owned, NOT yours. Declared once and read by all three planes (Python, CDK, Terraform), each set with a comment saying why it is closed. Listed here so you know where an "invalid value" message comes from |
| Your own asset shape | a Pydantic model in your agent's folder. `assetType`, `sourceType`, `sectionType` and `artifactType` are open strings, so your vocabulary survives into the UI. Narrow any of them on your own model (`artifact_type: Literal["dicom-study"]`) if you want the check — that is the place that knows. The UI chooses a layout by the SHAPE of each field, not by its name, so a contract the framework has never seen still renders as a first-class one |

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
| `runtimeInvoke.maxAttempts` | `config.py` → `agentcore_agent._agentcore` | **Total** attempts per call to a `dedicated` agent's runtime, not retries-after-the-first. Default **1, i.e. no retrying**, which is deliberate: `InvokeAgentRuntime` is synchronous, slow and **not idempotent**, so a retry does not replace the attempt it followed — the remote container is already working and cannot tell the caller stopped listening. boto3's own default (`legacy` mode, up to 5 attempts) therefore lets one transient blip run a research agent twice, bill both model calls, and return whichever answered last, with nothing in the timeline to show it: the node logs "Invoking dedicated AgentCore Runtime" once, before any retry exists. Observed on a live run — two invocations of the `web_search` runtime with different `requestId`s, 13s apart, for one node execution, $0.0158 spent on a discarded answer. For a call this long the failure that matters is a lost response to work that already succeeded, and retrying that is strictly worse than failing: an error reaches the reviewer, a duplicate just inflates the bill. Raise it only if you have made the call idempotent. |
| `runtimeInvoke.readTimeoutSeconds` | same | How long to wait for a dedicated agent's response. Default **120**, well above boto3's 60 and the ~15–20s the shipped research agents take. With retrying off this timeout is fatal to the run, so keep it comfortably above your slowest agent — otherwise you trade a duplicate for a truncated run, which is not the trade being made here. |
| `a2aInvoke.timeoutSeconds` | `config.py` → `a2a_agent` | Per HTTP request to a `runtime: "a2a"` agent (the card fetch, each RPC call). Default **30**; the shipped sample sets **150**, because two of its remote agents synthesize a full asset rather than answering a short question. Keep it **above** the remote agent's own timeout (the shipped stand-in's Lambda is 120s) so the side that gives up first is the side that can say why — a client that abandons the request leaves no diagnosis and cannot tell a slow agent from a dead one. |
| `a2aInvoke.pollIntervalSeconds` | same | Gap between `tasks/get` calls while a remote task is still working. Default **2**. Their rate limit is unknown to you; a busy loop is rude and may be throttled, which then looks like their agent failing. |
| `a2aInvoke.maxPollSeconds` | same | Total wall-clock before giving up on a remote task, failing the run with the last state seen. Default **300**. Separate from `runtimeInvoke` on purpose: a dedicated runtime is yours and you know how slow it is, whereas A2A models work as a *Task* precisely so it can take minutes — so the budget that matters is not one request timeout but how long you are willing to wait overall. Bounded rather than open-ended, because a task that never leaves `working` would otherwise hold the workflow until the runtime's own 8-hour ceiling. |
| `policy.enabled` | `policy.tf`, `tool-plane.ts` | Creates the Cedar policy engine and attaches it to the Gateway. |
| `policy.mode` | same | `ENFORCE` obeys a DENY and blocks the call; `LOG_ONLY` evaluates and logs without blocking — the safe way to roll out. |
| `modelRates` | `config.py:MODEL_RATES` → `observability/pricing.py` | OPTIONAL per-model token rates, keyed by a substring of the model id: `{"nova-pro": {"input": 0.80, "output": 3.20}}` (USD per 1M tokens). The built-in table knows a handful of Claude ids; **anything else is priced at a fallback and the telemetry row is marked `rates_known=false`** — the UI shows `~$` and the CSV/JSON export carries the flag. Set this to get real figures for your own model, or to correct a price that changed, without editing framework code. A longer key wins over a shorter one, so `claude-haiku-4-5` beats `claude`. |
| `insights.lookbackHours` | `config.py:INSIGHTS` → `features/optimization/insights.py` | How far back the cross-run AgentCore Insights analysis looks. Default **168** (7 days). |
| `insights.pollTimeoutSeconds` | same | How long to wait for the batch analysis before reporting that it is still running AWS-side. Default **900**. A timeout is now RECORDED with its reason rather than being stored as a permanent "in progress". |
| `insights.pollIntervalSeconds` | same | Gap between `GetBatchEvaluation` polls. Default **20**. |
| `chatbot.enabled` | `bff/chatbot.py`, UI | Shows the chat icon. |
| `chatbot.model` | `bff/chatbot.py` | Model for the assistant's tool-use loop. |
| `chatbot.greeting` / `chatbot.placeholder` | UI | The assistant's opening message and input placeholder. Shipped in the BFF projection (`orchestrator/bff/workflow.py`); the page keeps a short generic fallback if you omit them. |
| `chatbot.tools.<name>` | `bff/chatbot.py` | One flag per assistant capability: `status`, `sessions`, `outputs`, `costs`, `latency`, `guardrails`, `evals`, `runEval`, `rerun`, `review`. Anything unlisted defaults to **on**. |

You never write Cedar by hand — the rules are generated from the `tools` block.
Every tool you declare is permitted; anything not declared is refused by Cedar's
default-deny, including a tool name a prompt-injected instruction invents.

## `ui`

Every user-facing string in the shell, so re-branding is a config edit rather than an
`index.html` edit. All optional — the page has a neutral fallback for each — and all
guarded: `tests/test_config_keys.py` fails on a `ui` key nothing renders, because a
presentation key that does nothing is worse than an absent one (you edit it and see
no change, with nothing to tell you why).

| Key | Where it shows |
|---|---|
| `title` | Browser tab title |
| `heading` | The header, top left |
| `defaultTopic` | Pre-filled topic, AND the server-side default when a run is started with no topic (`config.py`, `bff/handler.py`) |
| `topicPlaceholder` | The topic input's placeholder and accessible name |
| `subjectPlaceholder` | The subject input's placeholder and accessible name |
| `subjectHint` | The subject input's tooltip |
| `assistantTitle` | The assistant panel's title |
| `assistantSubtitle` | The line under it |

## `agents.<id>`

The key **is** the agent id, and — for an agent whose code you ship — the folder name
under `app/subagents/`. It must match `^[a-zA-Z][a-zA-Z0-9_]*$` — no hyphens, because
the id becomes part of an AgentCore Runtime name.

| Key | Read by | Notes |
|---|---|---|
| `name` | `registry.py`, UI | Display name. |
| `runtime` | `registry.py`, `subagent_runtimes.tf` | `main` \| `dedicated` \| `a2a`. See below — the first two are placements of *your* code, the third is an agent you don't operate. |
| `model` | `registry.py` | **Optional.** Omit to use `orchestrator.defaultModel`. No shipped agent sets it; the mechanism is there when you want a bigger model for one step. |
| `maxTokens` | `registry.py` → `agent.max_tokens` | Output budget in tokens for this agent's model calls. |
| `temperature` | `registry.py` | **Optional**, defaults to 0. |
| `tool` | `registry.py` | A key in the `tools` block. Validated — a name that doesn't resolve fails at plan/synth. Omit and the agent reasons purely over upstream outputs. |
| `corpus` | `registry.py` | For a `type: "kb"` tool: which corpus to retrieve from. Must be one of that tool's declared `corpora`. |
| `produces` | `nodes.py` | The deliverable name, injected into the agent's task prompt. |
| `access` | UI chip | A short human label for the data source. **Only read when the agent has no `tool`** — with a tool, the chip is derived from the tool's type. Don't set both. |
| `agentCard` | `a2a_agent.py` | **`runtime: "a2a"` only.** The remote agent's base URL or Agent Card URL. Must be `https://`. |
| `auth` | `a2a_agent.py` | **`runtime: "a2a"` only.** `none` \| `bearer` \| `oauth2` \| `sigv4`. Defaults to `none`. Required to be `sigv4` with `source` — see the stand-in, below. |
| `source` | `a2a.tf`, `orchestrator-stack.ts` | **`runtime: "a2a"` only.** The framework-deployed stand-in to reach instead of a committed URL. `a2a_lambda` is the only value. Mutually exclusive with `agentCard`. |
| `skill` | `a2a_agent.py` (endpoint path), IaC | **With `source` only.** Which of the stand-in's published skills this agent is. An external agent advertises its skills in its own Agent Card, so this means nothing without `source` and is rejected there. |

### `runtime` — where the agent actually runs

Three placements. The first two differ only in *where your code executes*; the third
is a different thing entirely.

| | What it is | Needs a folder under `app/subagents/`? |
|---|---|---|
| `main` (default) | a LangGraph node inside the orchestrator container | yes |
| `dedicated` | its **own** AgentCore Runtime, called with `InvokeAgentRuntime` | yes |
| `a2a` | an agent **you do not operate**, called over the Agent2Agent protocol at its Agent Card URL | **no** |

`main` and `dedicated` run the same `run()` code — placement is config, so moving an
agent to its own container and its own scaling is a one-word edit.

`a2a` is not a placement, it is a **trust boundary.** The agent is somebody else's
service: a partner's, another team's, or a managed one. Nothing of it lives in this
repo, which is the point — it turns a multi-agent workflow into a multi-*organisation*
one. That is also why it is a `runtime` value and not a `tool`: a tool returns data
for one of *your* agents to reason over, whereas this replaces the agent.

```json
"credit_check": {
  "name": "Partner Credit Check",
  "runtime": "a2a",
  "agentCard": "https://agents.partner.example/credit",
  "auth": "bearer",
  "produces": "credit-assessment"
}
```

It is an ordinary node in the topology: put it in `steps` anywhere, gate it with
`hitl`, route to it with `branch`, re-run it, read its version history. The framework
sends it the same inputs an in-process agent reads — the request, the approved
upstream outputs, any reviewer feedback — as one opaque text part, because A2A carries
text and the remote agent has its own output contract. Demanding yours would make it
un-integrable.

**Discovery.** `agentCard` may be the agent's base URL (the card is read from
`<url>/.well-known/agent-card.json`) or the card URL itself. The card is *read*, not
assumed: if it declares `supportedInterfaces`, the first JSON-RPC entry wins, in the
card's preference order. So an agent may serve its RPC endpoint on a different host
from its card and still work.

**Auth.**

| `auth` | What happens | Where the credential comes from |
|---|---|---|
| `none` | no `Authorization` header | — |
| `bearer` | `Authorization: Bearer <token>` | `var.a2a_tokens` / `$A2A_TOKENS`, keyed by agent id |
| `oauth2` | a token minted per call | the agent's existing `agentcore.identity.outbound` provider |
| `sigv4` | every request signed with SigV4 | the orchestrator's own execution role. Nothing in config, nothing to rotate, nothing to leak — the mode for an AWS-hosted agent behind IAM, which is what the shipped stand-in is |

Tokens are **never** written in `workflow.json` — it is committed, and these are
credentials for somebody else's service. A `bearer` agent with no token supplied fails
at plan/synth, not at runtime, because the alternative is a 401 from a service you do
not control.

**Keys that do not apply.** `model`, `temperature`, `maxTokens`, `tool` and `corpus`
are all rejected on an `a2a` agent. A remote agent makes its own model call and reaches
its own data sources, so those would read as governing its cost and its access while
doing nothing. Setting them fails validation rather than misleading you. Its output
budget is its own: in the shipped stand-in each skill declares one
(`a2a_lambda/handler.py` `maxTokens`), which is why a reviewer skill and a full
synthesis skill can be backed by the same function without one starving the other.

**What you keep.** The framework wraps the call in *your* container, so **guardrails**
apply to it. **Long-term memory works in both directions**: `agentcore.memory.longTerm`
recalls this agent's past insights and carries them to the remote agent inside the A2A
task, under `recalledContext`, with the same caveat `ctx.llm` attaches locally — that
they are unverified recollections and never evidence. Its reply is then stored as a new
insight. This differs from `dedicated` on purpose: `InvokeAgentRuntime` has a fixed
payload that cannot carry a recollection, so recalling for one would be billed and
thrown away, whereas A2A carries opaque text and can. Worth knowing before you declare
it: this sends your deployment's accumulated recollections to an agent outside it, which
is why it follows `agentcore.memory` rather than happening unconditionally.

**What you lose.** **Evaluations** fall back to a role descriptor: there is no local
model call to capture a prompt from, because the reasoning happened on their side. The
descriptor plus the real output still supports Coherence, Helpfulness and
InstructionFollowing; it does not support **Faithfulness**, because there is no source
context to be faithful to — so prefer `auto: false` and a narrower evaluator list on a
remote agent, as the two shipped ones do. A number computed from a descriptor reads
exactly like a measured one. And any **tool** the remote agent uses is outside your
Cedar policy: you are trusting their boundary, not enforcing yours. The UI marks it with
an `a2a` chip and labels its source `A2A · <host>` (or `A2A · <source>` for the shipped
stand-in) so a reviewer can see which parts of a deliverable came from outside.

**When it fails** it raises `RemoteAgentUnavailable` with whatever the remote side
said, and the run fails with that reason on that node. There is no fallback: an empty
answer would flow into every downstream agent looking exactly like a real finding of
nothing. A SigV4 403 additionally reports which principal signed, because that is the
only question a rejected signature raises and the status alone does not answer it.

**What you get back, and who builds it.** If the remote agent replies with a JSON
object and the agent declares `produces`, the framework wraps that object in the asset
envelope — `assetId`, `assetType`, `version`, `status`, `createdAt`, `createdByAgent`,
`sourceAssetIds` — so a remote step is a first-class contract producer and a downstream
agent can cite it.

The framework does this rather than asking the remote agent to, because the envelope is
bookkeeping **about your run**: `version` is how many times that step has run in this
session, `createdByAgent` is the id *you* gave it, `assetType` is *your* `produces`,
and `sourceAssetIds` are the upstream assets *your* graph handed over. A remote agent
asked to invent them gets `version` wrong on every re-run — silently, because a wrong
integer still validates.

Fields the remote DID send win; the envelope only fills gaps. A **prose** reply is
returned exactly as written: a remote reviewer that answers in sentences is a
legitimate remote agent, and wrapping its text in a JSON envelope would make the
timeline and the report worse. Only a JSON object is treated as contract content.

> This was a real gap, not a hypothetical one. Without the envelope a remote reply had
> no `assetId`, and `synthesis.upstream_context` — which collects exactly that field for
> claim tracing — took the reply in as a block with nothing to cite. Measured on a live
> run: the remote output reached the analysis and the final report and was quoted there,
> but never appeared in any `sourceAssetIds`. Used and attributed in prose; not
> machine-traceable.

**The one thing that cannot be checked.** The merged asset is NOT validated against a
pydantic contract, because there is no local contract class for an agent whose code is
somebody else's — `produces` names the type, it does not import a model. That is the
real cost of the boundary, and it is why the UI renders an asset by *shape* rather than
by field name. If you need schema enforcement, put the remote agent behind a
`dedicated` wrapper that validates its reply, where the contract is yours to declare.

### The shipped stand-in (`source: "a2a_lambda"`)

`runtime: "a2a"` needs an agent you do not operate, so a committed placeholder URL would
fail every run. The framework therefore ships one: a real A2A server
(`a2a_lambda/handler.py`) in its own Lambda behind its own Function URL, deployed only
when an agent asks for it with `source: "a2a_lambda"`. `skill` picks which of its
published skills you are reaching, selected by PATH (`/analysis`), so one function backs
several genuinely different agents.

It publishes four, and they are a **catalogue rather than a roster** — `workflow.json`
decides which become agents, and a skill nobody names is simply never reached:

| `skill` | What it is | Its output shape |
|---|---|---|
| `analysis` | synthesizes the approved research into one analysis, tracing claims | the CONTENT of this sample's `Analysis` contract |
| `recommendation` | turns the approved analysis into prioritized actions | the CONTENT of its `Recommendation` contract |
| `compliance` | an outside reviewer on regulatory and data-handling exposure | its own |
| `resilience` | an outside reviewer on failure modes, blast radius and recovery | its own |

The shipped sample wires up the first two, as its Analysis → Recommendation stage. Add
`"skill": "compliance"` to a new `a2a` agent and you have a third with no other edit.

The split between those two groups is the interesting part. A reviewer's shape is *its
own*, which is the normal case for a third party and the reason A2A carries opaque text.
The two synthesis skills are asked for **this sample's contract content**, because they
are steps in the deliverable rather than commentary on it — and note what their shapes do
*not* contain: no `assetId`, `version`, `status`, `createdAt` or `sourceAssetIds`. Those
are your run's bookkeeping, and the framework stamps them (see above).

A skill that hits its output ceiling is **refused** as a failed task rather than
returned. A local agent detects its own truncation and stamps a `limitations` entry; that
signal cannot cross the boundary — from the client's side a cut-off reply is just a
reply, and JSON repair turns it into a complete-*looking* asset with the end of the
longest list missing. Silent partial content in front of an approver is worse than a
failed step.

The stand-in's Lambda timeout is deliberately **shorter** than
`orchestrator.a2aInvoke.timeoutSeconds` (120s against the sample's 150s): the side that
gives up first should be the side that can say why. A client that abandons the request
first leaves no diagnosis and cannot tell a slow agent from a dead one.

It requires `auth: "sigv4"`, and that is validated rather than defaulted: the endpoint is
an `AWS_IAM` Function URL, so an unsigned request is a guaranteed 403 that surfaces as
"could not read the Agent Card … HTTP 403" — which reads like a missing agent instead of
like wrong config. Observed on a live run, because `auth` defaults to `none`.

Point an agent at a real partner's `agentCard` instead and none of that infrastructure
is provisioned.

> **There is no `repair` key, and this file used to claim there was.** An earlier
> version of the framework shipped an output-rules engine that could re-ask an agent
> to fix its own violations. It was removed, because it encoded one editorial standard
> (`document`, `record`, `evidence`, `transcript` were treated as artifact nouns) that
> would misfire in a legal or medical domain — and a framework has no business holding
> that opinion. The section documenting it outlived it, which is exactly the failure
> `tests/test_config_keys.py` exists to prevent, in reverse: a key a reader believes in
> and nothing reads. If you want output checks, put them in your own agent under
> `app/subagents/<id>/`, where the standard is yours.

### `maxTokens` — why it is per agent

Different agents emit very differently sized payloads. A research agent returns a
full structured JSON — summary, several classified findings, sources, limitations —
and the report agent returns a whole sectioned document.

Set it too low and the model's output is **truncated mid-JSON**, which does not
surface as "budget exceeded". It surfaces as *"the model returned no parseable
research JSON"*, because the framework refuses to emit a half-parsed asset that
downstream agents would treat as real findings. If you see that error, this is the
first thing to raise.

Current values: `intake` 3000, the three evidence-gathering research agents 4000,
`cost_research` 1500 (it emits a small rates table, not prose), `report` 8000.

`analysis` and `recommendation` have none, because they are `runtime: "a2a"` and a remote
agent's budget is its own — for the shipped stand-in it is 6000 each, declared per skill
in `a2a_lambda/handler.py`.

These were literals buried at each call site until they were moved here. Nothing in
`app/subagents/` passes `max_tokens` any more, and a test enforces that — otherwise
the number in this file would be quietly overridden by code.

### `agents.<id>.agentcore` — the AgentCore feature switches

The framework applies these around the agent's `run()`, so turning a capability on
or off is a config change. Omit a block to leave the capability off.

| Key | Read by | Notes |
|---|---|---|
| `memory.longTerm` | `context.py` | List of strategies: `semantic` extracts discrete insights into `insights/{actor}` (cross-run), `summary` maintains a running summary in `summary/{actor}/{session}` (session-scoped). Before `run()` the framework recalls past insights into the system prompt; after, it stores new ones. Namespaced per agent **and** per subject, so insights don't leak between topics. **Only `semantic` and `summary`** — those are the strategies the IaC provisions, and an unrecognised name is rejected at container start. It used to be accepted and then searched as its own namespace, which nothing writes to, so recall returned nothing and reported success. **Where the recall/store runs depends on the placement:** for `main` it is the orchestrator; for `dedicated` it is inside that agent's own container, because that is where `ctx.llm` injects the insights; for `a2a` it is the orchestrator, which carries the recalled insights to the remote agent inside the A2A task (`recalledContext`, with the caveat attached) and stores its reply afterwards. |
| `identity.outbound` | `context.py` | Credential providers this agent may fetch an OAuth token from, to call an external API **directly** via `ctx.get_identity_token`. Not needed for anything reached through the Gateway. |
| `guardrails.input` | `context.py` | Run the Bedrock guardrail on the agent's input, before the model sees it. |
| `guardrails.output` | `context.py` | Run it on the agent's output. Use this on any agent handling untrusted text. |
| `evaluations.enabled` | `evaluations/service.py` | Enables AgentCore Evaluations (LLM-as-judge) and shows the Evaluate button. |
| `evaluations.auto` | same | `true` scores the agent automatically at run completion; `false` means on demand only. Each evaluator is a billed model call, so `false` is the cheaper default. |
| `evaluations.evaluators` | same | Built-ins, `Builtin.<Name>`: `Coherence`, `Conciseness`, `Correctness`, `Faithfulness`, `GoalSuccessRate`, `Harmfulness`, `Helpfulness`, `InstructionFollowing`, `Refusal`, `ResponseRelevance`, `Stereotyping`, `ToolParameterAccuracy`, `ToolSelectionAccuracy`. Validated on SHAPE (`Builtin.<Name>` or `Custom.<Name>`) rather than against this list, so an evaluator AWS adds later needs no framework edit — but a typo like `Faithfullness` is caught at container start instead of surfacing as an API error the first time an evaluation runs. |
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
| `rowFields` | Maps the roles an agent needs to the field names your payload actually uses, so an agent that *computes* from tool output stays config-driven. See [`rowFields`](#rowfields--for-an-agent-that-computes-rather-than-narrates) below. |
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
  value is `tool_lambda`, the pricing demo under `orchestrator/tool_lambda/`. It is
  not a general "deploy any directory" feature: a framework-deployed function needs
  an execution role config cannot express, so that role is fixed at logs plus
  `pricing:GetProducts` / `pricing:DescribeServices` — public list prices, no
  resource-level permissions available and none needed.

**What the shipped demo does, and why it is that.** `aws_prices(services, region)`
takes service names and returns their real on-demand unit rates from the AWS Price
List Query API. It answers the one question every design review asks about a
workload, for *any* use case, without the workflow needing to know the use case.

Three things in it are worth copying into your own Lambda:

- **It returns rates and never a total.** A total needs usage volumes, and volumes
  are a property of the workload, not of AWS. A tool that multiplies a real rate by
  a volume nobody supplied has invented the volume, and the invented half is the
  half that makes the total wrong. The tool instead names the volumes a total would
  need, and leaves supplying them to whoever knows them.
- **It resolves names explicitly, and reports what it cannot.** Pricing ServiceCodes
  are neither guessable nor consistent (`AmazonStates`, not `AWSStepFunctions`), so
  the mapping is a table, not a transformation. A name with no match or more than one
  comes back in `unresolvedServices` — pricing the wrong service is worse than
  admitting the gap, because the number looks exactly as authoritative as a correct
  one.
- **It curates which dimensions come back.** Unfiltered, "price AWS Lambda" returns
  EC2-shaped hourly instance rates. Instance and provisioned SKUs are excluded and a
  short per-service list of consumption dimensions is preferred, matched with a
  region-prefix tolerance so the same patterns work in every region.

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

### `rowFields` — for an agent that computes rather than narrates

Optional, and only meaningful on a tool whose results are structured. It maps the
role an agent needs to the field name **your** payload uses:

```json
"rowFields": { "service": "service", "dimension": "dimension",
               "price": "pricePerUnit", "unit": "unit", "currency": "currency" }
```

An agent then calls `ctx.call_tool_rows(...)` and reads `row[fields["price"]]`
instead of parsing a rendered sentence. This is what keeps "a Lambda can return
anything" true: `cost_research` works unchanged against a payload of
`service/dimension/pricePerUnit` or one of `svc/dim/rate`, because the field names
live in config and not in the agent. Regex-parsing the tool's prose would have tied
the agent to one payload shape.

Omit it and nothing changes — agents that hand evidence to a model use the `text`
each row also carries.

## `authorization`

Answers "may *this user* do this?", which the JWT authorizer does not. Seven actions:
`start` (begin a run), `decision` (approve / revise / deny a gate), `rerun`,
`cancel`, `evaluate`,
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
to name it (the id is how the BFF and UI address the gate). Add `branch` to let the
step's own output choose what runs next — see below.

Those five are the whole key set: `agent` / `parallel` / `sequence` (pick one),
`hitl`, `gateId`, `gateName`, `branch`.

Revise behaviour differs by shape, and the difference is the point: a **parallel**
gate re-runs only the agents the reviewer flags; a **sequence** gate re-runs the
whole chain from its first agent, because a later member's output depends on the
earlier ones.

Reorder, add or remove steps freely — the graph, the UI diagram and the IaC all
follow. The parallel stage renders two agents per row.

### `branch` — the output decides what runs next

`steps` is a fixed pipeline and `hitl` lets a *human* redirect it. `branch` is the
third case: the step's own output picks the next step. A triage agent sends a
high-risk case to a deep review and a routine one straight to settlement; an intake
agent ends the run when the request is not actionable. No agent code — the deciding
agent just returns its normal output.

```json
{ "agent": "triage",
  "branch": {
    "when": [
      { "field": "disposition", "equals": "escalate", "goto": "investigator" },
      { "field": "amount",      "gte": 10000,        "goto": "investigator" },
      { "field": "claimId",     "exists": false,     "goto": "END" }
    ],
    "default": "adjuster"
  } }
```

| Key | Meaning |
|---|---|
| `when` | rules, tried **in order**; the first match wins, so put the specific case first |
| `when[].field` | dot path into the agent's JSON output (`scope.tier`, `findings.0.claim` — a numeric segment indexes a list). **Omit it** and the comparison runs against the raw output text, so an agent that returns prose is still branchable |
| `when[].goto` | the step to run on a match, or `"END"` to finish the run |
| `default` | where to go when no rule matched. Omit it and the run simply continues to the next step |

A `default` **with no `when`** is an unconditional jump. That is what makes two
paths exclusive rather than merely optional: with steps
`[triage, investigator, adjuster, settlement]`, `triage` picks a specialist and
`investigator` carries `{ "default": "settlement" }` so the escalated path does not
fall into the adjuster's step on its way out.

**Operators.** One or more per rule; several are ANDed.

| | |
|---|---|
| `equals`, `notEquals` | single value |
| `in` | list of alternatives |
| `contains` | substring of a string, or membership of a list / dict keys |
| `exists` | `true` = present and not null/`""`/`[]`/`{}`; `false` = the negation |
| `gt`, `gte`, `lt`, `lte` | numeric. A **list or dict compares by its length**, so `{ "field": "openQuestions", "gt": 0 }` reads as "there is at least one" |

`equals`, `notEquals`, `in` and `contains` compare on stripped, case-folded text,
because the value was written by a model: one told to return `escalate` will
sometimes return `Escalate`. A branch that took the default because of a capital
letter would be an expensive thing to debug.

**Targets name a STEP, not an agent inside one** — a single-agent step's `agent` id,
or a group step's `gateId`. Jumping to a `parallel` stage therefore enters the whole
stage rather than stranding its gate on siblings that never ran. Targets must be
*later* steps; going backwards is what a review gate's `revise` is for.

**Where it may go.** On a single-agent step, or on a `sequence` step (its **last**
agent decides). Not on a `parallel` step — a group has no single agent whose output
decides — and not on the last step, which has nowhere to route.

**What you see.** The stage gets a `⑂ Branch` pill in the diagram (hover for the
rules), the timeline records the rule that matched and where it went, and the agents
the run bypassed are marked **skipped** rather than left looking queued. With a
review gate on the same step the human approves first, then the branch reads the
output they approved.

**The branch this sample ships**, on its intake step — two guards that fire only on a
degenerate brief, so the eight-agent demo is unchanged:

```json
{ "agent": "intake", "hitl": true,
  "branch": {
    "when": [
      { "field": "objective",    "exists": false, "goto": "END" },
      { "field": "keyQuestions", "lt": 1,         "goto": "analysis_reco" }
    ]
  } }
```

No objective in the brief and there is nothing downstream can work with, so end the
run rather than spend seven more agents. No research questions and there is nothing
for the research stage to gather, so jump to `analysis_reco` — the `gateId` of the
sequence step — and synthesize over the brief alone. On a normal request neither
matches and the run continues to the research stage, with one timeline line saying so.

Note what the second rule teaches: `keyQuestions` is a **list**, and the numeric
operators compare a collection by its **length**, so `lt: 1` means "empty". A field
that is *absent* is different again — it matches only `exists: false`, because every
other operator needs a value to compare.

Everything above is checked before anything deploys, because each of these mistakes
is otherwise silent — a misspelled operator, a rule with no comparison and a target
that names nothing all evaluate to "no match", so the run quietly takes the default
on every request and the branch looks like it is working.

## What is checked before anything deploys

Both IaC paths validate this file at plan/synth time and name the exact problem.
See §7 of [`GETTING_STARTED.md`](../../GETTING_STARTED.md) for the full list. The
corpora and `lambda` schema checks matter most, because those are the failures that
used to be silent.
