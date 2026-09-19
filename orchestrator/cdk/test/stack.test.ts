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

import { a2aLambdaAgents, buildBffWorkflow, OrchestratorStack } from "../lib/orchestrator-stack";

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
    a2aTokens: {},
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

describe("a remote (a2a) agent", () => {
  // `runtime: "a2a"` is an agent this deployment does NOT operate. The thing to prove
  // at the stack level is a negative: it must provision no compute, no IAM and no log
  // group, because there is nothing of ours to run. A dedicated runtime accidentally
  // created for it would be a container that boots, finds no module under
  // app/subagents/<id>/, and crash-loops — while the orchestrator happily called the
  // partner's URL and the run succeeded, so nothing would point at the waste.
  const withRemote = {
    ...shipped,
    agents: {
      ...shipped.agents,
      partner_check: {
        name: "Partner Check",
        runtime: "a2a",
        agentCard: "https://agents.partner.example/check",
        produces: "partner-assessment",
      },
    },
    steps: [...shipped.steps, { agent: "partner_check" }],
  };

  let remoteTemplate: Template;
  beforeAll(() => {
    remoteTemplate = synth({ workflow: withRemote, a2aTokens: {} });
  });

  it("provisions no AgentCore Runtime of its own", () => {
    const runtimes = Object.values<any>(
      remoteTemplate.findResources("AWS::BedrockAgentCore::Runtime")
    ).map((r) => r.Properties.AgentRuntimeName);
    // One per `dedicated` agent, plus the orchestrator. Never one for a2a.
    const dedicated = Object.entries<any>(withRemote.agents)
      .filter(([, a]) => (a.runtime ?? "main") === "dedicated")
      .map(([id]) => id);
    expect(runtimes).toHaveLength(dedicated.length + 1);
    expect(runtimes.join(" ")).not.toContain("partner_check");
  });

  it("still reaches the UI, labelled by its Agent Card host", () => {
    const projected = buildBffWorkflow(withRemote).agents.partner_check;
    expect(projected.runtime).toBe("a2a");
    expect(projected.source).toBe("A2A \u00b7 agents.partner.example");
  });

  it("ships an A2A_TOKENS env var to the orchestrator", () => {
    const orchestrator = Object.values<any>(
      remoteTemplate.findResources("AWS::BedrockAgentCore::Runtime")
    ).find((r) => r.Properties.AgentRuntimeName === "multiagent_orchestrator");
    expect(orchestrator.Properties.EnvironmentVariables).toHaveProperty("A2A_TOKENS");
  });

  it("refuses to synth when a bearer agent's token was not supplied", () => {
    // The alternative is a 401 from a service you do not control, which is a far
    // harder failure to read than a synth error naming the agent.
    const bearer = {
      ...withRemote,
      agents: {
        ...withRemote.agents,
        partner_check: { ...withRemote.agents.partner_check, auth: "bearer" },
      },
    };
    expect(() => synth({ workflow: bearer, a2aTokens: {} })).toThrow(
      /declare auth "bearer" but no token was supplied for them: partner_check/
    );
    expect(() => synth({ workflow: bearer, a2aTokens: { partner_check: "t0k" } })).not.toThrow();
  });
});

describe("the stand-in A2A agent", () => {
  // The shipped workflow points two agents at it via `source`, so the default template
  // must contain it. These assert the security posture and the wiring, because both are
  // easy to get subtly wrong: a Function URL defaults to being PUBLIC, and an endpoint
  // map that is absent or misassembled fails only at run time, on the partner's side of
  // a boundary where the error is hardest to read.

  it("deploys one Lambda behind an IAM-authed Function URL", () => {
    const names = Object.values<any>(template.findResources("AWS::Lambda::Function"))
      .map((f) => f.Properties.FunctionName)
      .filter(Boolean);
    expect(names).toContain("A2AAgent-multiagent_orchestrator");

    const urls = Object.values<any>(template.findResources("AWS::Lambda::Url"));
    expect(urls).toHaveLength(1);
    // NEVER "NONE". The client's whole `auth: "sigv4"` mode exists so this endpoint can
    // require a signature instead of a shared token.
    expect(urls[0].Properties.AuthType).toBe("AWS_IAM");
  });

  it("gives the orchestrator the only permission to call it", () => {
    expect(JSON.stringify(template.findResources("AWS::IAM::Policy")))
      .toContain("lambda:InvokeFunctionUrl");
  });

  it("injects one endpoint per remote agent, with the skill as a path segment", () => {
    const orchestrator = Object.values<any>(
      template.findResources("AWS::BedrockAgentCore::Runtime")
    ).find((r) => r.Properties.AgentRuntimeName === "multiagent_orchestrator");
    // A CloudFormation token, so assert on the assembled pieces rather than a string.
    const endpoints = JSON.stringify(orchestrator.Properties.EnvironmentVariables.A2A_ENDPOINTS);
    for (const [id, skill] of Object.entries(a2aLambdaAgents(shipped.agents))) {
      expect(endpoints).toContain(id);
      // A path, not `?skill=`: a client appends /.well-known/agent-card.json to this.
      expect(endpoints).toContain(`/${skill}`);
    }
    expect(endpoints).not.toContain("?skill=");
  });

  it("gives the remote agents no runtime of their own", () => {
    // They are somebody else's service. A runtime created for one would be a container
    // that boots, finds no module under app/subagents/, and crash-loops — while the run
    // succeeded, so nothing would point at the waste.
    const runtimes = Object.values<any>(
      template.findResources("AWS::BedrockAgentCore::Runtime")
    ).map((r) => r.Properties.AgentRuntimeName);
    for (const id of Object.keys(a2aLambdaAgents(shipped.agents))) {
      expect(runtimes.join(" ")).not.toContain(id);
    }
  });

  it("is not deployed at all when no agent asks for it", () => {
    // Point the agents at a real partner and none of this infrastructure exists — the
    // same deal a `tools` entry gets from lambdaArn vs source.
    const external = {
      ...shipped,
      agents: Object.fromEntries(
        Object.entries<any>(shipped.agents).map(([id, a]) =>
          a.source === "a2a_lambda"
            ? [id, { ...a, source: undefined, skill: undefined,
                     agentCard: "https://agents.partner.example/x" }]
            : [id, a]
        )
      ),
    };
    const t = synth({ workflow: JSON.parse(JSON.stringify(external)) });
    expect(Object.values<any>(t.findResources("AWS::Lambda::Url"))).toHaveLength(0);
    const names = Object.values<any>(t.findResources("AWS::Lambda::Function"))
      .map((f) => f.Properties.FunctionName)
      .filter(Boolean);
    expect(names).not.toContain("A2AAgent-multiagent_orchestrator");
  });
});
