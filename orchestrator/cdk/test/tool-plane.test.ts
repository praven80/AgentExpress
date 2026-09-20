/**
 * The ToolPlane construct, synthesized directly.
 *
 * `stack.test.ts` covers the whole stack against the shipped `workflow.json`, which
 * declares no `lambda` tool — a real `lambdaArn` is account-specific and would pin
 * the committed config to one AWS account. ToolPlane takes its `tools` as a prop,
 * though, so it can be instantiated with any tools block. That is what this file
 * does: it exercises the target shapes, the IAM grants and the Cedar permits for
 * configurations the sample does not itself ship.
 */

import * as cdk from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { aws_dynamodb as dynamodb } from "aws-cdk-lib";
import * as path from "path";

import { ToolPlane, ToolSpec } from "../lib/tool-plane";

const ORCH_ROOT = path.join(__dirname, "..", "..");
const ACCOUNT = "123456789012";

function plane(tools: Record<string, ToolSpec>, toolApiKeys: Record<string, string> = {}) {
  const app = new cdk.App();
  const stack = new cdk.Stack(app, "T", { env: { account: ACCOUNT, region: "us-east-1" } });
  new ToolPlane(stack, "ToolPlane", {
    agentName: "test_orch",
    tools,
    toolApiKeys,
    gatewayDiscoveryUrl:
      "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_abc/.well-known/openid-configuration",
    gatewayClientId: "client-abc",
    gatewayAudience: "gateway/invoke",
    isCognito: true,
    isAuth0: false,
    policyEnabled: true,
    policyMode: "ENFORCE",
    orchRoot: ORCH_ROOT,
  });
  return Template.fromStack(stack);
}

const QUERY_CLAIMS = {
  name: "query_claims",
  description: "Answer a question about claims using the warehouse.",
  properties: {
    question: { type: "string", required: true, description: "The question to answer." },
    limit: { type: "integer", required: false, description: "Max rows to consider." },
  },
};

const LAMBDA_TOOL: ToolSpec = {
  type: "lambda",
  description: "Read-only questions answered from the claims warehouse.",
  lambdaArn: `arn:aws:lambda:us-east-1:${ACCOUNT}:function:query-claims`,
  arg: "question",
  toolSchema: [QUERY_CLAIMS],
};

/** A statement's Resource, always as a list (CFN collapses a single element). */
function asList(v: any): any[] {
  return Array.isArray(v) ? v : [v];
}

/** All statements from every inline policy in the template, flattened. */
function statements(t: Template): any[] {
  return Object.values<any>(t.findResources("AWS::IAM::Policy")).flatMap(
    (p) => p.Properties.PolicyDocument.Statement
  );
}

function lambdaTarget(t: Template): any {
  const target = Object.values<any>(
    t.findResources("AWS::BedrockAgentCore::GatewayTarget")
  ).find((r) => r.Properties.TargetConfiguration?.Mcp?.Lambda);
  expect(target).toBeDefined();
  return target.Properties.TargetConfiguration.Mcp.Lambda;
}

function connectorTarget(t: Template): any {
  const target = Object.values<any>(
    t.findResources("AWS::BedrockAgentCore::GatewayTarget")
  ).find((r) => r.Properties.TargetConfiguration?.Mcp?.Connector);
  expect(target).toBeDefined();
  return target.Properties.TargetConfiguration.Mcp.Connector;
}

// The websearch connector target had NO coverage here at all, which is how collapsing
// four domain keys into `domains` passed this suite without a single change. A filter
// that silently stops being applied is the worst case for this particular key: results
// keep coming back and nothing says they are no longer scoped.
describe("type=websearch target", () => {
  it("applies `domains` on the TARGET, where the agent cannot reach it", () => {
    const connector = connectorTarget(
      plane({
        ws: {
          type: "websearch",
          description: "Managed web search.",
          domains: { include: ["docs.aws.amazon.com"], exclude: ["spam.example"] },
        },
      })
    );
    expect(connector.Source.ConnectorId).toBe("web-search");
    expect(connector.Configurations[0].ParameterValues).toEqual({
      domainFilter: { include: ["docs.aws.amazon.com"], exclude: ["spam.example"] },
    });
  });

  it("accepts either half on its own", () => {
    for (const domains of [{ include: ["a.example"] }, { exclude: ["b.example"] }]) {
      const values = connectorTarget(plane({ ws: { type: "websearch", domains } }))
        .Configurations[0].ParameterValues;
      expect(values.domainFilter).toEqual(domains);
    }
  });

  it("emits {} rather than an empty filter when no domains are configured", () => {
    // Not cosmetic: the API DISCARDS a configuration entry with no ParameterValues and
    // then reports "Connector configurations must not be empty", which reads as if the
    // list were absent rather than its single entry dropped.
    const connector = connectorTarget(plane({ ws: { type: "websearch" } }));
    expect(connector.Configurations[0].ParameterValues).toEqual({});
    expect(connector.Configurations[0].Name).toBe("WebSearch");
  });
});

describe("type=lambda target", () => {
  const template = plane({ claims: LAMBDA_TOOL });

  it("registers the function by the ARN from config", () => {
    expect(lambdaTarget(template).LambdaArn).toBe(LAMBDA_TOOL.lambdaArn);
  });

  it("publishes the declared tool with its description", () => {
    const payload = lambdaTarget(template).ToolSchema.InlinePayload;
    expect(payload).toHaveLength(1);
    expect(payload[0].Name).toBe("query_claims");
    expect(payload[0].Description).toBe(QUERY_CLAIMS.description);
  });

  it("turns per-property `required` into JSON Schema's object-level list", () => {
    // The config (and the Terraform provider's flattened `property` block) put
    // `required` on each property; JSON Schema puts it on the object as a list of
    // names. Both paths therefore accept the same JSON.
    //
    // The nested keys render PascalCase (Type/Description/Required) because CDK maps
    // the L1 property tree that way. That is the same shape the KB target has
    // produced all along, which is deployed and working — so it is the known-good
    // form rather than an assumption.
    const schema = lambdaTarget(template).ToolSchema.InlinePayload[0].InputSchema;
    expect(schema.Type).toBe("object");
    expect(Object.keys(schema.Properties).sort()).toEqual(["limit", "question"]);
    expect(schema.Properties.question).toEqual({
      Type: "string",
      Description: "The question to answer.",
    });
    expect(schema.Required).toEqual(["question"]);
  });

  it("produces the same target shape as the KB Lambda target", () => {
    // The KB target is the one Lambda target that has been deployed and exercised
    // end to end. Matching its shape is the strongest available evidence that a
    // customer's own function will register and invoke correctly.
    const lt = lambdaTarget(template);
    expect(Object.keys(lt).sort()).toEqual(["LambdaArn", "ToolSchema"]);
    expect(Object.keys(lt.ToolSchema)).toEqual(["InlinePayload"]);
    expect(Object.keys(lt.ToolSchema.InlinePayload[0]).sort()).toEqual([
      "Description",
      "InputSchema",
      "Name",
    ]);
  });

  it("lets the Gateway invoke the function with its own role", () => {
    // No secret is involved: the credential provider is GATEWAY_IAM_ROLE.
    const target = Object.values<any>(
      template.findResources("AWS::BedrockAgentCore::GatewayTarget")
    ).find((r) => r.Properties.TargetConfiguration?.Mcp?.Lambda);
    expect(target.Properties.CredentialProviderConfigurations).toEqual([
      { CredentialProviderType: "GATEWAY_IAM_ROLE" },
    ]);
  });

  it("grants lambda:InvokeFunction on exactly that function", () => {
    const invoke = statements(template).find((s) => s.Sid === "InvokeToolLambdas");
    expect(invoke).toBeDefined();
    expect(asList(invoke.Action)).toEqual(["lambda:InvokeFunction"]);
    expect(asList(invoke.Resource)).toEqual([LAMBDA_TOOL.lambdaArn]);
  });

  it("adds the function's resource policy statement for a same-account function", () => {
    template.hasResourceProperties("AWS::Lambda::Permission", {
      FunctionName: LAMBDA_TOOL.lambdaArn,
      Action: "lambda:InvokeFunction",
      Principal: "bedrock-agentcore.amazonaws.com",
      SourceAccount: ACCOUNT,
    });
  });

  it("emits a Cedar permit naming the declared tool", () => {
    const statement = Object.values<any>(
      template.findResources("AWS::BedrockAgentCore::Policy")
    )[0].Properties.Definition.Cedar.Statement;
    const text = (statement["Fn::Join"][1] as any[])
      .map((p) => (typeof p === "string" ? p : ""))
      .join("");
    // No `policy.tool`, so the whole target is permitted as an action group — right
    // for a function that may publish several tools.
    expect(text).toContain('action in AgentCore::Action::"claims"');
  });

  it("can be narrowed to one tool with an argument restriction", () => {
    const t = plane({
      claims: {
        ...LAMBDA_TOOL,
        policy: { tool: "query_claims", restrictTo: { region: ["emea", "apac"] } },
      },
    });
    const statement = Object.values<any>(t.findResources("AWS::BedrockAgentCore::Policy"))[0]
      .Properties.Definition.Cedar.Statement;
    const text = (statement["Fn::Join"][1] as any[])
      .map((p) => (typeof p === "string" ? p : ""))
      .join("");
    expect(text).toContain('action == AgentCore::Action::"claims___query_claims"');
    expect(text).toContain('["emea","apac"].contains(context.input.region)');
  });
});

describe("the built-in demo function (source: pricing)", () => {
  // This is what makes `type: "lambda"` demonstrable out of the box: the framework
  // deploys one function so the tool type has a live agent, without asking anyone to
  // stand up a database first. A customer's own function goes in via `lambdaArn` and
  // none of this applies to it.
  const BUILTIN: ToolSpec = {
    type: "lambda",
    source: "pricing",
    call: "aws_prices",
    arg: "services",
    toolSchema: [
      { name: "aws_prices", properties: { services: { type: "string", required: true } } },
    ],
  };

  function withTables() {
    const app = new cdk.App();
    const stack = new cdk.Stack(app, "T", { env: { account: ACCOUNT, region: "us-east-1" } });
    const status = new dynamodb.Table(stack, "Status", {
      partitionKey: { name: "session_id", type: dynamodb.AttributeType.STRING },
    });
    const telemetry = new dynamodb.Table(stack, "Telemetry", {
      partitionKey: { name: "session_id", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "sk", type: dynamodb.AttributeType.STRING },
    });
    new ToolPlane(stack, "ToolPlane", {
      agentName: "test_orch",
      tools: { pricing: BUILTIN },
      toolApiKeys: {},
      gatewayDiscoveryUrl: "https://example.test/.well-known/openid-configuration",
      gatewayClientId: "client-abc",
      gatewayAudience: "gateway/invoke",
      isCognito: true,
      isAuth0: false,
      policyEnabled: false,
      policyMode: "ENFORCE",
      orchRoot: ORCH_ROOT,
      statusTable: status,
      telemetryTable: telemetry,
    });
    return Template.fromStack(stack);
  }

  const template = withTables();

  it("deploys the function from orchestrator/app/tools/pricing/", () => {
    // Name prefixed with ToolLambda- so a scoped deploy policy can express it
    // without granting lambda:* on every function in the account.
    template.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: "ToolLambda-test_orch-pricing",
      Handler: "handler.lambda_handler",
      Runtime: "python3.12",
    });
  });

  it("pins the Price List API endpoint region, and passes nothing else", () => {
    // The Price List Query API is published in only two regions and describes prices
    // for all of them, so this is where the ENDPOINT lives — not the region being
    // priced. Pinned so the tool works on a deployment anywhere.
    const fn = Object.values<any>(template.findResources("AWS::Lambda::Function")).find(
      (f) => f.Properties.FunctionName === "ToolLambda-test_orch-pricing"
    );
    const vars = fn.Properties.Environment.Variables;
    expect(Object.keys(vars).sort()).toEqual(["PRICING_API_REGION"]);
    expect(vars.PRICING_API_REGION).toBe("us-east-1");
  });

  it("reads the PUBLIC price list and touches no customer data", () => {
    // This is what lets `source` deploy a framework-owned function at all: its
    // execution role is fixed and reads nothing belonging to the customer. A demo
    // function with access to the framework's own state would be a poor example.
    const actions = statements(template).flatMap((s) => asList(s.Action)).map(String);
    expect(actions).toContain("pricing:GetProducts");
    expect(actions).toContain("pricing:DescribeServices");
    expect(actions).not.toContain("pricing:*");
    // And NOTHING anywhere in this template touches the framework's own datastore.
    // The demo function used to hold a DynamoDB read grant on the run tables; it
    // reads the public price list now, so a `dynamodb:` action reappearing here
    // means a data-plane grant crept back into a framework-deployed function.
    expect(actions.filter((a) => /^dynamodb:/i.test(a))).toEqual([]);
  });

  it("registers the target against the function it just deployed", () => {
    // Not a literal ARN from config — a Fn::GetAtt on the function in this stack,
    // which is what keeps the committed workflow.json account-neutral.
    const arn = lambdaTarget(template).LambdaArn;
    expect(arn["Fn::GetAtt"][0]).toMatch(/ToolLambdapricing/);
    expect(arn["Fn::GetAtt"][1]).toBe("Arn");
  });

  it("grants the Gateway invoke on it and adds the resource policy", () => {
    const invoke = statements(template).find((s) => s.Sid === "InvokeToolLambdas");
    expect(invoke).toBeDefined();
    template.hasResourceProperties("AWS::Lambda::Permission", {
      Principal: "bedrock-agentcore.amazonaws.com",
      SourceAccount: ACCOUNT,
    });
  });

  it("needs no deployment tables at all", () => {
    // It reads only the public price list, so it deploys without being handed any
    // of this deployment's state. That is what makes the fixed execution role
    // behind `source` defensible: there is nothing of the customer's in it.
    expect(() => plane({ pricing: BUILTIN })).not.toThrow();
  });
});

describe("a cross-account lambda tool", () => {
  const CROSS = "arn:aws:lambda:us-east-1:999999999999:function:shared-tool";
  const template = plane({ shared: { ...LAMBDA_TOOL, lambdaArn: CROSS } });

  it("is still granted on the Gateway role", () => {
    const invoke = statements(template).find((s) => s.Sid === "InvokeToolLambdas");
    expect(asList(invoke.Resource)).toEqual([CROSS]);
  });

  it("gets NO resource-policy statement, because we cannot edit another account's", () => {
    // Emitting one would fail the deploy. The owning account adds it instead — which
    // is documented rather than silently required.
    const perms = Object.values<any>(template.findResources("AWS::Lambda::Permission")).map(
      (p) => p.Properties.FunctionName
    );
    expect(perms).not.toContain(CROSS);
  });
});

describe("several lambda tools", () => {
  const A = `arn:aws:lambda:us-east-1:${ACCOUNT}:function:tool-a`;
  const B = `arn:aws:lambda:us-east-1:${ACCOUNT}:function:tool-b`;
  const template = plane({
    b: { ...LAMBDA_TOOL, lambdaArn: B },
    a: { ...LAMBDA_TOOL, lambdaArn: A },
  });

  it("collects every function into ONE sorted IAM statement", () => {
    // One statement rather than one policy per tool, so the inline policy stays well
    // inside its size limit as the tool count grows. Sorted so the template is
    // stable and a no-op redeploy shows no diff.
    const invoke = statements(template).find((s) => s.Sid === "InvokeToolLambdas");
    expect(asList(invoke.Resource)).toEqual([A, B]);
  });

  it("creates a target and a permission for each", () => {
    template.resourceCountIs("AWS::BedrockAgentCore::GatewayTarget", 2);
    template.resourceCountIs("AWS::Lambda::Permission", 2);
  });
});

describe("one function publishing several tools", () => {
  const template = plane({
    warehouse: {
      ...LAMBDA_TOOL,
      call: "query_claims",
      toolSchema: [
        QUERY_CLAIMS,
        {
          name: "list_tables",
          properties: { schema: { type: "string", required: true } },
        },
      ],
    },
  });

  it("publishes both, so the handler can dispatch on the tool name", () => {
    const payload = lambdaTarget(template).ToolSchema.InlinePayload;
    expect(payload.map((p: any) => p.Name)).toEqual(["query_claims", "list_tables"]);
  });

  it("falls back to the tool name as its description", () => {
    const payload = lambdaTarget(template).ToolSchema.InlinePayload;
    expect(payload[1].Description).toBe("list_tables");
  });

  it("still grants only one function", () => {
    const invoke = statements(template).find((s) => s.Sid === "InvokeToolLambdas");
    expect(asList(invoke.Resource)).toEqual([LAMBDA_TOOL.lambdaArn]);
  });
});

describe("no lambda tools declared", () => {
  it("grants nothing and creates no permission", () => {
    // The grant is conditional, so a deployment with no lambda tool carries no
    // lambda:InvokeFunction for one.
    const template = plane({ docs: { type: "mcp", endpoint: "https://x.test/mcp" } });
    expect(statements(template).find((s) => s.Sid === "InvokeToolLambdas")).toBeUndefined();
    template.resourceCountIs("AWS::Lambda::Permission", 0);
  });
});

// ===========================================================================
// type=openapi
// ===========================================================================
// The shipped workflow.json now declares one of these, so stack.test.ts covers the
// `source` path end to end. What is here is the part that file cannot reach: the
// customer-hosted `schemaS3Uri` path, and the IAM grant that made the whole tool type
// work. That grant did not exist before — the Gateway role had no s3:GetObject at all,
// so an openapi target failed when the Gateway tried to LOAD its schema, with a message
// naming neither the bucket nor the permission. A tool type that cannot work is worse
// than one that is absent, because the config accepts it.
describe("type=openapi", () => {
  const SOURCE_TOOL: ToolSpec = {
    type: "openapi",
    description: "Product lifecycle dates, from a schema the framework uploads.",
    source: "lifecycle",
    call: "getProductLifecycle",
    arg: "product",
  };
  const HOSTED_TOOL: ToolSpec = {
    type: "openapi",
    description: "An API whose schema the customer already hosts.",
    schemaS3Uri: "s3://customer-schemas/orders/v2.json",
    call: "listOrders",
    arg: "query",
  };

  it("uploads a `source` schema and points the target at the derived URI", () => {
    // The whole reason `source` exists: a bucket name is as account-specific as a
    // function ARN, so a committed workflow.json cannot carry one. The URI is
    // therefore DERIVED, and the target must read the derived value rather than a
    // literal — otherwise the sample only deploys in the account that wrote it.
    const template = plane({ lifecycle: SOURCE_TOOL });
    template.hasResourceProperties("AWS::S3::Bucket", {
      BucketName: `agentcore-test-orch-schemas-${ACCOUNT}`,
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
      VersioningConfiguration: { Status: "Enabled" },
    });
    const target = Object.values<any>(
      template.findResources("AWS::BedrockAgentCore::GatewayTarget")
    )[0];
    const uri = target.Properties.TargetConfiguration.Mcp.OpenApiSchema.S3.Uri;
    // Built from the bucket ref, so it is a CFN join rather than a plain string.
    expect(JSON.stringify(uri)).toContain("lifecycle/openapi.json");
  });

  it("creates no bucket when every schema is customer-hosted", () => {
    // A deployment that hosts its own schemas should pay for nothing. The bucket is
    // conditional on some tool actually using `source`.
    const template = plane({ orders: HOSTED_TOOL });
    const buckets = Object.values<any>(template.findResources("AWS::S3::Bucket")).filter(
      (b) => String(b.Properties?.BucketName ?? "").includes("schemas")
    );
    expect(buckets).toHaveLength(0);
    const target = Object.values<any>(
      template.findResources("AWS::BedrockAgentCore::GatewayTarget")
    )[0];
    expect(target.Properties.TargetConfiguration.Mcp.OpenApiSchema.S3.Uri).toEqual(
      "s3://customer-schemas/orders/v2.json"
    );
  });

  it("grants the Gateway role GetObject on exactly the schema objects declared", () => {
    // Scoped to the object, not the bucket and not "*". A schema enumerates which
    // operations an agent may reach, so read access to all of them is a wider grant
    // than this tool type needs.
    const template = plane({ lifecycle: SOURCE_TOOL, orders: HOSTED_TOOL });
    const grant = statements(template).find((s) => s.Sid === "ReadOpenApiSchemas");
    expect(grant).toBeDefined();
    expect(grant.Action).toEqual("s3:GetObject");
    const resources = asList(grant.Resource);
    // The customer-hosted one is a literal ARN built from the parsed URI.
    expect(resources).toContain("arn:aws:s3:::customer-schemas/orders/v2.json");
    // The framework-uploaded one is a ref to the bucket it just created.
    expect(JSON.stringify(resources)).toContain("ToolSchemasBucket");
  });

  it("adds no grant at all when no openapi tool is declared", () => {
    const template = plane({ fn: LAMBDA_TOOL });
    expect(statements(template).find((s) => s.Sid === "ReadOpenApiSchemas")).toBeUndefined();
  });

  it("publishes a Cedar permit for an openapi tool like any other type", () => {
    // The tool type must not be a hole in authorization. `openapi` is the one type
    // whose tool NAMES come from the schema's operationIds rather than from config,
    // which is exactly the case where a permit could plausibly have been skipped.
    const template = plane({ lifecycle: SOURCE_TOOL });
    const policies = Object.values<any>(
      template.findResources("AWS::BedrockAgentCore::Policy")
    );
    expect(JSON.stringify(policies)).toContain("permit_lifecycle");
  });
});

// ===========================================================================
// type=kb — embedding model, dimensions, and the retrieval shape
// ===========================================================================
// All of this was hardcoded: the embedding model and dimension here and in
// terraform/kb.tf, and the retrieval request shape inside kb_lambda/handler.py. The
// dimension is the dangerous one to get wrong, because a model/dimension mismatch is not
// rejected at deploy — Bedrock fails at INGESTION afterwards, so the stack reports success
// and the corpus is silently empty. That is why the pair is validated at synth.
describe("type=kb config", () => {
  const kb = (over: Record<string, any> = {}): Record<string, ToolSpec> => ({
    kb: { type: "kb", description: "Corpus", corpora: ["reference"], ...over } as ToolSpec,
  });
  const kbLambdaEnv = (template: Template) =>
    Object.values<any>(template.findResources("AWS::Lambda::Function")).find((f) =>
      String(f.Properties.FunctionName ?? "").includes("KBRetrieve")
    ).Properties.Environment.Variables;

  it("defaults to Titan v2 at 1024 dimensions when nothing is declared", () => {
    // The previous hardcoded values, so an existing workflow.json deploys unchanged.
    const template = plane(kb());
    template.hasResourceProperties("AWS::S3Vectors::Index", { Dimension: 1024 });
    const knowledgeBase = Object.values<any>(
      template.findResources("AWS::Bedrock::KnowledgeBase")
    )[0];
    expect(
      JSON.stringify(knowledgeBase.Properties.KnowledgeBaseConfiguration)
    ).toContain("amazon.titan-embed-text-v2:0");
  });

  it("takes the model and dimension from config", () => {
    const template = plane(kb({ embeddingModel: "amazon.titan-embed-text-v2:0", dimensions: 256 }));
    template.hasResourceProperties("AWS::S3Vectors::Index", { Dimension: 256 });
  });

  it("derives the index and KB names from the dimension, so a change REPLACES them", () => {
    // Neither the dimension nor the model can be altered in place, and CloudFormation
    // refuses to replace a resource with a fixed custom name. Digest-derived names are
    // what make changing the embedding model a working deploy instead of a stuck one.
    const nameOf = (t: Template, type: string, prop: string) =>
      Object.values<any>(t.findResources(type))[0].Properties[prop];
    const a = plane(kb({ dimensions: 1024 }));
    const b = plane(kb({ dimensions: 256 }));
    expect(nameOf(a, "AWS::S3Vectors::Index", "IndexName")).not.toEqual(
      nameOf(b, "AWS::S3Vectors::Index", "IndexName")
    );
    expect(nameOf(a, "AWS::Bedrock::KnowledgeBase", "Name")).not.toEqual(
      nameOf(b, "AWS::Bedrock::KnowledgeBase", "Name")
    );
  });

  it("refuses a dimension the chosen model does not support", () => {
    // Caught at synth because the alternative is a green deploy and an empty corpus.
    expect(() => plane(kb({ embeddingModel: "cohere.embed-english-v3", dimensions: 256 }))).toThrow(
      /does not support .* accepts \[1024\]/s
    );
    // Titan v1 is 1536-only, so even the usual default is wrong for it — which is the
    // case a per-model dimension table exists to catch.
    expect(() => plane(kb({ embeddingModel: "amazon.titan-embed-text-v1", dimensions: 1024 }))).toThrow(
      /does not support/
    );
    expect(() => plane(kb({ embeddingModel: "amazon.titan-embed-text-v1" }))).not.toThrow();
  });

  it("passes the retrieval shape to the Lambda, with documented defaults", () => {
    const env = kbLambdaEnv(plane(kb()));
    expect(env.KB_CORPUS_KEY).toEqual("doc_type");
    expect(env.KB_CORPUS_OPERATOR).toEqual("equals");
    // Empty means "keep the handler's default", so the IaC does not restate a default
    // that already has one home. Mirrors terraform/kb.tf.
    expect(env.KB_STATIC_FILTER).toEqual("");
    expect(env.KB_RERANK).toEqual("");
  });

  it("passes a configured corpus key and target filter through", () => {
    const filter = { andAll: [{ equals: { key: "tier", value: "public" } }] };
    const env = kbLambdaEnv(
      plane(kb({ corpusKey: "product_line", corpusOperator: "startsWith", filter }))
    );
    expect(env.KB_CORPUS_KEY).toEqual("product_line");
    expect(env.KB_CORPUS_OPERATOR).toEqual("startsWith");
    expect(JSON.parse(env.KB_STATIC_FILTER)).toEqual(filter);
  });

  it("grants the reranking model only when reranking is configured, and only that model", () => {
    // Reranking is a second model call Bedrock makes on the function's behalf, so the
    // function's own role needs it — but granting foundation-model/* to a retrieve Lambda
    // would hand it every model in the account.
    const withRerank = plane(kb({ rerank: { model: "amazon.rerank-v1:0", count: 3 } }));
    const grant = statements(withRerank).find((s) => s.Sid === "InvokeRerankingModel");
    expect(grant).toBeDefined();
    expect(asList(grant.Resource)[0]).toContain("foundation-model/amazon.rerank-v1:0");
    expect(JSON.stringify(grant.Resource)).not.toContain("foundation-model/*");

    expect(statements(plane(kb())).find((s) => s.Sid === "InvokeRerankingModel")).toBeUndefined();
  });
});
