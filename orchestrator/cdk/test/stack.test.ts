/**
 * Synthesized-template assertions.
 *
 * The unit tests in config-plane.test.ts check the projections; this checks that
 * what they produce actually reaches CloudFormation. It synthesizes the real stack
 * against the real `workflow.json`, so it also covers the construct code that has
 * no testable pure function behind it (the Cognito groups, the route wiring, the
 * env var the BFF reads).
 *
 * No credentials and no container builder are needed. The account and region are
 * pinned below (so nothing is an unresolved token), and `Template.fromStack` renders
 * the template without staging the cloud assembly — which is the step that would
 * bundle the Docker image. Verified by running this file with
 * `CDK_DOCKER=/nonexistent/builder`.
 */

import * as cdk from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import * as path from "path";

import { OrchestratorStack } from "../lib/orchestrator-stack";

const ORCH_ROOT = path.join(__dirname, "..", "..");
const shipped = require(`${ORCH_ROOT}/app/workflow.json`);

function synth(overrides: Record<string, any> = {}) {
  const app = new cdk.App();
  const stack = new OrchestratorStack(app, "TestStack", {
    env: { account: "123456789012", region: "us-east-1" },
    agentName: "multiagent_orchestrator",
    modelId: "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    memoryEventExpiryDays: 30,
    idp: "cognito",
    createCognito: true,
    cognitoUserPoolId: "",
    cognitoClientId: "",
    cognitoDomainPrefix: "",
    auth0Domain: "",
    auth0ClientId: "",
    enableGateway: true,
    gatewayClientId: "",
    gatewayClientSecret: "dummy",
    gatewayAudience: "",
    toolApiKeys: {},
    transactionSearchIndexingPercentage: 100,
    ...overrides,
  });
  return Template.fromStack(stack);
}

// Synthesizing is the slow part, so do it once and assert many things.
let template: Template;
beforeAll(() => {
  template = synth();
});

describe("Cognito", () => {
  it("creates one group per group named in authorization.actions", () => {
    const groups = template.findResources("AWS::Cognito::UserPoolGroup");
    const names = Object.values(groups)
      .map((g: any) => g.Properties.GroupName)
      .sort();
    const expected = [
      ...new Set(Object.values<string[]>(shipped.authorization.actions).flat()),
    ].sort();
    expect(names).toEqual(expected);
  });

  it("describes each group with the actions it grants", () => {
    // So an operator reading the Cognito console can see what a group is for
    // without opening workflow.json.
    const groups = template.findResources("AWS::Cognito::UserPoolGroup");
    const byName: Record<string, string> = {};
    for (const g of Object.values<any>(groups)) {
      byName[g.Properties.GroupName] = g.Properties.Description;
    }
    for (const [action, allowed] of Object.entries<string[]>(shipped.authorization.actions)) {
      for (const group of allowed) {
        expect(byName[group]).toContain(action);
      }
    }
  });

  it("disables self-signup", () => {
    // The UI sits on a public CloudFront URL. This was `true`, diverging from the
    // Terraform path and leaving open registration on a public endpoint.
    template.hasResourceProperties("AWS::Cognito::UserPool", {
      AdminCreateUserConfig: { AllowAdminCreateUserOnly: true },
    });
  });
});

describe("HTTP API", () => {
  it("exposes /api/me", () => {
    template.hasResourceProperties("AWS::ApiGatewayV2::Route", {
      RouteKey: "GET /api/me",
    });
  });

  it("puts the JWT authorizer on every /api route", () => {
    // A route deployed without an authorizer is open to the internet. Every one of
    // them must carry the authorizer, not just most.
    const routes = template.findResources("AWS::ApiGatewayV2::Route");
    const apiRoutes = Object.values<any>(routes).filter((r) =>
      String(r.Properties.RouteKey).includes("/api/")
    );
    expect(apiRoutes.length).toBeGreaterThan(10);
    for (const r of apiRoutes) {
      expect(r.Properties.AuthorizationType).toBe("JWT");
      expect(r.Properties.AuthorizerId).toBeDefined();
    }
  });

  it("wires every mutating route the RBAC guards cover", () => {
    const keys = Object.values<any>(template.findResources("AWS::ApiGatewayV2::Route")).map(
      (r) => r.Properties.RouteKey
    );
    for (const k of [
      "POST /api/sessions/{id}/decision",
      "POST /api/sessions/{id}/rerun",
      "POST /api/sessions/{id}/cancel",
      "POST /api/sessions/{id}/evaluate",
      "POST /api/insights/run",
      "DELETE /api/sessions/{id}",
    ]) {
      expect(keys).toContain(k);
    }
  });
});

describe("the BFF Lambda environment", () => {
  function bffEnv(): Record<string, string> {
    const fns = Object.values<any>(template.findResources("AWS::Lambda::Function")).filter(
      (f) => f.Properties?.Environment?.Variables?.WORKFLOW_JSON
    );
    expect(fns).toHaveLength(1);
    return fns[0].Properties.Environment.Variables;
  }

  it("carries the authorization rules", () => {
    // Without this the BFF's authz module reads an empty block and treats every
    // action as unrestricted — RBAC configured and silently not enforced.
    const wf = JSON.parse(bffEnv().WORKFLOW_JSON);
    expect(wf.authorization).toEqual({
      groupsClaim: shipped.authorization.groupsClaim,
      actions: shipped.authorization.actions,
    });
  });

  it("carries the agents, steps, evalAgents, chatbot and ui projections", () => {
    const wf = JSON.parse(bffEnv().WORKFLOW_JSON);
    expect(Object.keys(wf.agents)).toEqual(Object.keys(shipped.agents));
    expect(wf.steps).toEqual(shipped.steps);
    expect(wf.evalAgents.length).toBeGreaterThan(0);
    expect(wf.chatbot.enabled).toBe(true);
    expect(wf.ui.title).toBe(shipped.ui.title);
  });

  it("keeps the whole environment inside Lambda's 4 KB limit", () => {
    const env = bffEnv();
    const bytes = Object.entries(env).reduce((n, [k, v]) => n + k.length + String(v).length, 0);
    expect(bytes).toBeLessThan(4096);
  });
});

describe("the tool plane", () => {
  it("creates one Gateway target per tools entry", () => {
    const targets = Object.values<any>(
      template.findResources("AWS::BedrockAgentCore::GatewayTarget")
    ).map((t) => t.Properties.Name);
    expect(targets.sort()).toEqual(Object.keys(shipped.tools).sort());
  });

  /**
   * The Cedar statements, flattened to text.
   *
   * Each one is an `Fn::Join` rather than a literal, because the gateway ARN is a
   * `Fn::GetAtt` resolved at deploy time — so the fragments have to be concatenated
   * before the policy text can be read.
   */
  function cedarStatements(): string[] {
    return Object.values<any>(template.findResources("AWS::BedrockAgentCore::Policy")).map((p) => {
      const s = p.Properties.Definition.Cedar.Statement;
      if (typeof s === "string") return s;
      return (s["Fn::Join"][1] as any[])
        .map((part) => (typeof part === "string" ? part : "<resolved-at-deploy>"))
        .join("");
    });
  }

  it("emits one Cedar permit per declared tool, attached to a policy engine", () => {
    template.resourceCountIs("AWS::BedrockAgentCore::PolicyEngine", 1);
    const statements = cedarStatements();
    expect(statements).toHaveLength(Object.keys(shipped.tools).length);
    for (const name of Object.keys(shipped.tools)) {
      // Every declared tool is permitted, by name or as a target action group.
      // Anything NOT declared is denied by Cedar's default-deny.
      expect(statements.some((s) => s.includes(`AgentCore::Action::"${name}`))).toBe(true);
    }
  });

  it("permits web search BY NAME and the MCP target at target level", () => {
    // Not cosmetic. Measured on a live gateway: a target-level permit did NOT
    // authorize the connector's tool — `action in AgentCore::Action::"websearch"`
    // produced ToolDenied for websearch___WebSearch. A remote MCP server's tool
    // names are unknown at deploy time, so that one has to stay target-level.
    const statements = cedarStatements();
    expect(
      statements.some((s) => s.includes('action == AgentCore::Action::"websearch___WebSearch"'))
    ).toBe(true);
    expect(statements.some((s) => s.includes('action in AgentCore::Action::"docs"'))).toBe(true);
    expect(statements.some((s) => s.includes('action == AgentCore::Action::"kb___retrieve"'))).toBe(
      true
    );
  });

  it("carries the kb corpus restriction into the permit", () => {
    const kb = cedarStatements().find((s) => s.includes("kb___retrieve"))!;
    expect(kb).toBeDefined();
    for (const corpus of shipped.tools.kb.corpora) expect(kb).toContain(corpus);
    expect(kb).toContain("context.input has filter");
  });

  it("scopes every permit to this gateway", () => {
    for (const s of cedarStatements()) {
      expect(s).toContain("resource == AgentCore::Gateway::");
    }
  });

  it("sets the MCP target's listingMode from config", () => {
    const docs = Object.values<any>(
      template.findResources("AWS::BedrockAgentCore::GatewayTarget")
    ).find((t) => t.Properties.Name === "docs");
    expect(JSON.stringify(docs)).toContain(shipped.tools.docs.listingMode);
  });
});

describe("idp=none", () => {
  it("is rejected while authorization.actions is non-empty", () => {
    // Belt and braces: config-plane.test.ts asserts the validator, this asserts the
    // stack actually calls it.
    expect(() => synth({ idp: "none", createCognito: false, enableGateway: false })).toThrow(
      /idp = "none" deploys the API with no authorizer/
    );
  });
});
