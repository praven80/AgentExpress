# Deployment Guide

Two infrastructure-as-code options. Pick one — don't run both against the same
account/region, the resource names overlap.

| | **Terraform** (`orchestrator/terraform/`) | **CDK / TypeScript** (`orchestrator/cdk/`) |
|---|---|---|
| Footprint | Full | Full — the same resources |
| AgentCore capabilities | 9 of 9 | 9 of 9 |
| Tool plane (MCP / RAG / Cedar) | `enable_gateway = true` (default) | `-c enableGateway=true` (default `false`) |
| Container image | Per-stack ECR repository | CDK `DockerImageAsset` (bootstrap ECR repo) |
| Remote state | S3 + native locking (`./bootstrap-state.sh`) | CloudFormation (managed by AWS) |
| Guide | [Terraform](#deploy-with-terraform) (below) | [CDK](#deploy-with-cdk), plus [`orchestrator/cdk/README.md`](orchestrator/cdk/README.md) |

Both build the same ARM64 image, provision the orchestrator plus one dedicated AgentCore
runtime per `dedicated` agent (three of the five research agents), and read
`app/workflow.json` as the single source of truth for agents and topology.

---

# Deploy with Terraform

All commands run from `orchestrator/terraform/`.

## 1. Prerequisites

- **Terraform ≥ 1.10** — `versions.tf` requires it for S3-native state locking
  (`use_lockfile`), which replaces the old DynamoDB lock table
- A container engine that builds `linux/arm64` — Finch, Docker or Podman
- **AWS CLI** with credentials for the target account

In the target account and region, enable **Amazon Bedrock model access** for:

- the model in `model_id` (default **Claude Haiku 4.5**), and
- the Knowledge Base embedding model — **Titan Text Embeddings V2** by default,
  overridable with `embeddingModel` on the `kb` tool.

Deploys and KB ingestion fail without both.

## 2. IAM permissions to deploy

Attach `orchestrator/terraform/deploy-role-policy.json` to the role or user you deploy
with. It grants exactly what Terraform needs: ECR, AgentCore runtimes/memories/gateway/
policy engines, Bedrock KB + S3 Vectors + guardrails, DynamoDB, Lambda, API Gateway, S3,
CloudFront, IAM execution roles, logs, and the X-Ray calls that enable Transaction Search.

```bash
aws sts get-caller-identity          # confirm the account
```

## 3. Identity provider

One variable, `idp`, selects the provider for **both** auth boundaries: end-user login on
the UI plus the API Gateway JWT authorizer on `/api/*`, and the machine-to-machine token
agents use to call the Gateway. Everything downstream — the authorizer's issuer and
audience, the Gateway's `CUSTOM_JWT` config, the token endpoint, and the `auth-config.js`
the SPA reads — is derived in **`terraform/identity.tf`**. That is the only file that knows
how the providers differ, and the only one to touch to add another.

| `idp` | End-user login | Terraform creates the IdP? |
|---|---|---|
| `"cognito"` | Cognito Hosted UI | Yes, if `cognito.create = true` |
| `"auth0"` | Auth0 Universal Login (`auth0-spa-js`) | No — existing tenant |
| `"none"` | **No login at all** | n/a |

If something required for your provider is missing, `terraform plan` fails naming the exact
variable. Nothing half-configured reaches AWS.

### Option A — Cognito, created for you (recommended)

```hcl
idp     = "cognito"
cognito = { create = true }
```

Terraform creates the User Pool, Hosted UI domain (`<agent-name>-<account-id>`), the public
SPA client and — when `enable_gateway = true` — the Resource Server plus the confidential
M2M client. CloudFront callback and sign-out URLs are wired automatically and the M2M
secret is read from state, so there is nothing to configure by hand. Skip to step 4.

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

Create first, in Cognito:

1. **User Pool** → `cognito.user_pool_id`
2. **Hosted UI domain** → `cognito.domain_prefix`
3. **App Client (public, no secret)** for the SPA → `cognito.client_id`. Authorization code
   grant; scopes `openid`, `email`, `profile`. After the first apply, add the `ui_url`
   output to its Allowed Callback and Sign-out URLs.
4. Only with `enable_gateway = true`: a **Resource Server** defining a custom scope (e.g.
   `gateway/invoke`) and a **confidential App Client** authorized for it → see
   `gateway_identity` below.

### Option C — Auth0

```hcl
idp = "auth0"
auth0 = {
  domain    = "your-tenant.us.auth0.com"   # no scheme, no trailing slash
  client_id = "YOUR_AUTH0_SPA_CLIENT_ID"
}
```

Terraform creates nothing inside Auth0. In your tenant:

1. An **Application** of type *Single Page Application* → `auth0.client_id`. Add the
   `ui_url` output to Allowed Callback URLs, Allowed Logout URLs **and** Allowed Web
   Origins after the first apply.
2. Only with `enable_gateway = true`: an **API** (its Identifier is the audience) and a
   **Machine to Machine** application authorized for it → `gateway_identity`.

### Option D — No login

```hcl
idp            = "none"
enable_gateway = false
```

The UI and `/api/*` deploy **open to anyone with the URL**. Use it for a throwaway sandbox,
never for anything shared. `enable_gateway` must be `false`, because the Gateway's
`CUSTOM_JWT` authorizer needs an OIDC provider — so every tool-bound agent (all five
research agents in this sample) fails fast rather than inventing evidence.

You must also **remove `authorization.actions`** from `workflow.json`, which ships
populated. With no authorizer there are no JWT claims, so every group check evaluates
against an empty set and denies everyone, locking you out of the app you just deployed.
Both IaC paths reject the combination at plan/synth time.

### Machine-to-machine identity (agent → Gateway)

Required when `enable_gateway = true`, except for Option A:

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
| M2M **client secret** | `TF_VAR_gateway_client_secret` (never in a file) |

The two providers issue different token shapes, which is why the Gateway authorizer and the
runtime's token request are both provider-aware:

| | Cognito M2M token | Auth0 M2M token |
|---|---|---|
| Claims present | `client_id` + `scope`, no `aud` | `aud` + `azp`, no `client_id` |
| Gateway pins on | `allowed_clients` | `allowed_audience` + an `azp` custom claim |
| Token request | HTTP Basic + `scope` | form body + `audience` |
| Token endpoint | `/oauth2/token` | `/oauth/token` |

## 4. Remote state (once per account)

State lives in S3 with native locking, so anyone with deploy credentials plans and applies
the same stack. Run the bootstrap once — it derives the account from your credentials,
creates a versioned, encrypted, private bucket, writes `backend.hcl` (git-ignored), and
runs `terraform init`:

```bash
cd orchestrator/terraform
./bootstrap-state.sh
```

```
Bucket: agentcore-multiagent-orchestrator-tfstate-<account-id>
Key:    orchestrator/terraform.tfstate
```

Safe to re-run. If you had previously applied with **local** state it migrates that state
into S3 (taking a timestamped backup first) rather than orphaning live resources.

Overrides: `AWS_REGION` for the bucket region, `STATE_NAME` for the bucket infix. For a
solo trial, comment out the `backend "s3"` block in `versions.tf` and run
`terraform init -migrate-state`.

## 5. Deploy

```bash
cd orchestrator/terraform
cp terraform.tfvars.example terraform.tfvars   # then edit
terraform apply                                # builds the image + provisions everything
```

Recommended starting point — everything created for you, full tool plane:

```hcl
region         = "us-west-2"
idp            = "cognito"
cognito        = { create = true }
enable_gateway = true          # the default; all five research agents need it
```

Auth0 with the Gateway:

```hcl
idp              = "auth0"
auth0            = { domain = "your-tenant.us.auth0.com", client_id = "..." }
gateway_identity = { client_id = "...", audience = "https://your-api-id" }
```

Export the M2M secret only when you bring your own client (any Auth0 setup, or Cognito with
`create = false`) and `enable_gateway = true`:

```bash
export TF_VAR_gateway_client_secret='<m2m client secret>'
```

The first apply builds and pushes the image; provisions the orchestrator runtime plus one
dedicated runtime per `dedicated` agent; creates both AgentCore Memory resources (the
checkpointer and the long-term semantic/summary store), the A2A stand-in Lambda, the Bedrock
Guardrail, and the status/events/telemetry/insights tables; enables CloudWatch Transaction
Search (idempotent); and with `enable_gateway = true` creates the Gateway, its Cedar policy
engine, and a KB ingestion job. Allow several minutes.

```bash
terraform output
# ui_url        -> the app URL (CloudFront)
# api_endpoint  -> HTTP API base
```

`bootstrap-state.sh` already ran `terraform init`.

## 6. Create a login user

Skip for `idp = "none"`. For `idp = "auth0"`, create the user in your tenant and make sure
`terraform output ui_url` is in the application's Allowed Callback URLs, Allowed Logout URLs
and Allowed Web Origins.

With `cognito = { create = true }` the callback and sign-out URLs are already wired to
CloudFront, so login works immediately.

The pool is **admin-create-only**: the UI sits on a public CloudFront URL, and self-signup
would let anyone register and spend your Bedrock budget.

```bash
POOL=$(terraform output -raw cognito_user_pool_id)
aws cognito-idp admin-create-user --user-pool-id "$POOL" \
  --username you@example.com \
  --user-attributes Name=email,Value=you@example.com Name=email_verified,Value=true \
  --message-action SUPPRESS
aws cognito-idp admin-set-user-password --user-pool-id "$POOL" \
  --username you@example.com --password '<a-strong-password>' --permanent
```

### Put that user in a group

`workflow.json` ships an `authorization` block, so being logged in is not enough to approve
a gate. Terraform creates one Cognito group per group the block names, but **membership is
not managed in IaC** — it is per-person and changes far more often than a deploy. A new user
is in no group and sees the Approve/Revise/Deny buttons greyed out.

```bash
for G in approvers operators; do
  aws cognito-idp admin-add-user-to-group --user-pool-id "$POOL" \
    --username you@example.com --group-name "$G"
done
```

Then **sign out and back in** — group membership rides in the ID token.

The shipped mapping (edit it in `orchestrator/app/workflow.json`):

| Action | Meaning | Groups |
|--------|---------|--------|
| `start`    | start a run | *unrestricted in the sample* |
| `decision` | approve / revise / deny a review gate | `approvers` |
| `rerun`    | re-run an agent and everything downstream | `approvers` |
| `cancel`   | stop a running workflow | `approvers`, `operators` |
| `evaluate` | run an AgentCore evaluation on demand | `operators` |
| `insights` | run the cross-run Insights batch analysis | `operators` |
| `delete`   | permanently remove a run and its timeline | `operators` |

An action **not listed** is unrestricted, so deleting the block gives every logged-in user
everything. An action listed with an **empty** group list is denied to everyone — that is
how you switch a capability off. Full semantics in the `authorization` section of
[`orchestrator/docs/WORKFLOW_REFERENCE.md`](orchestrator/docs/WORKFLOW_REFERENCE.md).

Check what the app thinks you can do:

```bash
curl -s -H "Authorization: Bearer $ID_TOKEN" "$(terraform output -raw api_endpoint)/api/me"
# {"user":"you@example.com","groups":["approvers","operators"],
#  "permittedActions":["start","decision","rerun","cancel","evaluate","insights","delete"]}
```

To *demonstrate* RBAC, create a second user in no group. Signed in as them, the approval
controls render greyed out with the reason, and the API refuses each action with a 403
naming the group required and the groups you hold.

**On Auth0**, set `authorization.groupsClaim` to a **namespaced** custom claim (e.g.
`https://your-app/roles`) and add a post-login Action that emits it. Auth0 will not issue an
unnamespaced custom claim, so the default `cognito:groups` finds nothing and every gated
action is denied.

> **Bringing your own pool** (`cognito = { create = false }`)? Add
> `terraform output ui_url` to your App Client's Allowed Callback and Sign-out URLs
> yourself, or login fails with a redirect mismatch. The `authorization` groups are still
> created in your pool.

## 7. Use it

Open `ui_url`, log in, type a request and start a session. Approve, revise or deny at each
gate.

Optionally fill the **Subject** box before starting: it scopes long-term memory, so agents
recall insights from earlier runs on the same subject (a customer, product, project —
whatever your domain groups knowledge by).

Once a run finishes:

- **Observability → Run detail** — per-agent cost, latency and tokens. Click
  **⤢ Prompts & I/O** on an agent for its exact prompts, tool queries, memory operations,
  guardrail and policy decisions, and evaluation scores, per run version.
- **Evaluate** — agents with `evaluations.auto: true` are scored at completion; use the
  button for the rest. Scores take 30–60s.
- **Observability → Insights** — pick a window and **Run insights** for the cross-run
  failure/intent/summary analysis. Takes a few minutes; needs Transaction Search (step 5
  enabled it) and at least one completed run.
- **Re-run** — open an agent's output and use *Rerun from here*, or pick several agents of
  one parallel stage in the **Re-run agents** panel. Downstream stages regenerate and their
  gates re-pause.
- **Assistant** (bottom right) — ask about status, cost or evals, or tell it to approve a
  gate, re-run an agent, or run an evaluation.
- **Timeline** — carries the branch rule that matched, any guardrail block, and any
  ungrounded figure the framework flagged for your attention at the next gate.

## 8. Point it at your own data sources

Every tool your agents can call is declared in the **`tools`** block of
`orchestrator/app/workflow.json`. Each key is both the Gateway target name and the label an
agent references via its `tool` field, and must be **letters and digits starting with a
letter** (the Gateway target forbids underscores, the generated Cedar permit forbids
hyphens). Both IaC paths generate the Gateway target **and** the Cedar permit from these
entries, so adding a data source is a JSON edit.

```json
"tools": {
  "kb":        { "type": "kb", "corpora": ["reference"],
                 "policy": { "tool": "retrieve",
                             "restrictTo": { "filter": ["reference"] } } },
  "websearch": { "type": "websearch", "maxResults": 10 },
  "docs":      { "type": "mcp", "endpoint": "https://knowledge-mcp.global.api.aws",
                 "call": "aws___search_documentation", "arg": "search_phrase",
                 "listingMode": "DYNAMIC" },
  "pricing":   { "type": "lambda", "source": "pricing",
                 "call": "aws_prices", "arg": "services" },
  "lifecycle": { "type": "openapi", "source": "lifecycle",
                 "call": "getProductLifecycle", "arg": "product" }
}
```

| `type` | Backend | Key fields |
|---|---|---|
| `kb` | Bedrock Knowledge Base on S3 Vectors, via a Lambda target | `corpora` — the top-level folders under `kb_docs/`. Plus `embeddingModel`/`dimensions`, `corpusKey`, `corpusOperator`, `rerank` and a target-level `filter` |
| `websearch` | The AWS-managed AgentCore Web Search connector | `maxResults`, `domains` (`{include, exclude}` — set on the target, enforced, agent-invisible), `publishedFrom`/`publishedTo`, `connectorVersion` (Terraform only) |
| `mcp` | Any remote MCP server over Streamable HTTP | `endpoint`, `call`, `arg`, `args`, `listingMode`, `auth` |
| `openapi` | Any REST API described by an OpenAPI schema | exactly one of `schemaS3Uri` (an s3:// object you host) or `source` (a folder under `app/tools/` holding `openapi.json`, which the framework uploads). Tool names come from the schema's `operationId`s |
| `lambda` | A function — a warehouse, an RDBMS, an internal service, anything in a VPC | exactly one of `lambdaArn` (one you deployed) or `source` (packaged from `app/tools/<source>/`), plus `toolSchema` — there is no `tools/list` for the Gateway to call, so the tools are declared |

At most one `kb` and one `websearch`; as many `mcp`, `openapi` and `lambda` as you like.
Every field of every type:
[`orchestrator/docs/WORKFLOW_REFERENCE.md`](orchestrator/docs/WORKFLOW_REFERENCE.md).

`arg` names the parameter the agent's query is passed as, and **defaults to `query`** — set
it to whatever your operation or tool actually calls that parameter.

**Your own MCP server** is just the endpoint:

```json
"internalTools": { "type": "mcp", "endpoint": "https://mcp.your-company.com/mcp" }
```

**Secrets never go in `workflow.json`.** For a tool whose endpoint needs a key, pass it at
apply time keyed by the tool name:

```bash
export TF_VAR_tool_api_keys='{"internalTools":"your-key"}'   # CDK: TOOL_API_KEYS
```

The key is vaulted in an AgentCore credential provider and sent by the Gateway as an
`X-API-Key` header, so the agent never sees it.

**Your own documents:** replace the contents of `orchestrator/kb_docs/`. Each top-level
folder becomes a corpus (a filterable `doc_type`), listed in the `kb` tool's `corpora` and
selected per agent with that agent's `corpus` field. The next apply uploads, re-ingests and
updates the authorization policy.

> **Check the ingestion job after adding documents.** S3 Vectors caps *filterable* metadata
> at 2048 bytes per vector, so one large document can fail on its own while the rest of the
> corpus succeeds: the job ends `FAILED` with `numberOfDocumentsFailed: 1`, the deploy
> reports success, and the KB never returns that content. Both IaC paths declare the two
> keys that grow with the document (`AMAZON_BEDROCK_TEXT`, `AMAZON_BEDROCK_METADATA`) as
> non-filterable, which is what keeps you under the cap.
>
> ```bash
> KB=$(terraform output -raw knowledge_base_id)   # CDK: the knowledgeBaseId output
> DS=$(aws bedrock-agent list-data-sources --knowledge-base-id $KB \
>        --query 'dataSourceSummaries[0].dataSourceId' --output text)
> aws bedrock-agent list-ingestion-jobs --knowledge-base-id $KB --data-source-id $DS \
>   --query 'sort_by(ingestionJobSummaries,&startedAt)[-1]'
> ```
>
> Expect `"status": "COMPLETE"` and `numberOfDocumentsFailed: 0`.

> **The index and KB names carry a digest** (`kb-index-<sha8>`, `<agent>-kb-<sha8>`) taken
> over their immutable properties. Neither the dimension nor the non-filterable key list can
> change in place, so changing one must *replace* the resource — and CloudFormation refuses
> to replace a resource with a fixed custom name. Deriving the name makes that replacement
> routine, at the cost of a new empty KB id and a fresh ingest: expect `knowledgeBaseId` to
> change, and re-check the ingestion job.

> **Verify what a new MCP target publishes, and paginate.** `tools/list` returns pages, so a
> target looks empty when its tools are on page two. This deployment has five targets and
> the remote MCP server alone publishes five tools, so the catalogue does not fit on one
> page — follow `nextCursor` until it is absent. The recipe is in
> [`GETTING_STARTED.md`](GETTING_STARTED.md).
>
> Set `call` to whatever name appears, minus the `<targetName>___` prefix. The Gateway
> composes every name as `<targetName>___<toolName>`, and AWS *also* uses `___` to namespace
> tools on its managed servers — so the sample's `docs` target publishes
> `docs___aws___search_documentation`, which is why `workflow.json` sets
> `"call": "aws___search_documentation"`.
>
> If a target genuinely lists nothing, the agent raises `ToolUnavailable` and the run fails
> with that reason. Nothing is substituted.

> **Web search: citations are contractual.** The connector returns `title`, `url` and
> `publishedDate` per result, and its acceptable-use terms require you to retain and
> *display* them. The framework preserves them into the model's evidence, verifies every
> citation URL against that evidence (dropping invented links and downgrading any
> `sourced-fact` that rested on one), and renders each source as a link. Use `domains` for a
> filter an agent cannot influence — it is set on the target, so the Gateway applies it to
> every request. `publishedFrom` / `publishedTo` are request-level and caller-supplied.

## 9. Tune the feature set

Each of these is a `orchestrator/app/workflow.json` edit followed by `terraform apply`:

| Want to… | Change |
|---|---|
| Turn a capability on/off for one agent | that agent's `agentcore` block (`memory.longTerm`, `guardrails.input/output`, `evaluations.enabled/auto`, `policy.enabled`) |
| Stop paying for automatic evaluation | `evaluations.auto: false` (the UI button still works) |
| Test Cedar rules without blocking | `orchestrator.policy.mode: "LOG_ONLY"` |
| Turn policy off entirely | `orchestrator.policy.enabled: false` (no engine is created) |
| Hide or restrict the assistant | `orchestrator.chatbot.enabled: false`, or set individual `chatbot.tools.*` to `false` (the action tools are `rerun`, `review`, `runEval`) |
| Move an agent to its own runtime | `"runtime": "dedicated"` |
| Add or swap a data source | a `tools` entry (step 8), plus a folder under `app/tools/<source>/` only when you use `source` |
| Add an agent | a folder under `app/subagents/<id>/` + an `agents` entry + a place in `steps` (see [`GETTING_STARTED.md`](GETTING_STARTED.md)) |
| Reduce span-indexing cost | `transaction_search_indexing_percentage` in `terraform.tfvars` (1% is free) |

The shipped **guardrail** and **Cedar policy** are both samples and both *generated* —
`terraform/guardrail.tf` and `policy.tf` carry no domain content. Replace the guardrail's
filters and denied topics in the `guardrail` block, and adjust tool authorization by editing
the `tools` block: the policy permits exactly what is declared there, and the `kb` entry's
`restrictTo` pins retrieval to the declared corpora.

## 10. Tear down

```bash
terraform destroy
```

Removes all resources and data. A destroy plus re-apply creates a new `ui_url`; Terraform
re-wires the Cognito callback URLs, but you need to re-create your login user (step 6).

`terraform destroy` cannot remove two things: **Transaction Search**, which is account-wide
and other workloads may rely on, and the **remote-state S3 bucket**, because
`bootstrap-state.sh` creates it outside Terraform. `./teardown.sh` does the destroy and then
both, plus the local `.terraform` artifacts. Every step is best-effort and idempotent, and
the account id and bucket name come from your active credentials.

```bash
./teardown.sh                            # prompts before destroying anything
FORCE=1 ./teardown.sh                    # no prompt
KEEP_TRANSACTION_SEARCH=1 ./teardown.sh  # leave the account-wide setting alone
```

### What a teardown leaves behind

Every resource holding your data is removed — DynamoDB tables, both S3 buckets, the S3
Vectors bucket and index, the Knowledge Base, the Gateway and its targets, all four
AgentCore runtimes, the Memory stores, the Cognito pool. What survives is CloudWatch **log
groups this stack does not create**:

- `/aws/bedrock-agentcore/runtimes/<runtime>-DEFAULT`, one per runtime — created by the
  AgentCore service under a generated id, so the stack cannot pre-declare them.
- The framework helper Lambdas' groups (the custom-resource provider, the S3 auto-delete
  handler, the Transaction Search provider).

Log groups for the Lambdas this project *does* create are declared explicitly (retention
`var.log_retention_days` / `LOG_RETENTION`, default 30 days) and go with the stack. Left
implicit they would persist with never-expire retention, which is a real cost leak. To clear
the residue:

```bash
aws logs describe-log-groups \
  --query "logGroups[?contains(logGroupName,'<agent_name>')].logGroupName" --output text \
  | xargs -n1 aws logs delete-log-group --log-group-name
```

---

# Deploy with CDK

Same resources as Terraform. Full details in
[`orchestrator/cdk/README.md`](orchestrator/cdk/README.md).

`enableGateway` is the only switch that changes the feature surface, exactly as
`enable_gateway` does — but it defaults to **`false`** here. Left off, no Gateway, KB or
policy engine is created and every tool-bound agent fails fast, while inference, guardrails,
memory, evaluations and telemetry stay real.

## Prerequisites

- Node.js ≥ 20 and the AWS CDK CLI (`npm i -g aws-cdk`, or the local dev dependency)
- A container engine that builds `linux/arm64` — Finch, Docker or Podman
- AWS credentials, with **Bedrock model access** for `modelId` (default Claude Haiku 4.5)
  and for the embedding model (default Titan Text Embeddings V2) when deploying with
  `enableGateway=true`
- Deploy permissions come from CDK's bootstrap execution role, so there is no separate
  policy file

## Deploy

```bash
cd orchestrator/cdk
npm install
export CDK_DOCKER=finch        # only if you don't have Docker
cdk bootstrap                  # first time per account/region
cdk deploy -c idp=cognito -c createCognito=true -c enableGateway=true
```

A bare `cdk deploy` fails at synth: `idp` defaults to `cognito`, which needs either
`-c createCognito=true` or the three `cognito*` ids.

Outputs include `uiUrl`, `apiEndpoint`, `cognitoUserPoolId`, `cognitoClientId`, `gatewayUrl`
and `knowledgeBaseId`.

### Identity provider options

| Mode | Command |
|------|---------|
| **Cognito, created for you** (recommended) | `cdk deploy -c idp=cognito -c createCognito=true` |
| **Cognito, bring your own** | `cdk deploy -c idp=cognito -c cognitoUserPoolId=us-east-1_XXX -c cognitoClientId=YOUR_ID -c cognitoDomainPrefix=your-app` |
| **Auth0** | `cdk deploy -c idp=auth0 -c auth0Domain=your-tenant.us.auth0.com -c auth0ClientId=YOUR_SPA_CLIENT_ID` |
| **No auth** (sandbox only) | `cdk deploy -c idp=none` |

With `-c enableGateway=true` you also need the M2M identity. For
`idp=cognito -c createCognito=true` CDK creates it; otherwise pass `-c gatewayClientId=…
-c gatewayAudience=…` and export `GATEWAY_CLIENT_SECRET`. `idp=none` cannot be combined with
`enableGateway=true`.

With `-c createCognito=true` the Hosted UI callback and sign-out URLs are wired to the
CloudFront domain by the stack. For Auth0, or a bring-your-own Cognito client, add the
`uiUrl` output to the Allowed Callback URLs, Allowed Logout URLs and Allowed Web Origins
yourself.

### Create a login user, and put them in a group

Same as the Terraform path: the pool is admin-create-only, and the groups
`authorization` names are created for you while membership is not.

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

See [step 6 of the Terraform path](#6-create-a-login-user) for the action → group table and
the Auth0 `groupsClaim` caveat.

## Tear down

```bash
cdk destroy
```

A destroy plus re-deploy mints a new CloudFront URL, API endpoint and Cognito pool, so
re-create your login user. Transaction Search is deliberately left enabled. The same residue
applies — see [what a teardown leaves behind](#what-a-teardown-leaves-behind).

---

# Deploying both (different accounts or regions)

CDK and Terraform coexist when targeting **different** accounts or regions; resource names
incorporate the account id, so there are no global collisions.

```bash
# Terminal 1 — CDK into Account A / us-east-1
export AWS_REGION=us-east-1 CDK_DEFAULT_REGION=us-east-1 CDK_DOCKER=finch
cd orchestrator/cdk && cdk deploy -c idp=cognito -c createCognito=true -c enableGateway=true

# Terminal 2 — Terraform into Account B / us-west-2
export AWS_REGION=us-west-2
cd orchestrator/terraform && terraform apply
```

Each produces an independent CloudFront URL with its own Cognito User Pool. Create a login
user in each (step 6).
