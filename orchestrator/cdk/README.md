# CDK deployment (TypeScript)

An **AWS CDK** alternative to the Terraform config in `../terraform`, for teams that
prefer `cdk deploy`. It provisions:

- the **orchestrator** AgentCore Runtime (built from `../Dockerfile`, ARM64),
- one **dedicated** AgentCore Runtime per `dedicated` agent in `../app/workflow.json`
  (three of the five research agents),
- **DynamoDB** tables — status, events, telemetry (+ `by_date` GSI), insights,
- **AgentCore Memory ×2** — the LangGraph checkpointer *and* the long-term store
  (semantic + summary strategies) that `memory.longTerm` agents recall from,
- a **Bedrock Guardrail**, injected as `GUARDRAIL_ID` so the per-agent
  `guardrails: {input/output}` flags in `workflow.json` take effect,
- IAM for **AgentCore Evaluations** (`Evaluate`) and **Insights**
  (`StartBatchEvaluation`) plus the GenAI Observability env (`AGENT_OBSERVABILITY_ENABLED`,
  `OTEL_TRACES_SAMPLER`, …),
- the **BFF Lambda** + **HTTP API** (JWT authorizer for the configured IdP —
  **Cognito**, **Auth0**, or none),
- the static **UI** on **S3 + CloudFront** (private bucket via Origin Access Control),
- **CloudWatch Transaction Search**, so agent spans reach the `aws/spans` log group
  that Observability and Insights read,
- and with `-c enableGateway=true` (see below), the whole **tool plane**: the
  AgentCore **Gateway** (MCP, `CUSTOM_JWT`), a Bedrock **Knowledge Base** on **S3
  Vectors** + its retrieve Lambda, and the **Cedar policy engine** attached to the
  Gateway.

> **Scope vs. Terraform: feature parity.** This app covers all nine AgentCore
> capabilities — Runtime, Gateway, Memory (both), Identity, Observability,
> Guardrails, Policy, Evaluations, and Optimization/Insights — and provisions the
> same resources as `../terraform`.
>
> `enableGateway` is the one switch that changes the feature surface, exactly like
> Terraform's `enable_gateway`:
>
> | | `-c enableGateway=true` | default (`false`) |
> |---|---|---|
> | LLM inference, guardrails, memory, evaluations, telemetry | real | real |
> | MCP tools + RAG retrieval | **real** (Gateway + KB) | not created — bound agents fail fast |
> | Cedar policy enforcement | **real** (server-side at the Gateway) | inert |
>
> Pick **one** IaC tool per account/region — don't run both against the same
> resources, since the names would collide.
>
> Two implementation differences from Terraform, both deliberate:
> * The container image is a CDK `DockerImageAsset` (pushed to the CDK bootstrap
>   ECR repo) rather than a per-stack ECR repository.
> * Transaction Search is enabled through a custom resource calling
>   `UpdateTraceSegmentDestination`, **not** `AWS::XRay::TransactionSearchConfig`.
>   Transaction Search is an account-wide singleton, so that resource can only ever
>   *create* it and fails with `AlreadyExists` on any account where it is already
>   on. The update API is idempotent, so this converges instead of failing.

## Prerequisites

- Node.js ≥ 20, the AWS CDK CLI (`npm i -g aws-cdk` or use the local dev dep)
- A container engine that builds `linux/arm64` — Finch, Docker, or Podman
- AWS credentials for the target account; **Bedrock model access** for the `modelId`
  (default Claude Haiku 4.5) enabled in the region
- A bootstrapped account/region: `cdk bootstrap aws://<account>/<region>`

If you use **Finch** (not Docker), tell the CDK asset builder to use it:

```bash
export CDK_DOCKER=finch
```

## Deploy

```bash
cd orchestrator/cdk
npm install
export CDK_DOCKER=finch                 # only if you don't have Docker
cdk bootstrap                           # first time per account/region
cdk deploy                              # builds the image, provisions everything
```

Outputs include `uiUrl` (CloudFront), `apiEndpoint`, `agentRuntimeArn`, and `memoryId`.

## Test

```bash
npm test        # 149 tests; no AWS credentials, no container builder
```

Config-plane tests for this path: the projections and validators, the synthesized
template, and **Terraform ↔ CDK parity** — the two paths are independent
implementations of the same projection and have drifted before. See
[`test/README.md`](test/README.md). The runtime half of the suite is
[`orchestrator/tests/`](../tests/README.md) (`pytest`).

### Login (`-c idp=...`)

Authentication is configuration driven, matching Terraform's `idp` variable. One
context key selects the provider; the API Gateway JWT authorizer and the
`auth-config.js` served to the SPA are derived from it. An inconsistent
combination fails at **synth**, before anything is deployed.

```bash
# Cognito, created for you (recommended)
cdk deploy -c idp=cognito -c createCognito=true

# Cognito, bring your own pool
cdk deploy -c idp=cognito \
  -c cognitoUserPoolId=us-east-1_XXXXXXXXX \
  -c cognitoClientId=YOUR_SPA_CLIENT_ID \
  -c cognitoDomainPrefix=your-app

# Auth0 (existing tenant — CDK creates nothing in Auth0)
cdk deploy -c idp=auth0 \
  -c auth0Domain=your-tenant.us.auth0.com \
  -c auth0ClientId=YOUR_SPA_CLIENT_ID

# No login — the UI/API deploy OPEN. Personal sandbox only.
cdk deploy -c idp=none
```

With `-c createCognito=true` the callback and sign-out URLs are wired to the CloudFront
domain by the stack, so there is nothing to do afterwards. On **Auth0** you still have to
add the `uiUrl` output to the application's Allowed Callback URLs, Allowed Logout URLs and
Allowed Web Origins; likewise for a **bring-your-own** Cognito App Client.

### Users and groups

The created pool is **admin-create-only** (self-signup off — the UI sits on a public
CloudFront URL). Create a user, then add them to the groups
`../app/workflow.json`'s `authorization` block names; the stack creates the *groups*, but
membership is per-person and not managed in IaC:

```bash
POOL=<cognitoUserPoolId output>
aws cognito-idp admin-create-user --user-pool-id "$POOL" --username you@example.com \
  --user-attributes Name=email,Value=you@example.com Name=email_verified,Value=true \
  --message-action SUPPRESS
aws cognito-idp admin-set-user-password --user-pool-id "$POOL" \
  --username you@example.com --password '<strong-password>' --permanent
for G in approvers operators; do
  aws cognito-idp admin-add-user-to-group --user-pool-id "$POOL" \
    --username you@example.com --group-name "$G"; done
```

Skip the group step and you can log in but the approval controls stay disabled. Note that
`-c idp=none` is **rejected at synth** while `authorization.actions` is non-empty: with no
authorizer there are no claims, so every rule would deny everyone.

## Configuration (context keys)

Set in `cdk.json` or via `-c key=value`:

| Key | Default | Purpose |
|---|---|---|
| `agentName` | `multiagent_orchestrator` | Resource name prefix + AgentCore runtime name |
| `modelId` | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | Default Bedrock model |
| `memoryEventExpiryDays` | `30` | AgentCore Memory event retention |
| `idp` | `cognito` | Identity provider: `cognito`, `auth0`, or `none` |
| `createCognito` | `false` | With `idp=cognito`, provision the User Pool + SPA client |
| `cognitoUserPoolId` | `""` | Existing Cognito User Pool ID |
| `cognitoClientId` | `""` | Existing Cognito App Client ID (public, for SPA) |
| `cognitoDomainPrefix` | `""` | Existing Cognito Hosted UI domain prefix |
| `auth0Domain` | `""` | Auth0 tenant domain (no scheme, no trailing slash) |
| `auth0ClientId` | `""` | Auth0 SPA application client ID |
| `enableGateway` | `false` | Create the Gateway + Knowledge Base + Cedar policy (live MCP/RAG) |
| `gatewayClientId` | `""` | Bring-your-own M2M client id (unneeded with `idp=cognito -c createCognito=true`) |
| `gatewayAudience` | `""` | OAuth2 scope (Cognito) or API identifier (Auth0) for the M2M token |
| `transactionSearchIndexingPercentage` | `100` | Percentage of spans indexed by Transaction Search |

Your **data sources are NOT context keys** — they live in the `tools` block of
`../app/workflow.json`, which this stack reads (the same file Terraform reads). The same
goes for **who may approve a run**: that is the `authorization` block, and the stack
provisions a Cognito group for each group it names. See
[`GETTING_STARTED.md`](../../GETTING_STARTED.md).

**Secrets** are read from the environment, never context (`cdk.json` is committed and
`-c` values land in `cdk.out`):

```bash
export GATEWAY_CLIENT_SECRET='<m2m client secret>'
export TOOL_API_KEYS='{"internal_tools":"your-key"}'   # keyed by workflow.json tool name
```

## The tool plane (`-c enableGateway=true`)

```bash
# Everything created for you: pool + M2M client + Gateway + KB + Cedar policy
cdk deploy -c idp=cognito -c createCognito=true -c enableGateway=true
```

With `idp=cognito -c createCognito=true` the resource server and confidential
client the Gateway needs are provisioned too, and the secret is read at deploy
time — nothing to configure. Any other combination needs `gatewayClientId`,
`gatewayAudience` and `$GATEWAY_CLIENT_SECRET`; synth fails naming what's missing.

`idp=none` cannot be combined with `enableGateway=true` — the Gateway's
`CUSTOM_JWT` authorizer requires an OIDC provider.

### Targets come from `workflow.json`

Every Gateway target — and its Cedar permit — is generated from the `tools` block of
`../app/workflow.json`. There is nothing to configure here:

```json
"tools": {
  "kb":        { "type": "kb", "corpora": ["reference"] },
  "websearch": { "type": "websearch", "maxResults": 10 },
  "docs":      { "type": "mcp", "endpoint": "https://knowledge-mcp.global.api.aws",
                 "call": "aws___search_documentation", "arg": "search_phrase",
                 "listingMode": "DYNAMIC" },
  "billing":   { "type": "openapi", "schemaS3Uri": "s3://your-bucket/api.yaml" }
}
```

A target's KEY must equal the agent's `tool` label in `workflow.json` — synth fails
if it doesn't. `validateTools()` in `lib/orchestrator-stack.ts` enforces the same
rules as Terraform's preconditions: a known `type`, `endpoint` for `mcp`,
`schemaS3Uri` for `openapi`, at most one `kb` and one `websearch`, a non-empty
`corpora` for `kb`, `websearch` only in a supported region, and valid `listingMode` /
`auth` values.

The `docs` default is AWS's fully managed **Knowledge MCP Server** — nothing is
deployed for it and no credential is needed. Point `endpoint` at your own server to
swap it.

For a tool that needs a key, add it to `$TOOL_API_KEYS` keyed by the tool name; it is
vaulted in an AgentCore credential provider and sent as an `X-API-Key` header.

> A target can reach status READY and still publish **zero** tools: the Gateway names
> each tool `<targetName>___<toolName>`, so a server whose own tool names already
> contain `___` gets dropped. Verify with a `tools/list` call against the `gatewayUrl`
> output — if the tools are missing, the agent raises `ToolUnavailable` (nothing is
> fabricated) and the fix is on the provider side.

## Tear down

```bash
cdk destroy
```

Transaction Search is deliberately **left enabled** — it is account-wide and other
stacks or services may depend on it, so tearing it down would be a surprise.

## Notes

- **Layout.** `lib/orchestrator-stack.ts` holds the runtimes, stores, IdP, BFF/API
  and UI; `lib/tool-plane.ts` holds the Gateway + Knowledge Base + Cedar policy
  subsystem, generated from the `tools` block (the counterpart to Terraform's
  `tools.tf` + `gateway.tf` + `kb.tf` + `policy.tf`).
- **You never write Cedar.** `cedarStatement()` in `lib/tool-plane.ts` emits the same
  policy Terraform does: a fine-grained permit when a tool sets `policy.tool` (with an
  optional argument restriction), otherwise a target-level permit
  (`action in AgentCore::Action::"<target>"`), which is the only form that works for a
  remote MCP server whose tool names aren't known at deploy time. Anything undeclared
  is refused by Cedar's default-deny.
- **Tool names are composed by the Gateway, not by this code.** Every published name
  is `<targetName>___<toolName>`, formed server-side. AWS also namespaces its own
  managed tools as `aws___<tool>`, so the `docs` target publishes
  `docs___aws___search_documentation` — hence `"call": "aws___search_documentation"`
  in `workflow.json`. Nothing splits on the delimiter; `call` is matched against the
  published name.
- **`tools/list` is paginated.** Follow `nextCursor` when verifying, or a target with
  tools on page two looks empty. This stack has four targets and the remote MCP server
  alone publishes five tools, so the catalogue does not fit on one page.
- **Two CDK-only limits, both surfaced as synth errors rather than silently ignored.**
  `connectorVersion` cannot be expressed (CloudFormation's connector source accepts
  only `connectorId` — use Terraform if you need a version pin), and the workflow
  projection shipped to the BFF must stay under 3 KB because Lambda caps the whole
  environment at 4 KB.
- **No stable L2 for AgentCore Memory/Runtime yet.** Those two are created via the
  underlying CloudFormation resource types (`AWS::BedrockAgentCore::Memory`,
  `AWS::BedrockAgentCore::Runtime`) — the same types Terraform's Cloud Control
  provider uses. The Gateway, gateway targets, policy engine and credential
  providers do have generated L1s (`aws-bedrockagentcore`), which is what
  `tool-plane.ts` uses. If AWS revises any schema, adjust the property names there.
- **KB ingestion** runs through a custom resource calling `StartIngestionJob`,
  keyed on a hash of `../kb_docs` — editing the corpus re-ingests on the next
  deploy, and nothing else triggers it.
- **IAM propagation.** AgentCore runtime creation occasionally races IAM role
  propagation on a first deploy; if it fails, re-run `cdk deploy`.
- See the repo root **README** "Security, cost & scalability" section before any
  non-sandbox use.
