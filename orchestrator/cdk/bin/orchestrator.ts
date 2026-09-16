#!/usr/bin/env node
import * as cdk from "aws-cdk-lib";
import { OrchestratorStack } from "../lib/orchestrator-stack";

const app = new cdk.App();

// Config resolution order: `-c key=value` on the CLI overrides cdk.json context.
const ctx = (k: string, d?: string) => (app.node.tryGetContext(k) ?? d) as string;

const agentName = ctx("agentName", "multiagent_orchestrator");
const region = process.env.CDK_DEFAULT_REGION || ctx("region", "us-east-1");

const idp = ctx("idp", "cognito");
if (!["cognito", "auth0", "none"].includes(idp)) {
  throw new Error(`idp must be one of "cognito", "auth0", "none" (got "${idp}")`);
}

/** Parse $TOOL_API_KEYS (a JSON object of toolName -> key). */
function parseToolApiKeys(raw?: string): Record<string, string> {
  if (!raw) return {};
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new Error('TOOL_API_KEYS must be a JSON object, e.g. \'{"billing":"sk-live-..."}\'.');
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error("TOOL_API_KEYS must be a JSON object keyed by the workflow.json tool name.");
  }
  return parsed as Record<string, string>;
}

new OrchestratorStack(app, `${agentName.replace(/_/g, "-")}-stack`, {
  // A concrete account/region is required for the AgentCore + CloudFront resources
  // and for the Docker image asset. Falls back to us-east-1 for `cdk synth` without creds.
  env: { account: process.env.CDK_DEFAULT_ACCOUNT, region },
  description: "Multi-agent LangGraph orchestrator on Amazon Bedrock AgentCore (CDK)",
  agentName,
  modelId: ctx("modelId", "us.anthropic.claude-haiku-4-5-20251001-v1:0"),
  memoryEventExpiryDays: Number(ctx("memoryEventExpiryDays", "30")),
  // --- Identity provider: one switch, same semantics as Terraform's `idp` ---
  //   -c idp=cognito   (default) Cognito; add -c createCognito=true to have CDK
  //                    provision the pool, or pass the cognito* ids below.
  //   -c idp=auth0     existing Auth0 tenant: -c auth0Domain=... -c auth0ClientId=...
  //   -c idp=none      NO authentication — the UI and /api/* deploy open.
  idp: idp,
  cognitoUserPoolId: ctx("cognitoUserPoolId", ""),
  cognitoClientId: ctx("cognitoClientId", ""),
  cognitoDomainPrefix: ctx("cognitoDomainPrefix", ""),
  auth0Domain: ctx("auth0Domain", ""),
  auth0ClientId: ctx("auth0ClientId", ""),
  // Only meaningful for idp=cognito.
  createCognito: idp === "cognito" && String(app.node.tryGetContext("createCognito")) === "true",

  // --- Tool plane: AgentCore Gateway + Knowledge Base + Cedar policy --------
  // Same switch as Terraform's `enable_gateway`. true = live MCP + RAG + policy.
  // false provisions no tool plane, so any agent with a `tool` fails loudly with
  // ToolUnavailable — nothing is simulated. Only tool-less agents can run.
  enableGateway: String(app.node.tryGetContext("enableGateway")) === "true",
  // Bring-your-own M2M client (not needed for idp=cognito + createCognito=true,
  // where CDK creates the resource server + confidential client itself).
  gatewayClientId: ctx("gatewayClientId", ""),
  gatewayAudience: ctx("gatewayAudience", ""),
  // Secret comes from the ENVIRONMENT, never a context key: `cdk.context.json`
  // is committed, and -c values land in cdk.out. Mirrors TF_VAR_gateway_client_secret.
  gatewayClientSecret: process.env.GATEWAY_CLIENT_SECRET ?? "",
  // API keys for tools that need one, keyed by the workflow.json tool name.
  // From the ENVIRONMENT, never context: cdk.json is committed and -c values
  // land in cdk.out. Mirrors TF_VAR_tool_api_keys.
  //   export TOOL_API_KEYS='{"billing":"sk-live-..."}'
  toolApiKeys: parseToolApiKeys(process.env.TOOL_API_KEYS),

  transactionSearchIndexingPercentage: Number(ctx("transactionSearchIndexingPercentage", "100")),
});

app.synth();
