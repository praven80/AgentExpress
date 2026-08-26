import * as path from "path";
import * as fs from "fs";
import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import {
  aws_dynamodb as dynamodb,
  aws_iam as iam,
  aws_lambda as lambda,
  aws_ecr_assets as ecrAssets,
  aws_s3 as s3,
  aws_s3_deployment as s3deploy,
  aws_cloudfront as cf,
  aws_cloudfront_origins as origins,
  aws_cognito as cognito,
} from "aws-cdk-lib";
import { HttpApi, HttpMethod } from "aws-cdk-lib/aws-apigatewayv2";
import { HttpLambdaIntegration } from "aws-cdk-lib/aws-apigatewayv2-integrations";
import { HttpJwtAuthorizer } from "aws-cdk-lib/aws-apigatewayv2-authorizers";

export interface OrchestratorStackProps extends cdk.StackProps {
  agentName: string;
  modelId: string;
  memoryEventExpiryDays: number;
  cognitoUserPoolId: string;
  cognitoClientId: string;
  cognitoDomainPrefix: string;
  enableGateway: boolean;
  createCognito: boolean;
}

// Repo layout: this file is orchestrator/cdk/lib/, so orchestrator/ is two levels up.
const ORCH_ROOT = path.join(__dirname, "..", "..");

/**
 * CDK equivalent of the Terraform deployment, covering the core (no-Gateway)
 * footprint: the orchestrator AgentCore Runtime, one dedicated runtime per
 * `dedicated` agent, DynamoDB progress/telemetry stores, the BFF Lambda + HTTP
 * API (optional Cognito JWT authorizer), and the S3 + CloudFront UI.
 *
 * The AgentCore Gateway + Bedrock Knowledge Base (live MCP + RAG) are NOT created
 * by this stack — that path lives in the Terraform config. With the Gateway
 * absent, MCP/RAG calls run simulated (LLM inference is still real via Bedrock),
 * exactly like `enable_gateway = false` in Terraform.
 */
export class OrchestratorStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: OrchestratorStackProps) {
    super(scope, id, props);

    const { agentName, modelId, memoryEventExpiryDays } = props;

    if (props.enableGateway) {
      throw new Error(
        "enableGateway=true is not implemented in the CDK path. Deploy with " +
          "enableGateway=false (MCP/RAG run simulated), or use the Terraform config " +
          "for the full Gateway + Knowledge Base deployment."
      );
    }

    // ---- workflow.json: the single source of truth for agents + topology ----
    const workflow = JSON.parse(
      fs.readFileSync(path.join(ORCH_ROOT, "app", "workflow.json"), "utf8")
    );
    const agents: Record<string, any> = workflow.agents;
    const dedicatedIds = Object.keys(agents).filter(
      (id) => (agents[id].runtime ?? "main") === "dedicated"
    );

    // ---- Cognito: create or reference existing --------------------------------
    let cognitoUserPoolId: string;
    let cognitoClientId: string;
    let cognitoDomainPrefix: string;

    if (props.createCognito) {
      const pool = new cognito.UserPool(this, "UserPool", {
        userPoolName: `${agentName}-users`,
        selfSignUpEnabled: true,
        signInAliases: { email: true },
        autoVerify: { email: true },
        passwordPolicy: {
          minLength: 8,
          requireUppercase: true,
          requireDigits: true,
          requireSymbols: false,
        },
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      });

      const domainPrefix = `${agentName.replace(/_/g, "-")}-${this.account.slice(-6)}`;
      pool.addDomain("CognitoDomain", {
        cognitoDomain: { domainPrefix },
      });

      const client = pool.addClient("SpaClient", {
        userPoolClientName: `${agentName}-spa`,
        generateSecret: false,
        authFlows: { userSrp: true },
        oAuth: {
          flows: { authorizationCodeGrant: true },
          scopes: [
            cognito.OAuthScope.OPENID,
            cognito.OAuthScope.EMAIL,
            cognito.OAuthScope.PROFILE,
          ],
          callbackUrls: ["https://localhost"],
          logoutUrls: ["https://localhost"],
        },
      });

      cognitoUserPoolId = pool.userPoolId;
      cognitoClientId = client.userPoolClientId;
      cognitoDomainPrefix = domainPrefix;

      new cdk.CfnOutput(this, "cognitoUserPoolId", { value: pool.userPoolId });
      new cdk.CfnOutput(this, "cognitoClientId", { value: client.userPoolClientId });
      new cdk.CfnOutput(this, "cognitoDomainPrefix", { value: domainPrefix });
    } else {
      cognitoUserPoolId = props.cognitoUserPoolId;
      cognitoClientId = props.cognitoClientId;
      cognitoDomainPrefix = props.cognitoDomainPrefix;
    }

    const authEnabled = cognitoUserPoolId.trim() !== "" || props.createCognito;

    // ---- Container image (ARM64) built from orchestrator/Dockerfile ----------
    // Build with finch by exporting CDK_DOCKER=finch (default is docker).
    const image = new ecrAssets.DockerImageAsset(this, "Image", {
      directory: ORCH_ROOT,
      file: "Dockerfile",
      platform: ecrAssets.Platform.LINUX_ARM64,
    });

    // ---- DynamoDB: status, events, telemetry --------------------------------
    const statusTable = new dynamodb.Table(this, "StatusTable", {
      tableName: `${agentName}_status`,
      partitionKey: { name: "session_id", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const eventsTable = new dynamodb.Table(this, "EventsTable", {
      tableName: `${agentName}_events`,
      partitionKey: { name: "session_id", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "ts", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const telemetryTable = new dynamodb.Table(this, "TelemetryTable", {
      tableName: `${agentName}_telemetry`,
      partitionKey: { name: "session_id", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "sk", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: "ttl",
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    telemetryTable.addGlobalSecondaryIndex({
      indexName: "by_date",
      partitionKey: { name: "date", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "sk", type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // ---- AgentCore Memory (durable LangGraph checkpointer) ------------------
    // No stable L2 for AgentCore yet; use the CloudFormation resource types
    // directly (the same types the Terraform awscc provider drives).
    const memory = new cdk.CfnResource(this, "Memory", {
      type: "AWS::BedrockAgentCore::Memory",
      properties: {
        Name: `${agentName}_memory`,
        EventExpiryDuration: memoryEventExpiryDays,
        Description: "Checkpoint/state store for the multi-agent orchestrator",
      },
    });
    const memoryId = memory.getAtt("MemoryId").toString();
    const memoryArn = memory.getAtt("MemoryArn").toString();

    // ---- Shared IAM trust: bedrock-agentcore assumes runtime roles ----------
    const agentcorePrincipal = new iam.ServicePrincipal("bedrock-agentcore.amazonaws.com", {
      conditions: {
        StringEquals: { "aws:SourceAccount": this.account },
        ArnLike: { "aws:SourceArn": `arn:aws:bedrock-agentcore:${this.region}:${this.account}:*` },
      },
    });

    const bedrockInvoke = new iam.PolicyStatement({
      actions: [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
        "bedrock:CountTokens",
      ],
      resources: [
        "arn:aws:bedrock:*::foundation-model/*",
        `arn:aws:bedrock:${this.region}:${this.account}:*`,
      ],
    });
    const observabilityPerms = (workloadSuffix: string): iam.PolicyStatement[] => [
      new iam.PolicyStatement({
        actions: [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "logs:DescribeLogStreams",
          "logs:DescribeLogGroups",
        ],
        resources: [`arn:aws:logs:${this.region}:${this.account}:log-group:/aws/bedrock-agentcore/runtimes/*`],
      }),
      new iam.PolicyStatement({
        actions: ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets"],
        resources: ["*"],
      }),
      new iam.PolicyStatement({
        actions: ["cloudwatch:PutMetricData"],
        resources: ["*"],
        conditions: { StringEquals: { "cloudwatch:namespace": "bedrock-agentcore" } },
      }),
      new iam.PolicyStatement({
        actions: [
          "bedrock-agentcore:GetWorkloadAccessToken",
          "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
          "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
        ],
        resources: [
          `arn:aws:bedrock-agentcore:${this.region}:${this.account}:workload-identity-directory/default`,
          `arn:aws:bedrock-agentcore:${this.region}:${this.account}:workload-identity-directory/default/workload-identity/${workloadSuffix}`,
        ],
      }),
    ];

    // ---- Dedicated agent runtimes (one per `dedicated` agent) ---------------
    const subagentRole = new iam.Role(this, "SubagentRole", {
      roleName: `AgentCoreSubagent-${agentName}`,
      assumedBy: agentcorePrincipal,
    });
    image.repository.grantPull(subagentRole);
    [bedrockInvoke, ...observabilityPerms(`${agentName}*`)].forEach((s) => subagentRole.addToPolicy(s));
    telemetryTable.grantWriteData(subagentRole);

    const dedicatedArns: Record<string, string> = {};
    for (const id of dedicatedIds) {
      const rt = new cdk.CfnResource(this, `Subagent-${id}`, {
        type: "AWS::BedrockAgentCore::Runtime",
        properties: {
          AgentRuntimeName: `${agentName}_${id}`,
          Description: `Dedicated runtime for agent ${id} (${agents[id].name})`,
          RoleArn: subagentRole.roleArn,
          AgentRuntimeArtifact: { ContainerConfiguration: { ContainerUri: image.imageUri } },
          NetworkConfiguration: { NetworkMode: "PUBLIC" },
          EnvironmentVariables: {
            AWS_REGION: this.region,
            BEDROCK_MODEL_ID: modelId,
            AGENT_ID: id,
            TELEMETRY_TABLE: telemetryTable.tableName,
            // Gateway not provisioned in the CDK path -> MCP/RAG run simulated.
            GATEWAY_URL: "",
            GATEWAY_TOKEN_URL: "",
            GATEWAY_CLIENT_ID: "",
            GATEWAY_CLIENT_SECRET: "",
            GATEWAY_AUDIENCE: "",
          },
        },
      });
      rt.node.addDependency(subagentRole);
      dedicatedArns[id] = rt.getAtt("AgentRuntimeArn").toString();
    }

    // JSON map {agentId: runtimeArn} for the orchestrator (Fn.join keeps tokens intact).
    const arnMapJson =
      dedicatedIds.length === 0
        ? "{}"
        : cdk.Fn.join("", [
            "{",
            ...dedicatedIds.flatMap((id, i) => [
              i ? "," : "",
              `"${id}":"`,
              dedicatedArns[id],
              '"',
            ]),
            "}",
          ]);
    const invokeRuntimeResources =
      dedicatedIds.length === 0
        ? [`arn:aws:bedrock-agentcore:${this.region}:${this.account}:runtime/none`]
        : dedicatedIds.flatMap((id) => [dedicatedArns[id], `${dedicatedArns[id]}/*`]);

    // ---- Orchestrator runtime ----------------------------------------------
    const runtimeRole = new iam.Role(this, "RuntimeRole", {
      roleName: `AgentCoreRuntime-${agentName}`,
      assumedBy: agentcorePrincipal,
    });
    image.repository.grantPull(runtimeRole);
    [bedrockInvoke, ...observabilityPerms(`${agentName}-*`)].forEach((s) => runtimeRole.addToPolicy(s));
    runtimeRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          "bedrock-agentcore:CreateEvent",
          "bedrock-agentcore:ListEvents",
          "bedrock-agentcore:GetEvent",
          "bedrock-agentcore:ListSessions",
          "bedrock-agentcore:RetrieveMemories",
        ],
        resources: [memoryArn, `${memoryArn}/*`],
      })
    );
    runtimeRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["bedrock-agentcore:InvokeAgentRuntime"],
        resources: invokeRuntimeResources,
      })
    );
    statusTable.grantReadWriteData(runtimeRole);
    eventsTable.grantReadWriteData(runtimeRole);
    telemetryTable.grantWriteData(runtimeRole);

    const orchestrator = new cdk.CfnResource(this, "Orchestrator", {
      type: "AWS::BedrockAgentCore::Runtime",
      properties: {
        AgentRuntimeName: agentName,
        Description: "Multi-agent LangGraph orchestrator (CDK-managed)",
        RoleArn: runtimeRole.roleArn,
        AgentRuntimeArtifact: { ContainerConfiguration: { ContainerUri: image.imageUri } },
        NetworkConfiguration: { NetworkMode: "PUBLIC" },
        EnvironmentVariables: {
          AWS_REGION: this.region,
          BEDROCK_MODEL_ID: modelId,
          MEMORY_ID: memoryId,
          STATUS_TABLE: statusTable.tableName,
          EVENTS_TABLE: eventsTable.tableName,
          TELEMETRY_TABLE: telemetryTable.tableName,
          AGENT_RUNTIME_ARNS: arnMapJson,
          GATEWAY_URL: "",
          GATEWAY_TOKEN_URL: "",
          GATEWAY_CLIENT_ID: "",
          GATEWAY_CLIENT_SECRET: "",
          GATEWAY_AUDIENCE: "",
        },
      },
    });
    orchestrator.node.addDependency(runtimeRole);
    const orchestratorArn = orchestrator.getAtt("AgentRuntimeArn").toString();

    // ---- BFF Lambda + HTTP API ---------------------------------------------
    const bffFunctionName = `AgentCoreBFF-${agentName}`;
    // Constructed ARN (not bff.functionArn) so the self-invoke policy below does not
    // create a function -> role-policy -> function circular dependency.
    const bffArn = `arn:aws:lambda:${this.region}:${this.account}:function:${bffFunctionName}`;
    const bff = new lambda.Function(this, "Bff", {
      functionName: bffFunctionName,
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: "handler.handler",
      code: lambda.Code.fromAsset(path.join(ORCH_ROOT, "bff")),
      timeout: cdk.Duration.seconds(60),
      memorySize: 256,
      environment: {
        STATUS_TABLE: statusTable.tableName,
        EVENTS_TABLE: eventsTable.tableName,
        TELEMETRY_TABLE: telemetryTable.tableName,
        RUNTIME_ARN: orchestratorArn,
        WORKFLOW_JSON: JSON.stringify(buildBffWorkflow(workflow)),
      },
    });
    statusTable.grantReadWriteData(bff);
    eventsTable.grantReadWriteData(bff);
    telemetryTable.grantReadData(bff);
    bff.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["bedrock-agentcore:InvokeAgentRuntime"],
        resources: [orchestratorArn, `${orchestratorArn}/*`],
      })
    );
    // The BFF self-invokes (async runner mode). Use the constructed ARN to avoid a
    // circular dependency with its own execution role.
    bff.addToRolePolicy(
      new iam.PolicyStatement({ actions: ["lambda:InvokeFunction"], resources: [bffArn] })
    );

    const api = new HttpApi(this, "HttpApi", { apiName: `AgentCoreBFF-${agentName}` });
    const integration = new HttpLambdaIntegration("BffIntegration", bff);
    const authorizer = authEnabled
      ? new HttpJwtAuthorizer("Cognito", `https://cognito-idp.${this.region}.amazonaws.com/${cognitoUserPoolId}`, { jwtAudience: [cognitoClientId] })
      : undefined;

    const routes: Array<{ path: string; methods: HttpMethod[] }> = [
      { path: "/api/workflow", methods: [HttpMethod.GET] },
      { path: "/api/sessions", methods: [HttpMethod.GET, HttpMethod.POST] },
      { path: "/api/sessions/{id}", methods: [HttpMethod.GET, HttpMethod.DELETE] },
      { path: "/api/sessions/{id}/decision", methods: [HttpMethod.POST] },
      { path: "/api/sessions/{id}/cancel", methods: [HttpMethod.POST] },
      { path: "/api/sessions/{id}/telemetry", methods: [HttpMethod.GET] },
      { path: "/api/telemetry/aggregate", methods: [HttpMethod.GET] },
    ];
    for (const r of routes) {
      api.addRoutes({ path: r.path, methods: r.methods, integration, authorizer });
    }

    // ---- Static UI: private S3 + CloudFront (OAC) --------------------------
    const uiBucket = new s3.Bucket(this, "UiBucket", {
      bucketName: `agentcore-${agentName.replace(/_/g, "-")}-ui-${this.account}-${this.region}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    const apiDomain = cdk.Fn.select(2, cdk.Fn.split("/", api.apiEndpoint));
    const distribution = new cf.Distribution(this, "Ui", {
      comment: "Multi-agent orchestrator UI",
      defaultRootObject: "index.html",
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(uiBucket),
        viewerProtocolPolicy: cf.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cf.CachePolicy.CACHING_OPTIMIZED,
      },
      additionalBehaviors: {
        "/api/*": {
          origin: new origins.HttpOrigin(apiDomain, { protocolPolicy: cf.OriginProtocolPolicy.HTTPS_ONLY }),
          viewerProtocolPolicy: cf.ViewerProtocolPolicy.HTTPS_ONLY,
          allowedMethods: cf.AllowedMethods.ALLOW_ALL,
          cachePolicy: cf.CachePolicy.CACHING_DISABLED,
          originRequestPolicy: cf.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
        },
      },
    });

    // Non-secret Cognito config the SPA reads (generated, like the Terraform tftpl).
    const authConfigJs =
      `// Generated by CDK. Non-secret Cognito settings for the SPA.\n` +
      `// When enabled is false the UI runs with no authentication.\n` +
      `window.AUTH_CONFIG = {\n` +
      `  enabled: ${authEnabled},\n` +
      `  region: ${JSON.stringify(this.region)},\n` +
      `  userPoolId: ${JSON.stringify(cognitoUserPoolId)},\n` +
      `  clientId: ${JSON.stringify(cognitoClientId)},\n` +
      `  domainPrefix: ${JSON.stringify(cognitoDomainPrefix)}\n` +
      `};\n`;

    new s3deploy.BucketDeployment(this, "UiDeploy", {
      destinationBucket: uiBucket,
      distribution,
      distributionPaths: ["/*"],
      sources: [
        s3deploy.Source.asset(path.join(ORCH_ROOT, "web"), { exclude: ["*.tftpl"] }),
        s3deploy.Source.data("auth-config.js", authConfigJs),
      ],
    });

    // ---- Outputs ------------------------------------------------------------
    new cdk.CfnOutput(this, "uiUrl", { value: `https://${distribution.distributionDomainName}` });
    new cdk.CfnOutput(this, "apiEndpoint", { value: api.apiEndpoint });
    new cdk.CfnOutput(this, "agentRuntimeArn", { value: orchestratorArn });
    new cdk.CfnOutput(this, "memoryId", { value: memoryId });
    new cdk.CfnOutput(this, "imageUri", { value: image.imageUri });
    new cdk.CfnOutput(this, "dedicatedAgents", { value: dedicatedIds.join(", ") || "(none)" });
    new cdk.CfnOutput(this, "authEnabled", { value: String(authEnabled) });
  }
}

/**
 * Trim workflow.json to the display fields the UI needs (agents + steps), mirroring
 * the Terraform `local.bff_workflow`. Shipped to the BFF as WORKFLOW_JSON.
 */
function buildBffWorkflow(workflow: any): any {
  const agentsOut: Record<string, any> = {};
  for (const [id, a] of Object.entries<any>(workflow.agents)) {
    const access0: string = Array.isArray(a.access) && a.access[0] ? a.access[0] : "";
    let source = "\u2014";
    if (a.mcp) source = `MCP \u00b7 ${a.mcp}`;
    else if (a.rag) source = `Knowledge Base \u00b7 ${a.rag}`;
    else if (/session/i.test(access0)) source = "Session input";
    else if (/upstream/i.test(access0)) source = "Upstream agent outputs";
    agentsOut[id] = {
      name: a.name,
      kind: a.kind ?? "sync",
      runtime: a.runtime ?? "main",
      mcp: a.mcp ?? null,
      rag: a.rag ?? null,
      model: a.model ?? null,
      source,
    };
  }
  return { agents: agentsOut, steps: workflow.steps };
}
