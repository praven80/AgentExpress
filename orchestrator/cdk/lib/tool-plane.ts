import * as crypto from "crypto";
import * as fs from "fs";
import * as path from "path";
import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import {
  aws_dynamodb as dynamodb,
  aws_iam as iam,
  aws_lambda as lambda,
  aws_logs as logs,
  aws_s3 as s3,
  aws_s3_deployment as s3deploy,
  aws_s3vectors as s3vectors,
  aws_bedrock as bedrock,
  aws_bedrockagentcore as agentcore,
  custom_resources as cr,
} from "aws-cdk-lib";

/** The tool types the framework knows how to provision. */
export type ToolType = "kb" | "websearch" | "mcp" | "openapi" | "lambda";

/** One tool published by a `type: "lambda"` target. */
export interface LambdaToolDef {
  name: string;
  description?: string;
  /** Argument name -> its schema. `required` is per-property, JSON-Schema style. */
  properties: Record<
    string,
    { type?: string; required?: boolean; description?: string }
  >;
}

/**
 * One entry from the `tools` block in app/workflow.json — the single place a
 * customer declares a data source. The KEY is the Gateway target name AND the
 * label an agent references via its `tool` field.
 */
export interface ToolSpec {
  type: ToolType;
  description?: string;
  /** type=kb: top-level folders under kb_docs/, each a filterable doc_type. */
  corpora?: string[];
  /**
   * Which field of this target's result ROWS carries which role, for an agent that
   * consumes the tool's data rather than its prose rendering (ctx.call_tool_rows).
   *
   * Optional, and only a DETERMINISTIC agent needs it — one whose output is a
   * transformation of the rows (counting them, quoting their fields) rather than a
   * model's reading of them. It is what keeps such an agent config-driven: repoint
   * the tool at another Lambda, warehouse or API and set these to its field names,
   * with no agent code change. Without it the agent would have to know one target's
   * field names and swapping the target would need an edit.
   *
   * Keys are the roles the agent asks for; values are this target's field names:
   *   { "id": "sessionId", "label": "topic", "outcome": "overall", "timestamp": "created" }
   */
  rowFields?: Record<string, string>;
  /** type=mcp: the MCP server's Streamable HTTP URL. Change this, nothing else. */
  endpoint?: string;
  /** type=openapi: s3:// URI of the OpenAPI schema. */
  schemaS3Uri?: string;
  /**
   * type=lambda: the ARN of an EXISTING function. The framework registers it; it
   * does not create or deploy it. This is the general-purpose escape hatch for
   * anything the Gateway cannot reach directly — a database (Redshift, Snowflake,
   * any RDBMS), an internal service, a resource inside a VPC.
   */
  lambdaArn?: string;
  /**
   * type=lambda alternative to `lambdaArn`: name a function the FRAMEWORK ships and
   * deploys, so the committed config stays account-neutral (a real ARN would pin
   * workflow.json to one AWS account).
   *
   * Exactly one built-in exists — "tool_lambda", the run-history demo under
   * orchestrator/tool_lambda/ — and the validator accepts no other value. This is
   * deliberately NOT a general "deploy any directory" feature: a framework-deployed
   * function needs an execution role that config cannot express. A function of your
   * own goes in via `lambdaArn`, and the framework then touches neither its code nor
   * its role.
   */
  source?: string;
  /**
   * type=lambda: the tools this function publishes. DECLARED, not discovered —
   * unlike an MCP server there is no tools/list for the Gateway to call. One
   * function may publish several; the Gateway passes the tool name through so the
   * handler can dispatch on it (set `call` to say which one an agent invokes).
   */
  toolSchema?: LambdaToolDef[];
  /**
   * type=websearch: TARGET-level domain lists. Hidden from the calling agent and
   * applied to every request — the enforceable layer, unlike the request-level
   * includeDomains/excludeDomains the app sends per call.
   */
  targetIncludeDomains?: string[];
  targetExcludeDomains?: string[];
  /** type=websearch: pin the connector version, e.g. "1.2.0". */
  connectorVersion?: string;
  /** type=websearch: request-level published-date bounds (ISO-8601 UTC). */
  publishedFrom?: string;
  publishedTo?: string;
  /** type=websearch: results per call (1-25). */
  maxResults?: number;
  /**
   * Which tool on the target to invoke. A target may publish several (the AWS
   * Documentation MCP server publishes five), in which case this is required.
   */
  call?: string;
  /** The parameter the query goes into. Default "query". */
  arg?: string;
  /** Fixed extra arguments sent on every call to this tool. */
  args?: Record<string, unknown>;
  /**
   * How the Gateway discovers the server's tools.
   *   DEFAULT — synchronise + cache the catalog at create/update time.
   *   DYNAMIC — forward tools/list to the server at invocation time instead.
   * DYNAMIC avoids a stale catalogue when a server's tool set changes. When
   * verifying with tools/list, follow nextCursor — the response is PAGINATED, and a
   * target whose tools sit on page two otherwise looks empty.
   */
  listingMode?: "DEFAULT" | "DYNAMIC";
  /**
   * OUTBOUND auth from the Gateway to the endpoint.
   *   "apikey" — vaulted key sent as X-API-Key (supply it via $TOOL_API_KEYS)
   *   "sigv4"  — the Gateway signs with its own execution role, so there is no
   *              secret. Only works behind a service that verifies SigV4:
   *              AgentCore Runtime/Gateway, API Gateway, Lambda Function URLs.
   */
  auth?: "none" | "apikey" | "sigv4";
  /** SigV4 signing name; auto-detected from the hostname when omitted. */
  service?: string;
  includeDomains?: string[];
  excludeDomains?: string[];
  /** Cedar shape. Omit for a target-level permit (all the target's tools). */
  policy?: {
    /** Narrow the permit to ONE tool name within this target. */
    tool?: string;
    /** Argument allow-lists, e.g. { filter: ["reference"] }. */
    restrictTo?: Record<string, string[]>;
    /** false = register the target but do NOT permit it (default-deny demo). */
    permit?: boolean;
  };
}

export interface ToolPlaneProps {
  agentName: string;
  /** The parsed `tools` block from workflow.json. */
  tools: Record<string, ToolSpec>;
  /** API keys by tool name, from $TOOL_API_KEYS. Never from workflow.json. */
  toolApiKeys: Record<string, string>;
  /** OIDC discovery URL of the configured IdP. */
  gatewayDiscoveryUrl: string;
  gatewayClientId: string;
  gatewayAudience: string;
  isCognito: boolean;
  isAuth0: boolean;
  /** Cedar policy engine on/off (workflow.json orchestrator.policy.enabled). */
  policyEnabled: boolean;
  /** "ENFORCE" | "LOG_ONLY". */
  policyMode: string;
  /** orchestrator/ root, for the kb_docs corpus and the kb_lambda / tool_lambda source. */
  orchRoot: string;
  /**
   * This deployment's own run-data tables. Needed only by the built-in
   * `source: "tool_lambda"` demo function, which reports on run history and is
   * granted READ-ONLY access to them. Omit when no tool asks for it.
   */
  statusTable?: dynamodb.ITable;
  telemetryTable?: dynamodb.ITable;
}

/**
 * The Gateway-backed TOOL PLANE — the CDK counterpart to terraform/tools.tf +
 * gateway.tf + kb.tf + policy.tf.
 *
 * Everything here is GENERATED from the `tools` block in app/workflow.json:
 * one Gateway target per entry, a vaulted credential provider for any entry with
 * an API key, and the Cedar permits that authorize exactly those tools. A
 * customer adds a data source by adding a JSON block — no TypeScript edits.
 */
export class ToolPlane extends Construct {
  /** MCP endpoint the runtimes call (GATEWAY_URL). */
  public readonly gatewayUrl: string;
  public readonly gatewayId: string;
  /** tool name -> ARN, for each function the framework deployed from `source`. */
  private readonly builtinLambdaArns: Record<string, string> = {};
  public readonly gatewayArn: string;
  /** Policy mode to report in the UI, "" when policy is off. */
  public readonly policyModeEnv: string;
  /** Empty when no tool declares type=kb. */
  public readonly knowledgeBaseId: string;

  constructor(scope: Construct, id: string, props: ToolPlaneProps) {
    super(scope, id);

    const stack = cdk.Stack.of(this);
    const { region, account } = stack;
    const { agentName, orchRoot, tools } = props;
    const dashName = agentName.replace(/_/g, "-");

    const KB_DIMS = 1024; // Titan Text Embeddings V2 default
    const embedModel = `arn:aws:bedrock:${region}::foundation-model/amazon.titan-embed-text-v2:0`;

    // ---- Split the declared tools by type ---------------------------------
    const entries = Object.entries(tools);
    const byType = (t: ToolType) => entries.filter(([, v]) => v.type === t);
    const kbEntry = byType("kb")[0];
    const kbName = kbEntry?.[0] ?? "";

    // ======================================================================
    // Gateway service role
    // ======================================================================
    const gatewayRole = new iam.Role(this, "GatewayRole", {
      roleName: `AgentCoreGateway-${agentName}`,
      assumedBy: new iam.ServicePrincipal("bedrock-agentcore.amazonaws.com", {
        conditions: {
          StringEquals: { "aws:SourceAccount": account },
          ArnLike: { "aws:SourceArn": `arn:aws:bedrock-agentcore:${region}:${account}:*` },
        },
      }),
    });
    gatewayRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
        resources: [`arn:aws:logs:${region}:${account}:*`],
      })
    );
    // Needed when a target uses an API-key / OAuth credential provider.
    gatewayRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["secretsmanager:GetSecretValue"],
        resources: [`arn:aws:secretsmanager:${region}:${account}:secret:bedrock-agentcore*`],
      })
    );
    // Read + evaluate the attached Cedar engine on every tool call.
    // AuthorizeAction is checked against BOTH the engine and the gateway.
    gatewayRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "PolicyEngineEvaluate",
        actions: [
          "bedrock-agentcore:GetPolicyEngine",
          "bedrock-agentcore:ListPolicies",
          "bedrock-agentcore:GetPolicy",
          "bedrock-agentcore:*Authorize*",
        ],
        resources: [
          `arn:aws:bedrock-agentcore:${region}:${account}:policy-engine/*`,
          `arn:aws:bedrock-agentcore:${region}:${account}:gateway/*`,
        ],
      })
    );

    // OUTBOUND permission for the managed web-search connector. The connector runs
    // inside AWS and the Gateway reaches it as itself, so without this a call fails
    // at INVOKE time (not at deploy time) with
    //   -32002 "Execution role is not authorized for connector web-search"
    // Granted only when a tools entry actually declares type=websearch.
    if (Object.values(props.tools).some((t) => t.type === "websearch")) {
      gatewayRole.addToPolicy(
        new iam.PolicyStatement({
          // Scoped to the AWS-OWNED tool ARN exactly as documented (note the
          // literal "aws" where the account id would normally be — authorization
          // is enforced per invocation against that ARN). This was "*" before,
          // which worked but granted more than the docs call for.
          sid: "InvokeWebSearch",
          actions: ["bedrock-agentcore:InvokeWebSearch"],
          resources: [`arn:aws:bedrock-agentcore:${region}:aws:tool/web-search.v1`],
        })
      );
      // The documented Web Search service-role policy pairs InvokeWebSearch with
      // InvokeGateway on the gateway.
      //
      // Scoped to gateway/* in this account and region rather than the exact ARN.
      // Using the concrete ARN creates a CloudFormation CIRCULAR DEPENDENCY: the
      // Gateway must wait for this role's policy to exist (dependOnDefaultPolicy
      // below), so the policy cannot in turn reference the Gateway. The existing
      // PolicyEngineEvaluate statement is scoped the same way for the same reason.
      gatewayRole.addToPolicy(
        new iam.PolicyStatement({
          sid: "InvokeGateway",
          actions: ["bedrock-agentcore:InvokeGateway"],
          resources: [`arn:aws:bedrock-agentcore:${region}:${account}:gateway/*`],
        })
      );
    }

    // ======================================================================
    // Cedar policy engine (before the Gateway, so it can be attached)
    // ======================================================================
    let policyEngine: agentcore.CfnPolicyEngine | undefined;
    if (props.policyEnabled) {
      policyEngine = new agentcore.CfnPolicyEngine(this, "PolicyEngine", {
        name: `${agentName}_policy`,
        description: "Cedar policy engine governing AgentCore Gateway tool access",
      });
    }
    this.policyModeEnv = props.policyEnabled ? props.policyMode : "";

    // ======================================================================
    // Gateway
    // ======================================================================
    // Inbound auth is provider-specific because the token formats differ:
    // Cognito client-credentials tokens carry `client_id` + `scope` and no
    // `aud`, so pin allowedClients. Auth0 M2M tokens are the mirror image —
    // `aud` + `azp`, no `client_id` — so pin the audience and the `azp` claim.
    const gateway = new agentcore.CfnGateway(this, "Gateway", {
      name: `${dashName}-gw`,
      roleArn: gatewayRole.roleArn,
      protocolType: "MCP",
      authorizerType: "CUSTOM_JWT",
      authorizerConfiguration: {
        customJwtAuthorizer: {
          discoveryUrl: props.gatewayDiscoveryUrl,
          ...(props.isCognito ? { allowedClients: [props.gatewayClientId] } : {}),
          ...(props.isAuth0
            ? {
                allowedAudience: [props.gatewayAudience],
                customClaims: [
                  {
                    inboundTokenClaimName: "azp",
                    inboundTokenClaimValueType: "STRING",
                    authorizingClaimMatchValue: {
                      claimMatchOperator: "EQUALS",
                      claimMatchValue: { matchValueString: props.gatewayClientId },
                    },
                  },
                ],
              }
            : {}),
        },
      },
      ...(policyEngine
        ? {
            policyEngineConfiguration: {
              arn: policyEngine.attrPolicyEngineArn,
              mode: props.policyMode,
            },
          }
        : {}),
    });
    gateway.node.addDependency(gatewayRole);

    this.gatewayUrl = gateway.attrGatewayUrl;
    this.gatewayId = gateway.attrGatewayIdentifier;
    this.gatewayArn = gateway.attrGatewayArn;



    // ======================================================================
    // Knowledge Base (only when a tool declares type=kb)
    // ======================================================================
    let kb: bedrock.CfnKnowledgeBase | undefined;
    let kbLambda: lambda.Function | undefined;
    this.knowledgeBaseId = "";

    if (kbName) {
      const vectorBucketName = `agentcore-${dashName}-kb-${account}`;
      const vectorBucket = new s3vectors.CfnVectorBucket(this, "KbVectorBucket", {
        vectorBucketName,
      });

      const vectorIndex = new s3vectors.CfnIndex(this, "KbVectorIndex", {
        // The NAME, not vectorBucket.ref: Ref on AWS::S3Vectors::VectorBucket
        // returns the ARN, and CfnIndex caps VectorBucketName at 63 chars, so
        // the ref fails validation at DEPLOY time (synth cannot catch it):
        //   #/VectorBucketName: expected maxLength: 63, actual: 97
        // The explicit addDependency below preserves the ordering the ref implied.
        vectorBucketName,
        // The name carries a digest of this index's IMMUTABLE properties (see
        // kbIndexName). It was the fixed literal "kb-index", and changing any
        // immutable property then failed the deploy outright:
        //   "CloudFormation cannot update a stack when a custom-named resource
        //    requires replacing. Rename ... and update the stack again."
        // Deriving the name means a change to those properties yields a NEW name,
        // so CloudFormation can replace the index cleanly. Both the Knowledge Base
        // and Terraform reference the index by ARN, so the rename propagates.
        indexName: kbIndexName(KB_DIMS, KB_NON_FILTERABLE),
        dataType: "float32",
        dimension: KB_DIMS,
        distanceMetric: "cosine",
        // S3 Vectors caps FILTERABLE metadata at 2048 bytes per vector, and both
        // of these grow with the document, so both must be excluded:
        //   AMAZON_BEDROCK_TEXT     — the chunk's own text.
        //   AMAZON_BEDROCK_METADATA — a JSON blob carrying `text`, `parentText`,
        //                             the source location and a document id.
        //
        // Only TEXT was listed here, which is why ingestion silently accepted the
        // two small sample documents and then FAILED on a larger one with
        //   "Invalid record ...: Filterable metadata must have at most 2048 bytes
        //    (Service: S3Vectors, Status Code: 400)"
        // — one document failed, the job went to FAILED, and the KB simply never
        // returned that content. Nothing filters on either key (the only filter
        // this framework uses is `doc_type`), so excluding both costs nothing.
        metadataConfiguration: { nonFilterableMetadataKeys: KB_NON_FILTERABLE },
      });
      vectorIndex.node.addDependency(vectorBucket);

      const docsBucket = new s3.Bucket(this, "KbDocsBucket", {
        bucketName: `agentcore-${dashName}-kbdocs-${account}`,
        blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
        autoDeleteObjects: true,
      });

      // Per-document metadata sidecars. Bedrock reads "<key>.metadata.json"
      // beside each source object and attaches its attributes to every chunk.
      // doc_type = the document's TOP-LEVEL FOLDER, which is what lets an agent
      // (and the Cedar permit) scope retrieval to one corpus.
      const kbDocsDir = path.join(orchRoot, "kb_docs");
      const kbFiles = listFilesRecursive(kbDocsDir).filter((f) => !f.endsWith(".DS_Store"));
      const sidecars = kbFiles.map((rel) =>
        s3deploy.Source.data(
          `${rel}.metadata.json`,
          JSON.stringify({ metadataAttributes: { doc_type: rel.split("/")[0] } })
        )
      );

      const docsDeployment = new s3deploy.BucketDeployment(this, "KbDocsDeploy", {
        logGroup: new logs.LogGroup(this, "KbDocsDeployLogGroup", {
          retention: LOG_RETENTION,
          removalPolicy: cdk.RemovalPolicy.DESTROY,
        }),
        destinationBucket: docsBucket,
        sources: [
          s3deploy.Source.asset(kbDocsDir, { exclude: [".DS_Store", "**/.DS_Store"] }),
          ...sidecars,
        ],
      });

      const kbRole = new iam.Role(this, "KbRole", {
        roleName: `AgentCoreKB-${agentName}`,
        assumedBy: new iam.ServicePrincipal("bedrock.amazonaws.com", {
          conditions: { StringEquals: { "aws:SourceAccount": account } },
        }),
      });
      kbRole.addToPolicy(
        new iam.PolicyStatement({ sid: "Embeddings", actions: ["bedrock:InvokeModel"], resources: [embedModel] })
      );
      kbRole.addToPolicy(
        new iam.PolicyStatement({
          sid: "S3VectorsData",
          // The verbs a Knowledge Base uses. The wildcard also granted DeleteIndex and
          // DeleteVectorBucket, which a KB never calls. Mirrors terraform/kb.tf.
          actions: [
            "s3vectors:GetVectorBucket", "s3vectors:GetIndex", "s3vectors:ListIndexes",
            "s3vectors:PutVectors", "s3vectors:GetVectors", "s3vectors:ListVectors",
            "s3vectors:QueryVectors", "s3vectors:DeleteVectors",
          ],
          resources: [vectorBucket.attrVectorBucketArn, `${vectorBucket.attrVectorBucketArn}/*`],
        })
      );
      kbRole.addToPolicy(
        new iam.PolicyStatement({
          sid: "DocsRead",
          actions: ["s3:GetObject", "s3:ListBucket"],
          resources: [docsBucket.bucketArn, `${docsBucket.bucketArn}/*`],
        })
      );

      kb = new bedrock.CfnKnowledgeBase(this, "KnowledgeBase", {
        // Digest-derived for the same reason as the index name, one level up: the
        // KB's storageConfiguration points at the index ARN and is immutable, so
        // replacing the index replaces the KB. See knowledgeBaseName.
        name: knowledgeBaseName(dashName, KB_DIMS, KB_NON_FILTERABLE),
        roleArn: kbRole.roleArn,
        knowledgeBaseConfiguration: {
          type: "VECTOR",
          vectorKnowledgeBaseConfiguration: {
            embeddingModelArn: embedModel,
            embeddingModelConfiguration: {
              bedrockEmbeddingModelConfiguration: {
                dimensions: KB_DIMS,
                embeddingDataType: "FLOAT32",
              },
            },
          },
        },
        storageConfiguration: {
          type: "S3_VECTORS",
          s3VectorsConfiguration: { indexArn: vectorIndex.attrIndexArn },
        },
      });
      // Bedrock validates the role's permissions at CREATE time, so the inline
      // POLICY (not just the role) must exist first.
      kb.node.addDependency(kbRole);
      dependOnDefaultPolicy(kb, kbRole);
      kb.node.addDependency(vectorIndex);
      this.knowledgeBaseId = kb.attrKnowledgeBaseId;

      const dataSource = new bedrock.CfnDataSource(this, "KbDataSource", {
        knowledgeBaseId: kb.attrKnowledgeBaseId,
        name: "docs",
        dataSourceConfiguration: {
          type: "S3",
          s3Configuration: { bucketArn: docsBucket.bucketArn },
        },
      });

      // StartIngestionJob has no declarative equivalent. The corpus hash is baked
      // into the physical id, so editing kb_docs/ re-ingests on the next deploy
      // and nothing else does.
      const corpusHash = hashCorpus(kbDocsDir, kbFiles);
      const ingestCall = {
        service: "bedrock-agent",
        action: "StartIngestionJob",
        parameters: {
          knowledgeBaseId: kb.attrKnowledgeBaseId,
          dataSourceId: dataSource.attrDataSourceId,
        },
        physicalResourceId: cr.PhysicalResourceId.of(`kb-ingest-${corpusHash}`),
      };
      const ingest = new cr.AwsCustomResource(this, "KbIngest", {
        logGroup: new logs.LogGroup(this, "KbIngestLogGroup", {
          retention: LOG_RETENTION,
          removalPolicy: cdk.RemovalPolicy.DESTROY,
        }),
        onCreate: ingestCall,
        onUpdate: ingestCall,
        policy: cr.AwsCustomResourcePolicy.fromStatements([
          new iam.PolicyStatement({
            actions: ["bedrock:StartIngestionJob"],
            resources: [kb.attrKnowledgeBaseArn],
          }),
        ]),
        installLatestAwsSdk: false,
      });
      ingest.node.addDependency(docsDeployment);
      ingest.node.addDependency(dataSource);

      kbLambda = new lambda.Function(this, "KbRetrieve", {
        // Owned explicitly so `destroy` removes it — see LOG_RETENTION in
        // lib/orchestrator-stack.ts for why an implicit group is a cost leak.
        logGroup: new logs.LogGroup(this, "KbRetrieveLogGroup", {
          logGroupName: `/aws/lambda/AgentCoreKBRetrieve-${agentName}`,
          retention: LOG_RETENTION,
          removalPolicy: cdk.RemovalPolicy.DESTROY,
        }),
        functionName: `AgentCoreKBRetrieve-${agentName}`,
        runtime: lambda.Runtime.PYTHON_3_13,
        handler: "handler.lambda_handler",
        code: lambda.Code.fromAsset(path.join(orchRoot, "kb_lambda")),
        timeout: cdk.Duration.seconds(30),
        memorySize: 256,
        environment: {
          KB_ID: kb.attrKnowledgeBaseId,
          // Retrieval depth, from `tools.<kb>.maxResults` in app/workflow.json. It was
          // a Lambda-only env var that NO IaC set, so depth was frozen at the
          // handler's default of 5 and the only way to change it was hand-editing a
          // deployed function — while `tools.<websearch>.maxResults` was a config key.
          // Same key name on both tool types now. Mirrors local.kb_max_results in
          // terraform/tools.tf.
          KB_NUM_RESULTS: String(kbEntry?.[1]?.maxResults ?? 5),
        },
      });
      kbLambda.addToRolePolicy(
        new iam.PolicyStatement({ actions: ["bedrock:Retrieve"], resources: [kb.attrKnowledgeBaseArn] })
      );
      gatewayRole.addToPolicy(
        new iam.PolicyStatement({ actions: ["lambda:InvokeFunction"], resources: [kbLambda.functionArn] })
      );
      kbLambda.addPermission("AllowAgentCoreGatewayInvoke", {
        principal: new iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
        action: "lambda:InvokeFunction",
        sourceAccount: account,
      });
    }

    // ---- type=lambda ------------------------------------------------------
    // Mirrors the tool_lambda resources + aws_iam_role_policy.gateway_invoke_lambda
    // + aws_lambda_permission.gateway_invoke_tool_lambda in terraform/tools.tf.
    //
    // Two ways a function arrives: `source` (one the framework ships and deploys)
    // or `lambdaArn` (one you already own, which is only registered).
    const lambdaTools = entries.filter(([, s]) => s.type === "lambda");
    if (lambdaTools.length) {
      // The framework-deployed built-in. Its execution role is FIXED — logs plus
      // READ-ONLY on the PUBLIC AWS price list — which is exactly why `source`
      // accepts no value other than "tool_lambda": a framework-deployed function
      // needs an execution role that config cannot express, so the one role that
      // exists reads nothing belonging to the customer.
      for (const [name, spec] of lambdaTools.filter(([, s]) => s.source)) {
        const fn = new lambda.Function(this, `ToolLambda-${name}`, {
          logGroup: new logs.LogGroup(this, `ToolLambdaLogGroup-${name}`, {
            logGroupName: `/aws/lambda/ToolLambda-${agentName}-${name}`,
            retention: LOG_RETENTION,
            removalPolicy: cdk.RemovalPolicy.DESTROY,
          }),
          // Prefixed to match this function's own IAM role (ToolLambda-…) and the
          // other framework-owned functions (AgentCoreBFF-…, AgentCoreKBRetrieve-…).
          // Without a prefix the name is just "<agentName>-<tool>", which a scoped
          // deploy policy cannot express without granting lambda:* on every
          // function in the account. Mirrors terraform/tools.tf.
          functionName: `ToolLambda-${agentName}-${name}`,
          runtime: lambda.Runtime.PYTHON_3_12,
          handler: "handler.lambda_handler",
          code: lambda.Code.fromAsset(path.join(props.orchRoot, spec.source!)),
          timeout: cdk.Duration.seconds(30),
          memorySize: 256,
          environment: {
            // Where the Price List Query API endpoint lives, which is NOT where
            // prices are being asked about: the API is published in only two
            // regions and describes prices for all of them. Pinned so the tool
            // works on a deployment in any region. Mirrors terraform/tools.tf.
            PRICING_API_REGION: "us-east-1",
          },
        });
        // Read only, and the only thing it reads is the PUBLIC price list — no
        // customer data, no account spend. The Price List Query API has no
        // resource-level permissions, so "*" is the only grant it accepts.
        // Mirrors aws_iam_role_policy.tool_lambda in terraform/tools.tf.
        fn.addToRolePolicy(
          new iam.PolicyStatement({
            sid: "ReadPublicPriceList",
            actions: ["pricing:GetProducts", "pricing:DescribeServices"],
            resources: ["*"],
          })
        );
        this.builtinLambdaArns[name] = fn.functionArn;
      }

      // Every declared lambda tool's effective ARN, whichever way it was supplied,
      // so the target and the IAM grant below need not care.
      const arnOf = (name: string, spec: ToolSpec) =>
        spec.source ? this.builtinLambdaArns[name] : spec.lambdaArn!;

      // One statement listing every function, rather than a policy per tool, so the
      // inline policy stays small as the tool count grows.
      gatewayRole.addToPolicy(
        new iam.PolicyStatement({
          sid: "InvokeToolLambdas",
          actions: ["lambda:InvokeFunction"],
          resources: lambdaTools.map(([n, s]) => arnOf(n, s)).sort(),
        })
      );
      for (const [name, spec] of lambdaTools) {
        // A Lambda's resource policy can only be edited from the account that owns
        // the function, so this is emitted for same-account functions only. A
        // cross-account function still works — its owner adds the statement. A
        // framework-deployed function is always local.
        // Only SKIP when we can prove the function belongs to another account. With
        // an env-agnostic stack `account` is an unresolved token, which never equals a
        // real 12-digit id — so this used to skip a same-account function silently and
        // the tool failed at first invoke with an authorization error. Line ~436 guards
        // the same hazard for the Cognito domain prefix; this site did not.
        if (!spec.source && !cdk.Token.isUnresolved(account)
            && spec.lambdaArn!.split(":")[4] !== account) continue;
        new lambda.CfnPermission(this, `ToolLambdaPermission-${name}`, {
          functionName: arnOf(name, spec),
          action: "lambda:InvokeFunction",
          principal: "bedrock-agentcore.amazonaws.com",
          sourceAccount: account,
        });
      }
    }

    // ======================================================================
    // Gateway targets — one per declared tool
    // ======================================================================
    // Registered one at a time: CloudFormation would otherwise create them in
    // parallel, and concurrent target registration on a single Gateway is not
    // something the service guarantees.
    let previousTarget: agentcore.CfnGatewayTarget | undefined;
    const targets: agentcore.CfnGatewayTarget[] = [];

    for (const [name, spec] of entries) {
      // A vaulted key, for the types that reach a third-party endpoint. The name
      // MUST start with "bedrock-agentcore" so the Gateway role's Secrets Manager
      // statement above can read it.
      const apiKey = props.toolApiKeys[name];
      let credProviderArn: string | undefined;
      if (apiKey && spec.auth !== "sigv4" && (spec.type === "mcp" || spec.type === "openapi")) {
        const cp = new agentcore.CfnApiKeyCredentialProvider(this, `Cred-${name}`, {
          name: `bedrock-agentcore-${dashName}-${name}`,
          apiKey,
        });
        credProviderArn = cp.attrCredentialProviderArn;
      }

      // How the Gateway authenticates OUTBOUND to this tool: a vaulted API key
      // when one was supplied, its own execution role for the KB Lambda, and
      // nothing for a public endpoint or a managed connector.
      const credentialProviderConfigurations = credProviderArn
        ? [
            {
              credentialProviderType: "API_KEY",
              credentialProvider: {
                apiKeyCredentialProvider: {
                  providerArn: credProviderArn,
                  credentialLocation: "HEADER",
                  credentialParameterName: "X-API-Key",
                },
              },
            },
          ]
        : // SigV4: the Gateway signs with its own execution role — no secret at
          // all. Also the mode for BOTH Lambda target kinds (the KB retrieve
          // function and a customer's own `type: "lambda"` function): the Gateway
          // invokes them as itself.
          // A `websearch` connector needs one too: without it CreateGatewayTarget
          // fails with "Credential provider configurations is not defined". The
          // connector runs inside AWS, so the Gateway uses its OWN execution role.
          spec.auth === "sigv4" ||
            spec.type === "kb" ||
            spec.type === "lambda" ||
            spec.type === "websearch"
          ? [
              {
                credentialProviderType: "GATEWAY_IAM_ROLE",
                ...(spec.auth === "sigv4" && spec.service
                  ? {
                      credentialProvider: {
                        iamCredentialProvider: {
                          service: spec.service,
                          region: cdk.Stack.of(this).region,
                        },
                      },
                    }
                  : {}),
              },
            ]
          : undefined;

      const target = new agentcore.CfnGatewayTarget(this, `Target-${name}`, {
        gatewayIdentifier: this.gatewayId,
        name, // MUST equal the agent's `tool` label in workflow.json
        // workflow.json descriptions are written to explain the tool to whoever
        // edits the config, so they can be long; CloudFormation caps this field
        // at 200 characters.
        description: truncate(spec.description ?? `Tool target ${name}`, 190),
        targetConfiguration: { mcp: this.mcpConfigFor(name, spec, kbLambda) },
        ...(credentialProviderConfigurations ? { credentialProviderConfigurations } : {}),
      });

      if (spec.type === "kb") dependOnDefaultPolicy(target, gatewayRole);
      if (previousTarget) target.node.addDependency(previousTarget);
      previousTarget = target;
      targets.push(target);
    }

    // ======================================================================
    // Cedar policy — generated from the same config
    // ======================================================================
    // Declaring a tool permits it. Anything NOT declared — including a tool name
    // a prompt-injected instruction invents — is refused by Cedar's default-deny.
    if (policyEngine) {
      for (const [name, spec] of entries) {
        if (spec.policy?.permit === false) continue; // registered but deliberately denied
        const policy = new agentcore.CfnPolicy(this, `Policy-${name}`, {
          name: `permit_${name}`,
          description: `Generated from workflow.json tools.${name}. Anything not permitted here is denied by Cedar's default-deny.`,
          policyEngineId: policyEngine.attrPolicyEngineId,
          definition: { cedar: { statement: cedarStatement(name, spec, this.gatewayArn) } },
          // The engine's analyser emits ADVISORY findings, and the default
          // (FAIL_ON_ANY_FINDINGS) turns one into a hard CloudFormation failure:
          //   "Overly Permissive: Policy Engine will allow every request for the
          //    specified principal (AgentCore::IamEntity), action
          //    (websearch___WebSearch) and resource ... combination"
          //    (HandlerErrorCode: NotStabilized)
          // That finding is CORRECT and intended: every agent shares one M2M
          // identity, so a tool permit deliberately applies to any authenticated
          // caller. The authorization boundary being demonstrated is per-TOOL (and
          // per-argument for the KB corpus), not per-principal. Findings are still
          // recorded; they just no longer block the deploy.
          validationMode: "IGNORE_ALL_FINDINGS",
        });
        // Cedar validates action names against the gateway's REGISTERED targets,
        // so every target must exist before any policy is created.
        targets.forEach((t) => policy.node.addDependency(t));
      }
    }

    dependOnDefaultPolicy(gateway, gatewayRole);
  }

  /** The `mcp` target configuration for one tool, by type. */
  private mcpConfigFor(
    name: string,
    spec: ToolSpec,
    kbLambda?: lambda.Function
  ): agentcore.CfnGatewayTarget.McpTargetConfigurationProperty {
    switch (spec.type) {
      case "kb":
        if (!kbLambda) throw new Error(`tools.${name} is type=kb but the KB Lambda was not created.`);
        return {
          lambda: {
            lambdaArn: kbLambda.functionArn,
            toolSchema: {
              inlinePayload: [
                {
                  name: "retrieve",
                  description:
                    "Retrieve relevant document chunks from the knowledge base for a query.",
                  inputSchema: {
                    type: "object",
                    properties: {
                      query: { type: "string", description: "The natural-language search query." },
                      filter: {
                        type: "string",
                        description: `Optional corpus (doc_type) to restrict retrieval to, e.g. ${(spec.corpora ?? []).join(" | ")}.`,
                      },
                    },
                    required: ["query"],
                  },
                },
              ],
            },
          },
        };

      case "websearch": {
        // A managed built-in connector: no endpoint, no schema, no API key.
        // The tool surfaces as "<name>___WebSearch".
        //
        // Two layers of domain filtering compose on every request (mirrors the
        // notes in terraform/tools.tf):
        //   TARGET level  — targetIncludeDomains / targetExcludeDomains, set here,
        //                   HIDDEN from the agent, applied to every request. The
        //                   enforceable layer.
        //   REQUEST level — includeDomains / excludeDomains / publishedFrom / To,
        //                   sent per call by the app. Caller-supplied, so scoping
        //                   rather than a boundary.
        const domainFilter: Record<string, string[]> = {};
        if (spec.targetIncludeDomains?.length) domainFilter.include = spec.targetIncludeDomains;
        if (spec.targetExcludeDomains?.length) domainFilter.exclude = spec.targetExcludeDomains;
        return {
          connector: {
            // NOTE: no version pin here. The CreateGatewayTarget API and the
            // Terraform provider both accept `version` on the connector source,
            // but the CloudFormation resource does NOT — ConnectorSourceProperty
            // exposes only connectorId. So `connectorVersion` is a Terraform-only
            // option; validateTools rejects it on this path rather than accepting
            // it and silently ignoring it.
            source: { connectorId: "web-search" },
            configurations: [
              {
                name: "WebSearch",
                description: truncate(spec.description ?? "AgentCore Web Search", 190),
                // REQUIRED, even when empty. The API DISCARDS a configuration entry
                // that has no parameterValues and then reports "Connector
                // configurations must not be empty" — which reads as if the list
                // were absent rather than its single entry dropped. Verified
                // directly against CreateGatewayTarget: "{}" is what makes it count.
                parameterValues: Object.keys(domainFilter).length ? { domainFilter } : {},
              },
            ],
          },
        };
      }

      case "mcp":
        if (!spec.endpoint) {
          throw new Error(`tools.${name} has type="mcp" and so requires "endpoint".`);
        }
        return {
          mcpServer: {
            endpoint: spec.endpoint,
            listingMode: spec.listingMode ?? "DEFAULT",
          },
        };

      case "openapi":
        if (!spec.schemaS3Uri)
          throw new Error(`tools.${name} has type="openapi" and so requires "schemaS3Uri".`);
        return { openApiSchema: { s3: { uri: spec.schemaS3Uri } } };

      case "lambda": {
        // Either a function you own (by ARN) or one the framework deployed from
        // `source`. The schema has to be spelled out either way, because there is no
        // tools/list to discover it from.
        const lambdaArn = spec.source ? this.builtinLambdaArns[name] : spec.lambdaArn;
        if (!lambdaArn)
          throw new Error(
            `tools.${name} has type="lambda" and so requires "lambdaArn" or "source".`
          );
        if (!spec.toolSchema?.length)
          throw new Error(`tools.${name} has type="lambda" and so requires a non-empty "toolSchema".`);
        return {
          lambda: {
            lambdaArn,
            toolSchema: {
              inlinePayload: spec.toolSchema.map((t) => ({
                name: t.name,
                description: truncate(t.description ?? t.name, 190),
                inputSchema: {
                  type: "object",
                  properties: Object.fromEntries(
                    Object.entries(t.properties ?? {}).map(([p, def]) => [
                      p,
                      { type: def.type ?? "string", description: def.description ?? p },
                    ])
                  ),
                  // JSON Schema puts `required` on the OBJECT as a list of names,
                  // while the config (and the Terraform provider's flattened
                  // `property` block) put it on each property. Collect them here so
                  // both paths accept the same JSON.
                  required: Object.entries(t.properties ?? {})
                    .filter(([, def]) => def.required)
                    .map(([p]) => p),
                },
              })),
            },
          },
        };
      }
    }
  }
}

/**
 * Build the Cedar permit for one tool.
 *
 * With `policy.tool`, emit a fine-grained permit on that single tool, optionally
 * argument-restricted. Without it, emit a TARGET-level permit: Cedar has no
 * wildcard actions, but a Gateway target IS an action group, so
 * `action in AgentCore::Action::"<target>"` covers every tool the target
 * publishes — which is the only workable form for a remote MCP server whose tool
 * names we cannot know at deploy time.
 */
export function cedarStatement(name: string, spec: ToolSpec, gatewayArn: string): string {
  const resource = `resource == AgentCore::Gateway::"${gatewayArn}"`;
  // The managed web-search connector always publishes exactly ONE tool, named
  // "WebSearch", so default to permitting it BY NAME. Not cosmetic: a
  // target-level permit does NOT authorize it. Verified on a live gateway —
  // `action in AgentCore::Action::"websearch"` produced
  // "ToolDenied: websearch___WebSearch was denied by the Cedar policy engine".
  const toolName = spec.policy?.tool ?? (spec.type === "websearch" ? "WebSearch" : undefined);
  if (!toolName) {
    return [
      "permit(",
      "  principal,",
      `  action in AgentCore::Action::"${name}",`,
      `  ${resource}`,
      ");",
    ].join("\n");
  }
  const restrict = spec.policy?.restrictTo ?? {};
  const conditions = Object.entries(restrict).map(
    ([arg, allowed]) =>
      `  context.input has ${arg} && ${JSON.stringify(allowed)}.contains(context.input.${arg})`
  );
  // The `when` clause is NOT optional. The policy engine REFUSES to create a
  // permit that is unconditioned for an unconstrained principal:
  //   "Overly Permissive: Policy Engine will allow every request for the
  //    specified principal (AgentCore::IamEntity), action (...) and resource ...
  //    combination"   (HandlerErrorCode: NotStabilized)
  // So when a tool declares no `policy.restrictTo`, still require the query
  // argument to be PRESENT: a real constraint (a call with no query is refused)
  // and enough to make the permit acceptable.
  if (conditions.length === 0) {
    conditions.push(`  context.input has ${spec.arg || "query"}`);
  }
  const head = [
    "permit(",
    "  principal,",
    `  action == AgentCore::Action::"${name}___${toolName}",`,
    `  ${resource}`,
    ")",
  ].join("\n");
  return `${head} when {\n${conditions.join(" &&\n")}\n};`;
}

/**
 * Metadata keys S3 Vectors must NOT treat as filterable.
 *
 * It caps filterable metadata at 2048 bytes per vector, and both of these grow
 * with the document, so both must be excluded:
 *   AMAZON_BEDROCK_TEXT     — the chunk's own text.
 *   AMAZON_BEDROCK_METADATA — a JSON blob carrying `text`, `parentText`, the source
 *                             location and a document id.
 * Nothing filters on either (this framework's only filter is `doc_type`).
 */
/**
 * Retention for the log groups of the Lambdas this construct creates.
 *
 * Duplicated rather than imported from lib/orchestrator-stack.ts, which imports THIS
 * file — the reverse import would be circular. cdk/test/parity.test.ts asserts the two
 * stay equal.
 */
export const LOG_RETENTION = logs.RetentionDays.ONE_MONTH;

export const KB_NON_FILTERABLE = ["AMAZON_BEDROCK_TEXT", "AMAZON_BEDROCK_METADATA"];

/**
 * Digest of the vector-store properties that CANNOT be changed in place.
 *
 * S3 Vectors rejects an update to an index's `dimension` or `metadataConfiguration`,
 * so a change to either requires REPLACING the index — and CloudFormation refuses to
 * replace a resource that carries a fixed custom name:
 *   "CloudFormation cannot update a stack when a custom-named resource requires
 *    replacing. Rename ... and update the stack again."
 * Folding those properties into the NAME means any change to them yields a new name,
 * so the replacement just works. Keep in step with terraform/kb.tf.
 */
function kbStorageDigest(dims: number, nonFilterable: string[]): string {
  return crypto
    .createHash("sha256")
    .update(`${dims}|${[...nonFilterable].sort().join(",")}`)
    .digest("hex")
    .slice(0, 8);
}

/** The vector index name (see kbStorageDigest). */
export function kbIndexName(dims: number, nonFilterable: string[]): string {
  return `kb-index-${kbStorageDigest(dims, nonFilterable)}`;
}

/**
 * The Knowledge Base name, carrying the SAME digest as its index.
 *
 * The index name alone is not enough. A replaced index has a new ARN, the KB's
 * `storageConfiguration` is itself immutable, so the KB is replaced too — and it hit
 * the identical wall one level up, as a 409 rather than a rename hint:
 *   "KnowledgeBase with name multiagent-orchestrator-kb already exists.
 *    (Service: BedrockAgent, Status Code: 409)"
 * because CloudFormation creates the replacement before deleting the original.
 */
export function knowledgeBaseName(dashName: string, dims: number, nonFilterable: string[]): string {
  return `${dashName}-kb-${kbStorageDigest(dims, nonFilterable)}`;
}

/** Clip a string to `max` characters, for fields with a CloudFormation limit. */
function truncate(s: string, max: number): string {
  return s.length <= max ? s : s.slice(0, max - 1).trimEnd() + "\u2026";
}

/** Relative paths of every file under `dir`, recursively (POSIX separators). */
function listFilesRecursive(dir: string, prefix = ""): string[] {
  if (!fs.existsSync(dir)) return [];
  const out: string[] = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const rel = prefix ? `${prefix}/${entry.name}` : entry.name;
    if (entry.isDirectory()) out.push(...listFilesRecursive(path.join(dir, entry.name), rel));
    else out.push(rel);
  }
  return out;
}

/** Stable hash of the corpus contents + doc_type mapping, for ingestion triggering. */
function hashCorpus(dir: string, files: string[]): string {
  const h = crypto.createHash("sha1");
  for (const f of [...files].sort()) {
    h.update(f);
    h.update(fs.readFileSync(path.join(dir, f)));
    h.update(f.split("/")[0]); // doc_type
  }
  return h.digest("hex").slice(0, 16);
}

/**
 * Make `resource` wait for a role's inline default policy, not just the role.
 * CDK only creates the implicit role -> resource dependency; several services
 * validate permissions at CREATE time and fail if the policy lands afterwards.
 */
function dependOnDefaultPolicy(resource: cdk.CfnResource, role: iam.Role): void {
  const policy = role.node.tryFindChild("DefaultPolicy") as iam.Policy | undefined;
  if (policy) resource.node.addDependency(policy);
}
