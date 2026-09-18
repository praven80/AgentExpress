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
  aws_logs as logs,
  custom_resources as cr,
} from "aws-cdk-lib";
import { HttpApi, HttpMethod } from "aws-cdk-lib/aws-apigatewayv2";
import { HttpLambdaIntegration } from "aws-cdk-lib/aws-apigatewayv2-integrations";
import { HttpJwtAuthorizer } from "aws-cdk-lib/aws-apigatewayv2-authorizers";
import { ToolPlane, ToolSpec, ToolType } from "./tool-plane";

/** Regions where the managed AgentCore Web Search connector is available. */
/**
 * Retention for the log groups of the Lambdas this stack creates.
 *
 * Every Lambda here declares its log group explicitly. Left implicit, Lambda creates
 * `/aws/lambda/<name>` itself with NEVER-EXPIRE retention and no stack ownership, so
 * `cdk destroy` leaves it behind accruing cost forever — a full destroy of this stack
 * was verified to orphan eight such groups. Keep in step with
 * terraform/variables.tf var.log_retention_days.
 */
const LOG_RETENTION = logs.RetentionDays.ONE_MONTH;

const WEB_SEARCH_REGIONS = ["us-east-1", "eu-west-1", "ap-northeast-1"];
const TOOL_TYPES: ToolType[] = ["kb", "websearch", "mcp", "openapi", "lambda"];
/** Property types the AgentCore inline tool schema accepts. */
const SCHEMA_TYPES = ["string", "number", "integer", "boolean", "array", "object"];
const LAMBDA_ARN =
  /^arn:aws[a-z-]*:lambda:[a-z0-9-]+:[0-9]{12}:function:[a-zA-Z0-9-_]+(:[a-zA-Z0-9-_$]+)?$/;
/** The only value `source` accepts — see the ToolSpec docs for why it is not open. */
const BUILTIN_LAMBDA_SOURCE = "tool_lambda";

/**
 * Validate the `tools` block from workflow.json, mirroring the preconditions in
 * terraform/tools.tf so both IaC paths reject the same mistakes with the same
 * message — at synth, before anything is deployed.
 */
export function validateTools(
  raw: Record<string, any>,
  agents: Record<string, any>,
  region: string
): Record<string, ToolSpec> {
  const tools: Record<string, ToolSpec> = {};
  for (const [name, t] of Object.entries(raw)) {
    if (!TOOL_TYPES.includes(t?.type)) {
      throw new Error(`workflow.json tools.${name} needs "type" = ${TOOL_TYPES.join(" | ")}.`);
    }
    if (t.type === "mcp" && !t.endpoint) {
      throw new Error(
        `workflow.json tools.${name} has type="mcp" and so requires "endpoint" (your MCP server's Streamable HTTP URL).`
      );
    }
    if (t.type === "openapi" && !t.schemaS3Uri) {
      throw new Error(
        `workflow.json tools.${name} has type="openapi" and so requires "schemaS3Uri" (s3:// URI of the OpenAPI schema).`
      );
    }
    // --- type=lambda ----------------------------------------------------
    // The Gateway will not discover a Lambda's tools for itself — there is no
    // tools/list to call — so the schema is declared. A missing or malformed one is
    // accepted by the API and then publishes a tool the agent cannot call, which
    // surfaces as an empty answer rather than an error.
    if (t.type === "lambda") {
      // Exactly one source of truth for WHICH function to call. Both set is
      // ambiguous; neither means there is nothing to register.
      if (!!t.lambdaArn === !!t.source) {
        throw new Error(
          `workflow.json tools.${name} needs EXACTLY ONE of "lambdaArn" (a function you already own — ` +
            `the framework only registers it) or "source" (a function the framework ships and deploys).`
        );
      }
      // `source` is deliberately NOT a general "deploy any directory" feature: a
      // framework-deployed function needs an execution role config cannot express.
      if (t.source && t.source !== BUILTIN_LAMBDA_SOURCE) {
        throw new Error(
          `workflow.json tools.${name} has "source": ${JSON.stringify(t.source)}, but the only value is ` +
            `"${BUILTIN_LAMBDA_SOURCE}" — the run-history demo function the framework ships under ` +
            `orchestrator/${BUILTIN_LAMBDA_SOURCE}/. To use a function of your own, deploy it yourself ` +
            `and set "lambdaArn" instead.`
        );
      }
      if (t.lambdaArn && !LAMBDA_ARN.test(String(t.lambdaArn))) {
        throw new Error(
          `workflow.json tools.${name} has type="lambda" and so requires "lambdaArn": the full ARN of an ` +
            `EXISTING function, e.g. "arn:aws:lambda:us-east-1:123456789012:function:my-tool" ` +
            `(an alias or version qualifier is allowed). Got: ${JSON.stringify(t.lambdaArn)}.`
        );
      }
      const schema: any[] = t.toolSchema ?? [];
      if (!schema.length) {
        throw new Error(
          `workflow.json tools.${name} has type="lambda" and so requires a non-empty "toolSchema" list. ` +
            `The Gateway cannot discover a Lambda's tools, so each must be declared: ` +
            `{ "name": "...", "description": "...", "properties": { "<arg>": { "type": "string", ` +
            `"required": true, "description": "..." } } }.`
        );
      }
      for (const s of schema) {
        if (!s?.name || !Object.keys(s.properties ?? {}).length) {
          throw new Error(
            `Every "toolSchema" entry in workflow.json tools.${name} needs a "name" and at least one ` +
              `entry in "properties" — a tool with no arguments has nothing for the agent's query to go into.`
          );
        }
        for (const [p, def] of Object.entries<any>(s.properties)) {
          const ty = String(def?.type ?? "string").toLowerCase();
          if (!SCHEMA_TYPES.includes(ty)) {
            throw new Error(
              `workflow.json tools.${name} toolSchema.${s.name}.properties.${p} has type "${def?.type}"; ` +
                `use one of ${SCHEMA_TYPES.join(", ")}.`
            );
          }
        }
      }
      const names = schema.map((s: any) => s.name);
      // Mirrors _select_tool in app/features/gateway/client.py, which REFUSES to
      // guess between several published tools.
      if (schema.length > 1 && !t.call) {
        throw new Error(
          `workflow.json tools.${name} declares ${schema.length} "toolSchema" entries (${names.join(", ")}), ` +
            `so it must also set "call" naming which one an agent invokes — the app refuses to guess.`
        );
      }
      if (t.call && !names.includes(t.call)) {
        throw new Error(
          `workflow.json tools.${name} has "call": "${t.call}", which is not one of its own "toolSchema" ` +
            `entries (${names.join(", ")}).`
        );
      }
      // `arg` decides which property the agent's query is written into, so it has
      // to exist — otherwise the query goes under a name the function ignores and
      // the tool answers as though none had been supplied.
      const called = t.call ? schema.find((s: any) => s.name === t.call) : schema[0];
      if (t.arg && called && !Object.keys(called.properties ?? {}).includes(t.arg)) {
        throw new Error(
          `workflow.json tools.${name} has "arg": "${t.arg}", which is not a property of "${called.name}" ` +
            `(${Object.keys(called.properties ?? {}).join(", ")}). That is where the agent's query is placed.`
        );
      }
    }
    if (t.type === "websearch" && t.connectorVersion) {
      // Fail loudly rather than accept-and-ignore: CloudFormation's
      // ConnectorSourceProperty has only connectorId, so a version pin cannot be
      // expressed on this path even though the API and Terraform support it.
      throw new Error(
        `workflow.json tools.${name} sets "connectorVersion", which CloudFormation cannot express ` +
          `(AWS::BedrockAgentCore::GatewayTarget's connector source accepts only connectorId). ` +
          `Remove it to track the latest connector, or deploy this stack with Terraform, which does ` +
          `support the pin.`
      );
    }
    if (t.listingMode && !["DEFAULT", "DYNAMIC"].includes(t.listingMode)) {
      throw new Error(
        `workflow.json tools.${name} has an invalid "listingMode" (${t.listingMode}); use "DEFAULT" or "DYNAMIC".`
      );
    }
    if (t.auth && !["none", "apikey", "sigv4"].includes(t.auth)) {
      throw new Error(
        `workflow.json tools.${name} has an invalid "auth" (${t.auth}); use "none", "apikey" or "sigv4".`
      );
    }
    tools[name] = t as ToolSpec;
  }

  const ofType = (ty: ToolType) => Object.entries(tools).filter(([, v]) => v.type === ty);

  const kbs = ofType("kb");
  if (kbs.length > 1) {
    throw new Error(
      'Only one workflow.json tools entry may have type="kb" (there is a single Knowledge Base per deployment). Scope agents to different document sets with `corpora` + each agent\'s `corpus` instead.'
    );
  }
  if (kbs.length === 1 && (kbs[0][1].corpora ?? []).length === 0) {
    throw new Error(
      `workflow.json tools.${kbs[0][0]} has type="kb" and so requires a non-empty "corpora" list naming the top-level folders under kb_docs/.`
    );
  }

  const ws = ofType("websearch");
  if (ws.length > 1) {
    throw new Error(
      'Only one workflow.json tools entry may have type="websearch" (the managed connector is a single target).'
    );
  }
  if (ws.length === 1 && !cdk.Token.isUnresolved(region) && !WEB_SEARCH_REGIONS.includes(region)) {
    throw new Error(
      `workflow.json tools.${ws[0][0]} has type="websearch", which is only available in ${WEB_SEARCH_REGIONS.join(", ")} (got ${region}). Remove the entry or deploy in one of those regions.`
    );
  }

  // Every agent's `tool` must resolve, or that agent silently runs with no data.
  for (const [id, a] of Object.entries(agents)) {
    const ref = (a as any)?.tool;
    if (ref && !(ref in tools)) {
      throw new Error(
        `workflow.json agent "${id}" references tool "${ref}", which is not a key in the "tools" block. The label must match exactly.`
      );
    }
  }
  return tools;
}

/**
 * Validate the AGENTS and STEPS, mirroring `terraform_data.workflow_validation`
 * in terraform/tools.tf so both IaC paths reject the same mistakes at synth.
 *
 * These used to go unchecked, which meant a bad id surfaced as a runtime
 * graph-build crash, an opaque deploy-time AgentCore error, or — worst — silently
 * empty retrievals with no error anywhere.
 */
/** The name a step is addressed by, and what a `branch` target names.
 *  Mirrors graph_builder._step_name and the same expression in terraform/tools.tf. */
export const stepName = (s: any, i: number): string => s.agent ?? s.gateId ?? `group${i}`;

const BRANCH_OPS = ["equals", "notEquals", "in", "contains", "exists", "gt", "gte", "lt", "lte"];
const BRANCH_NUM_OPS = ["gt", "gte", "lt", "lte"];
const BRANCH_TEXT_OPS = ["equals", "notEquals", "contains"];

/**
 * Validate every `branch` in `steps`, mirroring app/common/branching.validate_spec
 * and graph_builder.validate_branches.
 *
 * Each of these is a mistake that FAILS SILENTLY at runtime: a misspelled operator,
 * a rule with no comparison, or a target that names nothing all evaluate to "no
 * match", so the run quietly takes the default on every request and the branch looks
 * like it works. Catching them at synth is the difference between a typo and a
 * misrouted workload nobody notices.
 */
export function validateBranches(steps: any[]): void {
  const names = new Map<string, number>(steps.map((s, i) => [stepName(s, i), i]));
  if (steps.some((s) => s.branch) && names.size !== steps.length) {
    // A branch target names a step, so two steps sharing a name make a target
    // ambiguous — and the lookup above would silently resolve it to the later one.
    throw new Error(
      `app/workflow.json \`steps\` has duplicate step name(s), which makes a \`branch\` target ` +
        `ambiguous. Each step is named by its \`agent\` id or its \`gateId\`; give every step a ` +
        `distinct one. Names in order: ${steps.map((s, i) => stepName(s, i)).join(", ")}.`
    );
  }
  steps.forEach((step, i) => {
    const spec = step.branch;
    if (spec === undefined) return;
    const where = `app/workflow.json steps[${i}] (${stepName(step, i)})`;
    if (step.parallel) {
      throw new Error(
        `${where}: \`branch\` is not supported on a \`parallel\` step, because a group has no ` +
          `single agent whose output decides. Put the branch on a single-agent step, or on a ` +
          `\`sequence\` step (its LAST agent decides).`
      );
    }
    if (i === steps.length - 1) {
      throw new Error(
        `${where}: \`branch\` on the LAST step has nowhere to route. Use it on an earlier step, ` +
          `or drop it — "END" is already where the last step goes.`
      );
    }
    if (typeof spec !== "object" || Array.isArray(spec)) {
      throw new Error(`${where}: \`branch\` must be an object with \`when\` (and optionally \`default\`).`);
    }
    const unknown = Object.keys(spec).filter((k) => k !== "when" && k !== "default");
    if (unknown.length) {
      throw new Error(`${where}: \`branch\` has unknown key(s) ${unknown.join(", ")}. Valid keys: default, when.`);
    }
    if (spec.when === undefined && !String(spec.default ?? "").trim()) {
      throw new Error(
        `${where}: \`branch\` needs \`when\` (conditional rules), \`default\` (an unconditional ` +
          `jump to a later step), or both.`
      );
    }
    if (spec.when !== undefined && (!Array.isArray(spec.when) || !spec.when.length)) {
      throw new Error(
        `${where}: \`branch.when\` must be a non-empty list of rules. Omit it entirely for an ` +
          `unconditional jump via \`default\` alone.`
      );
    }
    (spec.when ?? []).forEach((rule: any, r: number) => {
      const at = `${where} branch.when[${r}]`;
      if (typeof rule !== "object" || rule === null || Array.isArray(rule)) {
        throw new Error(`${at}: each rule must be an object.`);
      }
      const bad = Object.keys(rule).filter((k) => k !== "field" && k !== "goto" && !BRANCH_OPS.includes(k));
      if (bad.length) {
        throw new Error(
          `${at}: unknown key(s) ${bad.join(", ")}. A rule is \`goto\`, an optional \`field\`, and ` +
            `one or more of ${BRANCH_OPS.join(", ")}.`
        );
      }
      if (!String(rule.goto ?? "").trim()) {
        throw new Error(`${at}: needs a \`goto\` naming the step to run when it matches (or "END").`);
      }
      if (!BRANCH_OPS.some((o) => o in rule)) {
        throw new Error(
          `${at}: has no comparison. Add one or more of ${BRANCH_OPS.join(", ")} — a rule with none ` +
            `would never match, so the branch would always take \`default\`.`
        );
      }
      if ("in" in rule && !Array.isArray(rule.in)) {
        throw new Error(`${at}: \`in\` takes a list of alternatives.`);
      }
      if ("exists" in rule && typeof rule.exists !== "boolean") {
        throw new Error(`${at}: \`exists\` takes true or false.`);
      }
      for (const op of BRANCH_NUM_OPS) {
        if (op in rule && !Number.isFinite(Number(rule[op]))) {
          throw new Error(`${at}: \`${op}\` takes a number, got ${JSON.stringify(rule[op])}.`);
        }
      }
      for (const op of BRANCH_TEXT_OPS) {
        if (op in rule && typeof rule[op] === "object" && rule[op] !== null) {
          throw new Error(
            `${at}: \`${op}\` takes a single value, not an object or list. Use \`in\` for a list of ` +
              `alternatives.`
          );
        }
      }
    });
    if ("default" in spec && !String(spec.default ?? "").trim()) {
      throw new Error(`${where}: \`branch.default\` is empty. Omit it to continue to the next step.`);
    }

    const targets = [
      ...(spec.when ?? []).map((r: any) => String(r.goto)),
      ...(spec.default ? [String(spec.default)] : []),
    ];
    for (const t of new Set(targets)) {
      if (t === "END") continue;
      const j = names.get(t);
      if (j === undefined) {
        throw new Error(
          `${where}: branch target "${t}" names no step. A target is "END", a single-agent step's ` +
            `\`agent\` id, or a group step's \`gateId\`. Known step names: ${[...names.keys()].join(", ")}.`
        );
      }
      if (j <= i) {
        throw new Error(
          `${where}: branch target "${t}" is step ${j}, at or before this one. Targets must be LATER ` +
            `steps — a backward edge is a cycle the run could not leave, and re-running earlier work ` +
            `is what a review gate's \`revise\` is for.`
        );
      }
    }
  });
}

export function validateWorkflow(workflow: any, orchRoot: string, agentName: string, idp: string): void {
  const agents: Record<string, any> = workflow.agents ?? {};
  const steps: any[] = workflow.steps ?? [];
  if (!steps.length) throw new Error("app/workflow.json `steps` is empty — there is no pipeline to run.");

  const stepIds = [
    ...new Set(steps.flatMap((s) => s.parallel ?? s.sequence ?? [s.agent])),
  ].filter(Boolean) as string[];

  const missing = stepIds.filter((id) => !(id in agents));
  if (missing.length) {
    throw new Error(
      `app/workflow.json \`steps\` names agent id(s) that are not in \`agents\`: ${missing.join(", ")}.`
    );
  }
  const orphans = Object.keys(agents).filter((id) => !stepIds.includes(id));
  if (orphans.length) {
    throw new Error(
      `app/workflow.json declares agent(s) that no \`steps\` entry runs: ${orphans.join(", ")}. ` +
        `Add them to \`steps\` or remove them.`
    );
  }

  validateBranches(steps);

  // The id becomes part of the AgentCore Runtime name, which only accepts
  // [a-zA-Z][a-zA-Z0-9_]* — a hyphen fails at DEPLOY time with an opaque error.
  const badIds = Object.keys(agents).filter((id) => !/^[a-zA-Z][a-zA-Z0-9_]*$/.test(id));
  if (badIds.length) {
    throw new Error(
      `Agent ids in app/workflow.json must match ^[a-zA-Z][a-zA-Z0-9_]*$ (letters, digits and ` +
        `underscores; no hyphens) because the id becomes part of the AgentCore Runtime name. ` +
        `Offending: ${badIds.join(", ")}.`
    );
  }
  const tooLong = Object.entries(agents)
    .filter(([, a]) => (a.runtime ?? "main") === "dedicated")
    .map(([id]) => `${agentName}_${id}`)
    .filter((n) => n.length > 48);
  if (tooLong.length) {
    throw new Error(
      `A dedicated agent's AgentCore Runtime name is "<agentName>_<agent id>" and must be 48 ` +
        `characters or fewer. Too long: ${tooLong.join(", ")}. Shorten agentName or the agent id.`
    );
  }

  // Corpora must be real top-level folders under kb_docs/, or the generated Cedar
  // permit filters on a doc_type no chunk carries and retrieval returns NOTHING
  // with no error at all.
  const kbEntry = Object.entries<any>(workflow.tools ?? {}).find(([, t]) => t?.type === "kb");
  const corpora: string[] = kbEntry ? kbEntry[1].corpora ?? [] : [];
  const kbDir = path.join(orchRoot, "kb_docs");
  if (corpora.length && fs.existsSync(kbDir)) {
    const entries = fs.readdirSync(kbDir, { withFileTypes: true });
    const folders = entries.filter((e) => e.isDirectory()).map((e) => e.name);
    const rootFiles = entries
      .filter((e) => e.isFile() && e.name !== ".DS_Store")
      .map((e) => e.name);
    const unknown = corpora.filter((c) => !folders.includes(c));
    if (unknown.length) {
      throw new Error(
        `tools.${kbEntry![0]}.corpora names folder(s) that do not exist under orchestrator/kb_docs/: ` +
          `${unknown.join(", ")}. Found: ${folders.join(", ") || "(none)"}.`
      );
    }
    if (rootFiles.length) {
      throw new Error(
        `These files sit at the root of orchestrator/kb_docs/: ${rootFiles.join(", ")}. Each document ` +
          `must live in a top-level FOLDER, because that folder name becomes its corpus (doc_type).`
      );
    }
  }

  // An agent's `corpus` must be one of the declared corpora.
  for (const [id, a] of Object.entries<any>(agents)) {
    if (a.corpus && !corpora.includes(a.corpus)) {
      throw new Error(
        `workflow.json agent "${id}" has corpus "${a.corpus}", which is not in ` +
          `tools.<kb>.corpora (${corpora.join(", ") || "none declared"}).`
      );
    }
  }

  // --- RBAC (authorization) ------------------------------------------------
  const authzActions: Record<string, string[]> = workflow.authorization?.actions ?? {};
  const restricted = Object.keys(authzActions);
  if (restricted.length && idp === "none") {
    // No authorizer means no claims, means no groups: every rule would evaluate
    // against an empty group set and DENY, locking the UI out of its own buttons.
    throw new Error(
      `app/workflow.json restricts ${restricted.join(", ")} in \`authorization.actions\`, but ` +
        `idp = "none" deploys the API with no authorizer, so there are no JWT claims to ` +
        `authorize against and every one of those actions would be denied. Use -c idp=cognito ` +
        `or -c idp=auth0, or remove \`authorization.actions\`.`
    );
  }
  // Mirrors ACTIONS in bff/authz.py. A typo'd key looks like a restriction but
  // gates nothing, leaving the real action wide open — so reject it at synth.
  const knownActions = ["cancel", "decision", "delete", "evaluate", "insights", "rerun", "start"];
  const unknownActions = restricted.filter((a) => !knownActions.includes(a));
  if (unknownActions.length) {
    throw new Error(
      `app/workflow.json \`authorization.actions\` names unknown action(s): ` +
        `${unknownActions.join(", ")}. Valid actions are ${knownActions.join(", ")} (see ACTIONS ` +
        `in bff/authz.py). An unrecognised key gates nothing.`
    );
  }
}

/** Every distinct group named anywhere in `authorization.actions`. */
export function authzGroups(workflow: any): string[] {
  const actions: Record<string, string[]> = workflow.authorization?.actions ?? {};
  return [...new Set(Object.values(actions).flat())].sort();
}

export interface OrchestratorStackProps extends cdk.StackProps {
  agentName: string;
  modelId: string;
  memoryEventExpiryDays: number;
  /** "cognito" | "auth0" | "none" — see terraform/identity.tf for the same switch. */
  idp: string;
  cognitoUserPoolId: string;
  cognitoClientId: string;
  cognitoDomainPrefix: string;
  auth0Domain: string;
  auth0ClientId: string;
  /** Create the AgentCore Gateway + Knowledge Base + Cedar policy (live MCP/RAG). */
  enableGateway: boolean;
  createCognito: boolean;
  /** Bring-your-own M2M client id (unused when Cognito is created here). */
  gatewayClientId: string;
  /** Bring-your-own M2M secret, from $GATEWAY_CLIENT_SECRET. Never a context key. */
  gatewayClientSecret: string;
  /** OAuth2 scope (Cognito) or API identifier (Auth0) for the M2M token. */
  gatewayAudience: string;
  /**
   * API keys for tools that need one, keyed by the workflow.json tool name.
   * From $TOOL_API_KEYS, never a context key (cdk.json is committed).
   */
  toolApiKeys: Record<string, string>;
  /** Percentage of spans indexed by CloudWatch Transaction Search (0-100). */
  transactionSearchIndexingPercentage: number;
}

// Repo layout: this file is orchestrator/cdk/lib/, so orchestrator/ is two levels up.
const ORCH_ROOT = path.join(__dirname, "..", "..");

/**
 * Full CDK equivalent of the Terraform deployment:
 *
 *   * the orchestrator AgentCore Runtime + one dedicated runtime per `dedicated` agent
 *   * AgentCore Memory ×2 (LangGraph checkpointer + long-term semantic/summary)
 *   * a Bedrock Guardrail, DynamoDB status/events/telemetry/insights stores
 *   * the BFF Lambda + HTTP API (JWT authorizer for the configured IdP)
 *   * the S3 + CloudFront UI
 *   * CloudWatch Transaction Search (so agent spans reach the aws/spans group)
 *   * with `enableGateway`: the AgentCore **Gateway**, a Bedrock **Knowledge Base**
 *     on S3 Vectors, the KB retrieve Lambda, and the **Cedar policy engine**
 *     (see ToolPlane in lib/tool-plane.ts)
 *
 * `enableGateway=false` is the same reduced mode as Terraform's
 * `enable_gateway = false`: no tool plane is provisioned, so any agent with a
 * `tool` raises ToolUnavailable rather than inventing evidence.
 */
export class OrchestratorStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: OrchestratorStackProps) {
    super(scope, id, props);

    const { agentName, modelId, memoryEventExpiryDays } = props;

    // ---- workflow.json: the single source of truth for agents + topology ----
    const workflow = JSON.parse(
      fs.readFileSync(path.join(ORCH_ROOT, "app", "workflow.json"), "utf8")
    );
    const agents: Record<string, any> = workflow.agents;
    const dedicatedIds = Object.keys(agents).filter(
      (id) => (agents[id].runtime ?? "main") === "dedicated"
    );

    // ---- Identity provider ----------------------------------------------------
    // Mirrors terraform/identity.tf: props.idp selects the provider, and the rest
    // of the stack consumes only the provider-agnostic values derived here
    // (authEnabled / jwtIssuer / jwtAudience / authConfig for the SPA).
    const isCognito = props.idp === "cognito";
    const isAuth0 = props.idp === "auth0";

    let cognitoUserPoolId = "";
    let cognitoClientId = "";
    let cognitoDomainPrefix = "";
    // The SPA client is created BEFORE the CloudFront distribution exists, so its
    // callback URLs can only be wired once we know the distribution domain. Held
    // here and completed further down (search: wire the Hosted UI callbacks).
    let spaClientL1: cognito.CfnUserPoolClient | undefined;
    // Machine-to-machine identity for agent -> Gateway. Created here only when
    // we're also creating the pool AND the Gateway is on (Terraform's
    // local.create_m2m); otherwise it's bring-your-own via gatewayIdentity*.
    let m2mClientId = "";
    let m2mClientSecret = "";
    let m2mAudience = "";

    if (isCognito && props.createCognito) {
      const pool = new cognito.UserPool(this, "UserPool", {
        userPoolName: `${agentName}-users`,
        // Admin-only user creation, matching terraform/cognito.tf
        // (allow_admin_create_user_only = true). This was `true`, which diverged
        // from the Terraform path and left OPEN self-registration on a public
        // CloudFront URL — anyone who found the URL could register and spend the
        // account's Bedrock budget. Create users deliberately:
        //   aws cognito-idp admin-create-user --user-pool-id <id> \
        //       --username you@example.com --message-action SUPPRESS \
        //       --user-attributes Name=email,Value=you@example.com Name=email_verified,Value=true
        selfSignUpEnabled: false,
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

      // The Hosted UI domain prefix must be globally unique, so it's suffixed with
      // the account. It also has to be a CONCRETE string at synth time (CDK
      // validates the charset), so fall back to a placeholder when synthesizing
      // without credentials — where this.account is an unresolved token.
      const acctSuffix = cdk.Token.isUnresolved(this.account) ? "local" : this.account.slice(-6);
      const domainPrefix = `${agentName.replace(/_/g, "-")}-${acctSuffix}`;
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

      spaClientL1 = client.node.defaultChild as cognito.CfnUserPoolClient;
      cognitoUserPoolId = pool.userPoolId;
      cognitoClientId = client.userPoolClientId;
      cognitoDomainPrefix = domainPrefix;

      // Groups named by workflow.json -> authorization.actions, so declaring RBAC
      // provisions the groups it needs (mirrors aws_cognito_user_group.authz).
      // Membership is NOT managed here — it is per-person and changes far more
      // often than a deploy:
      //   aws cognito-idp admin-add-user-to-group --user-pool-id <id> \
      //       --username you@example.com --group-name approvers
      const authzActionMap: Record<string, string[]> = workflow.authorization?.actions ?? {};
      for (const g of authzGroups(workflow)) {
        const grants = Object.entries(authzActionMap)
          .filter(([, gs]) => gs.includes(g))
          .map(([a]) => a);
        new cognito.CfnUserPoolGroup(this, `AuthzGroup${g.replace(/[^A-Za-z0-9]/g, "")}`, {
          userPoolId: pool.userPoolId,
          groupName: g,
          description: `Grants: ${grants.join(", ")} (from app/workflow.json authorization.actions).`,
        });
      }

      // ---- Cognito machine-to-machine identity (agent -> Gateway) ----------
      // The SPA client above logs USERS in. Gateway calls are a different
      // boundary: the runtime needs a client-credentials token, which requires a
      // Resource Server (to define the custom scope) plus a CONFIDENTIAL client.
      if (props.enableGateway) {
        const scopeName = "invoke";
        const resourceServer = pool.addResourceServer("GatewayResourceServer", {
          identifier: "gateway",
          userPoolResourceServerName: `${agentName}-gateway`,
          scopes: [
            new cognito.ResourceServerScope({
              scopeName,
              scopeDescription: "Invoke tools through the AgentCore Gateway",
            }),
          ],
        });
        const m2m = pool.addClient("M2mClient", {
          userPoolClientName: `${agentName}-m2m`,
          generateSecret: true,
          authFlows: {},
          oAuth: {
            flows: { clientCredentials: true },
            scopes: [
              cognito.OAuthScope.resourceServer(
                resourceServer,
                new cognito.ResourceServerScope({ scopeName, scopeDescription: "" })
              ),
            ],
          },
        });
        m2mClientId = m2m.userPoolClientId;
        // Resolved via a describe-user-pool-client lookup at deploy time, so the
        // secret never appears in the template body.
        m2mClientSecret = m2m.userPoolClientSecret.unsafeUnwrap();
        m2mAudience = "gateway/invoke";
      }

      new cdk.CfnOutput(this, "cognitoUserPoolId", { value: pool.userPoolId });
      new cdk.CfnOutput(this, "cognitoClientId", { value: client.userPoolClientId });
      new cdk.CfnOutput(this, "cognitoDomainPrefix", { value: domainPrefix });
    } else if (isCognito) {
      cognitoUserPoolId = props.cognitoUserPoolId;
      cognitoClientId = props.cognitoClientId;
      cognitoDomainPrefix = props.cognitoDomainPrefix;
    }

    // "none" is the only configuration that deploys without auth.
    const authEnabled = props.idp !== "none";

    // Fail at synth with a precise message rather than deploying a broken authorizer.
    if (isCognito && !props.createCognito && (!cognitoUserPoolId || !cognitoClientId || !cognitoDomainPrefix)) {
      throw new Error(
        'idp=cognito without -c createCognito=true requires -c cognitoUserPoolId=... -c cognitoClientId=... -c cognitoDomainPrefix=...'
      );
    }
    if (isAuth0 && (!props.auth0Domain || !props.auth0ClientId)) {
      throw new Error('idp=auth0 requires -c auth0Domain=... -c auth0ClientId=...');
    }

    // Cognito issues `iss` as the user-pool endpoint; Auth0 as the tenant domain
    // WITH a trailing slash. Both put the SPA client id in the ID token's `aud`.
    const jwtIssuer = isCognito
      ? `https://cognito-idp.${this.region}.amazonaws.com/${cognitoUserPoolId}`
      : isAuth0
        ? `https://${props.auth0Domain}/`
        : "";
    const jwtAudience = isCognito ? cognitoClientId : props.auth0ClientId;

    // ---- Agent -> Gateway machine identity (provider-agnostic) --------------
    // Either created above (Cognito + createCognito) or supplied by the operator.
    const createdM2m = m2mClientId !== "";
    const gatewayClientId = createdM2m ? m2mClientId : props.gatewayClientId;
    const gatewayClientSecret = createdM2m ? m2mClientSecret : props.gatewayClientSecret;
    // Cognito -> the OAuth2 scope; Auth0 -> the API identifier.
    const gatewayAudience = createdM2m ? m2mAudience : props.gatewayAudience;

    // OIDC discovery + token endpoints, per provider.
    const gatewayDiscoveryUrl = isCognito
      ? `https://cognito-idp.${this.region}.amazonaws.com/${cognitoUserPoolId}/.well-known/openid-configuration`
      : isAuth0
        ? `https://${props.auth0Domain}/.well-known/openid-configuration`
        : "";
    const gatewayTokenUrl = isCognito
      ? `https://${cognitoDomainPrefix}.auth.${this.region}.amazoncognito.com/oauth2/token`
      : isAuth0
        ? `https://${props.auth0Domain}/oauth/token`
        : "";

    // Same guardrails as terraform/identity.tf, enforced at synth.
    if (props.enableGateway) {
      if (props.idp === "none") {
        throw new Error(
          'idp=none cannot be combined with -c enableGateway=true: the Gateway\'s CUSTOM_JWT ' +
            "authorizer needs an OIDC provider. Use -c enableGateway=false (no tool plane is " +
            "then provisioned, so agents with a `tool` fail loudly), or pick an idp."
        );
      }
      if (!createdM2m && (!gatewayClientId || !gatewayAudience || !gatewayClientSecret)) {
        throw new Error(
          "enableGateway=true requires -c gatewayClientId=... -c gatewayAudience=... and " +
            "GATEWAY_CLIENT_SECRET in the environment (the OAuth2 scope for Cognito, or the API " +
            "identifier for Auth0), unless idp=cognito with -c createCognito=true."
        );
      }
    }

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
      // The writer stamps `ttl` on every row (app/features/observability/store.py,
      // TELEMETRY_TTL_DAYS, default 90 days). Mirrors terraform/observability.tf.
      timeToLiveAttribute: "ttl",
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    telemetryTable.addGlobalSecondaryIndex({
      indexName: "by_date",
      partitionKey: { name: "date", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "sk", type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // ---- Insights findings store (AgentCore Optimization) -------------------
    // One reserved item (id="latest") holding the newest cross-run findings.
    const insightsTable = new dynamodb.Table(this, "InsightsTable", {
      tableName: `${agentName}_insights`,
      partitionKey: { name: "id", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // ---- AgentCore Memory ×2 -----------------------------------------------
    // No stable L2 for AgentCore yet; use the CloudFormation resource types
    // directly (the same types the Terraform awscc provider drives).
    //
    // 1) the durable LangGraph checkpointer (short-term run state / HITL resume)
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

    // 2) LONG-TERM memory with extraction strategies. Separate resource: agents
    // that enable memory.longTerm in workflow.json recall past insights before
    // running and store their output after. Namespaces are derived at run time
    // from actorId = "<agentId>-<subjectSlug>", so adding an agent or subject
    // needs no infrastructure change.
    const semanticMemory = new cdk.CfnResource(this, "SemanticMemory", {
      type: "AWS::BedrockAgentCore::Memory",
      properties: {
        Name: `${agentName}_semantic`,
        EventExpiryDuration: memoryEventExpiryDays,
        Description: "Long-term semantic memory (per-agent, per-subject insights)",
        MemoryStrategies: [
          { SemanticMemoryStrategy: { Name: "insights", Namespaces: ["insights/{actorId}"] } },
          // Summarization is inherently per-session, so AgentCore REQUIRES
          // {sessionId} here (unlike semantic, which is actor-scoped).
          { SummaryMemoryStrategy: { Name: "summary", Namespaces: ["summary/{actorId}/{sessionId}"] } },
        ],
      },
    });
    const semanticMemoryId = semanticMemory.getAtt("MemoryId").toString();
    const semanticMemoryArn = semanticMemory.getAtt("MemoryArn").toString();

    // Guardrail policy, projected from workflow.json (see buildGuardrail).
    const gr = buildGuardrail(workflow);

    // ---- Bedrock Guardrail (content safety) --------------------------------
    // The POLICY comes from the `guardrail` block in workflow.json, so adapting
    // content safety to a customer's domain is a CONFIG edit — nothing here is
    // domain-specific. Mirrors terraform/guardrail.tf.
    //
    // Its id is injected as GUARDRAIL_ID, so workflow.json only needs the
    // per-agent flip ("guardrails": {"input": true, "output": true}).
    // Every sub-policy is optional: omit a key and that block is not emitted at
    // all, because Bedrock rejects an empty policy config.
    const guardrail = new cdk.CfnResource(this, "Guardrail", {
      type: "AWS::Bedrock::Guardrail",
      properties: {
        Name: `${agentName}-guardrail`,
        Description: "Content-safety guardrail generated from app/workflow.json",
        BlockedInputMessaging: gr.blockedInputMessage,
        BlockedOutputsMessaging: gr.blockedOutputMessage,
        ...(gr.contentFilters.length
          ? { ContentPolicyConfig: { FiltersConfig: gr.contentFilters } }
          : {}),
        ...(gr.deniedWords.length || gr.managedWordLists.length
          ? {
              WordPolicyConfig: {
                ...(gr.deniedWords.length ? { WordsConfig: gr.deniedWords } : {}),
                ...(gr.managedWordLists.length
                  ? { ManagedWordListsConfig: gr.managedWordLists }
                  : {}),
              },
            }
          : {}),
        ...(gr.deniedTopics.length
          ? { TopicPolicyConfig: { TopicsConfig: gr.deniedTopics } }
          : {}),
        ...(gr.piiEntities.length
          ? { SensitiveInformationPolicyConfig: { PiiEntitiesConfig: gr.piiEntities } }
          : {}),
      },
    });
    const guardrailId = guardrail.getAtt("GuardrailId").toString();
    const guardrailArn = guardrail.getAtt("GuardrailArn").toString();

    // Feature env vars shared by the orchestrator and every dedicated runtime.
    // GenAI Observability: AgentCore injects the ADOT distro + OTLP endpoint;
    // always_on defeats the sampled=0 context the UI path propagates; the
    // bundled LangChain instrumentation is disabled because it emits a second,
    // token-less LLM span. (Same reasoning as terraform/main.tf.)
    const observabilityEnv = {
      AGENT_OBSERVABILITY_ENABLED: "true",
      OTEL_TRACES_SAMPLER: "always_on",
      OTEL_PYTHON_DISABLED_INSTRUMENTATIONS: "aws_langchain",
    };
    const featureEnv = {
      ...observabilityEnv,
      SEMANTIC_MEMORY_ID: semanticMemoryId,
      GUARDRAIL_ID: guardrailId,
      GUARDRAIL_VERSION: "DRAFT",
      // Evaluations reads spans from the runtime log group(s); the exact
      // id-suffixed name is only known after the runtime exists, so inject the
      // stable prefix and discover the match at query time.
      SPAN_LOG_GROUP_PREFIX: `/aws/bedrock-agentcore/runtimes/${agentName}-`,
    };

    // Permissions the AgentCore features need, on top of observabilityPerms.
    const guardrailApply = new iam.PolicyStatement({
      actions: ["bedrock:ApplyGuardrail"],
      resources: [guardrailArn],
    });
    const longTermMemory = new iam.PolicyStatement({
      actions: [
        "bedrock-agentcore:CreateEvent",
        "bedrock-agentcore:RetrieveMemories",
        "bedrock-agentcore:RetrieveMemoryRecords",
        "bedrock-agentcore:ListMemoryRecords",
        "bedrock-agentcore:GetMemoryRecord",
      ],
      resources: [semanticMemoryArn, `${semanticMemoryArn}/*`],
    });
    // Evaluations + Insights: Evaluate/BatchEvaluation resources are created
    // dynamically and the Logs query verbs aren't resource-scopable, so "*".
    const evaluationsAndInsights = new iam.PolicyStatement({
      actions: [
        "bedrock-agentcore:Evaluate",
        "bedrock-agentcore:StartBatchEvaluation",
        "bedrock-agentcore:GetBatchEvaluation",
        "bedrock-agentcore:ListBatchEvaluations",
        // Read the spans Evaluations/Insights analyze.
        "logs:DescribeLogGroups",
        "logs:StartQuery",
        "logs:GetQueryResults",
        "logs:StopQuery",
        "logs:FilterLogEvents",
        "logs:GetLogEvents",
        // StartBatchEvaluation WRITES its output to a CloudWatch log group it
        // creates on the caller's behalf (a forward access session), so these are
        // required too — without them Insights fails with
        // "FAS credentials do not have permission to create CloudWatch log groups".
        // Note this cannot be scoped to the runtime log-group prefix: the group
        // BatchEvaluation creates is outside it.
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "logs:PutRetentionPolicy",
      ],
      resources: ["*"],
    });

    // ---- Tool plane: Gateway + Knowledge Base + Cedar policy ----------------
    // The whole subsystem lives in lib/tool-plane.ts (mirroring Terraform's
    // gateway.tf + kb.tf + policy.tf). Absent it, any agent with a `tool` fails
    // loudly (app/common/errors.py: ToolUnavailable) instead of inventing evidence.
    //
    // Cedar policy is on/off from workflow.json, exactly like Terraform:
    //   orchestrator.policy.enabled (default true), .mode (default ENFORCE).
    const policyEnabled =
      props.enableGateway && (workflow.orchestrator?.policy?.enabled ?? true);
    const policyMode = String(workflow.orchestrator?.policy?.mode ?? "ENFORCE").toUpperCase();

    // Every tool the agents may call is declared in workflow.json — the same
    // block Terraform reads. Validated before use so a typo fails at synth.
    const declaredTools: Record<string, ToolSpec> = validateTools(
      workflow.tools ?? {},
      workflow.agents ?? {},
      this.region
    );
    // Agents + steps + kb_docs corpora (see terraform_data.workflow_validation).
    validateWorkflow(workflow, ORCH_ROOT, agentName, props.idp);

    const toolPlane = props.enableGateway
      ? new ToolPlane(this, "ToolPlane", {
          agentName,
          tools: declaredTools,
          toolApiKeys: props.toolApiKeys,
          // Only used by the built-in `source: "tool_lambda"` demo function, which
          // reports on run history and gets READ-ONLY access to them.
          statusTable,
          telemetryTable,
          gatewayDiscoveryUrl,
          gatewayClientId,
          gatewayAudience,
          isCognito,
          isAuth0,
          policyEnabled,
          policyMode,
          orchRoot: ORCH_ROOT,
        })
      : undefined;

    // Gateway-backed MCP access. Empty strings when the Gateway is off, in which
    // case any agent with a `tool` fails loudly rather than inventing an answer
    // (see app/common/errors.py: ToolUnavailable).
    const gatewayEnv = {
      GATEWAY_URL: toolPlane?.gatewayUrl ?? "",
      GATEWAY_TOKEN_URL: props.enableGateway ? gatewayTokenUrl : "",
      GATEWAY_CLIENT_ID: props.enableGateway ? gatewayClientId : "",
      GATEWAY_CLIENT_SECRET: props.enableGateway ? gatewayClientSecret : "",
      // Which client-credentials request shape to build ("cognito" | "auth0").
      GATEWAY_AUTH_FLOW: props.enableGateway ? props.idp : "",
      GATEWAY_AUDIENCE: props.enableGateway ? gatewayAudience : "",
      // Cedar mode in effect at the Gateway, so the UI can label decisions.
      // Enforcement itself is server-side; this is display-only.
      GATEWAY_POLICY_MODE: toolPlane?.policyModeEnv ?? "",
      // How to CALL each tool (argument shape, corpus, per-type options), so an
      // agent reaches a newly declared data source with no code change. This was
      // MISSING on the CDK path: app/common/config.py then falls back to the `tools`
      // block of the workflow.json baked into the image, which keeps only a subset of
      // the fields — so a request-level option set in config was silently dropped
      // here while Terraform honoured it. Mirrors terraform/main.tf TOOLS_JSON.
      TOOLS_JSON: JSON.stringify(toolsEnv(declaredTools)),
    };

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
      // Inference profiles, not account-wide bedrock:* — that also covered custom
      // models, provisioned throughput, agents, guardrails and prompts, none of which
      // the runtime invokes. Mirrors terraform/main.tf.
      resources: [
        "arn:aws:bedrock:*::foundation-model/*",
        `arn:aws:bedrock:${this.region}:${this.account}:inference-profile/*`,
        `arn:aws:bedrock:${this.region}:${this.account}:application-inference-profile/*`,
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
        // AgentCore "unified" telemetry: AgentCore adds a CloudWatch Logs resource
        // policy so X-Ray can deliver spans to the agent's own log group. Without
        // this, unified spans never reach CloudWatch. Account-scoped API, so "*".
        // Mirrors terraform/main.tf AgentCoreUnifiedSpanDelivery.
        actions: ["logs:PutResourcePolicy"],
        resources: ["*"],
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
    [
      bedrockInvoke,
      ...observabilityPerms(`${agentName}*`),
      // A dedicated agent applies guardrails and uses long-term memory exactly
      // like an in-process one when its workflow.json config enables them.
      guardrailApply,
      longTermMemory,
    ].forEach((s) => subagentRole.addToPolicy(s));
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
            ...featureEnv,
            ...gatewayEnv,
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
    [
      bedrockInvoke,
      ...observabilityPerms(`${agentName}-*`),
      guardrailApply,
      longTermMemory,
      evaluationsAndInsights,
    ].forEach((s) => runtimeRole.addToPolicy(s));
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
    // Read/write, not write-only: Evaluations scores each prompt from its
    // persisted model-call I/O, which means querying the telemetry table back.
    telemetryTable.grantReadWriteData(runtimeRole);
    insightsTable.grantReadWriteData(runtimeRole);

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
          INSIGHTS_TABLE: insightsTable.tableName,
          AGENT_RUNTIME_ARNS: arnMapJson,
          ...featureEnv,
          ...gatewayEnv,
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
    // Lambda caps the TOTAL environment at 4 KB. The other variables are ARNs and
    // table names (~1 KB together), so fail at SYNTH with an actionable message
    // rather than letting a large workflow.json surface as an opaque Lambda error
    // ten minutes into a deploy. Terraform has the same guard in bff.tf.
    const workflowJson = JSON.stringify(buildBffWorkflow(workflow));
    const WORKFLOW_JSON_MAX = 3000;
    if (workflowJson.length > WORKFLOW_JSON_MAX) {
      throw new Error(
        `The workflow projection shipped to the BFF is ${workflowJson.length} bytes, over the ` +
          `${WORKFLOW_JSON_MAX}-byte budget (Lambda's whole environment is capped at 4 KB). ` +
          `Shorten agent "name" values in workflow.json, or move the projection to S3/SSM ` +
          `and have bff/handler.py read it from there.`
      );
    }
    // Own every Lambda log group explicitly. Left implicit, Lambda creates
    // /aws/lambda/<name> itself with NEVER-EXPIRE retention and no stack ownership,
    // so `cdk destroy` leaves it behind accruing cost forever — verified: a full
    // destroy of this stack orphaned eight such groups. Mirrors the
    // aws_cloudwatch_log_group resources on the Terraform path (var.log_retention_days).
    const lambdaLogGroup = (id: string, fnName: string) =>
      new logs.LogGroup(this, id, {
        logGroupName: `/aws/lambda/${fnName}`,
        retention: LOG_RETENTION,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      });

    const bff = new lambda.Function(this, "Bff", {
      functionName: bffFunctionName,
      logGroup: lambdaLogGroup("BffLogGroup", bffFunctionName),
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
        WORKFLOW_JSON: workflowJson,
        // The assistant's tool-use loop runs in this Lambda, so it needs the
        // deployment's model rather than its own copy of the id.
        MODEL_ID: props.modelId,
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
    // The in-app assistant's tool-use loop runs IN the BFF (bff/chatbot.py calls
    // bedrock-runtime Converse), so the BFF needs its own model grant. Without it
    // the chat UI deploys, accepts a message, and every reply is
    // "couldn't reach the model (AccessDeniedException)" — which is exactly what a
    // CDK deployment did, because only the runtime roles carried `bedrockInvoke`.
    // Mirrors terraform/bff.tf. Scoped to the inference profile + foundation
    // models, not the account-wide bedrock:* the runtime roles use.
    bff.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
        resources: [
          "arn:aws:bedrock:*::foundation-model/*",
          `arn:aws:bedrock:${this.region}:${this.account}:inference-profile/*`,
        ],
      })
    );

    const api = new HttpApi(this, "HttpApi", { apiName: `AgentCoreBFF-${agentName}` });
    const integration = new HttpLambdaIntegration("BffIntegration", bff);
    // Provider-agnostic JWT authorizer; omitted entirely when idp = "none",
    // which leaves /api/* OPEN.
    const authorizer = authEnabled
      ? new HttpJwtAuthorizer("JwtAuthorizer", jwtIssuer, { jwtAudience: [jwtAudience] })
      : undefined;

    const routes: Array<{ path: string; methods: HttpMethod[] }> = [
      { path: "/api/workflow", methods: [HttpMethod.GET] },
      // Who the caller is and which run actions they may take, so the UI can
      // disable controls instead of offering buttons that 403.
      { path: "/api/me", methods: [HttpMethod.GET] },
      { path: "/api/sessions", methods: [HttpMethod.GET, HttpMethod.POST] },
      { path: "/api/sessions/{id}", methods: [HttpMethod.GET, HttpMethod.DELETE] },
      { path: "/api/sessions/{id}/decision", methods: [HttpMethod.POST] },
      { path: "/api/sessions/{id}/cancel", methods: [HttpMethod.POST] },
      // Rewind & re-run a settled run from a chosen agent.
      { path: "/api/sessions/{id}/rerun", methods: [HttpMethod.POST] },
      // AgentCore Evaluations on demand (auto-eval also runs at completion).
      { path: "/api/sessions/{id}/evaluate", methods: [HttpMethod.POST] },
      // AgentCore Optimization / Insights: cross-run findings.
      { path: "/api/insights", methods: [HttpMethod.GET] },
      { path: "/api/insights/run", methods: [HttpMethod.POST] },
      { path: "/api/sessions/{id}/telemetry", methods: [HttpMethod.GET] },
      { path: "/api/telemetry/aggregate", methods: [HttpMethod.GET] },
      // In-app assistant (bff/chatbot.py).
      { path: "/api/chat", methods: [HttpMethod.POST] },
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

    // ---- wire the Hosted UI callbacks -------------------------------------
    // Cognito rejects an /authorize request whose redirect_uri is not registered
    // on the client, and the Hosted UI shows only "An error was encountered with
    // the requested page." So the CloudFront URL MUST be on the SPA client.
    // Terraform does this in cognito.tf; without it CDK login is simply broken.
    // "https://localhost" is kept for local development against the same pool.
    if (spaClientL1) {
      const uiOrigin = `https://${distribution.distributionDomainName}`;
      spaClientL1.callbackUrLs = [uiOrigin, "https://localhost"];
      spaClientL1.logoutUrLs = [uiOrigin, "https://localhost"];
    }

    // Non-secret IdP config the SPA reads. Same shape as the Terraform template
    // (web/auth-config.js.tftpl) so web/index.html works under either IaC:
    // `provider` selects the login strategy, and unused fields stay empty.
    const authConfigJs =
      `// Generated by CDK. Non-secret IdP settings for the SPA.\n` +
      `// provider: "cognito" | "auth0" | "none" (enabled=false).\n` +
      `window.AUTH_CONFIG = {\n` +
      `  enabled: ${authEnabled},\n` +
      `  provider: ${JSON.stringify(props.idp)},\n` +
      `  region: ${JSON.stringify(this.region)},\n` +
      `  userPoolId: ${JSON.stringify(cognitoUserPoolId)},\n` +
      `  domainPrefix: ${JSON.stringify(cognitoDomainPrefix)},\n` +
      `  domain: ${JSON.stringify(isAuth0 ? props.auth0Domain : "")},\n` +
      `  clientId: ${JSON.stringify(jwtAudience)}\n` +
      `};\n`;

    new s3deploy.BucketDeployment(this, "UiDeploy", {
      // The CDK framework Lambda behind this construct also leaves a never-expire log
      // group behind on destroy unless we own it.
      logGroup: new logs.LogGroup(this, "UiDeployLogGroup", {
        retention: LOG_RETENTION,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
      destinationBucket: uiBucket,
      distribution,
      distributionPaths: ["/*"],
      sources: [
        s3deploy.Source.asset(path.join(ORCH_ROOT, "web"), { exclude: ["*.tftpl"] }),
        s3deploy.Source.data("auth-config.js", authConfigJs),
      ],
    });

    // ---- CloudWatch Transaction Search --------------------------------------
    // AgentCore Observability and the Evaluations/Insights features read agent
    // OTEL spans from the CloudWatch "aws/spans" log group. Spans only land there
    // once Transaction Search is on for the account (it switches X-Ray span
    // ingestion into CloudWatch Logs) — normally a manual console step per new
    // account, provisioned here so a fresh-account deploy is self-contained.

    // Resource policy letting X-Ray deliver spans to those log groups. Keyed by
    // name and updated in place, so it is safe to manage declaratively.
    const tsLogPolicy = new logs.CfnResourcePolicy(this, "TransactionSearchLogPolicy", {
      policyName: `${agentName}-transaction-search`,
      policyDocument: JSON.stringify({
        Version: "2012-10-17",
        Statement: [
          {
            Sid: "TransactionSearchXRayAccess",
            Effect: "Allow",
            Principal: { Service: "xray.amazonaws.com" },
            Action: "logs:PutLogEvents",
            Resource: [
              `arn:aws:logs:${this.region}:${this.account}:log-group:aws/spans:*`,
              `arn:aws:logs:${this.region}:${this.account}:log-group:/aws/application-signals/data:*`,
            ],
            Condition: {
              ArnLike: { "aws:SourceArn": `arn:aws:xray:${this.region}:${this.account}:*` },
              StringEquals: { "aws:SourceAccount": this.account },
            },
          },
        ],
      }),
    });

    // Enablement needs a READ-THEN-WRITE, so it is a small Lambda-backed custom
    // resource rather than anything declarative. Two dead ends ruled this out:
    //
    //   * AWS::XRay::TransactionSearchConfig — Transaction Search is an
    //     ACCOUNT-WIDE SINGLETON, so that resource can only ever CREATE it and
    //     fails with AlreadyExists wherever it is already on.
    //   * a bare UpdateTraceSegmentDestination call — NOT idempotent: it raises
    //     InvalidRequestException("The destination is already set to
    //     CloudWatchLogs") on a second call, which fails the whole stack.
    //
    // So this checks the current destination first and only writes when it needs
    // to change — the same logic as the Terraform null_resource. Re-deploying, or
    // deploying into an account where Transaction Search is already on, is a
    // clean no-op instead of an error.
    const tsHandler = new lambda.Function(this, "TransactionSearchHandler", {
      // Unnamed function -> CDK generates the name, so let it generate the log group
      // too and just pin retention + removal.
      logGroup: new logs.LogGroup(this, "TransactionSearchHandlerLogGroup", {
        retention: LOG_RETENTION,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: "index.handler",
      timeout: cdk.Duration.minutes(2),
      description: "Idempotently enables CloudWatch Transaction Search for this account/region",
      code: lambda.Code.fromInline(`
import boto3

def handler(event, context):
    # Deletion is a deliberate no-op: Transaction Search is account-wide and
    # other stacks or services may depend on it, so tearing it down on a
    # \`cdk destroy\` of this stack would be a surprise.
    if event["RequestType"] == "Delete":
        return {"PhysicalResourceId": event["PhysicalResourceId"]}

    pct = int(event["ResourceProperties"]["IndexingPercentage"])
    xray = boto3.client("xray")

    current = xray.get_trace_segment_destination().get("Destination")
    changed = False
    if current != "CloudWatchLogs":
        # Enabling Transaction Search the FIRST time in an account makes X-Ray call
        # Application Signals and CloudTrail on your behalf, so it needs more than
        # the xray:* actions (see the role policy below). Those calls can still be
        # refused by an SCP or permissions boundary we cannot see from here.
        #
        # Transaction Search is an OBSERVABILITY enhancement, not something the
        # agents need in order to run, so a refusal must NOT fail the deployment
        # and take the whole stack with it. Degrade and report instead.
        try:
            xray.update_trace_segment_destination(Destination="CloudWatchLogs")
            changed = True
        except Exception as e:
            return {
                "PhysicalResourceId": "transaction-search",
                "Data": {
                    "Destination": current or "None",
                    "Changed": "False",
                    "Indexing": "skipped",
                    "Warning": (
                        "Transaction Search NOT enabled: "
                        + type(e).__name__ + ": " + str(e)[:180]
                        + " -- tracing still works; enable it later with "
                        + "'aws xray update-trace-segment-destination "
                        + "--destination CloudWatchLogs'."
                    ),
                },
            }

    # Best effort: never fail the deploy over the indexed percentage, since
    # Transaction Search itself is on by this point.
    try:
        xray.update_indexing_rule(
            Name="Default",
            Rule={"Probabilistic": {"DesiredSamplingPercentage": pct}},
        )
        indexing = "set"
    except Exception as e:
        indexing = f"skipped ({type(e).__name__})"

    return {
        "PhysicalResourceId": "transaction-search",
        "Data": {
            "Destination": "CloudWatchLogs",
            "Changed": str(changed),
            "Indexing": indexing,
        },
    }
`),
    });
    tsHandler.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          "xray:GetTraceSegmentDestination",
          "xray:UpdateTraceSegmentDestination",
          "xray:UpdateIndexingRule",
          // UpdateTraceSegmentDestination calls Application Signals on your
          // behalf the FIRST time Transaction Search is enabled in an account, so
          // without this the custom resource fails with
          //   AccessDeniedException ... not authorized to perform:
          //   application-signals:StartDiscovery
          // It is easy to miss because an account where Application Signals is
          // already discovered never triggers the call — this only bites on a
          // clean account. Terraform doesn't need the grant: it runs the
          // equivalent step via local-exec using the deployer's own credentials.
          "application-signals:StartDiscovery",
          "xray:GetIndexingRules",
          // StartDiscovery creates a service-linked CloudTrail event channel and
          // the Application Signals service-linked role, so both of these are
          // required on a first-time enable.
          "cloudtrail:CreateServiceLinkedChannel",
          "iam:CreateServiceLinkedRole",
        ],
        resources: ["*"],
      })
    );

    const tsProvider = new cr.Provider(this, "TransactionSearchProvider", {
      onEventHandler: tsHandler,
    });
    const tsEnable = new cdk.CustomResource(this, "TransactionSearchEnable", {
      serviceToken: tsProvider.serviceToken,
      properties: {
        // Included so a changed percentage triggers an Update.
        IndexingPercentage: props.transactionSearchIndexingPercentage,
      },
    });
    tsEnable.node.addDependency(tsLogPolicy);

    // ---- Outputs ------------------------------------------------------------
    new cdk.CfnOutput(this, "uiUrl", { value: `https://${distribution.distributionDomainName}` });
    new cdk.CfnOutput(this, "apiEndpoint", { value: api.apiEndpoint });
    new cdk.CfnOutput(this, "idp", { value: props.idp });
    if (toolPlane) {
      new cdk.CfnOutput(this, "gatewayUrl", { value: toolPlane.gatewayUrl });
      new cdk.CfnOutput(this, "gatewayId", { value: toolPlane.gatewayId });
      new cdk.CfnOutput(this, "knowledgeBaseId", { value: toolPlane.knowledgeBaseId });
    }
    new cdk.CfnOutput(this, "agentRuntimeArn", { value: orchestratorArn });
    new cdk.CfnOutput(this, "memoryId", { value: memoryId });
    new cdk.CfnOutput(this, "imageUri", { value: image.imageUri });
    new cdk.CfnOutput(this, "dedicatedAgents", { value: dedicatedIds.join(", ") || "(none)" });
    new cdk.CfnOutput(this, "authEnabled", { value: String(authEnabled) });
  }
}

/**
 * Project the `guardrail` block of workflow.json into CloudFormation shapes for
 * AWS::Bedrock::Guardrail, mirroring the dynamic blocks in terraform/guardrail.tf.
 *
 * Returns already-shaped arrays so the caller can omit an empty policy entirely —
 * Bedrock rejects an empty *PolicyConfig rather than treating it as "no policy".
 */
export function buildGuardrail(workflow: any): {
  blockedInputMessage: string;
  blockedOutputMessage: string;
  contentFilters: Array<Record<string, string>>;
  deniedWords: Array<{ Text: string }>;
  managedWordLists: Array<{ Type: string }>;
  deniedTopics: Array<Record<string, unknown>>;
  piiEntities: Array<{ Type: string; Action: string }>;
} {
  const g = workflow.guardrail ?? {};
  const STRENGTHS = ["NONE", "LOW", "MEDIUM", "HIGH"];
  const ACTIONS = ["BLOCK", "ANONYMIZE"];

  const contentFilters = Object.entries<string>(g.contentFilters ?? {}).map(([type, strength]) => {
    const t = type.toUpperCase();
    const s = String(strength).toUpperCase();
    if (!STRENGTHS.includes(s)) {
      throw new Error(
        `workflow.json guardrail.contentFilters.${type} is "${strength}"; use NONE, LOW, MEDIUM or HIGH.`
      );
    }
    return {
      Type: t,
      InputStrength: s,
      // Prompt-attack filtering is input-only; Bedrock rejects an output strength.
      OutputStrength: t === "PROMPT_ATTACK" ? "NONE" : s,
    };
  });

  const piiEntities = Object.entries<string>(g.piiEntities ?? {}).map(([type, action]) => {
    const a = String(action).toUpperCase();
    if (!ACTIONS.includes(a)) {
      throw new Error(
        `workflow.json guardrail.piiEntities.${type} is "${action}"; use BLOCK or ANONYMIZE.`
      );
    }
    return { Type: type.toUpperCase(), Action: a };
  });

  const deniedTopics = (g.deniedTopics ?? []).map((t: any) => {
    if (!t?.name || !t?.definition) {
      throw new Error(
        'Each workflow.json guardrail.deniedTopics entry needs a "name" and a "definition".'
      );
    }
    return {
      Name: t.name,
      Definition: t.definition,
      ...(t.examples?.length ? { Examples: t.examples } : {}),
      Type: "DENY",
    };
  });

  return {
    blockedInputMessage:
      g.blockedInputMessage ?? "This request was blocked by the content guardrail.",
    blockedOutputMessage:
      g.blockedOutputMessage ?? "The generated content was blocked by the content guardrail.",
    contentFilters,
    deniedWords: (g.deniedWords ?? []).map((w: string) => ({ Text: w })),
    managedWordLists: (g.managedWordLists ?? []).map((w: string) => ({ Type: String(w).toUpperCase() })),
    deniedTopics,
    piiEntities,
  };
}

/**
 * Trim workflow.json to what the BFF and UI need, mirroring the Terraform
 * `local.bff_workflow` in terraform/bff.tf EXACTLY. Shipped as WORKFLOW_JSON.
 *
 * Keep the two in step. This projection previously emitted `mcp`/`rag`, which
 * workflow.json renamed to `tool`/`corpus`, and omitted `evalAgents`/`chatbot`
 * altogether — so CDK deployments silently lost the data-source chips, the
 * Evaluate buttons and the in-app assistant while Terraform ones kept them.
 *
 * Trimmed and compact because it travels in a Lambda env var, which is capped at
 * 4 KB for the whole environment (see the size guard in the caller).
 */
export function buildBffWorkflow(workflow: any): any {
  // tool name -> declared type, for the UI's data-source chip. Read from the raw
  // tools block so the label is right even when the Gateway is disabled.
  const toolTypes: Record<string, string> = {};
  for (const [n, t] of Object.entries<any>(workflow.tools ?? {})) {
    toolTypes[n] = String(t?.type ?? "mcp").toLowerCase();
  }

  const agentsOut: Record<string, any> = {};
  for (const [id, a] of Object.entries<any>(workflow.agents)) {
    const access0: string = Array.isArray(a.access) && a.access[0] ? a.access[0] : "";
    const tool: string | null = a.tool ?? null;
    const corpus: string | null = a.corpus ?? null;
    // Display-ready label, derived from the tool's declared TYPE so a new tool
    // type gets a sensible chip with no UI change.
    let source = "\u2014";
    if (tool) {
      const ty = toolTypes[tool];
      if (ty === "kb") source = `Knowledge Base \u00b7 ${corpus ?? "all"}`;
      else if (ty === "websearch") source = "Web Search";
      else if (ty === "openapi") source = `REST API \u00b7 ${tool}`;
      else if (ty === "lambda") source = `Function \u00b7 ${tool}`;
      else source = `MCP \u00b7 ${tool}`;
    } else if (/session/i.test(access0)) source = "Session input";
    else if (/upstream/i.test(access0)) source = "Upstream agent outputs";

    agentsOut[id] = {
      name: a.name,
      kind: a.kind ?? "sync",
      runtime: a.runtime ?? "main",
      tool,
      corpus,
      model: a.model ?? null,
      source,
    };
  }

  // Which agents show an "Evaluate" button (short id array, to stay under 4 KB).
  const evalAgents = Object.entries<any>(workflow.agents)
    .filter(([, a]) => a?.agentcore?.evaluations?.enabled === true)
    .map(([id]) => id);

  // In-app assistant. Lean: enabled + model + greeting/placeholder + only the
  // DISABLED tool flags (the backend defaults any tool it isn't told about to ON).
  // greeting/placeholder ARE shipped — they were not, which made those two
  // workflow.json keys decorative: a customer edited them and the UI kept showing
  // its own hardcoded strings. Mirrors terraform/bff.tf.
  const cb = workflow.orchestrator?.chatbot;
  const chatbot =
    cb?.enabled === undefined
      ? null
      : {
          enabled: cb.enabled,
          model: cb.model ?? null,
          greeting: cb.greeting ?? null,
          placeholder: cb.placeholder ?? null,
          tools: Object.fromEntries(
            Object.entries<any>(cb.tools ?? {}).filter(([, v]) => v === false)
          ),
        };

  // Presentation strings, so branding is config rather than an index.html edit.
  const ui = workflow.ui ? stripForEnv(workflow.ui) : null;

  // RBAC rules for bff/authz.py. Only shipped when something is actually
  // restricted — with no `actions` map the module is a no-op, and omitting it
  // keeps those bytes out of the 4 KB Lambda env.
  const actions = workflow.authorization?.actions ?? {};
  const authorization = Object.keys(actions).length
    ? { groupsClaim: workflow.authorization.groupsClaim ?? null, actions }
    : null;

  return { agents: agentsOut, steps: workflow.steps, evalAgents, chatbot, ui, authorization };
}

/**
 * The `tools` block projected down to what the APP needs: how to call each tool.
 *
 * Deploy-time detail (endpoint, policy, schema, credentials) is deliberately left
 * out — the IaC consumes that, and the app must never send it in a request. Mirrors
 * `tools_env` in terraform/tools.tf; `cdk/test/parity.test.ts` asserts the key sets
 * agree, because a field present on one path and absent on the other is a config
 * key that silently does nothing on half your deployments.
 */
export function toolsEnv(tools: Record<string, ToolSpec>): Record<string, any> {
  const out: Record<string, any> = {};
  for (const [name, t] of Object.entries(tools)) {
    const spec: Record<string, any> = { type: t.type };
    if (t.type === "kb") spec.corpora = t.corpora ?? [];
    if (t.type === "websearch") {
      spec.maxResults = t.maxResults ?? 10;
      if (t.includeDomains?.length) spec.includeDomains = t.includeDomains;
      if (t.excludeDomains?.length) spec.excludeDomains = t.excludeDomains;
      if (t.publishedFrom) spec.publishedFrom = t.publishedFrom;
      if (t.publishedTo) spec.publishedTo = t.publishedTo;
    }
    if (t.call) spec.call = t.call;
    if (t.arg) spec.arg = t.arg;
    if (t.args && Object.keys(t.args).length) spec.args = t.args;
    // rowFields: which result-row field carries which role, for an agent that reads
    // the tool's DATA rather than its prose rendering (ctx.call_tool_rows). Keeps a
    // deterministic agent config-driven: repoint the tool at another source and set
    // these to its field names, with no agent code change.
    if (t.rowFields && Object.keys(t.rowFields).length) spec.rowFields = t.rowFields;
    out[name] = spec;
  }
  return out;
}

/**
 * Drop empty values and the explanatory `*Note` keys before shipping to the 4 KB
 * Lambda env. The Notes are for whoever edits workflow.json, not for the runtime;
 * terraform/bff.tf drops them the same way.
 */
export function stripForEnv(o: Record<string, any>): Record<string, any> {
  return Object.fromEntries(
    Object.entries(o).filter(
      ([k, v]) => !k.endsWith("Note") && v !== undefined && v !== null && v !== ""
    )
  );
}
