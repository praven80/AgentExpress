#!/usr/bin/env node
import * as cdk from "aws-cdk-lib";
import { OrchestratorStack } from "../lib/orchestrator-stack";

const app = new cdk.App();

// Config resolution order: `-c key=value` on the CLI overrides cdk.json context.
const ctx = (k: string, d?: string) => (app.node.tryGetContext(k) ?? d) as string;

const agentName = ctx("agentName", "multiagent_orchestrator");
const region = process.env.CDK_DEFAULT_REGION || ctx("region", "us-east-1");

new OrchestratorStack(app, `${agentName.replace(/_/g, "-")}-stack`, {
  // A concrete account/region is required for the AgentCore + CloudFront resources
  // and for the Docker image asset. Falls back to us-east-1 for `cdk synth` without creds.
  env: { account: process.env.CDK_DEFAULT_ACCOUNT, region },
  description: "Multi-agent LangGraph orchestrator on Amazon Bedrock AgentCore (CDK)",
  agentName,
  modelId: ctx("modelId", "us.anthropic.claude-haiku-4-5-20251001-v1:0"),
  memoryEventExpiryDays: Number(ctx("memoryEventExpiryDays", "30")),
  cognitoUserPoolId: ctx("cognitoUserPoolId", ""),
  cognitoClientId: ctx("cognitoClientId", ""),
  cognitoDomainPrefix: ctx("cognitoDomainPrefix", ""),
  enableGateway: String(app.node.tryGetContext("enableGateway")) === "true",
  createCognito: String(app.node.tryGetContext("createCognito")) === "true",
});

app.synth();
