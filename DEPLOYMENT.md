# Deployment Guide

The minimum steps to deploy this orchestrator into an AWS account. There are **two
infrastructure-as-code options** — pick one (don't run both against the same
account/region; they use overlapping resource names):

| | **Terraform** (`orchestrator/terraform/`) | **CDK / TypeScript** (`orchestrator/cdk/`) |
|---|---|---|
| Footprint | **Full** | **Full** — the same resources |
| AgentCore capabilities | **9 of 9** | **9 of 9** |
| MCP / RAG / Cedar policy | Live with `enable_gateway = true` | Live with `-c enableGateway=true` |
| Container image | Per-stack ECR repository | CDK `DockerImageAsset` (bootstrap ECR repo) |
| Remote state | S3 + native locking (`./bootstrap-state.sh`) | CloudFormation (managed by AWS) |
| Guide | This document (below) | [`orchestrator/cdk/README.md`](orchestrator/cdk/README.md) + the quick steps at the end here |

Both build the same ARM64 container image and provision the orchestrator plus one
dedicated AgentCore runtime per `dedicated` agent (three of the four research
agents), and both
read `app/workflow.json` as the single source of truth for agents and topology.
**Pick whichever you prefer — the deployed feature surface is the same.**

---

# Deploy with Terraform (full — adds Gateway + Knowledge Base)

All commands run from `orchestrator/terraform/`.

## 1. Prerequisites

- **Terraform** ≥ 1.5
- A container engine that builds `linux/arm64` images — **Finch** (default), Docker, or Podman
- **AWS CLI**, configured with credentials for the target account

> **Login is configuration driven** — one variable, `idp`, picks the provider:
> 1. **`"cognito"`** with `cognito = { create = true }` — Terraform creates the User
>    Pool, domain, and SPA client for you. Recommended for demos and sandboxes.
> 2. **`"cognito"`** with `create = false` — bring an existing pool.
> 3. **`"auth0"`** — an existing Auth0 tenant (`domain` + SPA `client_id`).
> 4. **`"none"`** — the UI/API deploy **open** (no login). Do **not** use this mode
>    for anything shared.
>
> See step 3 for each provider's setup.

In the target AWS account/region, enable **Amazon Bedrock model access** for the
model in `model_id` (default **Claude Haiku 4.5**) and **Titan Text Embeddings V2**
(used by the Knowledge Base). Deploys and KB ingestion fail without this.

## 2. IAM permissions to deploy

Attach the ready-made policy to the IAM role/user you deploy with:

```
orchestrator/terraform/deploy-role-policy.json
```

It grants exactly what Terraform needs (ECR, AgentCore runtimes/memories/gateway
**and policy engines**, Bedrock KB + S3 Vectors **and guardrails**, DynamoDB,
Lambda, API Gateway, S3, CloudFront, IAM execution roles, logs, and the X-Ray calls
that enable **Transaction Search**). Confirm you're in the right account:

```bash
aws sts get-caller-identity
```

## 3. Identity provider setup

Login is **configuration driven**. A single variable, `idp`, selects the provider
for *both* auth boundaries at once:

* end-user login on the UI + the API Gateway JWT authorizer on `/api/*`
* the machine-to-machine (client-credentials) token agents use to call the
  AgentCore Gateway

Everything downstream — the authorizer's issuer/audience, the Gateway's
`CUSTOM_JWT` config, the OAuth token endpoint, and the `auth-config.js` the SPA
reads — is derived in **`terraform/identity.tf`**. That's the only file that knows
how the providers differ, and the only file to touch to add another one.

| `idp` | End-user login | Terraform creates the IdP? |
|---|---|---|
| `"cognito"` | Cognito Hosted UI | Yes, if `cognito.create = true` |
| `"auth0"` | Auth0 Universal Login (`auth0-spa-js`) | No — existing tenant |
| `"none"` | **No login at all** | n/a |

If something required for your provider is missing, `terraform plan` fails with a
message naming the exact variable — nothing half-configured reaches AWS.

### Option A — Cognito, created for you (recommended for demos)

```hcl
idp     = "cognito"
cognito = { create = true }
```

Terraform creates the User Pool, Hosted UI domain (`<agent-name>-<account-id>`),
the public SPA client, and — when `enable_gateway = true` — the Resource Server
plus the confidential M2M client. The CloudFront callback/sign-out URLs are wired
automatically, and the M2M secret is read straight from state, so there is
nothing to configure by hand. Skip to step 4.

### Option B — Cognito, bring your own pool

```hcl
idp = "cognito"
cognito = {
  create        = false
  user_pool_id  = "us-west-2_XXXXXXXXX"
  client_id     = "YOUR_SPA_APP_CLIENT_ID"
  domain_prefix = "your-app-domain"
}
```

Create in Cognito first:

1. **User Pool** → `cognito.user_pool_id`
2. **Hosted UI domain** → `cognito.domain_prefix`
3. **App Client (public, no secret)** for the SPA → `cognito.client_id`
   - Allowed OAuth flows: Authorization code grant
   - Allowed scopes: `openid`, `email`, `profile`
   - After the first apply, add the `ui_url` output to its Allowed Callback and
     Sign-out URLs
4. Only if `enable_gateway = true`:
   - **Resource Server** defining a custom scope (e.g. `gateway/invoke`)
   - **App Client (confidential, with secret)** authorized for that scope →
     `gateway_identity` below

### Option C — Auth0

```hcl
idp = "auth0"
auth0 = {
  domain    = "your-tenant.us.auth0.com"   # no scheme, no trailing slash
  client_id = "YOUR_AUTH0_SPA_CLIENT_ID"
}
```

Terraform creates nothing inside Auth0. In your tenant:

1. **Application** of type *Single Page Application* → `auth0.client_id`.
   Add the `ui_url` output to Allowed Callback URLs, Allowed Logout URLs **and**
   Allowed Web Origins after the first apply.
2. Only if `enable_gateway = true`: an **API** (its *Identifier* is the audience)
   and a **Machine to Machine** application authorized for it →
   `gateway_identity` below.

### Option D — No login

```hcl
idp            = "none"
enable_gateway = false
```

The UI and `/api/*` deploy **open to anyone with the URL**. Use it for a local
trial or a throwaway sandbox, never for anything shared. `enable_gateway` must be
`false`, because the Gateway's `CUSTOM_JWT` authorizer needs an OIDC provider —
Any agent bound to a `tool` then fails fast rather than inventing evidence.

You must also **remove `authorization.actions`** from `workflow.json` (the sample ships
it populated). With no authorizer there are no JWT claims, so every group check would
evaluate against an empty group set and deny *everyone* — locking the UI out of the app
it just deployed. Both IaC paths reject the combination at plan/synth time rather than
letting you find out in the browser.

### Machine-to-machine identity (agent → Gateway)

Required when `enable_gateway = true`, **except** for Option A. The same two
variables serve both providers:

```hcl
gateway_identity = {
  client_id = "YOUR_M2M_CLIENT_ID"
  audience  = "gateway/invoke"   # Cognito: the OAuth2 scope
                                 # Auth0:   the API Identifier
}
```

| Value | Goes to |
|---|---|
| `idp`, `cognito`, `auth0`, `gateway_identity` | `terraform.tfvars` |
| M2M **client secret** | env var `TF_VAR_gateway_client_secret` (never in a file) |

The two providers issue different token shapes, which is why the Gateway
authorizer and the runtime's token request are both provider-aware:

| | Cognito M2M token | Auth0 M2M token |
|---|---|---|
| Claims present | `client_id` + `scope`, **no** `aud` | `aud` + `azp`, **no** `client_id` |
| Gateway pins on | `allowed_clients` | `allowed_audience` + an `azp` custom claim |
| Token request | HTTP Basic + `scope` | form body + `audience` |
| Token endpoint | `/oauth2/token` | `/oauth/token` |

## 4. Remote state (once per account)

State lives in **S3** with native locking, so anyone with deploy credentials
plans/applies the same stack. Run the bootstrap once — it derives the account from
your credentials, creates a versioned + encrypted + private bucket, writes
`backend.hcl` (git-ignored), and wires the working directory:

```bash
cd orchestrator/terraform
./bootstrap-state.sh
```

It's safe to re-run, and safe on a workspace that already has state: if you'd
previously applied with **local** state it **migrates** that state into S3 (taking a
timestamped backup first) rather than orphaning your live resources.

```
Bucket: agentcore-multiagent-orchestrator-tfstate-<account-id>
Key:    orchestrator/terraform.tfstate
```

Overrides: `AWS_REGION` for the bucket region, `STATE_NAME` for the bucket infix.
Prefer local state for a solo trial? Comment out the `backend "s3"` block in
`versions.tf` and run `terraform init -migrate-state`.

> Requires Terraform **≥ 1.10** (for S3-native `use_lockfile`, which replaces the
> old DynamoDB lock table).

## 5. Deploy

```bash
cd orchestrator/terraform

cp terraform.tfvars.example terraform.tfvars   # then edit

# Simplest (Cognito created for you, no Gateway):
#   region         = "us-west-2"
#   idp            = "cognito"
#   cognito        = { create = true }
#   enable_gateway = false

# Full (Cognito created for you + Gateway):
#   region         = "us-west-2"
#   idp            = "cognito"
#   cognito        = { create = true }
#   enable_gateway = true

# Auth0 + Gateway:
#   idp              = "auth0"
#   auth0            = { domain = "your-tenant.us.auth0.com", client_id = "..." }
#   gateway_identity = { client_id = "...", audience = "https://your-api-id" }

# Only needed when you BRING YOUR OWN M2M client (any Auth0 setup, or Cognito with
# create = false) AND enable_gateway = true. With cognito = { create = true },
# Terraform creates the client and reads its secret from state — leave this unset.
export TF_VAR_gateway_client_secret='<m2m client secret>'

terraform apply                                # builds the image + provisions everything
```

(`bootstrap-state.sh` in step 4 already ran `terraform init`.)

The first apply builds/pushes the container image, provisions the **orchestrator
runtime plus one dedicated AgentCore runtime per `dedicated` agent** (three of the
four research agents), creates both **AgentCore Memory** resources (the checkpointer and
the long-term semantic/summary store), the **Bedrock Guardrail**, the telemetry and
Insights tables, enables **CloudWatch Transaction Search** (idempotent — a no-op if
already on), and — when `enable_gateway = true` — the Gateway with its **Cedar
policy engine** plus a KB ingestion job (a few minutes). Then read the outputs:

```bash
terraform output
# ui_url        -> the app URL (CloudFront)
# api_endpoint  -> HTTP API base
```

## 6. Create a login user

*(Skip this step for `idp = "none"`. For `idp = "auth0"`, create the user in your
Auth0 tenant instead, and make sure `terraform output ui_url` is listed in the
application's Allowed Callback URLs, Allowed Logout URLs and Allowed Web Origins.)*

With `cognito = { create = true }` the callback/sign-out URLs are wired to the
CloudFront URL **by Terraform**, so there is no post-apply URL step — login works
immediately.

The pool is **admin-create-only** on purpose: the UI sits on a public CloudFront
URL, and self-signup would let anyone register and spend your Bedrock budget. Create
yourself a user:

```bash
POOL=$(terraform output -raw cognito_user_pool_id)
aws cognito-idp admin-create-user --user-pool-id "$POOL" \
  --username you@example.com \
  --user-attributes Name=email,Value=you@example.com Name=email_verified,Value=true \
  --message-action SUPPRESS
aws cognito-idp admin-set-user-password --user-pool-id "$POOL" \
  --username you@example.com --password '<a-strong-password>' --permanent
```

### Put that user in a group (or you can log in but not approve)

`workflow.json` ships an **`authorization`** block, so being logged in is not enough to
approve a review gate. The groups it names are **created for you** — Terraform reads the
block and provisions one Cognito group per group name — but *membership* is not managed
in IaC, because it is per-person and changes far more often than a deploy. A brand-new
user is therefore in no group and will see the Approve/Revise/Deny buttons greyed out
with a "you don't have permission" note.

Add yourself to every group the shipped config uses:

```bash
for G in approvers operators; do
  aws cognito-idp admin-add-user-to-group --user-pool-id "$POOL" \
    --username you@example.com --group-name "$G"
done
```

Then **sign out and back in** — group membership is carried in the ID token, so an
existing token will not pick it up.

To *demonstrate* RBAC rather than just enable it, create a second user in no group:

```bash
aws cognito-idp admin-create-user --user-pool-id "$POOL" \
  --username viewer@example.com \
  --user-attributes Name=email,Value=viewer@example.com Name=email_verified,Value=true \
  --message-action SUPPRESS
aws cognito-idp admin-set-user-password --user-pool-id "$POOL" \
  --username viewer@example.com --password '<a-strong-password>' --permanent
```

Signed in as that user, the approval controls render greyed out with the reason, and
the API refuses each action with a 403 naming the group required and the groups you
hold. Side by side with the grouped user, that is the whole feature in one screen.

The shipped mapping (edit it in `orchestrator/app/workflow.json`):

| Action | Meaning | Groups |
|--------|---------|--------|
| `start`    | start a run (the most expensive action in the app) | *unrestricted in the sample* |
| `decision` | approve / revise / deny a review gate | `approvers` |
| `rerun`    | re-run an agent and everything downstream | `approvers` |
| `cancel`   | stop a running workflow | `approvers`, `operators` |
| `evaluate` | run an AgentCore evaluation on demand | `operators` |
| `insights` | run the cross-run Insights batch analysis | `operators` |
| `delete`   | permanently remove a run and its timeline | `operators` |

An action **not listed** in `authorization.actions` is unrestricted, so deleting the
block gives you the pre-RBAC behaviour where any logged-in user can do anything. An
action listed with an **empty** group list is denied to everyone — that is how you
switch a capability off entirely. See the `authorization` section of
[`orchestrator/docs/WORKFLOW_REFERENCE.md`](orchestrator/docs/WORKFLOW_REFERENCE.md)
for the full semantics, and check `GET /api/me` if you are unsure what the app thinks you can do:

```bash
curl -s -H "Authorization: Bearer $ID_TOKEN" "$(terraform output -raw api_endpoint)/api/me"
# {"user":"you@example.com","groups":["approvers","operators"],
#  "permittedActions":["decision","rerun","cancel","evaluate","insights","delete"], ...}
```

**On Auth0**, set `authorization.groupsClaim` to a **namespaced** custom claim (e.g.
`https://your-app/roles`) and add a post-login Action that emits it — Auth0 will not
issue an unnamespaced custom claim, so the default `cognito:groups` finds nothing and
every gated action is denied.

> **Bringing your own pool** (`cognito = { create = false }`)? Then you *do* have to
> add `terraform output ui_url` to your App Client's Allowed Callback and Sign-out
> URLs yourself, or login fails with a redirect-mismatch error. The `authorization`
> groups are still created in your pool.

## 7. Use it

Open `ui_url`, log in (unless `idp = "none"`), type a request in the topic box, and
start a session. Approve, revise, or deny at each human-review gate.

Optionally fill the **Subject** box before starting: it scopes long-term memory, so
agents recall insights from earlier runs on that same subject (a customer, product,
project — whatever your domain groups knowledge by).

Once a run finishes:
- **Observability tab → Run detail** — per-agent cost/latency/tokens; click
  **⤢ Prompts & I/O** on an agent for its exact prompts, tool queries, memory ops,
  guardrail/policy decisions and **evaluation scores**, per run version.
- **Evaluate** — agents with `evaluations.auto: true` are scored automatically at
  completion; use the button for the rest. Scores take ~30–60s to appear.
- **Observability tab → Insights** — pick a window and **Run insights** for the
  cross-run failure/intent/summary analysis (a few minutes; needs at least one
  completed run and Transaction Search, which step 4 enabled).
- **Re-run** — open an agent's output and use *Rerun from here*, or pick several
  agents of one parallel stage in the **Re-run agents** panel. Downstream stages
  regenerate and their gates re-pause.
- **💬 assistant** (bottom right) — ask about status/cost/evals, or tell it to
  approve a gate, re-run an agent, or run an evaluation.

## 8. Point it at your own data sources

Every tool your agents can call is declared in the **`tools`** block of
`orchestrator/app/workflow.json`. Each key is both the Gateway target name and the
label an agent references via its `tool` field. Terraform and CDK generate the
Gateway target **and** the Cedar permit from these entries, so adding a data source
is a JSON edit — no HCL, no TypeScript, no policy to write.

```json
"tools": {
  "kb":         { "type": "kb", "corpora": ["reference"],
                  "policy": { "tool": "retrieve",
                              "restrictTo": { "filter": ["reference"] } } },
  "websearch":  { "type": "websearch", "maxResults": 10 },
  "docs":       { "type": "mcp", "endpoint": "https://knowledge-mcp.global.api.aws",
                  "call": "aws___search_documentation", "arg": "search_phrase",
                  "listingMode": "DYNAMIC" },
  "billing":    { "type": "openapi", "schemaS3Uri": "s3://your-bucket/billing.yaml" }
}
```

| `type` | Backend | Key fields |
|---|---|---|
| `kb` | Bedrock Knowledge Base on S3 Vectors, via a Lambda target | `corpora` — the top-level folders under `kb_docs/` |
| `websearch` | The AWS-managed AgentCore Web Search connector | `maxResults`, `targetIncludeDomains`/`targetExcludeDomains` (enforced, agent-invisible), `includeDomains`/`excludeDomains`, `publishedFrom`/`publishedTo`, `connectorVersion` (Terraform only) |
| `mcp` | **Any** remote MCP server over Streamable HTTP | `endpoint`, `call`, `arg`, `args`, `listingMode`, `auth` |
| `openapi` | Any REST API described by an OpenAPI schema in S3 | `schemaS3Uri` |
| `lambda` | **Any function you own** — a warehouse, an RDBMS, an internal service, anything inside a VPC | `lambdaArn` (yours) *or* `source: "tool_lambda"` (the shipped demo), plus `toolSchema` — there is no `tools/list` for the Gateway to call, so the tools are declared |

At most one `kb` and one `websearch` entry; as many `mcp`, `openapi` and `lambda` as
you like. Every field of every type:
[`orchestrator/docs/WORKFLOW_REFERENCE.md`](orchestrator/docs/WORKFLOW_REFERENCE.md).

**Your own MCP server** is just the endpoint:

```json
"internal_tools": { "type": "mcp", "endpoint": "https://mcp.your-company.com/mcp" }
```

**Secrets never go in `workflow.json`.** For a tool whose endpoint needs a key, pass
it at apply time keyed by the tool name:

```bash
export TF_VAR_tool_api_keys='{"internal_tools":"your-key"}'   # CDK: TOOL_API_KEYS
```

The key is vaulted in an AgentCore credential provider and sent by the Gateway as an
`X-API-Key` header, so the agent never sees it.

**Your own documents:** replace the contents of `orchestrator/kb_docs/`. Each
top-level folder becomes a corpus (a filterable `doc_type`), listed in the `kb`
tool's `corpora` and selected per agent with that agent's `corpus` field. The next
apply uploads, re-ingests, and updates the authorization policy.

> **Check the ingestion job after you add documents.** S3 Vectors caps *filterable*
> metadata at 2048 bytes per vector, so a single large document can fail on its own
> while the rest of the corpus succeeds — the job ends `FAILED` with
> `numberOfDocumentsFailed: 1`, the deploy still reports success, and the KB simply
> never returns that content. Both IaC paths declare the two keys that grow with the
> document (`AMAZON_BEDROCK_TEXT`, `AMAZON_BEDROCK_METADATA`) as non-filterable, which
> is what keeps you under the cap.
> ```bash
> KB=$(terraform output -raw knowledge_base_id)   # CDK: the knowledgeBaseId output
> DS=$(aws bedrock-agent list-data-sources --knowledge-base-id $KB \
>        --query 'dataSourceSummaries[0].dataSourceId' --output text)
> aws bedrock-agent list-ingestion-jobs --knowledge-base-id $KB --data-source-id $DS \
>   --query 'sort_by(ingestionJobSummaries,&startedAt)[-1]'
> ```
> Expect `"status": "COMPLETE"` and `numberOfDocumentsFailed: 0`.
>
> **The index and KB names carry a digest** (`kb-index-<sha8>`,
> `<agent>-kb-<sha8>`) taken over their immutable properties. Neither dimension nor
> the non-filterable key list can change in place, so changing one must *replace* the
> resource — and CloudFormation refuses to replace a resource with a fixed custom
> name. Deriving the name makes that replacement routine, at the cost of a **new
> empty KB id** and a fresh ingest on such a change: expect `knowledgeBaseId` in the
> outputs to change, and re-check the ingestion job above.

> **Always verify what a new MCP target actually publishes — and PAGINATE.**
> `tools/list` returns pages: read only the first and a target looks empty when its
> tools are simply on page two. This deployment has four targets and the remote MCP
> server alone publishes five tools, so the catalogue does not fit on one
> page 2. Follow `nextCursor` until it is absent. The recipe is in
> [`GETTING_STARTED.md`](GETTING_STARTED.md).
>
> Set `"call"` to whatever name appears, minus the `<targetName>___` prefix. Note the
> Gateway composes every name as `<targetName>___<toolName>`, and AWS *also* uses
> `___` to namespace tools on its managed servers — so the sample's `docs` target
> publishes `docs___aws___search_documentation`, which is why `workflow.json` sets
> `"call": "aws___search_documentation"`. That works; it just looks surprising.
>
> If a target genuinely lists nothing, the agent raises `ToolUnavailable` and the run
> fails with that reason — nothing is ever substituted.
>
> The sample's `docs` target needs no deployment at all: it points at
> `https://knowledge-mcp.global.api.aws`, the AWS-managed Knowledge MCP Server (five
> tools over live AWS content, no credentials).

> **Web search: citations are contractual.** The connector returns `title`, `url` and
> `publishedDate` per result, and its acceptable-use terms require you to retain and
> *display* them. The framework preserves them into the model's evidence, verifies
> every citation URL against that evidence (dropping invented links and downgrading
> any `sourced-fact` that rested on one), and renders each source as a link in the UI.
> Configure `targetIncludeDomains` / `targetExcludeDomains` for filters an agent
> cannot influence; `includeDomains` / `excludeDomains` / `publishedFrom` /
> `publishedTo` are request-level and caller-supplied.

An OpenAPI operation an agent calls should accept a parameter named `query`.

## 9. (Optional) Tune the feature set

Everything below is a `orchestrator/app/workflow.json` edit followed by
`terraform apply` (Terraform reads that file too):

| Want to… | Change |
|---|---|
| Turn a capability on/off for one agent | that agent's `agentcore` block (`memory.longTerm`, `guardrails.input/output`, `evaluations.enabled/auto`, `policy.enabled`) |
| Stop paying for automatic evaluation | set `evaluations.auto: false` (the UI button still works on demand) |
| Test Cedar rules without blocking | `orchestrator.policy` → add `"mode": "LOG_ONLY"` |
| Turn policy off entirely | `orchestrator.policy.enabled: false` (no engine is created) |
| Hide or restrict the assistant | `orchestrator.chatbot.enabled: false`, or set individual `chatbot.tools.*` to `false` (e.g. the action tools `rerun`, `review`, `runEval`) |
| Move an agent to its own runtime | `"runtime": "dedicated"` — Terraform provisions it |
| Add / swap a data source | the `tools` block (see step 8) |
| Add an agent | a folder under `app/subagents/<id>/` + an `agents` entry + a place in `steps` (see [`GETTING_STARTED.md`](GETTING_STARTED.md)) |
| Reduce span-indexing cost | `transaction_search_indexing_percentage` in `terraform.tfvars` (1% is free) |

The shipped **guardrail** is a sample, and like the Cedar policy it is *generated* —
`terraform/guardrail.tf` carries no domain content. Replace its filters and denied
topics in the `guardrail` block of `app/workflow.json` before real use.

The **Cedar policy is generated**, so there is nothing to hand-edit: it permits
exactly the tools declared in the `tools` block, and the `kb` entry's `restrictTo`
pins retrieval to the declared corpora. Widen or narrow it by editing that config.

**Tear down:** `terraform destroy` (removes all resources and data). A destroy +
re-apply creates a new `ui_url`; Terraform re-wires the Cognito callback URLs for you, but you'll need to re-create your login user (step 6).

> `terraform destroy` leaves **Transaction Search** enabled — it is an account-wide
> setting that other workloads may rely on, so it is deliberately not torn down.
> Disable it manually if you want to (`aws xray update-trace-segment-destination
> --destination XRay`).

> **What a teardown leaves behind.** Verified by running a full destroy of this stack:
> every resource that holds your data — DynamoDB tables, both S3 buckets, the S3
> Vectors bucket and index, the Knowledge Base, the Gateway and its targets, all four
> AgentCore runtimes, the Memory stores, the Cognito pool — is removed. What survives
> is CloudWatch **log groups** that this stack does not create:
>
> * `/aws/bedrock-agentcore/runtimes/<runtime>-DEFAULT` (one per runtime) — created by
>   the AgentCore service under a generated id, so the stack cannot pre-declare them.
> * three CDK/Terraform framework helper Lambdas' groups (the custom-resource provider,
>   the S3 auto-delete handler, the Transaction Search provider).
>
> The log groups for the Lambdas this project *does* create are declared explicitly
> (retention `var.log_retention_days` / `LOG_RETENTION`, default 30 days) and go with
> the stack. Left implicit they would persist with NEVER-EXPIRE retention — a real
> cost leak, which is why they are declared. To clear the residue:
>
> ```bash
> aws logs describe-log-groups \
>   --query "logGroups[?contains(logGroupName,'<agent_name>')].logGroupName" --output text \
>   | xargs -n1 aws logs delete-log-group --log-group-name
> ```


---

# Deploy with CDK (full — same surface as Terraform)

The CDK path provisions the same resources as Terraform: the orchestrator + three
dedicated runtimes, AgentCore Memory ×2, the Guardrail, DynamoDB stores, the
BFF/API, the S3 + CloudFront UI, Transaction Search, and — with
`-c enableGateway=true` — the AgentCore Gateway, the Bedrock Knowledge Base on S3
Vectors, and the Cedar policy engine. Full details in
[`orchestrator/cdk/README.md`](orchestrator/cdk/README.md).

`enableGateway` is the only switch that changes the feature surface, exactly as
`enable_gateway` does in Terraform: left `false`, no Gateway/KB/policy engine is
created and any agent bound to a `tool` fails fast, while LLM inference, guardrails,
memory, evaluations and full telemetry stay real.

## Prerequisites
- Node.js ≥ 20 and the AWS CDK CLI (`npm i -g aws-cdk`, or use the local dev dep)
- A container engine that builds `linux/arm64` — Finch, Docker, or Podman
- AWS credentials; **Bedrock model access** for `modelId` (default Claude Haiku 4.5)
  **and Titan Text Embeddings V2** when deploying with `enableGateway=true` (the
  Knowledge Base embeds with it)
- The IAM permissions to deploy are covered by CDK's bootstrap execution role
  (`AdministratorAccess` by default) — no separate policy file needed

## Deploy
```bash
cd orchestrator/cdk
npm install
export CDK_DOCKER=finch        # only if you don't have Docker
cdk bootstrap                  # first time per account/region
# Core footprint (no Gateway; agents bound to a tool will fail fast):
cdk deploy -c idp=cognito -c createCognito=true

# Full surface — adds Gateway + Knowledge Base + Cedar policy (live MCP/RAG):
cdk deploy -c idp=cognito -c createCognito=true -c enableGateway=true
```

Outputs include `uiUrl` (CloudFront), `apiEndpoint`, `cognitoUserPoolId`, and
`cognitoClientId`.

### Identity provider options

`-c idp=...` is the CDK equivalent of Terraform's `idp` variable, with the same
three providers. A missing or inconsistent value fails at **synth**, before
anything is deployed.

| Mode | Command |
|------|---------|
| **Cognito, created for you** (recommended) | `cdk deploy -c idp=cognito -c createCognito=true` |
| **Cognito, bring your own** | `cdk deploy -c idp=cognito -c cognitoUserPoolId=us-east-1_XXX -c cognitoClientId=YOUR_ID -c cognitoDomainPrefix=your-app` |
| **Auth0** | `cdk deploy -c idp=auth0 -c auth0Domain=your-tenant.us.auth0.com -c auth0ClientId=YOUR_SPA_CLIENT_ID` |
| **No auth** (sandbox only) | `cdk deploy -c idp=none` |

> With `-c enableGateway=true` you also need the machine-to-machine identity, just
> as in Terraform. For `idp=cognito -c createCognito=true` CDK creates it for you;
> otherwise pass `-c gatewayClientId=... -c gatewayAudience=...` and export
> `GATEWAY_CLIENT_SECRET`. `idp=none` cannot be combined with `enableGateway=true`.

For Auth0, add the `uiUrl` output to the application's Allowed Callback URLs,
Allowed Logout URLs and Allowed Web Origins.

With `-c createCognito=true` the Hosted UI callback and sign-out URLs are wired to the
CloudFront domain **by the stack itself**, so there is no post-deploy URL step. (Doing it
by CLI afterwards used to be the instruction here and was a trap: the next `cdk deploy`
reverted it and login broke again.)

### Create a login user, and put them in a group

Same as the Terraform path — the pool is **admin-create-only** (self-signup is off, since
the UI sits on a public CloudFront URL), and the groups named by
`workflow.json`'s `authorization` block are created for you while *membership* is not:

```bash
POOL=<cognitoUserPoolId output>
aws cognito-idp admin-create-user --user-pool-id "$POOL" \
  --username you@example.com \
  --user-attributes Name=email,Value=you@example.com Name=email_verified,Value=true \
  --message-action SUPPRESS
aws cognito-idp admin-set-user-password --user-pool-id "$POOL" \
  --username you@example.com --password '<a-strong-password>' --permanent
for G in approvers operators; do
  aws cognito-idp admin-add-user-to-group --user-pool-id "$POOL" \
    --username you@example.com --group-name "$G"
done
```

Without the group step you can log in but the approval controls stay disabled. See
[step 6 of the Terraform path](#6-create-a-login-user) for the full action → group table
and the Auth0 `groupsClaim` caveat.

**Tear down:** `cdk destroy`. A destroy + re-deploy mints a new CloudFront URL, API
endpoint and Cognito pool, so re-create your login user afterwards. The same residue
note as the Terraform path applies — see
[what a teardown leaves behind](#what-a-teardown-leaves-behind) above.

---

# Deploying both (different accounts or regions)

CDK and Terraform can coexist when targeting **different** AWS accounts or regions.
Resource names incorporate the account ID, so there are no global naming collisions.

Example: CDK → us-east-1 (Account A), Terraform → us-west-2 (Account B):

```bash
# Terminal 1 — CDK
export AWS_REGION=us-east-1 CDK_DEFAULT_REGION=us-east-1 CDK_DOCKER=finch
cd orchestrator/cdk && cdk deploy -c idp=cognito -c createCognito=true

# Terminal 2 — Terraform (with Account B credentials)
export AWS_REGION=us-west-2
cd orchestrator/terraform && terraform apply
```

Both produce independent CloudFront URLs, each with its own Cognito User Pool.
Update the callback URLs for each after deployment (step 5 above).
