# CDK deployment (TypeScript)

An **AWS CDK** alternative to `../terraform`, for teams that prefer `cdk deploy`. It
provisions:

- the **orchestrator** AgentCore Runtime (built from `../Dockerfile`, ARM64),
- one **dedicated** AgentCore Runtime per `dedicated` agent in `../app/workflow.json`
  (three of the five research agents),
- the **A2A stand-in Lambda** behind an IAM-authed Function URL, so the
  `runtime: "a2a"` agents work out of the box,
- **DynamoDB** tables — status, events, telemetry (+ `by_date` GSI), insights,
- **AgentCore Memory ×2** — the LangGraph checkpointer *and* the long-term store
  (semantic + summary strategies) that `memory.longTerm` agents recall from,
- a **Bedrock Guardrail**, injected as `GUARDRAIL_ID` so the per-agent
  `guardrails: {input/output}` flags take effect,
- IAM for **AgentCore Evaluations** (`Evaluate`) and **Insights**
  (`StartBatchEvaluation`) plus the GenAI Observability env,
- the **BFF Lambda** + **HTTP API** (JWT authorizer for the configured IdP),
- the **UI** on **S3 + CloudFront** (private bucket via Origin Access Control), built
  from source at synth time — a Vite + React + Cloudscape application, not static files,
- **CloudWatch Transaction Search**, so agent spans reach the `aws/spans` log group that
  Observability and Insights read,
- and with `-c enableGateway=true`, the whole **tool plane**: the AgentCore **Gateway**
  (MCP, `CUSTOM_JWT`), a Bedrock **Knowledge Base** on **S3 Vectors** + its retrieve
  Lambda, and the **Cedar policy engine**.

**Feature parity with Terraform.** All nine AgentCore capabilities, the same resources.
`enableGateway` is the one switch that changes the surface:

| | `-c enableGateway=true` | default (`false`) |
|---|---|---|
| Inference, guardrails, memory, evaluations, telemetry | real | real |
| MCP tools + RAG retrieval | real (Gateway + KB) | not created — bound agents fail fast |
| Cedar policy enforcement | real (server-side at the Gateway) | inert |

All five research agents are tool-bound, so leave it on unless you are deliberately
testing the failure path.

Pick **one** IaC tool per account/region — the resource names collide.

Two deliberate implementation differences from Terraform:

- The container image is a CDK `DockerImageAsset` (pushed to the CDK bootstrap ECR repo)
  rather than a per-stack ECR repository.
- Transaction Search is enabled through a custom resource calling
  `UpdateTraceSegmentDestination`, not `AWS::XRay::TransactionSearchConfig`. Transaction
  Search is an account-wide singleton, so that resource can only ever *create* it and
  fails with `AlreadyExists` where it is already on. The update API is idempotent.

## Prerequisites

- Node.js ≥ 20 and the AWS CDK CLI (`npm i -g aws-cdk`, or use the local dev dependency).
  Node is needed twice: for the CDK app, and to build the UI. `cdk synth` runs
  `npm ci && npm run build` in `../web` — in a `node:22-alpine` container when one is
  available, and locally otherwise, so a synth still works without a container engine. A
  TypeScript error in the UI fails the synth rather than shipping a broken page
- A container engine that builds `linux/arm64` — Finch, Docker or Podman
- AWS credentials for the target account, with **Bedrock model access** for `modelId`
  (default Claude Haiku 4.5) enabled in the region
- A bootstrapped account/region: `cdk bootstrap aws://<account>/<region>`

Using **Finch** rather than Docker:

```bash
export CDK_DOCKER=finch
```

## Deploy

```bash
cd orchestrator/cdk
npm install
export CDK_DOCKER=finch                 # only if you don't have Docker
cdk bootstrap                           # first time per account/region
cdk deploy -c idp=cognito -c createCognito=true -c enableGateway=true
```

`idp` defaults to `cognito`, which then requires either `-c createCognito=true` or the
three `cognito*` ids — a bare `cdk deploy` fails at synth saying so.

Outputs include `uiUrl` (CloudFront), `apiEndpoint`, `agentRuntimeArn`, `memoryId`,
`gatewayUrl`, `knowledgeBaseId` and the `cognito*` ids.

## Test

```bash
npm test        # 168 tests; no AWS credentials, no container builder
```

The projections and validators, the synthesized template, and **Terraform ↔ CDK parity** —
the two paths are independent implementations of one projection and have drifted before.
See [`test/README.md`](test/README.md). The runtime half of the suite is
[`orchestrator/tests/`](../tests/README.md).

## Login (`-c idp=...`)

One context key selects the provider; the API Gateway JWT authorizer and the
`auth-config.js` served to the SPA are derived from it. An inconsistent combination fails
at **synth**.

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
domain by the stack. On **Auth0**, or a **bring-your-own** Cognito client, add the `uiUrl`
output to the application's Allowed Callback URLs, Allowed Logout URLs and Allowed Web
Origins yourself.

`-c idp=none` is **rejected at synth** while `authorization.actions` is non-empty: with no
authorizer there are no claims, so every rule would deny everyone.

### Users and groups

The created pool is **admin-create-only** (self-signup off — the UI sits on a public
CloudFront URL). The stack creates the *groups* `authorization` names; membership is
per-person and not managed in IaC:

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

Skip the group step and you can log in, but the approval controls stay disabled.

## Configuration (context keys)

Set in `cdk.json` or via `-c key=value`:

| Key | Default | Purpose |
|---|---|---|
| `region` | `us-east-1` (or `CDK_DEFAULT_REGION`) | Target region |
| `agentName` | `multiagent_orchestrator` | Resource name prefix + AgentCore runtime name |
| `modelId` | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | Default Bedrock model |
| `memoryEventExpiryDays` | `30` | AgentCore Memory event retention |
| `idp` | `cognito` | `cognito`, `auth0`, or `none` |
| `createCognito` | `false` | With `idp=cognito`, provision the User Pool + SPA client |
| `cognitoUserPoolId` | `""` | Existing Cognito User Pool ID |
| `cognitoClientId` | `""` | Existing Cognito App Client ID (public, for SPA) |
| `cognitoDomainPrefix` | `""` | Existing Cognito Hosted UI domain prefix |
| `auth0Domain` | `""` | Auth0 tenant domain (no scheme, no trailing slash) |
| `auth0ClientId` | `""` | Auth0 SPA application client ID |
| `enableGateway` | `false` | Create the Gateway + Knowledge Base + Cedar policy |
| `gatewayClientId` | `""` | Bring-your-own M2M client id (unneeded with `createCognito=true`) |
| `gatewayAudience` | `""` | OAuth2 scope (Cognito) or API identifier (Auth0) for the M2M token |
| `transactionSearchIndexingPercentage` | `100` | Percentage of spans indexed by Transaction Search |

Your **data sources are not context keys** — they live in the `tools` block of
`../app/workflow.json`, which this stack reads (the same file Terraform reads). Likewise
**who may approve a run**: that is the `authorization` block, and the stack provisions a
Cognito group for each group it names. See [`GETTING_STARTED.md`](../../GETTING_STARTED.md).

**Secrets come from the environment, never context** (`cdk.json` is committed and `-c`
values land in `cdk.out`):

```bash
export GATEWAY_CLIENT_SECRET='<m2m client secret>'
export TOOL_API_KEYS='{"<tool name>":"your-key"}'   # keyed by workflow.json tool name
export A2A_TOKENS='{"<agent id>":"bearer-token"}'   # for runtime "a2a" agents
```

## The tool plane (`-c enableGateway=true`)

With `idp=cognito -c createCognito=true` the resource server and confidential client the
Gateway needs are provisioned too, and the secret is read at deploy time. Any other
combination needs `gatewayClientId`, `gatewayAudience` and `$GATEWAY_CLIENT_SECRET`; synth
fails naming what is missing.

`idp=none` cannot be combined with `enableGateway=true` — the Gateway's `CUSTOM_JWT`
authorizer requires an OIDC provider.

### Targets come from `workflow.json`

Every Gateway target and its Cedar permit is generated from the `tools` block. There is
nothing to configure here. The five this sample ships:

```json
"tools": {
  "kb":        { "type": "kb", "corpora": ["reference"] },
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

(Abridged — the shipped entries also carry `description`, `rowFields`/`rowPath` and a
`toolSchema`. See [`docs/WORKFLOW_REFERENCE.md`](../docs/WORKFLOW_REFERENCE.md).)

A target's key must equal the agent's `tool` label — synth fails if it does not.
`validateTools()` in `lib/orchestrator-stack.ts` enforces the same rules as Terraform's
preconditions: a known `type`, `endpoint` for `mcp`, exactly one of `schemaS3Uri` or
`source` for `openapi`, at most one `kb` and one `websearch`, a non-empty `corpora` for `kb`,
`websearch` only in a supported region, `auth: "apikey"` only on `mcp` or `openapi`, and
valid `listingMode` / `auth` values.

The `docs` default is AWS's managed **Knowledge MCP Server** — nothing is deployed for it
and no credential is needed. Point `endpoint` at your own server to swap it.

For a tool that needs a key, add it to `$TOOL_API_KEYS` keyed by the tool name; it is
vaulted in an AgentCore credential provider and sent as an `X-API-Key` header.

> A target can reach READY and still publish **zero** tools: the Gateway names each tool
> `<targetName>___<toolName>`, so a server whose own tool names already contain `___` gets
> dropped. Verify with a `tools/list` call against the `gatewayUrl` output — if the tools
> are missing, the agent raises `ToolUnavailable` and the fix is on the provider side.

## Tear down

```bash
cdk destroy
```

Transaction Search is deliberately **left enabled** — it is account-wide, and other stacks
may depend on it.

## Notes

- **Layout.** `lib/orchestrator-stack.ts` holds the runtimes, stores, IdP, A2A stand-in,
  BFF/API and UI; `lib/tool-plane.ts` holds the Gateway + Knowledge Base + Cedar policy
  subsystem (the counterpart to Terraform's `tools.tf` + `gateway.tf` + `kb.tf` +
  `policy.tf`). `lib/defaults.ts` and `lib/vocabulary.ts` read `app/defaults.json` and
  `app/vocabulary.json`, so no default or allowed value is restated here.
- **You never write Cedar.** `cedarStatement()` emits the same policy Terraform does: a
  fine-grained permit when a tool sets `policy.tool` (with an optional argument
  restriction), otherwise a target-level permit — the only form that works for a remote MCP
  server whose tool names are unknown at deploy time. Anything undeclared is refused by
  Cedar's default-deny.
- **Tool names are composed by the Gateway.** Every published name is
  `<targetName>___<toolName>`, formed server-side. AWS also namespaces its managed tools as
  `aws___<tool>`, so the `docs` target publishes `docs___aws___search_documentation` —
  hence `"call": "aws___search_documentation"`. Nothing splits on the delimiter; `call` is
  matched against the published name.
- **`tools/list` is paginated.** Follow `nextCursor` when verifying, or a target with tools
  on page two looks empty. This stack has five targets and the remote MCP server alone
  publishes five tools, so the catalogue does not fit on one page.
- **One CDK-only limit, surfaced as a synth error.** `connectorVersion` cannot be expressed
  — CloudFormation's connector source accepts only `connectorId`, so use Terraform for a
  version pin.
- **No stable L2 for any AgentCore resource yet**, so everything here is L1. The Gateway,
  gateway targets, policy engine, policy and credential provider use the generated L1s from
  `aws-cdk-lib/aws-bedrockagentcore` (`CfnGateway`, `CfnGatewayTarget`, …). Memory and
  Runtime are still declared as raw `CfnResource` (`AWS::BedrockAgentCore::Memory`,
  `AWS::BedrockAgentCore::Runtime`) — the same types Terraform's Cloud Control provider
  uses. `CfnMemory` and `CfnRuntime` **do** exist in the pinned `aws-cdk-lib`, so those two
  could move to the typed constructs; they have not, because the swap would rewrite the
  logical ids and replace live runtimes and memory stores for no functional gain.
- **KB ingestion** runs through a custom resource calling `StartIngestionJob`, keyed on a
  hash of `../kb_docs` — editing the corpus re-ingests on the next deploy, and nothing else
  triggers it.
- **IAM propagation.** AgentCore runtime creation occasionally races IAM role propagation
  on a first deploy; if it fails, re-run `cdk deploy`.
- See the repo root README's "Security, cost & scalability" section before any non-sandbox
  use.
