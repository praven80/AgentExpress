/**
 * Terraform ↔ CDK parity.
 *
 * The project promises both IaC paths deploy the same thing from the same
 * `workflow.json`. Nothing enforces that: they are two independent
 * implementations of the same projection, and they HAVE drifted — `buildBffWorkflow`
 * once emitted `mcp`/`rag` after workflow.json renamed them to `tool`/`corpus`, and
 * omitted `evalAgents` and `chatbot` entirely, so CDK deployments silently lost the
 * data-source chips, the Evaluate buttons and the in-app assistant while Terraform
 * ones kept them. Nobody noticed until a live CDK deploy was compared to a live
 * Terraform one.
 *
 * These tests read the HCL as text and compare the things a drift would show up in:
 * the key set of the BFF projection, the API route list, the tool types, the web
 * search regions, and the RBAC action list (which is duplicated in three places by
 * necessity — Terraform cannot read the Python, and TypeScript cannot read either).
 *
 * Reading HCL with regexes is crude, but the alternative is an HCL parser
 * dependency for a handful of lists, and each check below asserts it actually
 * extracted something before comparing — so a regex that stops matching fails loudly
 * rather than passing vacuously.
 */

import * as crypto from "crypto";
import * as fs from "fs";
import * as path from "path";


const ORCH_ROOT = path.join(__dirname, "..", "..");
const TF = path.join(ORCH_ROOT, "terraform");

const read = (p: string) => fs.readFileSync(p, "utf8");
const shipped = JSON.parse(read(path.join(ORCH_ROOT, "app", "workflow.json")));

/** The agent ids in one `steps` entry, whatever its shape. */
const stepIds = (s: any): string[] => s.parallel ?? s.sequence ?? [s.agent];

/**
 * Agent ids resolved from the shipped TOPOLOGY rather than written out.
 *
 * These tests used to name `agents.intake` directly, which quietly made the test
 * suite part of the customer-editable surface: renaming an agent in workflow.json
 * turned them red even though the framework was fine. The promise is that
 * workflow.json + app/subagents/ is all you touch.
 */
const firstAgent: string = stepIds(shipped.steps[0])[0];
/** The first agent whose data source is the tool type `t`, or "" if none. */
const agentWithToolType = (t: string): string =>
  Object.entries<any>(shipped.agents).find(
    ([, a]) => a.tool && shipped.tools[a.tool]?.type === t
  )?.[0] ?? "";
/**
 * The first agent that has no tool and reads its upstream peers.
 *
 * `runtime: "a2a"` is excluded: a remote agent also has no tool, but its label comes
 * from the A2A branch of the projection, not from `access`. Without that exclusion this
 * resolved to a remote agent as soon as the sample gained one, and the assertion about
 * the "Upstream agent outputs" label started checking the wrong branch.
 */
const upstreamAgent: string =
  Object.entries<any>(shipped.agents).find(
    ([id, a]) => !a.tool && id !== firstAgent && (a.runtime ?? "main") !== "a2a"
  )?.[0] ?? "";

/** Balance braces from the `{` at or after `from`, returning the body between them. */
function balanced(src: string, from: number): string {
  let depth = 0;
  let i = src.indexOf("{", from);
  const open = i;
  for (; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}" && --depth === 0) return src.slice(open + 1, i);
  }
  throw new Error(`unbalanced HCL block at offset ${from}`);
}

/** The body of a `<name> = { ... }` HCL block. */
function hclBlock(src: string, name: string): string {
  const start = src.indexOf(`${name} = {`);
  expect(start).toBeGreaterThan(-1);
  return balanced(src, start);
}

/**
 * The body of the OBJECT a `<name> = { for ... : k => { ... } }` comprehension
 * produces — i.e. the per-item shape, not the comprehension's own braces.
 */
function hclForValue(src: string, name: string): string {
  const start = src.indexOf(`${name} = {`);
  expect(start).toBeGreaterThan(-1);
  const arrow = src.indexOf("=> {", start);
  expect(arrow).toBeGreaterThan(start);
  return balanced(src, arrow);
}

/** Top-level `key =` names inside an HCL block body, ignoring comments. */
function hclKeys(body: string): string[] {
  const keys = new Set<string>();
  let depth = 0;
  for (const line of body.split("\n")) {
    const code = line.replace(/#.*$/, "");
    if (depth === 0) {
      const m = code.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=/);
      if (m) keys.add(m[1]);
    }
    depth += (code.match(/[{[(]/g) ?? []).length - (code.match(/[}\])]/g) ?? []).length;
  }
  return [...keys].sort();
}

// ---------------------------------------------------------------------------
// API routes
// ---------------------------------------------------------------------------

describe("API routes", () => {
  it("both paths expose exactly the same route set", () => {
    // A route present in one path and not the other is a 404 that only appears on
    // one kind of deployment — which is how /api/me could have gone missing.
    const tfRoutes = [
      ...read(path.join(TF, "bff.tf")).matchAll(
        /^\s*"((?:GET|POST|PUT|DELETE|PATCH) \/api\/[^"]*)",/gm
      ),
    ]
      .map((m) => m[1])
      .sort();

    const stack = read(path.join(__dirname, "..", "lib", "orchestrator-stack.ts"));
    const cdkRoutes = [
      ...stack.matchAll(/\{\s*path:\s*"(\/api\/[^"]*)",\s*methods:\s*\[([^\]]*)\]/g),
    ]
      .flatMap(([, p, methods]) =>
        [...methods.matchAll(/HttpMethod\.(\w+)/g)].map((m) => `${m[1]} ${p}`)
      )
      .sort();

    expect(tfRoutes.length).toBeGreaterThan(10);
    expect(cdkRoutes).toEqual(tfRoutes);
  });

  it("includes the RBAC-relevant routes", () => {
    const stack = read(path.join(__dirname, "..", "lib", "orchestrator-stack.ts"));
    for (const p of ["/api/me", "/api/sessions/{id}/decision", "/api/insights/run"]) {
      expect(stack).toContain(`"${p}"`);
    }
  });
});

// ---------------------------------------------------------------------------
// Duplicated constants
// ---------------------------------------------------------------------------

describe("the framework vocabulary has ONE home", () => {
  /**
   * This describe used to be called "constants duplicated across languages", and it
   * compared hand-written copies of every closed value set against each other: the tool
   * types in Python, TypeScript and HCL; the RBAC action names in all three; the schema
   * property types; the a2a auth modes; the web search regions.
   *
   * It was a test that the duplicates had not drifted — not a reason for them to exist.
   * And the drift was real: a value added to one copy and missed in another rejected a
   * config the other planes accepted, so whether a workflow deployed depended on which
   * IaC path you used.
   *
   * There is one copy now, app/vocabulary.json, and JSON is the one format all three
   * planes read natively. So these assert that each plane READS it, which is a much
   * stronger guarantee than three lists agreeing on one particular day.
   */
  const VOCAB_PATH = path.join(ORCH_ROOT, "app", "vocabulary.json");
  const vocab = JSON.parse(read(VOCAB_PATH));

  it("declares every set with values and an explanation of why it is closed", () => {
    const names = Object.keys(vocab).filter((k) => !k.startsWith("$"));
    expect(names.length).toBeGreaterThan(10);
    for (const name of names) {
      expect(Array.isArray(vocab[name].values)).toBe(true);
      expect(vocab[name].values.length).toBeGreaterThan(0);
      // A closed set a customer can trip over has to say why it is closed, or the
      // error message is a dead end.
      expect(String(vocab[name].$comment ?? "").length).toBeGreaterThan(20);
    }
  });

  it("is read by the TypeScript plane, not restated in it", () => {
    const stack = read(path.join(__dirname, "..", "lib", "orchestrator-stack.ts"));
    expect(stack).toContain('from "./vocabulary"');
    const vocabTs = read(path.join(__dirname, "..", "lib", "vocabulary.ts"));
    expect(vocabTs).toContain("vocabulary.json");
    // The literals must be GONE from the stack, or the duplicate is back.
    expect(stack).not.toMatch(/\["kb", "websearch", "mcp", "openapi", "lambda"\]/);
    expect(stack).not.toMatch(/\["main", "dedicated", "a2a"\]/);
    expect(stack).not.toMatch(/\["none", "bearer", "oauth2", "sigv4"\]/);
    expect(stack).not.toMatch(/"cancel", "decision", "delete"/);
  });

  it("is read by the Python plane, not restated in it", () => {
    const registry = read(path.join(ORCH_ROOT, "app", "orchestrator", "registry.py"));
    expect(registry).toContain("vocabulary.");
    expect(registry).not.toMatch(/RUNTIMES = \("main"/);
    expect(registry).not.toMatch(/TOOL_TYPES = \("kb"/);
    const authz = read(path.join(ORCH_ROOT, "bff", "authz.py"));
    expect(authz).toContain("authorizationActions");
    expect(authz).not.toMatch(/ACTIONS = \("start"/);
  });

  it("is read by the Terraform plane, not restated in it", () => {
    const tools = read(path.join(TF, "tools.tf"));
    const identity = read(path.join(TF, "identity.tf"));
    const guardrail = read(path.join(TF, "guardrail.tf"));
    for (const src of [tools, identity, guardrail]) {
      expect(src).toContain("app/vocabulary.json");
    }
    expect(tools).not.toMatch(/\["kb", "websearch", "mcp", "openapi", "lambda"\]/);
    expect(tools).not.toMatch(/\["DEFAULT", "DYNAMIC"\]/);
    expect(identity).not.toMatch(/"cancel", "decision", "delete"/);
    expect(guardrail).not.toMatch(/\["NONE", "LOW", "MEDIUM", "HIGH"\]/);
  });

  it("ships the file to the BFF, which reads the action names from it", () => {
    // authz.py enforces RBAC inside the Lambda, so the file has to travel with it.
    const stack = read(path.join(__dirname, "..", "lib", "orchestrator-stack.ts"));
    expect(stack).toContain('"vocabulary.json"');
    const bffTf = read(path.join(TF, "bff.tf"));
    expect(bffTf).toContain('filename = "vocabulary.json"');
  });

  it("still holds the values the shipped workflow relies on", () => {
    // A vocabulary that lost a value would pass every test above and reject the
    // shipped config, so pin the ones workflow.json actually uses.
    expect(vocab.runtimes.values).toEqual(expect.arrayContaining(["main", "dedicated", "a2a"]));
    expect(vocab.toolTypes.values).toEqual(
      expect.arrayContaining(["kb", "websearch", "mcp", "openapi", "lambda"])
    );
    expect(vocab.a2aAuthModes.values).toContain("sigv4");
    expect(vocab.a2aLambdaSkills.values).toEqual(["compliance", "resilience"]);
    expect(vocab.memoryStrategies.values).toEqual(["semantic", "summary"]);
    expect(vocab.authorizationActions.values.sort()).toEqual([
      "cancel", "decision", "delete", "evaluate", "insights", "rerun", "start",
    ]);
  });
});

// ---------------------------------------------------------------------------
// The shipped config is coherent for both paths
// ---------------------------------------------------------------------------

describe("the shipped workflow.json", () => {
  it("declares an authorization block with groups for every restricted action", () => {
    const actions: Record<string, string[]> = shipped.authorization.actions;
    expect(Object.keys(actions).length).toBeGreaterThan(0);
    for (const [action, groups] of Object.entries(actions)) {
      expect(Array.isArray(groups)).toBe(true);
      expect(groups.length).toBeGreaterThan(0); // no accidentally-closed action
    }
  });

  it("names every agent's tool in the tools block", () => {
    for (const [id, a] of Object.entries<any>(shipped.agents)) {
      if (a.tool) expect(Object.keys(shipped.tools)).toContain(a.tool);
    }
  });

});
