/**
 * Config-plane tests for the CDK path.
 *
 * These are the pure functions that turn `workflow.json` into CloudFormation
 * shapes: the validators that reject bad config at synth, the BFF projection, the
 * guardrail projection and the Cedar generator. All of them are deterministic and
 * take a plain object, so they can be asserted exactly — no synth, no AWS.
 *
 * The parity with Terraform is checked separately, in parity.test.ts.
 */

import {
  authzGroups,
  buildBffWorkflow,
  buildGuardrail,
  stripForEnv,
  validateBranches,
  validateTools,
  validateWorkflow,
} from "../lib/orchestrator-stack";
import { cedarStatement, ToolSpec } from "../lib/tool-plane";

const ORCH_ROOT = `${__dirname}/../..`;
const GW = "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/gw-abc";

/** A minimal valid `type: "lambda"` spec, spread into tests that need one. */
const LAMBDA_TOOL = {
  lambdaArn: "arn:aws:lambda:us-east-1:123456789012:function:query-claims",
  toolSchema: [
    {
      name: "query_claims",
      description: "Answer a question about claims.",
      properties: {
        question: { type: "string", required: true, description: "The question." },
        limit: { type: "integer", required: false, description: "Max rows." },
      },
    },
  ],
};

/** A workflow whose `agents` are derived from `steps`, like the pytest helper. */
function wf(steps: any[], extra: Record<string, any> = {}) {
  const ids = steps.flatMap((s) => s.parallel ?? s.sequence ?? [s.agent]);
  const agents: Record<string, any> = {};
  for (const id of ids) agents[id] = { name: id, runtime: "main" };
  return { orchestrator: {}, agents, steps, tools: {}, ...extra };
}

// ---------------------------------------------------------------------------
// validateTools
// ---------------------------------------------------------------------------

describe("validateTools", () => {
  it("accepts the five supported types", () => {
    const tools = {
      kb: { type: "kb", corpora: ["reference"] },
      ws: { type: "websearch" },
      mcp: { type: "mcp", endpoint: "https://example.test/mcp" },
      api: { type: "openapi", schemaS3Uri: "s3://bucket/schema.json" },
      fn: { type: "lambda", ...LAMBDA_TOOL },
    };
    expect(Object.keys(validateTools(tools, {}, "us-east-1"))).toEqual([
      "kb",
      "ws",
      "mcp",
      "api",
      "fn",
    ]);
  });

  it("rejects an unknown type by naming the valid ones", () => {
    expect(() => validateTools({ x: { type: "graphql" } }, {}, "us-east-1")).toThrow(
      /needs "type" = kb \| websearch \| mcp \| openapi \| lambda/
    );
  });

  it("requires an endpoint for an mcp tool", () => {
    // Without this the target is created pointing nowhere and every call fails at
    // RUNTIME, long after a successful deploy.
    expect(() => validateTools({ m: { type: "mcp" } }, {}, "us-east-1")).toThrow(
      /requires "endpoint"/
    );
  });

  it("requires a schema URI for an openapi tool", () => {
    expect(() => validateTools({ a: { type: "openapi" } }, {}, "us-east-1")).toThrow(
      /requires "schemaS3Uri"/
    );
  });

  it("requires a non-empty corpora list for a kb tool", () => {
    // An empty list means the generated Cedar permit filters on nothing, and
    // retrievals come back empty with no error at all.
    expect(() => validateTools({ kb: { type: "kb" } }, {}, "us-east-1")).toThrow(
      /requires a non-empty "corpora" list/
    );
    expect(() =>
      validateTools({ kb: { type: "kb", corpora: [] } }, {}, "us-east-1")
    ).toThrow(/requires a non-empty "corpora" list/);
  });

  it("allows only one kb tool, because there is one Knowledge Base", () => {
    const tools = {
      a: { type: "kb", corpora: ["x"] },
      b: { type: "kb", corpora: ["y"] },
    };
    expect(() => validateTools(tools, {}, "us-east-1")).toThrow(/Only one .* type="kb"/);
  });

  it("allows only one websearch tool", () => {
    const tools = { a: { type: "websearch" }, b: { type: "websearch" } };
    expect(() => validateTools(tools, {}, "us-east-1")).toThrow(
      /Only one .* type="websearch"/
    );
  });

  it("rejects websearch in a region where the connector does not exist", () => {
    expect(() => validateTools({ ws: { type: "websearch" } }, {}, "us-west-2")).toThrow(
      /only available in us-east-1, eu-west-1, ap-northeast-1 \(got us-west-2\)/
    );
    for (const r of ["us-east-1", "eu-west-1", "ap-northeast-1"]) {
      expect(() => validateTools({ ws: { type: "websearch" } }, {}, r)).not.toThrow();
    }
  });

  it("rejects connectorVersion rather than silently ignoring it", () => {
    // CloudFormation's connector source accepts only connectorId, so the pin
    // cannot be expressed on this path. Accepting and dropping it would mean the
    // deployment quietly disagreed with the config.
    expect(() =>
      validateTools({ ws: { type: "websearch", connectorVersion: "1.2.0" } }, {}, "us-east-1")
    ).toThrow(/CloudFormation cannot express/);
  });

  it("rejects an invalid listingMode or auth", () => {
    expect(() =>
      validateTools(
        { m: { type: "mcp", endpoint: "https://x.test", listingMode: "LAZY" } },
        {},
        "us-east-1"
      )
    ).toThrow(/invalid "listingMode"/);
    expect(() =>
      validateTools(
        { m: { type: "mcp", endpoint: "https://x.test", auth: "basic" } },
        {},
        "us-east-1"
      )
    ).toThrow(/invalid "auth"/);
  });

  it("rejects an agent bound to a tool that is not declared", () => {
    // The failure this prevents: the agent runs with no data and reports findings
    // it reasoned out of thin air.
    const agents = { research: { tool: "knowledge" } };
    expect(() =>
      validateTools({ kb: { type: "kb", corpora: ["r"] } }, agents, "us-east-1")
    ).toThrow(/references tool "knowledge", which is not a key in the "tools" block/);
  });

  it("accepts an agent with no tool at all", () => {
    expect(() => validateTools({}, { analysis: { name: "A" } }, "us-east-1")).not.toThrow();
  });

  // --- type=lambda -------------------------------------------------------
  // The Gateway cannot discover a Lambda's tools (there is no tools/list to call),
  // so every one of these mistakes would otherwise be accepted at deploy time and
  // then publish a tool the agent cannot call — an empty answer, not an error.
  describe("type=lambda", () => {
    const fn = (over: Record<string, any> = {}) =>
      validateTools({ fn: { type: "lambda", ...LAMBDA_TOOL, ...over } }, {}, "us-east-1");

    it("accepts a well-formed entry", () => {
      expect(() => fn()).not.toThrow();
    });

    it("requires a full function ARN", () => {
      for (const bad of ["query-claims", "arn:aws:lambda:us-east-1:123:function:x"]) {
        expect(() => fn({ lambdaArn: bad })).toThrow(/requires "lambdaArn"/);
      }
    });

    it("requires exactly one of lambdaArn and source", () => {
      // Both is ambiguous about which function to call; neither means there is
      // nothing to register.
      expect(() => fn({ lambdaArn: undefined })).toThrow(/EXACTLY ONE of "lambdaArn"/);
      expect(() => fn({ lambdaArn: "" })).toThrow(/EXACTLY ONE of "lambdaArn"/);
      expect(() => fn({ source: "tool_lambda" })).toThrow(/EXACTLY ONE of "lambdaArn"/);
      expect(() => fn({ lambdaArn: undefined, source: "tool_lambda" })).not.toThrow();
    });

    it("accepts only the built-in value for source", () => {
      // `source` is not a general "deploy any directory" feature: a
      // framework-deployed function needs an execution role config cannot express.
      expect(() => fn({ lambdaArn: undefined, source: "my_functions/claims" })).toThrow(
        /the only value is "tool_lambda"/
      );
    });

    it("does not require an ARN when source is used", () => {
      // The framework resolves the ARN of the function it deploys, which keeps the
      // committed config account-neutral.
      expect(() =>
        validateTools(
          { runs: { type: "lambda", source: "tool_lambda", ...LAMBDA_TOOL, lambdaArn: undefined } },
          {},
          "us-east-1"
        )
      ).not.toThrow();
    });

    it("accepts an alias or version qualifier on the ARN", () => {
      for (const arn of [
        "arn:aws:lambda:us-east-1:123456789012:function:query-claims:live",
        "arn:aws:lambda:us-east-1:123456789012:function:query-claims:3",
        "arn:aws-us-gov:lambda:us-gov-west-1:123456789012:function:query-claims",
      ]) {
        expect(() => fn({ lambdaArn: arn })).not.toThrow();
      }
    });

    it("requires a non-empty toolSchema", () => {
      expect(() => fn({ toolSchema: undefined })).toThrow(/non-empty "toolSchema"/);
      expect(() => fn({ toolSchema: [] })).toThrow(/non-empty "toolSchema"/);
    });

    it("requires each tool to have a name and at least one property", () => {
      expect(() => fn({ toolSchema: [{ properties: { q: {} } }] })).toThrow(
        /needs a "name" and at least one/
      );
      expect(() => fn({ toolSchema: [{ name: "t", properties: {} }] })).toThrow(
        /needs a "name" and at least one/
      );
      expect(() => fn({ toolSchema: [{ name: "t" }] })).toThrow(/needs a "name" and at least one/);
    });

    it("rejects a property type the inline schema does not support", () => {
      expect(() =>
        fn({ toolSchema: [{ name: "t", properties: { q: { type: "date" } } }], call: "t", arg: "q" })
      ).toThrow(/use one of string, number, integer, boolean, array, object/);
    });

    it("defaults a property with no type to string", () => {
      expect(() =>
        fn({ toolSchema: [{ name: "t", properties: { q: {} } }], call: "t", arg: "q" })
      ).not.toThrow();
    });

    it("requires `call` when the function publishes several tools", () => {
      // Mirrors _select_tool in the app, which refuses to guess between candidates.
      const two = [
        { name: "a", properties: { q: { type: "string" } } },
        { name: "b", properties: { q: { type: "string" } } },
      ];
      expect(() => fn({ toolSchema: two, call: undefined })).toThrow(
        /must also set "call" naming which one/
      );
      expect(() => fn({ toolSchema: two, call: "b", arg: "q" })).not.toThrow();
    });

    it("requires `call` to name one of its own tools", () => {
      expect(() => fn({ call: "query_claim" })).toThrow(/is not one of its own "toolSchema"/);
    });

    it("requires `arg` to be a property of the tool being called", () => {
      // Otherwise the query is sent under a name the function ignores, and the tool
      // answers as though no query had been supplied.
      expect(() => fn({ call: "query_claims", arg: "prompt" })).toThrow(
        /is not a property of "query_claims"/
      );
      expect(() => fn({ call: "query_claims", arg: "question" })).not.toThrow();
    });

    it("checks `arg` against the first tool when `call` is omitted", () => {
      expect(() => fn({ arg: "nope" })).toThrow(/is not a property of "query_claims"/);
    });
  });
});

// ---------------------------------------------------------------------------
// validateWorkflow
// ---------------------------------------------------------------------------

describe("validateWorkflow", () => {
  const ok = (w: any, idp = "cognito") =>
    validateWorkflow(w, ORCH_ROOT, "multiagent_orchestrator", idp);

  it("accepts the shipped workflow", () => {
    const shipped = require(`${ORCH_ROOT}/app/workflow.json`);
    expect(() => ok(shipped)).not.toThrow();
  });

  it("rejects an empty steps list", () => {
    expect(() => ok({ agents: {}, steps: [] })).toThrow(/`steps` is empty/);
  });

  it("rejects a step naming an agent that is not declared", () => {
    expect(() => ok({ agents: { a: {} }, steps: [{ agent: "a" }, { agent: "b" }] })).toThrow(
      /names agent id\(s\) that are not in `agents`: b/
    );
  });

  it("rejects an agent that no step runs", () => {
    // Dead config that provisions a runtime nothing ever calls.
    expect(() => ok({ agents: { a: {}, orphan: {} }, steps: [{ agent: "a" }] })).toThrow(
      /declares agent\(s\) that no `steps` entry runs: orphan/
    );
  });

  it("rejects an agent id with a hyphen", () => {
    // The id becomes part of the AgentCore Runtime name, which rejects hyphens —
    // otherwise this fails at DEPLOY time with an opaque API error.
    expect(() => ok(wf([{ agent: "web-search" }]))).toThrow(
      /must match \^\[a-zA-Z\]\[a-zA-Z0-9_\]\*\$/
    );
  });

  it("rejects a dedicated runtime name over 48 characters", () => {
    const id = "a".repeat(40);
    const w = wf([{ agent: id }]);
    w.agents[id].runtime = "dedicated";
    expect(() => ok(w)).toThrow(/48 characters or fewer/);
  });

  it("only applies the 48-character limit to dedicated agents", () => {
    // An in-process agent has no runtime of its own, so the limit does not apply.
    const id = "a".repeat(40);
    expect(() => ok(wf([{ agent: id }]))).not.toThrow();
  });

  it("rejects a corpus that is not a folder under kb_docs/", () => {
    // The quiet failure: the Cedar permit filters on a doc_type no chunk carries
    // and retrieval returns nothing, with no error anywhere.
    const w = wf([{ agent: "a" }], {
      tools: { kb: { type: "kb", corpora: ["reference", "nope"] } },
    });
    expect(() => ok(w)).toThrow(/do not exist under orchestrator\/kb_docs\/: nope/);
  });

  it("rejects an agent corpus outside the declared corpora", () => {
    const w = wf([{ agent: "a" }], { tools: { kb: { type: "kb", corpora: ["reference"] } } });
    w.agents.a.corpus = "other";
    expect(() => ok(w)).toThrow(/which is not in\s+tools\.<kb>\.corpora/);
  });

  // --- authorization -------------------------------------------------------

  it("rejects restricted actions with idp=none", () => {
    // No authorizer means no claims, so every rule would deny everyone — locking
    // the UI out of the app it just deployed.
    const w = wf([{ agent: "a" }], {
      authorization: { actions: { decision: ["approvers"] } },
    });
    expect(() => ok(w, "none")).toThrow(/idp = "none" deploys the API with no authorizer/);
  });

  it("allows idp=none when nothing is restricted", () => {
    expect(() => ok(wf([{ agent: "a" }]), "none")).not.toThrow();
    expect(() =>
      ok(wf([{ agent: "a" }], { authorization: { actions: {} } }), "none")
    ).not.toThrow();
  });

  it("rejects an unknown action name", () => {
    // A typo looks like a restriction in the config but gates nothing, leaving the
    // real action wide open.
    const w = wf([{ agent: "a" }], {
      authorization: { actions: { aprove: ["approvers"] } },
    });
    expect(() => ok(w)).toThrow(/names unknown action\(s\): aprove/);
  });

  it("accepts every action bff/authz.py recognises", () => {
    const actions: Record<string, string[]> = {};
    for (const a of ["cancel", "decision", "delete", "evaluate", "insights", "rerun"]) {
      actions[a] = ["g"];
    }
    expect(() => ok(wf([{ agent: "a" }], { authorization: { actions } }))).not.toThrow();
  });
});

// ---------------------------------------------------------------------------
// authzGroups
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// validateBranches — content-based branching
// ---------------------------------------------------------------------------
//
// Every case here FAILS SILENTLY at runtime: a misspelled operator, a rule with no
// comparison, or a target naming nothing all evaluate to "no match", so the run
// quietly takes the default on every request and the branch looks like it works.
// The same checks live in app/common/branching.py and terraform/tools.tf; this is
// the CDK path's copy, so a customer sees them at synth rather than at container
// start.

describe("validateBranches", () => {
  const branchOn = (spec: any, rest: any[] = [{ agent: "b" }, { agent: "c" }]) =>
    wf([{ agent: "a", branch: spec }, ...rest]);
  const check = (w: any) => validateBranches(w.steps);

  it("accepts a well-formed branch", () => {
    expect(() =>
      check(
        branchOn({
          when: [
            { field: "disposition", equals: "escalate", goto: "b" },
            { field: "amount", gte: 10000, goto: "c" },
            { field: "claimId", exists: false, goto: "END" },
          ],
          default: "c",
        })
      )
    ).not.toThrow();
  });

  it("accepts `default` alone as an unconditional jump", () => {
    expect(() => check(branchOn({ default: "c" }))).not.toThrow();
  });

  it("rejects a branch on a parallel step", () => {
    // A group has no single agent whose output decides.
    expect(() =>
      check(wf([{ parallel: ["a", "b"], gateId: "g", branch: { default: "c" } }, { agent: "c" }]))
    ).toThrow(/not supported on a `parallel` step/);
  });

  it("rejects a branch on the last step", () => {
    expect(() => check(wf([{ agent: "a" }, { agent: "b", branch: { default: "a" } }]))).toThrow(
      /nowhere to route/
    );
  });

  it("rejects a branch with neither rules nor a default", () => {
    expect(() => check(branchOn({}))).toThrow(/needs `when`/);
  });

  it("rejects an empty rule list", () => {
    expect(() => check(branchOn({ when: [] }))).toThrow(/non-empty list of rules/);
  });

  it("rejects an unknown key on the branch or on a rule", () => {
    expect(() => check(branchOn({ whn: [{ equals: 1, goto: "b" }] }))).toThrow(/unknown key/);
    expect(() => check(branchOn({ when: [{ feild: "x", equals: 1, goto: "b" }] }))).toThrow(
      /unknown key\(s\) feild/
    );
  });

  it("rejects a misspelled operator rather than ignoring it", () => {
    // `gt3` leaves a rule with no comparison, so it never matches and every run
    // takes the default — a branch that looks wired and routes nothing.
    expect(() => check(branchOn({ when: [{ field: "amount", gt3: 10000, goto: "b" }] }))).toThrow(
      /unknown key\(s\) gt3/
    );
  });

  it("rejects a rule with no goto, and a rule with no comparison", () => {
    expect(() => check(branchOn({ when: [{ field: "x", equals: 1 }] }))).toThrow(/needs a `goto`/);
    expect(() => check(branchOn({ when: [{ field: "x", goto: "b" }] }))).toThrow(
      /has no comparison/
    );
  });

  it("rejects operator values of the wrong shape", () => {
    expect(() => check(branchOn({ when: [{ field: "x", in: "y", goto: "b" }] }))).toThrow(
      /`in` takes a list/
    );
    expect(() => check(branchOn({ when: [{ field: "x", exists: "yes", goto: "b" }] }))).toThrow(
      /`exists` takes true or false/
    );
    expect(() => check(branchOn({ when: [{ field: "x", gte: "big", goto: "b" }] }))).toThrow(
      /`gte` takes a number/
    );
    expect(() =>
      check(branchOn({ when: [{ field: "x", equals: ["p", "q"], goto: "b" }] }))
    ).toThrow(/takes a single value/);
  });

  it("rejects a target that names no step", () => {
    expect(() => check(branchOn({ default: "typo" }))).toThrow(/names no step/);
  });

  it("rejects a backward target, which would be a cycle", () => {
    expect(() =>
      check(wf([{ agent: "a" }, { agent: "b", branch: { default: "a" } }, { agent: "c" }]))
    ).toThrow(/at or before this one/);
    // Its own step counts as backward too.
    expect(() => check(branchOn({ default: "a" }))).toThrow(/at or before this one/);
  });

  it("names a group step by its gateId", () => {
    // A target names a STEP, and a group's name is its gateId — so jumping to a
    // parallel stage enters the whole stage rather than one member.
    expect(() =>
      check(
        wf([
          { agent: "a", branch: { default: "review" } },
          { agent: "b" },
          { parallel: ["p1", "p2"], hitl: true, gateId: "review" },
        ])
      )
    ).not.toThrow();
  });

  it("rejects duplicate step names once anything branches", () => {
    const dup = [
      { agent: "a", branch: { default: "dup" } },
      { parallel: ["b", "c"], gateId: "dup" },
      { sequence: ["d", "e"], gateId: "dup" },
    ];
    expect(() => check(wf(dup))).toThrow(/duplicate step name/);
    // Scoped to branching: without a branch nothing looks a step up by name, and
    // failing an existing workflow would be a gratuitous breaking change.
    expect(() => check(wf(dup.map(({ branch, ...s }) => s)))).not.toThrow();
  });

  it("is a no-op for a workflow with no branches", () => {
    expect(() => check(wf([{ agent: "a" }, { agent: "b" }]))).not.toThrow();
  });

  it("runs as part of validateWorkflow", () => {
    // The checks are worthless if the entry point does not call them.
    const w = wf([{ agent: "a", branch: { default: "typo" } }, { agent: "b" }]);
    expect(() => validateWorkflow(w, ORCH_ROOT, "multiagent_orchestrator", "none")).toThrow(
      /names no step/
    );
  });
});

describe("authzGroups", () => {
  it("returns every distinct group, sorted", () => {
    const w = {
      authorization: {
        actions: {
          decision: ["approvers"],
          cancel: ["operators", "approvers"],
          delete: ["operators"],
        },
      },
    };
    expect(authzGroups(w)).toEqual(["approvers", "operators"]);
  });

  it("returns nothing when there is no authorization block", () => {
    expect(authzGroups({})).toEqual([]);
    expect(authzGroups({ authorization: { actions: {} } })).toEqual([]);
  });

  it("ignores an action closed to everyone", () => {
    // An empty list denies the action; it names no group to create.
    expect(authzGroups({ authorization: { actions: { delete: [] } } })).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// buildBffWorkflow
// ---------------------------------------------------------------------------

describe("buildBffWorkflow", () => {
  it("emits tool and corpus, not the old mcp and rag keys", () => {
    // The live bug this pins: workflow.json renamed these, the projection did not,
    // so CDK deployments silently lost the UI's data-source chips.
    const w = wf([{ agent: "r" }], { tools: { kb: { type: "kb", corpora: ["reference"] } } });
    w.agents.r.tool = "kb";
    w.agents.r.corpus = "reference";
    const out = buildBffWorkflow(w);
    expect(out.agents.r.tool).toBe("kb");
    expect(out.agents.r.corpus).toBe("reference");
    expect(out.agents.r).not.toHaveProperty("mcp");
    expect(out.agents.r).not.toHaveProperty("rag");
  });

  it("derives the data-source chip from the tool's declared TYPE", () => {
    // So a new tool type gets a sensible label with no UI change.
    const tools = {
      kb: { type: "kb", corpora: ["reference"] },
      ws: { type: "websearch" },
      docs: { type: "mcp", endpoint: "https://x.test" },
      api: { type: "openapi", schemaS3Uri: "s3://b/s.json" },
    };
    const w = wf([{ parallel: ["a", "b", "c", "d"] }], { tools });
    Object.assign(w.agents.a, { tool: "kb", corpus: "reference" });
    Object.assign(w.agents.b, { tool: "ws" });
    Object.assign(w.agents.c, { tool: "docs" });
    Object.assign(w.agents.d, { tool: "api" });
    const out = buildBffWorkflow(w);
    expect(out.agents.a.source).toBe("Knowledge Base \u00b7 reference");
    expect(out.agents.b.source).toBe("Web Search");
    expect(out.agents.c.source).toBe("MCP \u00b7 docs");
    expect(out.agents.d.source).toBe("REST API \u00b7 api");
  });

  it("labels a kb tool with no corpus as 'all'", () => {
    const w = wf([{ agent: "a" }], { tools: { kb: { type: "kb", corpora: ["reference"] } } });
    w.agents.a.tool = "kb";
    expect(buildBffWorkflow(w).agents.a.source).toBe("Knowledge Base \u00b7 all");
  });

  it("derives a source for a toolless agent from its access[] wording", () => {
    const w = wf([{ parallel: ["a", "b", "c"] }]);
    w.agents.a.access = ["Session input (topic)"];
    w.agents.b.access = ["Upstream assets (orchestrator graph state)"];
    w.agents.c.access = ["Something else"];
    const out = buildBffWorkflow(w);
    expect(out.agents.a.source).toBe("Session input");
    expect(out.agents.b.source).toBe("Upstream agent outputs");
    expect(out.agents.c.source).toBe("\u2014");
  });

  it("lists only the agents whose evaluations are enabled", () => {
    const w = wf([{ parallel: ["a", "b"] }]);
    w.agents.a.agentcore = { evaluations: { enabled: true } };
    w.agents.b.agentcore = { evaluations: { enabled: false } };
    expect(buildBffWorkflow(w).evalAgents).toEqual(["a"]);
  });

  it("ships only the DISABLED chatbot tool flags", () => {
    // The backend defaults anything it is not told about to ON, so shipping the
    // "true" flags would waste bytes in a 4 KB environment.
    const w = wf([{ agent: "a" }], {
      orchestrator: { chatbot: { enabled: true, model: "m", tools: { costs: false, evals: true } } },
    });
    expect(buildBffWorkflow(w).chatbot).toEqual({
      enabled: true,
      model: "m",
      greeting: null,
      placeholder: null,
      tools: { costs: false },
    });
  });

  it("ships the assistant's greeting and placeholder", () => {
    // These were NOT shipped, which made two workflow.json keys decorative: a
    // customer reworded the assistant and the UI kept showing its own hardcoded
    // copy. The UI still has a short generic fallback for when they are omitted.
    const w = wf([{ agent: "a" }], {
      orchestrator: {
        chatbot: { enabled: true, greeting: "Hello there", placeholder: "Ask me…" },
      },
    });
    const cb = buildBffWorkflow(w).chatbot!;
    expect(cb.greeting).toBe("Hello there");
    expect(cb.placeholder).toBe("Ask me…");
  });

  it("emits a null chatbot when the block is absent", () => {
    expect(buildBffWorkflow(wf([{ agent: "a" }])).chatbot).toBeNull();
  });

  it("ships the authorization rules only when something is restricted", () => {
    const open = buildBffWorkflow(wf([{ agent: "a" }]));
    expect(open.authorization).toBeNull();

    const restricted = buildBffWorkflow(
      wf([{ agent: "a" }], {
        authorization: { groupsClaim: "cognito:groups", actions: { decision: ["approvers"] } },
      })
    );
    expect(restricted.authorization).toEqual({
      groupsClaim: "cognito:groups",
      actions: { decision: ["approvers"] },
    });
  });

  it("passes the steps through untouched, so the UI renders the real topology", () => {
    const steps = [
      { agent: "a", hitl: true },
      { parallel: ["b", "c"], hitl: true, gateId: "g" },
    ];
    expect(buildBffWorkflow(wf(steps)).steps).toEqual(steps);
  });

  it("drops the explanatory Note keys from the ui block", () => {
    const w = wf([{ agent: "a" }], {
      ui: { title: "T", heading: "H", uiNote: "for humans editing the file", empty: "" },
    });
    expect(buildBffWorkflow(w).ui).toEqual({ title: "T", heading: "H" });
  });
});

describe("stripForEnv", () => {
  it("drops Note keys and empty values but keeps false and 0", () => {
    expect(
      stripForEnv({ a: 1, bNote: "x", c: "", d: null, e: undefined, f: false, g: 0 })
    ).toEqual({ a: 1, f: false, g: 0 });
  });
});

// ---------------------------------------------------------------------------
// buildGuardrail
// ---------------------------------------------------------------------------

describe("buildGuardrail", () => {
  it("forces PROMPT_ATTACK output strength to NONE", () => {
    // Bedrock rejects an output strength on the prompt-attack filter, which is
    // input-only. Applying the configured strength to both would fail the deploy.
    const out = buildGuardrail({
      guardrail: { contentFilters: { PROMPT_ATTACK: "HIGH", HATE: "MEDIUM" } },
    });
    expect(out.contentFilters).toEqual([
      { Type: "PROMPT_ATTACK", InputStrength: "HIGH", OutputStrength: "NONE" },
      { Type: "HATE", InputStrength: "MEDIUM", OutputStrength: "MEDIUM" },
    ]);
  });

  it("rejects an invalid filter strength", () => {
    expect(() => buildGuardrail({ guardrail: { contentFilters: { HATE: "MAX" } } })).toThrow(
      /use NONE, LOW, MEDIUM or HIGH/
    );
  });

  it("rejects an invalid PII action", () => {
    expect(() => buildGuardrail({ guardrail: { piiEntities: { EMAIL: "MASK" } } })).toThrow(
      /use BLOCK or ANONYMIZE/
    );
  });

  it("requires a name and definition on every denied topic", () => {
    expect(() =>
      buildGuardrail({ guardrail: { deniedTopics: [{ name: "X" }] } })
    ).toThrow(/needs a "name" and a "definition"/);
  });

  it("omits Examples when a topic has none, and shapes the rest", () => {
    const out = buildGuardrail({
      guardrail: {
        deniedTopics: [
          { name: "A", definition: "d" },
          { name: "B", definition: "d", examples: ["e"] },
        ],
        deniedWords: ["w"],
        managedWordLists: ["profanity"],
        piiEntities: { email: "anonymize" },
      },
    });
    expect(out.deniedTopics).toEqual([
      { Name: "A", Definition: "d", Type: "DENY" },
      { Name: "B", Definition: "d", Examples: ["e"], Type: "DENY" },
    ]);
    expect(out.deniedWords).toEqual([{ Text: "w" }]);
    expect(out.managedWordLists).toEqual([{ Type: "PROFANITY" }]);
    expect(out.piiEntities).toEqual([{ Type: "EMAIL", Action: "ANONYMIZE" }]);
  });

  it("returns empty arrays for an absent guardrail block, plus default messages", () => {
    // The caller omits an empty policy entirely: Bedrock rejects an empty
    // *PolicyConfig rather than reading it as "no policy".
    const out = buildGuardrail({});
    expect(out.contentFilters).toEqual([]);
    expect(out.deniedTopics).toEqual([]);
    expect(out.piiEntities).toEqual([]);
    expect(out.blockedInputMessage).toMatch(/blocked by the content guardrail/);
  });
});

// ---------------------------------------------------------------------------
// cedarStatement
// ---------------------------------------------------------------------------

describe("cedarStatement", () => {
  const stmt = (spec: ToolSpec, name = "t") => cedarStatement(name, spec, GW);

  it("emits a TARGET-level permit when no tool name is known", () => {
    // Cedar has no wildcard actions, but a Gateway target IS an action group — the
    // only workable form for a remote MCP server whose tool names are unknown at
    // deploy time.
    const out = stmt({ type: "mcp", endpoint: "https://x.test" });
    expect(out).toContain('action in AgentCore::Action::"t"');
    expect(out).toContain(`resource == AgentCore::Gateway::"${GW}"`);
    expect(out).not.toContain("when");
  });

  it("permits web search BY NAME, not at target level", () => {
    // Measured on a live gateway: a target-level permit did NOT authorize it —
    // `action in AgentCore::Action::"websearch"` produced ToolDenied for
    // websearch___WebSearch.
    const out = cedarStatement("websearch", { type: "websearch" }, GW);
    expect(out).toContain('action == AgentCore::Action::"websearch___WebSearch"');
    expect(out).not.toContain("action in");
  });

  it("emits a named permit with the declared argument restriction", () => {
    const out = cedarStatement(
      "kb",
      { type: "kb", corpora: ["reference"], policy: { tool: "retrieve", restrictTo: { filter: ["reference"] } } },
      GW
    );
    expect(out).toContain('action == AgentCore::Action::"kb___retrieve"');
    expect(out).toContain("context.input has filter");
    expect(out).toContain('["reference"].contains(context.input.filter)');
  });

  it("ANDs several argument restrictions together, so all must hold", () => {
    const out = stmt({
      type: "kb",
      policy: { tool: "retrieve", restrictTo: { filter: ["a"], mode: ["x", "y"] } },
    } as ToolSpec);
    // Each restriction is a presence check AND a membership check, and the
    // restrictions are ANDed with each other — never ORed, which would let one
    // satisfied argument authorize a call that violated the other.
    expect(out).toContain('context.input has filter && ["a"].contains(context.input.filter)');
    expect(out).toContain('context.input has mode && ["x","y"].contains(context.input.mode)');
    expect(out).toContain("&&\n");
    expect(out).not.toContain("||");
  });

  it("always adds a condition, because an unconditioned permit is refused", () => {
    // The policy engine returns "Overly Permissive ... NotStabilized" for a permit
    // with no `when` on an unconstrained principal. So a tool with no restrictTo
    // still requires its query argument to be PRESENT — a real constraint.
    const out = stmt({ type: "mcp", endpoint: "https://x.test", policy: { tool: "search" } });
    expect(out).toContain("when {");
    expect(out).toContain("context.input has query");
  });

  it("names the query argument from the tool's own arg field", () => {
    const out = stmt({
      type: "mcp",
      endpoint: "https://x.test",
      arg: "search_phrase",
      policy: { tool: "aws___search_documentation" },
    });
    expect(out).toContain("context.input has search_phrase");
  });

  it("survives a doubly prefixed tool name", () => {
    // The Gateway composes "<target>___<tool>" and AWS also prefixes its managed
    // tools with "aws___", so the action id is legitimately double-prefixed.
    const out = stmt(
      { type: "mcp", endpoint: "https://x.test", policy: { tool: "aws___search_documentation" } },
      "docs"
    );
    expect(out).toContain('action == AgentCore::Action::"docs___aws___search_documentation"');
  });

  it("every statement it emits terminates with a semicolon", () => {
    for (const spec of [
      { type: "mcp", endpoint: "https://x.test" },
      { type: "websearch" },
      { type: "kb", policy: { tool: "retrieve", restrictTo: { filter: ["a"] } } },
    ] as ToolSpec[]) {
      expect(stmt(spec).trimEnd().endsWith(";")).toBe(true);
    }
  });
});
