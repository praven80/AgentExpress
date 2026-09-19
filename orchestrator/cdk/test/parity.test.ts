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

import { buildBffWorkflow } from "../lib/orchestrator-stack";

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
// The BFF projection
// ---------------------------------------------------------------------------

describe("BFF workflow projection", () => {
  const tfKeys = hclKeys(hclBlock(read(path.join(TF, "bff.tf")), "bff_workflow"));

  it("emits the same top-level keys on both paths", () => {
    const cdkKeys = Object.keys(buildBffWorkflow(shipped)).sort();
    expect(tfKeys.length).toBeGreaterThan(3); // the extraction actually worked
    expect(cdkKeys).toEqual(tfKeys);
  });

  it("emits the same per-agent keys on both paths", () => {
    // `agents = { for id, a in ... : id => { name = ..., kind = ... } }` — the shape
    // we want is the comprehension's VALUE, not the comprehension itself.
    const tfAgentKeys = hclKeys(
      hclForValue(hclBlock(read(path.join(TF, "bff.tf")), "bff_workflow"), "agents")
    );
    const cdkAgentKeys = Object.keys(buildBffWorkflow(shipped).agents[firstAgent]).sort();
    expect(tfAgentKeys.length).toBeGreaterThan(3);
    expect(cdkAgentKeys).toEqual(tfAgentKeys);
  });

  it("fits the byte budget both paths enforce", () => {
    const bff = read(path.join(TF, "bff.tf"));
    const tfBudget = Number(bff.match(/length\(jsonencode\(local\.bff_workflow\)\) <= (\d+)/)![1]);
    const cdkBudget = Number(
      read(path.join(__dirname, "..", "lib", "orchestrator-stack.ts")).match(
        /WORKFLOW_JSON_MAX = (\d+)/
      )![1]
    );
    expect(cdkBudget).toBe(tfBudget);
    // And the shipped config is actually under it, with room to spare.
    expect(JSON.stringify(buildBffWorkflow(shipped)).length).toBeLessThan(tfBudget);
  });

  it("derives the same data-source labels as the Terraform expression", () => {
    // The four labels are written out longhand in both places, so compare the
    // literal strings rather than trusting they were copied correctly.
    const bff = read(path.join(TF, "bff.tf"));
    for (const label of [
      "Knowledge Base \u00b7 ",
      "Web Search",
      "REST API \u00b7 ",
      "MCP \u00b7 ",
      "Session input",
      "Upstream agent outputs",
      "A2A \u00b7 ",
    ]) {
      expect(bff).toContain(label);
    }
    // Agents resolved by their tool TYPE, not by name, so the assertion is about the
    // label derivation rather than about this sample's agent ids.
    const out = buildBffWorkflow(shipped);
    const kb = agentWithToolType("kb");
    const ws = agentWithToolType("websearch");
    const mcp = agentWithToolType("mcp");
    expect([kb, ws, mcp, firstAgent, upstreamAgent].every(Boolean)).toBe(true);
    expect(out.agents[kb].source).toMatch(/^Knowledge Base \u00b7 /);
    expect(out.agents[ws].source).toBe("Web Search");
    expect(out.agents[mcp].source).toMatch(/^MCP \u00b7 /);
    expect(out.agents[firstAgent].source).toBe("Session input");
    expect(out.agents[upstreamAgent].source).toBe("Upstream agent outputs");
  });

  it("labels a remote (a2a) agent by its Agent Card host, identically on both paths", () => {
    // A remote agent has no `tool`, so it hits a branch of its own. The two paths
    // compute the host with different primitives — a JS regex here, HCL replace/split
    // there — and they DID disagree on an uppercase scheme and on an empty card until
    // both were run against these four inputs. Hence the literal expectations: the
    // point is that the strings match, not that each side is individually plausible.
    const { a2aHost } = require("../lib/orchestrator-stack");
    expect(a2aHost("https://agents.partner.example/credit/v2")).toBe("agents.partner.example");
    expect(a2aHost("https://a.example")).toBe("a.example");
    expect(a2aHost("HTTPS://B.example/z")).toBe("B.example"); // scheme match is case-insensitive
    expect(a2aHost("")).toBe("remote"); // never an empty chip

    const out = buildBffWorkflow({
      orchestrator: {}, tools: {}, steps: [{ agent: "remote_one" }],
      agents: {
        remote_one: {
          name: "Partner Agent", runtime: "a2a",
          agentCard: "https://agents.partner.example/credit/v2",
        },
      },
    });
    expect(out.agents.remote_one.source).toBe("A2A \u00b7 agents.partner.example");
    expect(out.agents.remote_one.runtime).toBe("a2a");

    // And the HCL must strip the scheme case-insensitively and default to "remote",
    // which is what the two divergences were.
    const bff = read(path.join(TF, "bff.tf"));
    expect(bff).toContain('"A2A \u00b7 ');
    expect(bff).toContain("(?i)^https?:");
    expect(bff).toContain('"remote"');
  });
});

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

describe("constants duplicated across languages", () => {
  it("the RBAC action list matches in all three places", () => {
    // Necessarily duplicated: bff/authz.py enforces it, and both IaC paths need it
    // at plan/synth time to reject a typo'd action key. Terraform cannot read the
    // Python and TypeScript cannot read either, so this test is the link.
    const fromPython = read(path.join(ORCH_ROOT, "bff", "authz.py"))
      .match(/^ACTIONS = \(([^)]*)\)/m)![1]
      .match(/"([a-z]+)"/g)!
      .map((s) => s.replace(/"/g, ""))
      .sort();

    const fromTf = read(path.join(TF, "identity.tf"))
      .match(/authz_known_actions = \[([^\]]*)\]/)![1]
      .match(/"([a-z]+)"/g)!
      .map((s) => s.replace(/"/g, ""))
      .sort();

    const fromTs = read(path.join(__dirname, "..", "lib", "orchestrator-stack.ts"))
      .match(/const knownActions = \[([^\]]*)\]/)![1]
      .match(/"([a-z]+)"/g)!
      .map((s) => s.replace(/"/g, ""))
      .sort();

    expect(fromPython).toEqual([
      "cancel",
      "decision",
      "delete",
      "evaluate",
      "insights",
      "rerun",
      "start",
    ]);
    expect(fromTf).toEqual(fromPython);
    expect(fromTs).toEqual(fromPython);
  });

  it("the shipped authorization block only uses recognised actions", () => {
    const known = read(path.join(ORCH_ROOT, "bff", "authz.py"))
      .match(/^ACTIONS = \(([^)]*)\)/m)![1]
      .match(/"([a-z]+)"/g)!
      .map((s) => s.replace(/"/g, ""));
    for (const action of Object.keys(shipped.authorization?.actions ?? {})) {
      expect(known).toContain(action);
    }
  });

  /**
   * The allow-list from the `for n, t in local.tools : contains([...], t.<attr>)`
   * precondition — anchored on the comprehension so it can't match one of the
   * other `contains()` calls in the file (there is an unrelated
   * `contains(["mcp","openapi"], t.type)` used for API-key applicability).
   */
  function tfAllowList(src: string, attr: string): string[] {
    const m = src.match(
      new RegExp(`for n, t in local\\.tools\\s*:\\s*contains\\(\\[([^\\]]*)\\],\\s*t\\.${attr}\\)`)
    );
    expect(m).not.toBeNull();
    return m![1]
      .match(/"([\w-]*)"/g)!
      .map((s) => s.replace(/"/g, ""))
      .sort();
  }

  const stackSrc = read(path.join(__dirname, "..", "lib", "orchestrator-stack.ts"));
  const toolsTf = read(path.join(TF, "tools.tf"));

  it("the supported tool types match", () => {
    const fromTs = stackSrc
      .match(/const TOOL_TYPES: ToolType\[\] = \[([^\]]*)\]/)![1]
      .match(/"(\w+)"/g)!
      .map((s) => s.replace(/"/g, ""))
      .sort();
    expect(fromTs).toEqual(["kb", "lambda", "mcp", "openapi", "websearch"]);
    expect(tfAllowList(toolsTf, "type")).toEqual(fromTs);
    // And every type the shipped config uses is one of them.
    for (const t of Object.values<any>(shipped.tools)) expect(fromTs).toContain(t.type);
  });

  it("every tool type has an evidence label in the app", () => {
    // A third place the type list appears: the sample's shared research runner labels
    // the evidence block by tool type so the model knows what kind of source it is
    // reading. A type with no label silently falls back to the generic "TOOL" heading.
    // It lives under app/subagents/ because it is sample code, not framework — the
    // IaC still has to agree with it, since a customer declaring a tool type the
    // runner cannot label gets an unlabelled evidence block.
    const fromTs = stackSrc
      .match(/const TOOL_TYPES: ToolType\[\] = \[([^\]]*)\]/)![1]
      .match(/"(\w+)"/g)!
      .map((s) => s.replace(/"/g, ""))
      .sort();
    const labels = read(
      path.join(ORCH_ROOT, "app", "subagents", "_shared", "research.py"))
      .match(/_EVIDENCE_LABELS = \{([\s\S]*?)\n\}/)![1]
      .split("\n")
      .map((l) => l.replace(/#.*$/, "").match(/^\s*"(\w+)":/)?.[1])
      .filter(Boolean)
      .sort();
    expect(labels).toEqual(fromTs);
  });

  it("the accepted listingMode values match", () => {
    const fromTs = stackSrc
      .match(/\[("DEFAULT",\s*"DYNAMIC")\]\.includes\(t\.listingMode\)/)![1]
      .match(/"(\w+)"/g)!
      .map((s) => s.replace(/"/g, ""))
      .sort();
    expect(fromTs).toEqual(["DEFAULT", "DYNAMIC"]);
    expect(tfAllowList(toolsTf, "listing_mode")).toEqual(fromTs);
  });

  it("the accepted outbound auth values match", () => {
    const fromTs = stackSrc
      .match(/\[("none",\s*"apikey",\s*"sigv4")\]\.includes\(t\.auth\)/)![1]
      .match(/"(\w+)"/g)!
      .map((s) => s.replace(/"/g, ""))
      .sort();
    expect(fromTs).toEqual(["apikey", "none", "sigv4"]);
    // Terraform additionally accepts "" (an unset optional attribute), which on the
    // CDK path is `undefined` and skips the check entirely — same meaning.
    expect(tfAllowList(toolsTf, "auth").filter((v) => v !== "")).toEqual(fromTs);
  });

  it("the same S3 Vectors metadata keys are non-filterable on both paths", () => {
    // S3 Vectors caps FILTERABLE metadata at 2048 bytes per vector. Miss a key that
    // grows with the document and ingestion FAILS on a larger file — one document
    // fails, the job goes to FAILED, and the KB simply never returns that content.
    // Both paths must exclude the same set or one deployment silently loses docs.
    const keys = (src: string) => {
      // TS declares KB_NON_FILTERABLE; HCL declares local.kb_non_filterable. Both
      // are then referenced by name at the resource, so read the declarations.
      const m = src.match(/KB_NON_FILTERABLE\s*=\s*\[([^\]]*)\]/)
        ?? src.match(/kb_non_filterable\s*=\s*\[([^\]]*)\]/);
      expect(m).not.toBeNull();
      return m![1].match(/"(\w+)"/g)!.map((s) => s.replace(/"/g, "")).sort();
    };
    const fromTs = keys(read(path.join(__dirname, "..", "lib", "tool-plane.ts")));
    const fromTf = keys(read(path.join(TF, "kb.tf")));
    expect(fromTs).toEqual(["AMAZON_BEDROCK_METADATA", "AMAZON_BEDROCK_TEXT"]);
    expect(fromTf).toEqual(fromTs);
  });

  it("both paths derive the SAME vector index name", () => {
    // The name folds in the index's immutable properties so a change to them can be
    // replaced rather than failing the deploy. If the two schemes drift, a Terraform
    // and a CDK deployment of the same config would build different indexes.
    const { kbIndexName, KB_NON_FILTERABLE } = require("../lib/tool-plane");
    const fromTs = kbIndexName(1024, KB_NON_FILTERABLE);
    const digest = crypto
      .createHash("sha256")
      .update(`1024|${[...KB_NON_FILTERABLE].sort().join(",")}`)
      .digest("hex")
      .slice(0, 8);
    expect(fromTs).toBe(`kb-index-${digest}`);
    // And the HCL builds it the same way, from the same inputs.
    const tf = read(path.join(TF, "kb.tf"));
    expect(tf).toContain("kb_storage_digest = substr(sha256(");
    expect(tf).toContain('join(",", sort(local.kb_non_filterable))');
    expect(tf).toContain("local.kb_dims}|");
    expect(tf).toContain('kb_index_name     = "kb-index-${local.kb_storage_digest}"');
  });

  it("both paths derive the SAME knowledge base name, sharing the index digest", () => {
    // The index name alone is not enough. A replaced index has a new ARN, and the KB's
    // storage_configuration is itself immutable, so the KB is replaced too — and hit
    // the identical wall one level up, as a 409 "already exists" rather than a rename
    // hint. The KB name must therefore carry the SAME digest as its index.
    const { knowledgeBaseName, kbIndexName, KB_NON_FILTERABLE } = require("../lib/tool-plane");
    const digest = kbIndexName(1024, KB_NON_FILTERABLE).replace("kb-index-", "");
    expect(knowledgeBaseName("multiagent-orchestrator", 1024, KB_NON_FILTERABLE))
      .toBe(`multiagent-orchestrator-kb-${digest}`);
    const tf = read(path.join(TF, "kb.tf"));
    expect(tf).toContain('kb_name     = "${replace(var.agent_name, "_", "-")}-kb-${local.kb_storage_digest}"');
    // And the resource uses the local rather than re-deriving a name of its own.
    expect(tf).toContain("name     = local.kb_name");
  });

  it("the index AND kb names change when an immutable property changes", () => {
    const { kbIndexName, knowledgeBaseName } = require("../lib/tool-plane");
    for (const f of [
      (d: number, k: string[]) => kbIndexName(d, k),
      (d: number, k: string[]) => knowledgeBaseName("x", d, k),
    ]) {
      expect(f(1024, ["AMAZON_BEDROCK_TEXT"]))
        .not.toBe(f(1024, ["AMAZON_BEDROCK_TEXT", "AMAZON_BEDROCK_METADATA"]));
      expect(f(1024, ["A"])).not.toBe(f(512, ["A"]));
      // Order must not matter, or a harmless reorder would force a replacement.
      expect(f(1024, ["A", "B"])).toBe(f(1024, ["B", "A"]));
    }
  });

  it("both paths expose the outputs the docs tell an operator to read", () => {
    // Not a full set comparison — the two paths legitimately differ at the edges
    // (Terraform also emits the ECR url and the runtime status; CDK emits the
    // dedicated-agent list). But an output a documented command consumes must exist on
    // both, and one did NOT: DEPLOYMENT.md's ingestion check reads the knowledge base
    // id, which only the CDK path emitted, so the documented command was unrunnable
    // on Terraform.
    const cdkOutputs = new Set(
      [...read(path.join(__dirname, "..", "lib", "orchestrator-stack.ts")).matchAll(
        /new cdk\.CfnOutput\(this,\s*"(\w+)"/g
      )].map((m) => m[1])
    );
    expect(cdkOutputs.size).toBeGreaterThan(5);
    const tfOutputs = new Set(
      ["outputs.tf", "identity.tf"].flatMap((f) =>
        [...read(path.join(TF, f)).matchAll(/^output\s+"(\w+)"/gm)].map((m) => m[1])
      )
    );
    expect(tfOutputs.size).toBeGreaterThan(5);
    // CDK name -> Terraform name, where the two paths chose different words.
    for (const [cdkName, tfName] of [
      ["uiUrl", "ui_url"],
      ["apiEndpoint", "api_endpoint"],
      ["agentRuntimeArn", "agent_runtime_arn"],
      ["memoryId", "memory_id"],
      ["gatewayUrl", "gateway_url"],
      ["gatewayId", "gateway_id"],
      ["knowledgeBaseId", "knowledge_base_id"],
      ["idp", "idp"],
      ["authEnabled", "auth_enabled"],
      ["cognitoUserPoolId", "cognito_user_pool_id"],
      ["cognitoDomainPrefix", "cognito_domain_prefix"],
      ["cognitoClientId", "login_client_id"],
    ]) {
      expect(cdkOutputs.has(cdkName)).toBe(true);
      expect(tfOutputs.has(tfName)).toBe(true);
    }
  });

  it("the BFF gets a Bedrock model grant on BOTH paths", () => {
    // The in-app assistant's tool-use loop runs IN the BFF Lambda (bff/chatbot.py
    // calls bedrock-runtime Converse). Terraform granted it; the CDK path did not —
    // so a CDK deployment served the chat UI and every reply was
    // "couldn't reach the model (AccessDeniedException)". The route-list test above
    // could not catch it, because the route existed and returned 200.
    const tf = read(path.join(TF, "bff.tf"));
    const stack = read(path.join(__dirname, "..", "lib", "orchestrator-stack.ts"));
    expect(tf).toContain("bedrock:InvokeModel");
    // On the CDK side the grant must be attached to the BFF, not only to the runtime
    // roles — assert the call, not just the string.
    expect(stack).toMatch(/bff\.addToRolePolicy\([\s\S]{0,400}?bedrock:InvokeModel/);
  });

  it("the BFF gets the deployment's model id on BOTH paths", () => {
    // bff/chatbot.py falls back to $MODEL_ID rather than carrying its own copy of the
    // model literal, so both paths have to inject it.
    expect(read(path.join(TF, "bff.tf"))).toContain("MODEL_ID        = var.model_id");
    expect(read(path.join(__dirname, "..", "lib", "orchestrator-stack.ts")))
      .toContain("MODEL_ID: props.modelId");
  });

  it("both paths inject TOOLS_JSON with the same field projection", () => {
    // How to CALL each tool. The CDK path did not inject it at all, so the app fell
    // back to the workflow.json baked into the image — which keeps a NARROWER set of
    // fields, making a request-level option work under Terraform and silently do
    // nothing under CDK.
    const tf = read(path.join(TF, "main.tf"));
    const stack = read(path.join(__dirname, "..", "lib", "orchestrator-stack.ts"));
    expect(tf).toContain("TOOLS_JSON = local.tools_env");
    expect(stack).toContain("TOOLS_JSON: JSON.stringify(toolsEnv(");

    // The projected field set must match the app's keep-list, or a field survives on
    // one path and is dropped on the other.
    const { toolsEnv } = require("../lib/orchestrator-stack");
    const projected = toolsEnv({
      kb: { type: "kb", corpora: ["a"] },
      ws: {
        type: "websearch", maxResults: 5, includeDomains: ["x"], excludeDomains: ["y"],
        publishedFrom: "2026-01-01", publishedTo: "2026-02-01",
      },
      mcp: { type: "mcp", endpoint: "https://e", call: "c", arg: "q", args: { r: 1 } },
      // rowFields is what keeps a DETERMINISTIC agent config-driven: it maps the
      // roles such an agent needs onto whatever the target calls its fields, so
      // repointing the tool at another Lambda/warehouse needs no agent code change.
      // Dropped on one deploy path, that agent silently finds nothing on that path.
      lam: {
        type: "lambda", source: "tool_lambda", call: "aws_prices", arg: "services",
        rowFields: { service: "service", dimension: "dimension", unit: "unit", price: "pricePerUnit" },
      },
    });
    expect(Object.keys(projected.ws).sort()).toEqual([
      "excludeDomains", "includeDomains", "maxResults", "publishedFrom",
      "publishedTo", "type",
    ]);
    expect(Object.keys(projected.mcp).sort()).toEqual(["arg", "args", "call", "type"]);
    expect(projected.kb).toEqual({ type: "kb", corpora: ["a"] });
    expect(Object.keys(projected.lam).sort()).toEqual([
      "arg", "call", "rowFields", "type",
    ]);
    expect(projected.lam.rowFields.price).toBe("pricePerUnit");

    // Terraform must project it too, or it works under CDK and not under Terraform.
    expect(read(path.join(TF, "tools.tf"))).toContain("rowFields = t.row_fields");
    expect(read(path.join(TF, "tools.tf"))).toContain("row_fields = try(t.rowFields");

    const keep = read(path.join(ORCH_ROOT, "app", "common", "config.py"))
      .match(/keep = \(([\s\S]*?)\)/)![1]
      .match(/"(\w+)"/g)!
      .map((x) => x.replace(/"/g, ""));
    for (const f of Object.keys(projected.ws)
      .concat(Object.keys(projected.mcp))
      .concat(Object.keys(projected.lam))) {
      expect(keep).toContain(f);
    }
  });

  it("the RBAC action list includes `start` in all four places", () => {
    // Starting a run is the most expensive action in the app. It had no action name,
    // so it could not be restricted from config while every cheaper action could.
    expect(read(path.join(ORCH_ROOT, "bff", "authz.py"))).toContain('"start"');
    expect(read(path.join(ORCH_ROOT, "bff", "handler.py"))).toContain('_forbidden("start", event)');
    expect(read(path.join(TF, "identity.tf"))).toContain('"start"');
    expect(read(path.join(__dirname, "..", "lib", "orchestrator-stack.ts")))
      .toContain('"start"');
  });

  it("every Lambda log group is stack-owned with retention, on BOTH paths", () => {
    // Left implicit, Lambda creates /aws/lambda/<name> itself with NEVER-EXPIRE
    // retention and no stack ownership, so a destroy leaves it accruing cost forever.
    // A verified full destroy of this stack orphaned EIGHT such groups.
    const stack = read(path.join(__dirname, "..", "lib", "orchestrator-stack.ts"));
    const tp = read(path.join(__dirname, "..", "lib", "tool-plane.ts"));

    // Every lambda.Function must declare a logGroup. Sliced rather than regexed over
    // the whole construct, so a formatting change cannot make this pass vacuously.
    for (const [file, src] of [["orchestrator-stack.ts", stack], ["tool-plane.ts", tp]]) {
      let found = 0;
      for (let i = src.indexOf("new lambda.Function"); i !== -1;
           i = src.indexOf("new lambda.Function", i + 1)) {
        found++;
        const head = src.slice(i, i + 500);
        expect(head).toContain("logGroup:");
      }
      expect(found).toBeGreaterThan(0);
      expect(src).toContain("removalPolicy: cdk.RemovalPolicy.DESTROY");
      void file;
    }

    // The two LOG_RETENTION constants are duplicated (a shared import would be
    // circular), so they must agree.
    const a = stack.match(/const LOG_RETENTION = logs\.RetentionDays\.(\w+)/)![1];
    const b = tp.match(/LOG_RETENTION = logs\.RetentionDays\.(\w+)/)![1];
    expect(a).toBe(b);

    // And Terraform owns the same groups, with a configurable retention.
    for (const [f, name] of [
      ["bff.tf", "AgentCoreBFF-"],
      ["kb.tf", "AgentCoreKBRetrieve-"],
      ["tools.tf", "ToolLambda-"],
    ]) {
      const hcl = read(path.join(TF, f));
      expect(hcl).toContain("aws_cloudwatch_log_group");
      expect(hcl).toContain(`/aws/lambda/${name}`);
      expect(hcl).toContain("retention_in_days = var.log_retention_days");
    }
    expect(read(path.join(TF, "variables.tf"))).toContain('variable "log_retention_days"');
  });

  it("the web search regions match", () => {
    const fromTs = read(path.join(__dirname, "..", "lib", "orchestrator-stack.ts"))
      .match(/const WEB_SEARCH_REGIONS = \[([^\]]*)\]/)![1]
      .match(/"([\w-]+)"/g)!
      .map((s) => s.replace(/"/g, ""))
      .sort();
    expect(fromTs).toEqual(["ap-northeast-1", "eu-west-1", "us-east-1"]);
    const tools = read(path.join(TF, "tools.tf"));
    for (const r of fromTs) expect(tools).toContain(r);
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

  it("keeps every *Note key out of the BFF projection", () => {
    // The Notes are for whoever edits the file, and every byte counts in a 4 KB
    // Lambda environment.
    expect(JSON.stringify(buildBffWorkflow(shipped))).not.toMatch(/"[a-zA-Z]+Note"/);
  });
});
