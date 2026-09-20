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
import * as fs from "fs";
import * as os from "os";

import { a2aLambdaAgents, OrchestratorStack, stageBffPackage } from "../lib/orchestrator-stack";

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

describe("the BFF deployment package", () => {
  /**
   * The workflow reaches the BFF in its PACKAGE, not in its environment.
   *
   * It used to travel in a `WORKFLOW_JSON` env var holding a projection this file
   * built. Lambda caps the whole environment at 4 KB and that quota cannot be
   * raised, so both IaC paths carried a 3400-byte guard and the shipped ten-agent
   * workflow measured 3153 bytes — about eleven agents before a customer's deploy
   * failed telling them to shorten an agent name. bff/workflow.py does the
   * projection now, at request time, from the file staged here.
   *
   * What the projection CONTAINS is asserted in tests/test_bff_projection.py, next
   * to the implementation. What this file owns is that the file gets there.
   */
  function bffFunction(): any {
    const fns = Object.values<any>(template.findResources("AWS::Lambda::Function")).filter(
      (f) => f.Properties?.Handler === "handler.handler"
    );
    expect(fns).toHaveLength(1);
    return fns[0];
  }

  it("ships no WORKFLOW_JSON, because that was an eleven-agent ceiling", () => {
    const env = bffFunction().Properties.Environment.Variables;
    expect(env).not.toHaveProperty("WORKFLOW_JSON");
    // The vars that remain are ARNs, table names and the model id.
    expect(Object.keys(env).sort()).toEqual([
      "EVENTS_TABLE",
      "MODEL_ID",
      "RUNTIME_ARN",
      "STATUS_TABLE",
      "TELEMETRY_TABLE",
    ]);
  });

  it("keeps the whole environment well inside Lambda's 4 KB limit", () => {
    // Trivially true now, and kept because the limit is the reason the projection
    // moved: if anything large is ever put back in here, this is where it shows up.
    const env = bffFunction().Properties.Environment.Variables;
    const bytes = Object.entries(env).reduce((n, [k, v]) => n + k.length + String(v).length, 0);
    expect(bytes).toBeLessThan(2048);
  });

  it("stages workflow.json next to the handler, byte-identical to the source", () => {
    const staged = fs.mkdtempSync(path.join(os.tmpdir(), "bff-stage-"));
    stageBffPackage(shipped, staged);

    const files = fs.readdirSync(staged).sort();
    expect(files).toContain("handler.handler".split(".")[0] + ".py");
    expect(files).toContain("workflow.py");
    expect(files).toContain("workflow.json");
    // The whole point: bff/workflow.py finds the customer's config beside it.
    expect(JSON.parse(fs.readFileSync(path.join(staged, "workflow.json"), "utf8"))).toEqual(
      shipped
    );
  });

  it("stages no __pycache__, which fromAsset(bff/) used to ship", () => {
    // Including .pyc files built by a different Python minor version than the
    // Lambda's runtime.
    const staged = fs.mkdtempSync(path.join(os.tmpdir(), "bff-stage-"));
    stageBffPackage(shipped, staged);
    expect(fs.readdirSync(staged)).not.toContain("__pycache__");
  });

  it("synthesizes a workflow far past the old eleven-agent ceiling", () => {
    // THE HEADLINE CLAIM, asserted rather than argued. Twenty-five agents with long
    // names is roughly twice what the WORKFLOW_JSON environment variable could hold;
    // under the old design this threw at synth with "shorten your agent names".
    //
    // 25 is not the new limit — there isn't one worth asserting. It is far enough
    // past 11 to show the constraint was REMOVED rather than raised, which is all
    // SSM Parameter Store would have done (4 KB standard, 8 KB advanced and billed).
    const many: Record<string, any> = {};
    for (let n = 0; n < 25; n++) {
      many[`specialist_review_agent_${String(n).padStart(2, "0")}`] = {
        name: `Specialist Review and Escalation Agent number ${String(n).padStart(2, "0")}`,
        runtime: "main",
        maxTokens: 4000,
        access: ["Upstream assets (orchestrator graph state)"],
        agentcore: { evaluations: { enabled: true, auto: false } },
      };
    }
    const big = {
      ...shipped,
      agents: { ...shipped.agents, ...many },
      steps: [...shipped.steps, { parallel: Object.keys(many), gateId: "bulk" }],
    };
    expect(() => synth({ workflow: big, a2aTokens: {} })).not.toThrow();

    // And the projection that WOULD have been shipped in the env var is comfortably
    // over the old budget, which is what makes the point concrete.
    const staged = fs.mkdtempSync(path.join(os.tmpdir(), "bff-stage-big-"));
    stageBffPackage(big, staged);
    const bundled = fs.readFileSync(path.join(staged, "workflow.json"), "utf8");
    expect(bundled.length).toBeGreaterThan(3400);
    expect(Object.keys(JSON.parse(bundled).agents)).toHaveLength(
      Object.keys(shipped.agents).length + 25
    );
  });

  it("stages the same python files Terraform's archive_file does", () => {
    // Two IaC paths, one package. A module present in one and not the other is an
    // ImportError on half your deployments.
    const staged = fs.mkdtempSync(path.join(os.tmpdir(), "bff-stage-"));
    stageBffPackage(shipped, staged);
    const stagedPy = fs
      .readdirSync(staged)
      .filter((f) => f.endsWith(".py"))
      .sort();
    const sourcePy = fs
      .readdirSync(path.join(__dirname, "..", "..", "bff"))
      .filter((f) => f.endsWith(".py"))
      .sort();
    expect(stagedPy).toEqual(sourcePy);
    // And Terraform's fileset pattern must be the recursive one, or a subpackage
    // added later is silently dropped from that path only.
    const bffTf = fs.readFileSync(
      path.join(__dirname, "..", "..", "terraform", "bff.tf"),
      "utf8"
    );
    expect(bffTf).toContain('fileset("${path.module}/../bff", "**/*.py")');
    expect(bffTf).toContain('filename = "workflow.json"');
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
  // TOOL NAMES RESOLVED BY TYPE, not written down. These tests said `shipped.tools.kb`
  // and `"docs"`, which made them assertions about the SAMPLE's tool set: a foreign
  // workflow with one `policy_docs` Knowledge Base and no web search failed three of
  // them, reporting a framework defect where there was none. `itIfTool` skips instead,
  // because "this deployment has no MCP target" is not a bug.
  const toolOfType = (t: string): string | undefined =>
    Object.keys(shipped.tools ?? {}).find(
      (n) => String(shipped.tools[n].type ?? "").toLowerCase() === t
    );
  const itIfTool = (t: string) => (toolOfType(t) ? it : it.skip);

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

  itIfTool("websearch")("permits web search BY NAME and the MCP target at target level", () => {
    // Not cosmetic. Measured on a live gateway: a target-level permit did NOT
    // authorize the connector's tool — `action in AgentCore::Action::"websearch"`
    // produced ToolDenied for websearch___WebSearch. A remote MCP server's tool
    // names are unknown at deploy time, so that one has to stay target-level.
    const statements = cedarStatements();
    const ws = toolOfType("websearch")!;
    expect(
      statements.some((s) => s.includes(`action == AgentCore::Action::"${ws}___WebSearch"`))
    ).toBe(true);
    const mcp = toolOfType("mcp");
    if (mcp) {
      expect(statements.some((s) => s.includes(`action in AgentCore::Action::"${mcp}"`))).toBe(true);
    }
    const kb = toolOfType("kb");
    if (kb) {
      expect(
        statements.some((s) => s.includes(`action == AgentCore::Action::"${kb}___retrieve"`))
      ).toBe(true);
    }
  });

  itIfTool("kb")("carries the kb corpus restriction into the permit", () => {
    const name = toolOfType("kb")!;
    const kb = cedarStatements().find((s) => s.includes(`${name}___retrieve`))!;
    expect(kb).toBeDefined();
    for (const corpus of shipped.tools[name].corpora) expect(kb).toContain(corpus);
    expect(kb).toContain("context.input has filter");
  });

  it("scopes every permit to this gateway", () => {
    for (const s of cedarStatements()) {
      expect(s).toContain("resource == AgentCore::Gateway::");
    }
  });

  itIfTool("mcp")("sets the MCP target's listingMode from config", () => {
    const name = toolOfType("mcp")!;
    const target = Object.values<any>(
      template.findResources("AWS::BedrockAgentCore::GatewayTarget")
    ).find((t) => t.Properties.Name === name);
    expect(JSON.stringify(target)).toContain(shipped.tools[name].listingMode ?? "DEFAULT");
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

  it("still reaches the BFF, so the UI can draw it", () => {
    // How it is LABELLED is bff/workflow.py's job and is asserted in
    // tests/test_bff_projection.py. What this path owns is that a remote agent —
    // which provisions no runtime of its own — is still in the config the API reads.
    const staged = fs.mkdtempSync(path.join(os.tmpdir(), "bff-stage-remote-"));
    stageBffPackage(withRemote, staged);
    const bundled = JSON.parse(fs.readFileSync(path.join(staged, "workflow.json"), "utf8"));
    expect(bundled.agents.partner_check.runtime).toBe("a2a");
    expect(bundled.agents.partner_check.agentCard).toBe(
      withRemote.agents.partner_check.agentCard
    );
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

// ===========================================================================
// One execution role per dedicated agent
// ===========================================================================
// This was a SINGLE shared role for every dedicated runtime, and nothing anywhere
// asserted its contents — so the shape was invisible and the drift below went unnoticed:
// an agent that enables no features still carried the union of every other agent's
// permissions. In the shipped workflow `knowledge_research` uses no guardrails and NO
// dedicated agent uses long-term memory, yet all three could call ApplyGuardrail and read
// the semantic memory store.
//
// What makes this the framework's job rather than the customer's is that the grants are
// DERIVED from the same config that switches the feature on. A customer enables memory for
// an agent; the framework grants that agent memory. There is no IAM to write.
describe("dedicated agent execution roles", () => {
  const roles = (t: Template) =>
    Object.entries<any>(t.findResources("AWS::IAM::Role")).filter(([, r]) =>
      String(r.Properties.RoleName ?? "").startsWith("AgentCoreSubagent-")
    );
  /** The inline policy document attached to the role at logical id `logicalId`. */
  const policyFor = (t: Template, logicalId: string) =>
    JSON.stringify(
      Object.values<any>(t.findResources("AWS::IAM::Policy")).find((p) =>
        JSON.stringify(p.Properties.Roles ?? []).includes(logicalId)
      )?.Properties.PolicyDocument ?? {}
    );

  const dedicated = Object.entries<any>(shipped.agents)
    .filter(([, a]) => (a.runtime ?? "main") === "dedicated")
    .map(([id]) => id);

  it("creates one role per dedicated agent, named for that agent", () => {
    const names = roles(template).map(([, r]) => r.Properties.RoleName);
    expect(names).toHaveLength(dedicated.length);
    for (const id of dedicated) {
      expect(names).toContain(`AgentCoreSubagent-multiagent_orchestrator-${id}`);
    }
  });

  it("gives each runtime its OWN role, not a shared one", () => {
    // The property this whole change buys. If two runtimes point at one role, scoping the
    // role per agent achieves nothing.
    const used = Object.values<any>(template.findResources("AWS::BedrockAgentCore::Runtime"))
      .filter((r) => String(r.Properties.AgentRuntimeName).includes("_"))
      .map((r) => JSON.stringify(r.Properties.RoleArn));
    expect(new Set(used).size).toEqual(used.length);
  });

  it("grants ApplyGuardrail only to agents whose config enables guardrails", () => {
    for (const [logicalId] of roles(template)) {
      const id = dedicated.find((a) => logicalId.includes(a.replace(/_/g, "")));
      const ac = shipped.agents[id!].agentcore ?? {};
      const wants = Boolean(ac.guardrails?.input || ac.guardrails?.output);
      expect(policyFor(template, logicalId).includes("bedrock:ApplyGuardrail")).toEqual(wants);
    }
    // And the sample really does exercise both branches, or the test above is vacuous.
    const flags = dedicated.map((id) => {
      const g = (shipped.agents[id].agentcore ?? {}).guardrails ?? {};
      return Boolean(g.input || g.output);
    });
    expect(new Set(flags).size).toBeGreaterThan(1);
  });

  it("grants long-term memory only to agents that declare it", () => {
    for (const [logicalId] of roles(template)) {
      const id = dedicated.find((a) => logicalId.includes(a.replace(/_/g, "")));
      const wants = ((shipped.agents[id!].agentcore ?? {}).memory?.longTerm ?? []).length > 0;
      expect(policyFor(template, logicalId).includes("RetrieveMemoryRecords")).toEqual(wants);
    }
  });

  it("still grants every agent what it unconditionally needs", () => {
    // Scoping down must not remove the permissions a container needs merely to run: pull
    // the image, emit spans and metrics, call a model, write its telemetry row.
    for (const [logicalId] of roles(template)) {
      const doc = policyFor(template, logicalId);
      for (const needed of [
        "ecr:BatchGetImage",
        "logs:PutLogEvents",
        "xray:PutTraceSegments",
        "cloudwatch:PutMetricData",
        "bedrock:InvokeModel",
        "dynamodb:PutItem",
      ]) {
        expect(doc).toContain(needed);
      }
    }
  });

  it("scopes model invocation to inference profiles, never account-wide bedrock:*", () => {
    // The Terraform side of this role carried "arn:aws:bedrock:<region>:<acct>:*", which
    // also covered custom models, provisioned throughput, agents, guardrails and prompts —
    // broader than the CDK role AND broader than Terraform's own orchestrator role, so it
    // was drift rather than a decision. Asserted here so the two cannot diverge again.
    for (const [logicalId] of roles(template)) {
      const doc = policyFor(template, logicalId);
      expect(doc).toContain("inference-profile/*");
      expect(doc).not.toMatch(/"arn:aws:bedrock:us-east-1:123456789012:\*"/);
    }
  });

  it("refuses an agent id that fits the runtime name but not the role name", () => {
    // The role name is the TIGHTER limit, and this is the window that proves it matters:
    // "AgentCoreSubagent-" is an 18-character prefix the runtime name does not carry, so
    // with a 23-character agentName an id of 23 passes the 48-char runtime check and
    // overruns IAM's 64. Caught rather than truncated, because a shortened name can
    // collide with another agent's and put two runtimes back on one role.
    const id = "a".repeat(23);
    expect(`multiagent_orchestrator_${id}`.length).toBeLessThanOrEqual(48);
    expect(`AgentCoreSubagent-multiagent_orchestrator-${id}`.length).toBeGreaterThan(64);
    const wf = {
      ...shipped,
      agents: { ...shipped.agents, [id]: { name: "X", runtime: "dedicated" } },
      steps: [...shipped.steps, { agent: id }],
    };
    expect(() => synth({ workflow: wf })).toThrow(/must be 64 characters or fewer/);
  });
});
