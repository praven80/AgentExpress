# Deployment Guide

The minimum steps to deploy this orchestrator into an AWS account. There are **two
infrastructure-as-code options** — pick one (don't run both against the same
account/region; they use overlapping resource names):

| | **Terraform** (`orchestrator/terraform/`) | **CDK / TypeScript** (`orchestrator/cdk/`) |
|---|---|---|
| Footprint | **Full** — adds the AgentCore **Gateway + Bedrock Knowledge Base** (live MCP + RAG) | **Core** — orchestrator + dedicated runtimes + DynamoDB + BFF/API + UI |
| MCP / RAG | Live (when `enable_gateway = true`) | **Simulated** (no Gateway/KB); LLM inference is still real |
| Guide | This document (below) | [`orchestrator/cdk/README.md`](orchestrator/cdk/README.md) + the quick steps at the end here |

Both build the same ARM64 container image and provision the orchestrator plus one
dedicated AgentCore runtime per `dedicated` agent (the two research agents).

---

# Deploy with Terraform (full — adds Gateway + Knowledge Base)

All commands run from `orchestrator/terraform/`.

## 1. Prerequisites

- **Terraform** ≥ 1.5
- A container engine that builds `linux/arm64` images — **Finch** (default), Docker, or Podman
- **AWS CLI**, configured with credentials for the target account

> **Cognito is flexible.** Three modes:
> 1. **Auto-create** (`create_cognito = true`) — Terraform creates the User Pool, domain,
>    and SPA client for you. Recommended for demos and sandboxes.
> 2. **Bring your own** — pass existing `cognito_*` values in `terraform.tfvars`.
> 3. **No auth** — leave `create_cognito = false` and all `cognito_*` empty. The UI/API
>    deploy **open** (no login). Do **not** use this mode for anything shared.

In the target AWS account/region, enable **Amazon Bedrock model access** for the
model in `model_id` (default **Claude Haiku 4.5**) and **Titan Text Embeddings V2**
(used by the Knowledge Base). Deploys and KB ingestion fail without this.

## 2. IAM permissions to deploy

Attach the ready-made policy to the IAM role/user you deploy with:

```
orchestrator/terraform/deploy-role-policy.json
```

It grants exactly what Terraform needs (ECR, AgentCore, Bedrock KB + S3 Vectors,
DynamoDB, Lambda, API Gateway, S3, CloudFront, IAM execution roles, logs). Confirm
you're in the right account:

```bash
aws sts get-caller-identity
```

## 3. Cognito setup

You have **two options**:

### Option A — Auto-create Cognito (recommended for demos)

Set `create_cognito = true` in `terraform.tfvars`. Terraform will automatically
create a User Pool, Hosted UI domain (prefix: `<agent-name>-<account-id>`), and a
public SPA App Client. No manual setup needed — skip to step 4.

> After deploying, add the `ui_url` output to the Cognito App Client's Allowed
> Callback/Sign-out URLs (step 5).

### Option B — Bring your own Cognito

If you already have a Cognito User Pool (e.g. shared across services), create:

1. **User Pool** → note the `cognito_user_pool_id` (e.g. `us-west-2_XXXXXXXXX`)
2. **Domain** (Cognito Hosted UI domain prefix) → `cognito_domain_prefix`
3. **App Client (public, no secret)** for the SPA → `cognito_user_pool_client_id`
   - Allowed OAuth flows: Authorization code grant
   - Allowed scopes: `openid`, `email`, `profile`
4. **Resource Server** (for M2M scopes) → define a custom scope (e.g. `gateway/invoke`)
5. **App Client (confidential, with secret)** for M2M → `cognito_gateway_client_id`
   - Allowed OAuth flows: Client credentials
   - Allowed scopes: `<resource-server-identifier>/invoke` (the scope you defined)

| Value | Goes to |
|---|---|
| `cognito_user_pool_id` | `terraform.tfvars` |
| `cognito_user_pool_client_id` | `terraform.tfvars` |
| `cognito_domain_prefix` | `terraform.tfvars` |
| `cognito_gateway_client_id` | `terraform.tfvars` |
| `cognito_gateway_scope` | `terraform.tfvars` |
| M2M **Client Secret** | env var `TF_VAR_cognito_gateway_client_secret` (never in a file) |

## 4. Deploy

```bash
cd orchestrator/terraform

cp terraform.tfvars.example terraform.tfvars   # then edit

# Minimal (auto-create Cognito, no Gateway):
#   region         = "us-west-2"
#   create_cognito = true
#   enable_gateway = false

# Full (bring your own Cognito + Gateway):
#   region         = "us-west-2"
#   enable_gateway = true
#   cognito_user_pool_id = "us-west-2_XXXXXXXXX"
#   ...

export TF_VAR_cognito_gateway_client_secret='<cognito m2m client secret>'  # only if enable_gateway = true

terraform init                                 # first time only
terraform apply                                # builds the image + provisions everything
```

The first apply builds/pushes the container image, provisions the **orchestrator
runtime plus one dedicated AgentCore runtime per `dedicated` agent** (the two
research agents), and — when `enable_gateway = true` — runs a KB ingestion job (a
few minutes). Then read the outputs:

```bash
terraform output
# ui_url        -> the app URL (CloudFront)
# api_endpoint  -> HTTP API base
```

## 5. Whitelist the app URL in Cognito

Add `terraform output ui_url` to the **public App Client's** (step 3.3):

- Allowed Callback URLs
- Allowed Sign-out URLs

Login fails with a redirect-mismatch error until this is done.

## 6. Use it

Open `ui_url`, log in (if Cognito is configured), type a request in the topic box, and
start a session. Approve, revise, or deny at each human-review gate. Steps 3 and 5
(Cognito setup and callback-URL whitelisting) only apply when Cognito is configured.

## 7. (Optional) Front your own MCP server or REST API

The `web_research` agent uses the public AWS Knowledge MCP server by default (no
key). To point it at a different MCP server, set `gateway_mcp_endpoint` in
`terraform.tfvars`. To add an extra provider (a second MCP target or a REST/OpenAPI
target, with an API key if needed), follow the inline notes in
`orchestrator/terraform/gateway.tf`. A Gateway target's `name` must equal the
agent's `mcp` label in `orchestrator/app/workflow.json`.

**Tear down:** `terraform destroy` (removes all resources and data). A destroy +
re-apply creates a new `ui_url`, so re-do step 5. Never delete `terraform.tfstate`.

---

# Deploy with CDK (core — no Gateway/KB)

The CDK path provisions the same core footprint as Terraform with
`enable_gateway = false`: the orchestrator + two dedicated runtimes, DynamoDB, the
BFF/API, and the S3 + CloudFront UI. **MCP and RAG run simulated** (no Gateway or
Knowledge Base); LLM inference is real. Full details in
[`orchestrator/cdk/README.md`](orchestrator/cdk/README.md).

## Prerequisites
- Node.js ≥ 20 and the AWS CDK CLI (`npm i -g aws-cdk`, or use the local dev dep)
- A container engine that builds `linux/arm64` — Finch, Docker, or Podman
- AWS credentials; **Bedrock model access** for `modelId` (default Claude Haiku 4.5)
- The IAM permissions to deploy are covered by CDK's bootstrap execution role
  (`AdministratorAccess` by default) — no separate policy file needed

## Deploy
```bash
cd orchestrator/cdk
npm install
export CDK_DOCKER=finch        # only if you don't have Docker
cdk bootstrap                  # first time per account/region
cdk deploy -c createCognito=true   # auto-creates Cognito User Pool + SPA client
```

Outputs include `uiUrl` (CloudFront), `apiEndpoint`, `cognitoUserPoolId`, and
`cognitoClientId`.

### Cognito options

| Mode | Command |
|------|---------|
| **Auto-create** (recommended) | `cdk deploy -c createCognito=true` |
| **Bring your own** | `cdk deploy -c cognitoUserPoolId=us-east-1_XXX -c cognitoClientId=YOUR_ID -c cognitoDomainPrefix=your-app` |
| **No auth** (sandbox only) | `cdk deploy` (no Cognito context values) |

After deploying with Cognito enabled, add the `uiUrl` output to the Cognito App
Client's Allowed Callback/Sign-out URLs:

```bash
aws cognito-idp update-user-pool-client \
  --user-pool-id <cognitoUserPoolId output> \
  --client-id <cognitoClientId output> \
  --callback-urls "https://<uiUrl output>" "https://localhost" \
  --logout-urls "https://<uiUrl output>" "https://localhost" \
  --allowed-o-auth-flows code \
  --allowed-o-auth-scopes openid email profile \
  --supported-identity-providers COGNITO \
  --allowed-o-auth-flows-user-pool-client \
  --explicit-auth-flows ALLOW_USER_SRP_AUTH ALLOW_REFRESH_TOKEN_AUTH
```

**Tear down:** `cdk destroy`.

---

# Deploying both (different accounts or regions)

CDK and Terraform can coexist when targeting **different** AWS accounts or regions.
Resource names incorporate the account ID, so there are no global naming collisions.

Example: CDK → us-east-1 (Account A), Terraform → us-west-2 (Account B):

```bash
# Terminal 1 — CDK
export AWS_REGION=us-east-1 CDK_DEFAULT_REGION=us-east-1 CDK_DOCKER=finch
cd orchestrator/cdk && cdk deploy -c createCognito=true

# Terminal 2 — Terraform (with Account B credentials)
export AWS_REGION=us-west-2
cd orchestrator/terraform && terraform apply
```

Both produce independent CloudFront URLs, each with its own Cognito User Pool.
Update the callback URLs for each after deployment (step 5 above).
