# Getting started — your own multi-agent system in a day

This framework is **configuration driven**. To run your own multi-agent architecture
against your own documents and your own MCP servers, you edit **two files** and add **one
folder per agent** — plus, only if you ask the framework to supply a tool's artifact, one
folder per such tool:

| What | Where | You change |
|---|---|---|
| The workflow | `orchestrator/app/workflow.json` | your tools, your agents, your topology, gates, RBAC, guardrail, UI strings |
| Each agent's logic | `orchestrator/app/subagents/<agent_id>/` | a prompt, a contract, a `run()` |
| A tool whose artifact the framework supplies | `orchestrator/app/tools/<source>/` | a `handler.py` for `type: "lambda"`, or an `openapi.json` for `type: "openapi"` — only when the entry declares `source`. Skip it entirely if you use `lambdaArn` / `schemaS3Uri`, or neither type |
| Your documents | `orchestrator/kb_docs/` | each top-level folder becomes a corpus |
| The deployment | `orchestrator/terraform/terraform.tfvars` | region, login provider, model, Gateway on/off |

The two code folders line up with the two blocks of `workflow.json` that can carry
code of your own: `agents` → `app/subagents/`, `tools` → `app/tools/`. So there is one
place to look for custom logic per kind of thing you declared.

Nothing else. No Terraform edits, no CDK edits, no Cedar policy to write, no UI
changes, and no test to fix. Both IaC paths read `workflow.json`, so declaring a
tool or an agent provisions the infrastructure for it, and the test suite derives
its expectations from your file rather than naming this sample's agents.

`terraform.tfvars` is the one file that is about your ACCOUNT rather than your
workflow, which is why it is gitignored — copy `terraform.tfvars.example` and keep
your copy local. The `workflow.json`, `app/subagents/` and `app/tools/` you write are
yours to commit.

---

## 1. Deploy the sample as-is (about 20 minutes)

Get the reference workflow running first, so you have a known-good baseline.

```bash
cd orchestrator/terraform
cp terraform.tfvars.example terraform.tfvars      # defaults are fine to start
./bootstrap-state.sh                              # S3 remote state, once per account
terraform apply                                   # builds the image, provisions everything
```

Prerequisites: Terraform, a container engine that builds `linux/arm64`
(Finch/Docker/Podman), AWS credentials, and **Bedrock model access** in your
region for the model in `terraform.tfvars` plus the Knowledge Base's embedding model
(**Titan Text Embeddings V2** by default; override with `embeddingModel` on the `kb` tool).

Then create yourself a login and open the UI:

```bash
POOL=$(terraform output -raw cognito_user_pool_id)
aws cognito-idp admin-create-user --user-pool-id "$POOL" \
  --username you@example.com \
  --user-attributes Name=email,Value=you@example.com Name=email_verified,Value=true \
  --message-action SUPPRESS
aws cognito-idp admin-set-user-password --user-pool-id "$POOL" \
  --username you@example.com --password '<a-strong-password>' --permanent

terraform output ui_url
```

Type a request, approve the review gates, and watch the run. Full deployment
detail — including CDK, Auth0 and no-login — is in
**[`DEPLOYMENT.md`](DEPLOYMENT.md)**.

---

## 2. Point it at your own data

Everything an agent can call is declared in the **`tools`** block of `workflow.json`. The
key of each entry is both the Gateway target name and the label an agent references, and
must be **letters and digits starting with a letter** — `internalTools`, not
`internal_tools` or `internal-tools`. (The Gateway target forbids underscores and the
generated Cedar permit forbids hyphens, so camelCase is the only form that survives both.)
Five types cover the common cases:

### Your documents (RAG)

```json
"tools": {
  "kb": {
    "type": "kb",
    "corpora": ["policies", "runbooks"],
    "policy": { "tool": "retrieve", "restrictTo": { "filter": ["policies", "runbooks"] } }
  }
}
```

Drop your files into `orchestrator/kb_docs/`. **Each top-level folder is a
corpus** — `kb_docs/policies/` and `kb_docs/runbooks/` give you two, and an agent
is scoped to one via its `corpus` field. The next `terraform apply` uploads the
documents, re-ingests them, and updates the authorization policy.

### The live web

```json
"websearch": { "type": "websearch", "maxResults": 10 }
```

The AWS-managed AgentCore Web Search connector. No API key, no endpoint —
available in `us-east-1`, `eu-west-1`, `ap-northeast-1`.

### Your MCP server

```json
"internalTools": {
  "type": "mcp",
  "endpoint": "https://mcp.your-company.com/mcp",
  "call": "search_docs",
  "arg": "query"
}
```

`call` names **which** tool on that server to invoke (a server usually publishes
several) and `arg` is the parameter your query goes into. An optional `args` object
supplies fixed extra arguments, so a server whose search tool needs
`question` + `repoName` is still pure config:

```json
"wiki": { "type": "mcp", "endpoint": "https://mcp.deepwiki.com/mcp",
          "call": "ask_question", "arg": "question",
          "args": { "repoName": "aws/aws-cdk" } }
```

Change the endpoint; that's the whole change. For an authenticated server, keep
the key out of `workflow.json`:

```bash
export TF_VAR_tool_api_keys='{"internalTools":"your-key"}'   # CDK: TOOL_API_KEYS
```

It's vaulted in an AgentCore credential provider and sent by the Gateway as an
`X-API-Key` header, so the agent never sees it.

> **Two things about tool names.**
>
> **1. The Gateway composes them.** Every published tool is
> `<targetName>___<toolName>`, formed server-side — `kb.tf` declares the tool as
> `retrieve` and the Gateway publishes `kb___retrieve`. There is no flag to turn that
> off. AWS *also* uses `___` to namespace the tools on its own managed servers, so
> names can arrive doubly prefixed. That is fine and it works; you just have to set
> `call` to the published name minus the `<targetName>___` part.
>
> **2. `tools/list` is PAGINATED. Follow `nextCursor`.** This trips people up: read
> only the first page and a target looks empty when its tools are simply on page two.
> This deployment has five targets, and the remote MCP server alone publishes five
> tools, so the catalogue does not fit on one page. The app paginates correctly —
> verification scripts often don't.
>
> Confirm what actually got published after deploying:
>
> ```bash
> TOKEN=$(curl -s -X POST "https://$(terraform output -raw cognito_domain_prefix).auth.${AWS_REGION:-us-east-1}.amazoncognito.com/oauth2/token" \
>   -H 'Content-Type: application/x-www-form-urlencoded' \
>   -u "<m2m-client-id>:<secret>" -d 'grant_type=client_credentials&scope=gateway/invoke' | jq -r .access_token)
> GW=$(terraform output -raw gateway_url)
> CURSOR=""
> while : ; do
>   BODY=$(jq -nc --arg c "$CURSOR" '{jsonrpc:"2.0",id:1,method:"tools/list",
>            params: (if $c == "" then {} else {cursor:$c} end)}')
>   RESP=$(curl -s -X POST "$GW" -H "Authorization: Bearer $TOKEN" \
>     -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
>     -d "$BODY")
>   echo "$RESP" | jq -r '.result.tools[].name'
>   CURSOR=$(echo "$RESP" | jq -r '.result.nextCursor // empty')
>   [ -z "$CURSOR" ] && break
> done
> ```
>
> If a target really lists nothing, the agent raises `ToolUnavailable` and the run
> fails with that reason — the framework never substitutes invented data.

### The default: AWS's managed Knowledge MCP Server

The sample's `docs` tool needs nothing deployed and no credentials:

```json
"docs": {
  "type": "mcp",
  "endpoint": "https://knowledge-mcp.global.api.aws",
  "call": "aws___search_documentation",
  "arg": "search_phrase",
  "args": { "limit": 5 },
  "listingMode": "DYNAMIC"
}
```

That's the **AWS Knowledge MCP Server**, hosted and operated by AWS. Five real tools
over live AWS content — note the `aws___` prefix is AWS's own, so the Gateway
publishes them as `docs___aws___…`:

| Tool | Takes |
|---|---|
| `aws___search_documentation` | `search_phrase` |
| `aws___read_documentation` | `url` |
| `aws___list_regions` | — |
| `aws___get_regional_availability` | `resource_type` |
| `aws___retrieve_skill` | `skill_name` |

Optional knobs on any `mcp` entry:

| Field | Values | What it does |
|---|---|---|
| `call` | a published tool name | Which tool on the target to invoke, minus the `<targetName>___` prefix. Required when a target publishes more than one. |
| `arg` | parameter name (default `query`) | Which parameter the query goes into. |
| `args` | object | Fixed extra arguments sent on every call. |
| `listingMode` | `DEFAULT` (default), `DYNAMIC` | Whether the Gateway caches the tool catalogue at create time or fetches it per invocation. `DYNAMIC` avoids a stale cache when a server's tools change. |
| `auth` | `none` (default), `apikey`, `sigv4` | `apikey` sends a vaulted key as `X-API-Key`. `sigv4` makes the Gateway sign with its own execution role, so no secret exists — only for endpoints that verify SigV4 (API Gateway, Lambda Function URLs, AgentCore Runtime). |

### Web search options

`websearch` needs no endpoint or key, but it has two filter layers and a citation
obligation worth understanding:

| Field | What it does |
|---|---|
| `maxResults` | 1–25, default 10. |
| `domains` | `{"include": [...], "exclude": [...]}`. Set on the Gateway target, so it is **hidden from the agent** and applied to every request — a boundary, not a preference. One key; it replaced a request-level `includeDomains`/`excludeDomains` pair and a target-level `targetIncludeDomains`/`targetExcludeDomains` pair that said the same thing twice, the weaker layer having the more obvious name. |
| `publishedFrom` / `publishedTo` | Inclusive ISO-8601 UTC bounds on publication date. |
| `connectorVersion` | Pins the connector, e.g. `"1.2.0"` (request-level filters need 1.2.0+). **Terraform only** — CloudFormation's connector source accepts just `connectorId`, so CDK rejects this field rather than silently ignoring it. |

A domain is dropped if it appears on **either** exclude list, and returned only if it
appears on **every** include list that is set — so a request-level filter can never
override a target-level exclude or widen beyond a target-level include.

> **Citations are mandatory.** Web Search's acceptable-use terms require you to retain
> and *display* the source citations and links returned with each result. The
> framework does this for you: the connector's `title`/`url`/`publishedDate` are
> preserved into the evidence the model sees, every citation URL is then **verified**
> against that evidence (an invented link is dropped and a `sourced-fact` resting on
> it is downgraded), and the UI renders each source as a link. Keep that behaviour if
> you write your own agents.

### Your REST API

```json
"billing": {
  "type": "openapi",
  "schemaS3Uri": "s3://your-bucket/billing-openapi.yaml"
}
```

Tool names come from the schema's `operationId`s, so you control them. An
operation your agent calls should accept a parameter named `query`.

### Your database, or anything else — `type: "lambda"`

The Gateway cannot reach a warehouse, an RDBMS, an internal service or a VPC
resource directly. Point it at a Lambda that can, and it becomes a tool:

```json
"claims": {
  "type": "lambda",
  "lambdaArn": "arn:aws:lambda:us-east-1:123456789012:function:query-claims",
  "call": "query_claims",
  "arg": "question",
  "toolSchema": [{
    "name": "query_claims",
    "description": "Answer a question from the claims warehouse.",
    "properties": {
      "question": { "type": "string", "required": true, "description": "The question." },
      "limit":    { "type": "integer", "required": false, "description": "Max rows." }
    }
  }]
}
```

You deploy the function however you already deploy Lambdas — the framework does not
create it, and never touches its code or its execution role. It registers it as a
target, grants the Gateway `lambda:InvokeFunction` on exactly that ARN, and (for a
same-account function) adds the resource-policy statement. There is no secret
anywhere: the Gateway invokes it with its own role. A **cross-account** function
works too, but the owning account has to add that statement itself.

Two things differ from the other types:

- **The schema is declared, not discovered.** A Lambda has no `tools/list` for the
  Gateway to call, so `toolSchema` spells out each tool and its arguments. Get it
  wrong and the API accepts it, then publishes a tool the agent cannot call — which
  looks like an empty answer, not an error. Hence the plan/synth guards: a malformed
  ARN, an empty schema, a tool with no properties, an unsupported property type, a
  `call` naming nothing, or an `arg` that isn't a property of the called tool are all
  rejected before anything deploys.
- **One function may publish several tools.** The Gateway passes the tool name
  through, so your handler can dispatch on it. When it does, `call` is **required** —
  the app refuses to guess between candidates, and both IaC paths enforce that.

The sample ships this live, so you can see it work before writing anything: the
`pricing` tool and the `cost_research` agent. It uses `"source": "pricing"`
instead of `lambdaArn`, which asks the framework to package and deploy the function
it ships in `orchestrator/app/tools/pricing/` — that keeps the committed config
account-neutral, since a real ARN would pin it to one AWS account. `source` names the
**folder under `orchestrator/app/tools/`**, which is why the tool key and the folder
read the same here. The function publishes `aws_prices`, returning real AWS on-demand
unit rates from the Price List Query API, so the rows are real without you standing up
a database first. It returns RATES and never a total: a total needs usage volumes,
which are a property of your workload and not of AWS.

`source` accepts no other value today, and that is deliberate rather than unfinished:
a framework-deployed function runs on a role the framework writes, and that role is
fixed at CloudWatch Logs plus read-only access to the public price list. A connector of
your own almost certainly needs something config cannot express — a VPC, a secret, a
table grant — so the framework does not pretend it can deploy it. For your own
connector, deploy the function however you deploy functions and declare `lambdaArn`;
the framework then deploys nothing and only wires the Gateway target and the Cedar
permit. You may keep that source beside `app/tools/pricing/` for symmetry; nothing
reads it there unless `source` names it.

---

## 3. Add your own agent

Two steps, and one command does both:

```bash
cd orchestrator
python3 scaffold.py agent contract_review --tool contracts_kb   # --dry-run to preview
```

That writes the `workflow.json` entry (in the canonical key order) and the folder, and
what it generates is complete rather than a stub — the `run()` really calls the model, so
you can deploy and then make it yours. The one thing it leaves you is placing the agent
in `steps`, because that depends on what it consumes.

The rest of this section is what the command writes, for when you want to write it
yourself.

**a) Create the folder.** The folder name IS the agent id.

```
orchestrator/app/subagents/contract_review/
├── __init__.py      # from .agent import agent
├── agent.py
└── prompts.py
```

**The contract is three lines long**, and it is all the framework asks: a package that
exports `agent`, a subclass of `Agent`, and a `run()`. Miss one and
`registry.check_agent_module` says which file and what to add — including the quiet case,
a class that never overrode `run()`, which otherwise imports fine, appears on the diagram
and fails the instant the run reaches it. `pytest` catches that before you deploy.

Everything else here is yours. Three files and prompts in `prompts.py` is convention, held
to by the shipped agents so they stay worth copying, not enforced on you.

```python
# agent.py
from app.common.base import Agent
from app.subagents._shared import research
from app.common.context import AgentContext

from .prompts import SYSTEM_PROMPT


class ContractReviewAgent(Agent):
    system_prompt = SYSTEM_PROMPT

    async def run(self, ctx: AgentContext) -> str:
        # research.synthesize handles evidence gathering from whatever tool this
        # agent is bound to in workflow.json, then returns a validated asset.
        return await research.synthesize(ctx, system_prompt=SYSTEM_PROMPT)


agent = ContractReviewAgent()
```

Or write whatever you like in `run()` — you have `ctx.llm(...)`,
`ctx.call_tool(label, query)`, `ctx.retrieve(query, doc_type=...)`,
`ctx.input(other_agent_id)`, `ctx.heartbeat(pct)` and `ctx.log(msg)`.

That includes driving **another agentic framework** inside `run()`, per agent.
`web_search` reasons inside a Strands agent and `knowledge_research` inside a nested
LangGraph; the other two research agents use no framework at all, and all four emit
the same contract. Pass `think=` to `research.synthesize` to swap only the reasoning
step:

```python
from app.subagents._shared.strands_bridge import strands_thinker

return await research.synthesize(ctx, system_prompt=SYSTEM_PROMPT,
                                 think=strands_thinker(ctx))
```

Keep the model call on `ctx.llm` — that is where guardrails, cost telemetry, memory
and truncation detection live, and a framework with its own Bedrock client loses
them silently. See *Author an agent with any agentic framework* in the README for
the two caveats (no native tool calling, and the image-size bill).

**b) Declare it in `workflow.json`** and put it in the topology:

```json
"agents": {
  "contract_review": {
    "name": "Contract Review",
    "runtime": "dedicated",
    "tool": "kb",
    "corpus": "policies",
    "maxTokens": 4000,
    "agentcore": {
      "memory": { "longTerm": ["semantic"] },
      "guardrails": { "output": true },
      "evaluations": { "enabled": true, "auto": true,
                       "evaluators": ["Builtin.Faithfulness"] },
      "policy": { "enabled": true }
    }
  }
},
"steps": [
  { "agent": "intake", "hitl": true },
  { "parallel": ["knowledge_research", "contract_review"], "hitl": true,
    "gateId": "research", "gateName": "Research" },
  { "agent": "report" }
]
```

`terraform apply` then creates its dedicated AgentCore Runtime, wires its IAM,
adds it to the UI diagram, and includes it in the downstream agents' inputs —
because every downstream step reads its upstream list from the topology rather than
hardcoding it. `report` does that in its own code (`upstream_of("report")`); the
two remote agents get it from the framework, which puts the approved upstream
outputs into the A2A task it sends them.

`"runtime": "main"` runs the agent in-process instead; `"dedicated"` gives it its
own container and its own scaling.

### Using an agent you don't own

A third `runtime` value delegates a step to an agent operated by somebody else, over
the **A2A (Agent2Agent) protocol**. There is no folder to write — the code is theirs:

```json
"credit_check": {
  "name": "Partner Credit Check",
  "runtime": "a2a",
  "agentCard": "https://agents.partner.example/credit",
  "auth": "bearer",
  "produces": "credit-assessment"
}
```

Then put `credit_check` in `steps` like any other agent. Its Agent Card is read for the
RPC endpoint, it receives the request plus the approved upstream outputs, and its answer
becomes that node's output — so a review gate, a `branch`, a re-run and the version
history all work on it unchanged.

`auth` is `none`, `bearer` (token from `TF_VAR_a2a_tokens` / `$A2A_TOKENS`, keyed by
agent id — never in `workflow.json`, which is committed), `oauth2` (minted per call
from the agent's `agentcore.identity.outbound` provider, so no long-lived secret
exists) or `sigv4` (signed with your own execution role — the mode for an AWS-hosted
agent behind IAM, where there is no token to leak at all). `model`, `maxTokens`, `tool`
and `corpus` are rejected on an `a2a` agent: it makes its own model call and reaches its
own data sources, so those keys would read as governing its cost and access while doing
nothing.

**This is not a second-class step.** If the remote agent replies with a JSON object and
you declared `produces`, the framework wraps it in your asset envelope — `assetId`,
`version`, `status`, `createdByAgent`, `sourceAssetIds` — so a downstream agent can
cite it the way it cites a local one. The framework supplies the envelope rather than
asking the remote agent to, because it is bookkeeping about *your* run: a remote agent
cannot know how many times you have re-run this step. A prose reply is left exactly as
written.

**What carries across, and what does not.** Guardrails apply, because the framework
wraps the call on your side. Long-term memory works both ways: the recall is sent to the
remote agent inside the task (with the caveat that it is recollection, not evidence) and
the reply is stored as a new insight, which means your
accumulated recollections leave your deployment, which is why it follows
`agentcore.memory` rather than happening unconditionally. Two things genuinely degrade:
evaluations drop to role-level, because there is no local model call to capture a prompt
from (so prefer `auto: false`, and drop Faithfulness — there is no source context to be
faithful to), and the remote agent's tool calls are outside your Cedar policy.

**No local agent for it means no folder — and the sample proves it.** Its own stage 3
(`analysis` → `recommendation`) is two `a2a` agents, so `app/subagents/` has seven
packages for nine agents.

The `runtime: "a2a"` placement needs an agent you do not operate, so the framework also
ships a real one to point at: an A2A server in its own Lambda behind an IAM-authed
Function URL, deployed only when an agent asks for it with `"source": "a2a_lambda"` and
`"skill": "<one of analysis|recommendation|compliance|resilience>"`. Point an agent at a
real partner's `agentCard` instead and none of that infrastructure is created.

---

## 4. Branding and content safety — also config

Two more top-level blocks in `workflow.json`, so neither needs a code or IaC edit.

**`ui`** — presentation strings, applied at load time. Every field is optional and
the page's own text is the fallback.

```json
"ui": {
  "title": "Claims Adjudicator",
  "heading": "Claims Adjudicator",
  "defaultTopic": "Assess claim CL-4471 for storm damage",
  "topicPlaceholder": "What should the workflow work on?",
  "assistantTitle": "Run Assistant"
}
```

**`guardrail`** — the Bedrock Guardrail's policy. Omit any key and that policy block
isn't created at all; omit the whole block for an empty guardrail. Agents opt in
individually via their `agentcore.guardrails.{input,output}` flags — this block
defines *what* is enforced.

```json
"guardrail": {
  "contentFilters": { "HATE": "HIGH", "VIOLENCE": "MEDIUM", "PROMPT_ATTACK": "HIGH" },
  "deniedWords": ["SETTLEMENT_OFFER"],
  "managedWordLists": ["PROFANITY"],
  "deniedTopics": [
    { "name": "LegalAdvice",
      "definition": "Statements that give specific legal advice.",
      "examples": ["You are legally required to sign this."] }
  ],
  "piiEntities": { "US_SOCIAL_SECURITY_NUMBER": "BLOCK", "EMAIL": "ANONYMIZE" }
}
```

`contentFilters` maps a filter type to `NONE|LOW|MEDIUM|HIGH`, applied to input and
output (`PROMPT_ATTACK` is input-only, so its output strength is forced to `NONE`).
`piiEntities` maps a Bedrock PII entity type to `BLOCK` or `ANONYMIZE`.

> The sample ships a denied word `BLOCKED_DEMO_TERM` purely so you can prove blocking
> works end to end. **Replace it**, along with the `LegalAdvice` topic, with your own
> domain's policies.

---

## 5. What you get for free

Every capability below is a flag in `workflow.json`, applied by the framework
around your `run()` — you never write the integration:

| Flag | What happens |
|---|---|
| `memory.longTerm` | past insights recalled into the prompt before the run, output stored after |
| `guardrails.input` / `.output` | Bedrock `ApplyGuardrail` before/after the model call |
| `evaluations.enabled` / `.auto` | LLM-as-judge scoring, per prompt per version |
| `policy.enabled` | Cedar authorization on every tool call, server-side at the Gateway |
| `maxTokens` on an agent | the model output budget for that agent's calls |
| `hitl: true` on a step | an approve / revise / deny gate, with rewind-and-re-run |
| `branch` on a step | the agent's own output picks the next step (or ends the run); bypassed agents are marked skipped |

Always on, with no flag to set: LangGraph checkpointing (so a run survives a review
pause) and full observability (OTEL spans, token counts, cost and latency in the UI).

Guardrails, memory, evaluations and policy are all **on** for the sample agents,
in different combinations, so one deployment shows the whole surface.

---

## 6. Authorization: two layers, both config

There are two different questions here, with two different subjects:

| Question | Subject | Where | Config |
|----------|---------|-------|--------|
| May this **agent** call this **tool**? | an agent | Cedar, at the Gateway | the `tools` block |
| May this **person** approve this **gate**? | a human | `bff/authz.py`, at the BFF | the `authorization` block |

### Agents calling tools — you don't write Cedar

Declaring a tool permits it. Anything **not** declared is denied by Cedar's
default-deny, including a tool name a prompt-injected instruction invents.

The `kb` entry in the sample shows the fine-grained form: `kb___retrieve` is
permitted **only** when the corpus filter is one of the declared `corpora`. A
retrieval with no filter, or naming a corpus outside that list, is refused at the
Gateway — not by your code.

Two knobs in `workflow.json`:

```json
"policy": { "enabled": true, "mode": "ENFORCE" }
```

`LOG_ONLY` evaluates and logs without blocking, which is the safe way to roll out.
Set `"policy": { "permit": false }` on a tool to register it but deliberately deny
it, if you want to see default-deny in action.

### People acting on runs — the `authorization` block

Logging in proves *who* someone is. It says nothing about whether they may approve a
review gate, and "any authenticated user can approve" is the wrong default for a
human-in-the-loop product. Map the seven mutating actions to JWT groups:

```json
"authorization": {
  "groupsClaim": "cognito:groups",
  "actions": {
    "start":    ["approvers", "operators"],
    "decision": ["approvers"],
    "rerun":    ["approvers"],
    "cancel":   ["approvers", "operators"],
    "evaluate": ["operators"],
    "insights": ["operators"],
    "delete":   ["operators"]
  }
}
```

- `start` is starting a run — the most expensive action in the app. `decision` is
  approve / revise / deny. The rest are re-run an agent, stop a run, run an
  evaluation, run cross-run Insights, and delete a run.
- **Absent = unrestricted.** An action you don't list stays open to any authenticated
  caller, so removing the block gives you the old behaviour. Listing an action with an
  **empty** array denies it to everyone — that is how you turn a capability off.
- The **groups are created for you** in Cognito from this block. Adding a *person* to a
  group is a one-liner you run once (`admin-add-user-to-group`), not a deploy — see
  [DEPLOYMENT.md](DEPLOYMENT.md#put-that-user-in-a-group-or-you-can-log-in-but-not-approve).
- The **assistant obeys the same rules.** It can approve gates and re-run agents, so the
  BFF withholds the action tools a caller isn't authorized for. Asking the chatbot is not
  a way around the buttons.
- The UI hides or disables what you can't use, driven by `GET /api/me`. That is a
  courtesy, not the control — every action is re-checked server-side.
- **On Auth0**, change `groupsClaim` to a namespaced claim (e.g. `https://your-app/roles`)
  and emit it from a post-login Action. Auth0 will not issue an unnamespaced one.
- Restricting anything needs an IdP. `idp = "none"` plus a non-empty `actions` map is
  rejected at plan/synth time, because with no claims every rule would deny everyone.

---

## 7. Guardrails against your own mistakes

Both IaC paths validate `workflow.json` **before** anything is deployed, and name
the exact problem:

**Tools**
- a tool with an unknown `type`, or `type: "mcp"` with no `endpoint`
- `type: "openapi"` with neither `schemaS3Uri` nor `source`, or with both; a `source`
  whose `app/tools/<source>/openapi.json` is not on disk; a `schemaS3Uri` that names a
  bucket but no object key
- more than one `kb` or `websearch` tool
- a `kb` tool with an empty `corpora` list
- an agent whose `tool` doesn't match any key in `tools`
- `websearch` in a region where the connector isn't available
- an invalid `listingMode` or `auth`, or `auth: "apikey"` with no key supplied
- `connectorVersion` on the CDK path, which CloudFormation cannot express

**Agents and topology**
- a `steps` entry naming an agent id that isn't in `agents`
- an agent declared in `agents` that no `steps` entry ever runs
- an agent id that isn't `^[a-zA-Z][a-zA-Z0-9_]*$` (the id becomes part of the
  AgentCore Runtime name, which rejects hyphens)
- `<agentName>_<agentId>` longer than the 48-character runtime-name limit

**`runtime` and A2A**
- a `runtime` that isn't `main`, `dedicated` or `a2a` (a typo would silently become
  `main` and then die on a missing module under `app/subagents/`)
- `agentCard` or `auth` on an agent that isn't `runtime: "a2a"` — read by nothing there
- `runtime: "a2a"` with no `agentCard`, or a non-`https` one (the request may carry a
  bearer token, and `file://` would read a local path instead of making a request)
- an `auth` value that isn't `none`/`bearer`/`oauth2`; `oauth2` with no
  `agentcore.identity.outbound` provider to mint from
- `auth: "bearer"` with no token supplied for that agent — the alternative is a 401 from
  a service you do not control, which is far harder to read
- `model`/`temperature`/`maxTokens`/`tool`/`corpus` on an `a2a` agent

**`branch`** — also all quiet failures: an unmatchable rule or an unresolvable target
means the run just takes the default on every request, and the branch looks wired
- a rule with an unknown key (a misspelled operator), no `goto`, or no comparison
- an operator value of the wrong shape (`in` not a list, `exists` not a boolean,
  `gte` not a number, `equals` given a list instead of using `in`)
- `branch` with neither `when` nor `default`, or with an empty `when`
- a `goto`/`default` naming no step, or naming a step at or before its own (a
  backward edge would be a cycle the run could not leave)
- `branch` on a `parallel` step (no single agent decides) or on the last step

**Knowledge Base corpora** — the checks whose failure mode is silent
- a `corpora` entry with no matching folder under `kb_docs/`
- an agent whose `corpus` isn't in the declared `corpora`
- a document sitting at the root of `kb_docs/` instead of in a corpus folder

**Guardrail** — invalid filter strength, invalid PII action, a denied topic
missing its `name` or `definition`

**Authorization** — the failures are quiet ones, so both are hard errors
- a non-empty `authorization.actions` with `idp: "none"`: no authorizer means no
  claims, so every rule would deny everyone and lock you out of your own UI
- an unrecognised action name (`"aprove"`): it *looks* like a restriction in the
  config but gates nothing, leaving the real action wide open

**Other** — `idp: "none"` with `enable_gateway = true`; an unprovisioned
`memory.longTerm` strategy; a malformed evaluator name; an unrecognised tool `type`

> There used to be one more: a workflow too large for the BFF Lambda's 4 KB
> environment, which capped a deployment at about **eleven agents**. The workflow now
> travels inside the BFF's deployment package instead, so there is no such limit — see
> `orchestrator/bff/workflow.py`.

So a typo is a failed `terraform plan` or `cdk synth`, not a broken deployment. The
corpora checks matter most: before them, a mistyped corpus produced a Cedar filter on
a `doc_type` no chunk carried, and retrieval quietly returned nothing at all.

---

## 8. Iterating quickly

```bash
cd orchestrator
pip install -r requirements.txt -r requirements-dev.txt
(cd web && npm ci && npm run build)              # the UI is compiled, not copied
uvicorn app.orchestrator.server:app --port 8090   # needs AWS credentials + Bedrock access
```

Iterating on the UI itself? Run Vite for hot reload instead of rebuilding, and let it
proxy the API back to the local server:

```bash
cd orchestrator/web
VITE_API_BASE=http://127.0.0.1:8090 npm run dev   # then open http://127.0.0.1:5173
```

> **No offline mode, deliberately.** This framework never fabricates data. A failed
> model call raises `ModelUnavailable`; an unreachable or unpublished tool raises
> `ToolUnavailable`; a Cedar refusal raises `ToolDenied`. The run fails with the
> reason attached to the agent that failed, instead of a report full of
> plausible-looking placeholder text. See
> [`app/common/errors.py`](orchestrator/app/common/errors.py).
>
> To iterate on prompts and topology without a Gateway, drop the `tool` binding from
> the agents you're not exercising — they'll reason over upstream inputs only.

Then redeploy only what changed: `terraform apply` rebuilds the image and updates
the runtimes; editing `kb_docs/` re-ingests the corpus and nothing else.

### Check your config before you deploy it

```bash
cd orchestrator     && pytest      # runtime side — 815 tests, ~11s
cd orchestrator/cdk && npm test    # IaC side + Terraform↔CDK parity — 168 tests
cd orchestrator/web && npm test    # the UI — 23 tests
```

Neither needs AWS credentials, a model, or a container builder.

Together they test the **config plane** — the code that turns your `workflow.json`
into a running system: which agent reads whose output, whether your `steps` topology
compiles into a graph, what a tool call's arguments look like, which citations
survive, what Cedar permit each tool gets, who is allowed to approve, and whether the
Terraform and CDK paths still agree. They do not test prompt quality (that is what the
review gates and AgentCore Evaluations are for).

Worth running after any `workflow.json` edit, because the failures they catch are the
quiet kind — a renamed first agent that silently drops the request brief from every
downstream prompt, a topology the graph builder cannot wire, a report section
discarded because its type is unrecognised, an RBAC rule that gates five routes out of
six, a projection that drifts so a CDK deployment loses features a Terraform one
keeps. See [`orchestrator/tests/README.md`](orchestrator/tests/README.md) and
[`orchestrator/cdk/test/README.md`](orchestrator/cdk/test/README.md).

---

## Where to look next

| Doc | For |
|---|---|
| **[`DEPLOYMENT.md`](DEPLOYMENT.md)** | full deploy guide: Terraform + CDK, Cognito / Auth0 / no-login, remote state, teardown |
| **[`ARCHITECTURE.md`](ARCHITECTURE.md)** | what's deployed and why, and where each concern lives |
| **[`README.md`](README.md)** | feature tour and repository map |
| **[`orchestrator/cdk/README.md`](orchestrator/cdk/README.md)** | the CDK path, context keys, teardown |
| **[`orchestrator/docs/WORKFLOW_REFERENCE.md`](orchestrator/docs/WORKFLOW_REFERENCE.md)** | every `workflow.json` key: what reads it and what changing it does |
| **[`orchestrator/tests/README.md`](orchestrator/tests/README.md)** | the config-plane test suite (runtime): what it covers and why |
| **[`orchestrator/cdk/test/README.md`](orchestrator/cdk/test/README.md)** | the config-plane test suite (IaC) + Terraform↔CDK parity |

Before anything beyond a sandbox, read the **Security, cost & scalability**
section of the README — this is a reference sample, and it names what to harden.
