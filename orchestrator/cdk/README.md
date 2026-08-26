# CDK deployment (TypeScript)

An **AWS CDK** alternative to the Terraform config in `../terraform`, for teams that
prefer `cdk deploy`. It provisions the **core** footprint of the sample:

- the **orchestrator** AgentCore Runtime (built from `../Dockerfile`, ARM64),
- one **dedicated** AgentCore Runtime per `dedicated` agent in `../app/workflow.json`
  (the two research agents),
- **DynamoDB** tables (status, events, telemetry + `by_date` GSI),
- **AgentCore Memory** (LangGraph checkpointer),
- the **BFF Lambda** + **HTTP API** (optional **Cognito** JWT authorizer),
- the static **UI** on **S3 + CloudFront** (private bucket via Origin Access Control).

> **Scope vs. Terraform.** This CDK app deploys the same footprint as Terraform with
> `enable_gateway = false`: LLM inference is **real** (Bedrock), while **MCP and RAG
> run simulated** because it does **not** create the AgentCore Gateway or the Bedrock
> Knowledge Base. For live MCP + RAG (Gateway + S3 Vectors KB), use the Terraform
> config in `../terraform`. Pick **one** IaC tool per account/region — don't run both
> against the same resources (they use overlapping names).

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

### With Cognito (optional)

Without Cognito the UI/API deploy **open** (fine for a personal sandbox, not for shared
use). To require login, pass your Cognito User Pool values as context:

```bash
cdk deploy -c cognitoUserPoolId=us-east-1_XXXXXXXXX -c cognitoClientId=YOUR_CLIENT_ID -c cognitoDomainPrefix=your-app
```

Then add the `ui_url` to the Cognito App Client's Allowed Callback/Sign-out URLs.

## Configuration (context keys)

Set in `cdk.json` or via `-c key=value`:

| Key | Default | Purpose |
|---|---|---|
| `agentName` | `multiagent_orchestrator` | Resource name prefix + AgentCore runtime name |
| `modelId` | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | Default Bedrock model |
| `memoryEventExpiryDays` | `30` | AgentCore Memory event retention |
| `cognitoUserPoolId` | `""` | Cognito User Pool ID (empty = no auth) |
| `cognitoClientId` | `""` | Cognito App Client ID (public, for SPA) |
| `cognitoDomainPrefix` | `""` | Cognito Hosted UI domain prefix |
| `enableGateway` | `false` | Reserved; `true` is not implemented in CDK (use Terraform) |

## Tear down

```bash
cdk destroy
```

## Notes

- **No stable L2 for AgentCore yet.** The Memory and Runtime resources are created via
  the underlying CloudFormation resource types (`AWS::BedrockAgentCore::Memory`,
  `AWS::BedrockAgentCore::Runtime`) — the same types Terraform's Cloud Control provider
  uses. If AWS revises those schemas, adjust the property/attribute names in
  `lib/orchestrator-stack.ts`.
- **IAM propagation.** AgentCore runtime creation occasionally races IAM role
  propagation on a first deploy; if it fails, re-run `cdk deploy`.
- See the repo root **README** "Security, cost & scalability" section before any
  non-sandbox use.
