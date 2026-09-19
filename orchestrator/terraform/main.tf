data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  ecr_repo   = "agentcore-${var.agent_name}"

  # Rebuild the image whenever the Dockerfile, deps, or any app/ file changes.
  # Exclude Python bytecode caches so the image tag is deterministic regardless of
  # locally-generated __pycache__/*.pyc (which are not shipped in the image anyway).
  app_files = [
    for f in fileset("${path.module}/../app", "**") :
    f if !can(regex("(^|/)__pycache__/", f)) && !endswith(f, ".pyc")
  ]
  source_hash = substr(sha1(join("", concat(
    [filesha1("${path.module}/../Dockerfile"), filesha1("${path.module}/../requirements.txt")],
    [for f in sort(local.app_files) : filesha1("${path.module}/../app/${f}")]
  ))), 0, 12)
  image_uri = "${aws_ecr_repository.orchestrator.repository_url}:${local.source_hash}"
}

# --- AgentCore Memory (durable checkpointer backend) ----------------------

resource "awscc_bedrockagentcore_memory" "orchestrator" {
  name                  = "${var.agent_name}_memory"
  event_expiry_duration = var.memory_event_expiry_days
  description           = "Checkpoint/state store for the multi-agent orchestrator"
}

# --- AgentCore Memory: long-term semantic (cross-session, per-subject) ------
# SEPARATE from the checkpointer above. This one has LONG-TERM strategies:
# AgentCore asynchronously extracts durable insights from what agents store, and
# groups them under a namespace template. The app sets
# actorId = "<agentId>-<subjectSlug>", so each agent's insights are isolated per
# subject and recalled across runs. Adding a new agent or subject needs NO
# Terraform change — the namespace is derived from actorId at run time.
resource "awscc_bedrockagentcore_memory" "semantic" {
  name                  = "${var.agent_name}_semantic"
  event_expiry_duration = var.memory_event_expiry_days
  description           = "Long-term semantic memory (per-agent, per-subject insights)"

  # Two long-term strategies, so more than one is exercised:
  #   * SEMANTIC — extracts discrete facts/insights  -> namespace insights/{actorId}
  #   * SUMMARY  — maintains a running summary        -> namespace summary/{actorId}/{sessionId}
  # An agent's workflow.json memory.longTerm list (["semantic","summary"]) selects
  # which namespaces it reads; both extract from the same stored turns.
  memory_strategies = [
    {
      semantic_memory_strategy = {
        name       = "insights"
        namespaces = ["insights/{actorId}"]
      }
    },
    {
      # Summarization is inherently per-session, so AgentCore REQUIRES {sessionId}
      # in its namespace (unlike semantic, which is actor-scoped and cross-session).
      summary_memory_strategy = {
        name       = "summary"
        namespaces = ["summary/{actorId}/{sessionId}"]
      }
    }
  ]
}

# --- Container registry ---------------------------------------------------

resource "aws_ecr_repository" "orchestrator" {
  name                 = local.ecr_repo
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }
}

# --- Build & push the ARM64 image -----------------------------------------

resource "null_resource" "build_push" {
  triggers = {
    image_uri = local.image_uri
  }

  provisioner "local-exec" {
    working_dir = path.module
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      REG="${local.account_id}.dkr.ecr.${var.region}.amazonaws.com"
      aws ecr get-login-password --region ${var.region} \
        | ${var.container_engine} login --username AWS --password-stdin "$REG"
      ${var.container_engine} build --platform linux/arm64 \
        -t "${local.image_uri}" -f ../Dockerfile ..
      ${var.container_engine} push "${local.image_uri}"
    EOT
  }

  # No depends_on needed: triggers.image_uri -> local.image_uri -> the repository's
  # own repository_url, which is an implicit dependency.
}

# --- IAM execution role ----------------------------------------------------

resource "aws_iam_role" "runtime" {
  name = "AgentCoreRuntime-${var.agent_name}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "bedrock-agentcore.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account_id }
        ArnLike      = { "aws:SourceArn" = "arn:aws:bedrock-agentcore:${var.region}:${local.account_id}:*" }
      }
    }]
  })
}

resource "aws_iam_role_policy" "runtime" {
  name = "AgentCoreRuntimeExecutionPolicy-${var.agent_name}"
  role = aws_iam_role.runtime.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ECRImageAccess"
        Effect   = "Allow"
        Action   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
        Resource = [aws_ecr_repository.orchestrator.arn]
      },
      {
        Sid      = "ECRTokenAccess"
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams", "logs:DescribeLogGroups"]
        Resource = ["arn:aws:logs:${var.region}:${local.account_id}:log-group:/aws/bedrock-agentcore/runtimes/*"]
      },
      {
        # Required for AgentCore "unified" telemetry: AgentCore uses this to add a
        # CloudWatch Logs resource policy so X-Ray can deliver spans to the agent's
        # own log group. Without it, unified spans never reach CloudWatch. (Docs:
        # observability-configure — "Span destination for agents hosted in
        # AgentCore runtime".) PutResourcePolicy is account-scoped -> Resource "*".
        Sid      = "AgentCoreUnifiedSpanDelivery"
        Effect   = "Allow"
        Action   = ["logs:PutResourcePolicy"]
        Resource = ["*"]
      },
      {
        Effect   = "Allow"
        Action   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets"]
        Resource = ["*"]
      },
      {
        # AgentCore Evaluations: the runtime scores an agent's run (LLM-as-judge
        # over its OTEL spans / persisted prompt I/O). Evaluate uses the built-in
        # evaluators (public ARNs); the Logs Insights query downloads the session's
        # spans from the runtime log group. GetQueryResults/StopQuery are not
        # resource-scopable, so this statement uses Resource "*".
        Sid    = "AgentCoreEvaluations"
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:Evaluate",
          "logs:DescribeLogGroups", "logs:StartQuery", "logs:GetQueryResults", "logs:StopQuery"
        ]
        Resource = ["*"]
      },
      {
        Effect    = "Allow"
        Action    = "cloudwatch:PutMetricData"
        Resource  = "*"
        Condition = { StringEquals = { "cloudwatch:namespace" = "bedrock-agentcore" } }
      },
      {
        Sid    = "AgentCoreMemoryDataPlane"
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:CreateEvent",
          "bedrock-agentcore:ListEvents",
          "bedrock-agentcore:GetEvent",
          "bedrock-agentcore:ListSessions",
          "bedrock-agentcore:RetrieveMemories",
          # Long-term semantic recall/store (the SDK's search_long_term_memories
          # calls RetrieveMemoryRecords; list/get for inspection).
          "bedrock-agentcore:RetrieveMemoryRecords",
          "bedrock-agentcore:ListMemoryRecords",
          "bedrock-agentcore:GetMemoryRecord"
        ]
        Resource = [
          awscc_bedrockagentcore_memory.orchestrator.memory_arn,
          "${awscc_bedrockagentcore_memory.orchestrator.memory_arn}/*",
          awscc_bedrockagentcore_memory.semantic.memory_arn,
          "${awscc_bedrockagentcore_memory.semantic.memory_arn}/*"
        ]
      },
      {
        # Bedrock Guardrails: apply content safety to agent input/output.
        Sid      = "BedrockGuardrails"
        Effect   = "Allow"
        Action   = ["bedrock:ApplyGuardrail"]
        Resource = [aws_bedrock_guardrail.main.guardrail_arn]
      },
      {
        Sid    = "AgentCoreWorkloadIdentity"
        Effect = "Allow"
        Action = ["bedrock-agentcore:GetWorkloadAccessToken", "bedrock-agentcore:GetWorkloadAccessTokenForJWT", "bedrock-agentcore:GetWorkloadAccessTokenForUserId"]
        Resource = [
          "arn:aws:bedrock-agentcore:${var.region}:${local.account_id}:workload-identity-directory/default",
          "arn:aws:bedrock-agentcore:${var.region}:${local.account_id}:workload-identity-directory/default/workload-identity/${var.agent_name}-*"
        ]
      },
      {
        Sid    = "BedrockModelInvocation"
        Effect = "Allow"
        Action = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:CountTokens"]
        # Inference profiles, not account-wide bedrock:* — that also covered custom
        # models, provisioned throughput, agents, guardrails and prompts, none of which
        # the runtime invokes. bff.tf already used this narrower form.
        Resource = [
          "arn:aws:bedrock:*::foundation-model/*",
          "arn:aws:bedrock:${var.region}:${local.account_id}:inference-profile/*",
          "arn:aws:bedrock:${var.region}:${local.account_id}:application-inference-profile/*"
        ]
      },
      {
        Sid      = "ProgressStoreWrite"
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:GetItem"]
        Resource = [aws_dynamodb_table.status.arn, aws_dynamodb_table.events.arn]
      },
      {
        # Scan is needed on the STATUS table only: Insights flags whether each
        # analyzed session still exists, so the UI can disable dead links. The
        # comment used to say "status only" while the grant covered both tables.
        Sid      = "ProgressStoreScanStatus"
        Effect   = "Allow"
        Action   = ["dynamodb:Scan"]
        Resource = [aws_dynamodb_table.status.arn]
      },
      {
        # Invoke the dedicated per-agent runtimes (runtime: "dedicated").
        Sid      = "InvokeDedicatedAgentRuntimes"
        Effect   = "Allow"
        Action   = ["bedrock-agentcore:InvokeAgentRuntime"]
        Resource = local.invoke_runtime_resources
      },
    ]
  })
}

# Give the execution role a moment to propagate before the runtime validates it.
resource "time_sleep" "iam_propagation" {
  depends_on      = [aws_iam_role_policy.runtime]
  create_duration = "25s"
}

# --- AgentCore Runtime -----------------------------------------------------

resource "awscc_bedrockagentcore_runtime" "orchestrator" {
  agent_runtime_name = var.agent_name
  description        = "Multi-agent LangGraph orchestrator (Terraform-managed)"
  role_arn           = aws_iam_role.runtime.arn

  agent_runtime_artifact = {
    container_configuration = {
      container_uri = local.image_uri
    }
  }

  network_configuration = {
    network_mode = "PUBLIC"
  }

  environment_variables = {
    AWS_REGION         = var.region
    BEDROCK_MODEL_ID   = var.model_id
    MEMORY_ID          = awscc_bedrockagentcore_memory.orchestrator.memory_id
    SEMANTIC_MEMORY_ID = awscc_bedrockagentcore_memory.semantic.memory_id
    STATUS_TABLE       = aws_dynamodb_table.status.name
    EVENTS_TABLE       = aws_dynamodb_table.events.name
    TELEMETRY_TABLE    = aws_dynamodb_table.telemetry.name

    # Default guardrail for agents that enable guardrails in workflow.json but
    # don't specify their own guardrailId. DRAFT tracks the latest config.
    GUARDRAIL_ID      = aws_bedrock_guardrail.main.guardrail_id
    GUARDRAIL_VERSION = "DRAFT"

    # AgentCore Optimization: the cross-run Insights findings store.
    INSIGHTS_TABLE = aws_dynamodb_table.insights.name

    # AgentCore Evaluations reads this app's OTEL spans from the runtime log
    # group(s). We inject the stable name PREFIX; the exact id-suffixed group
    # name is discovered at query time (it can't be referenced here without a
    # self-dependency cycle on the runtime resource).
    SPAN_LOG_GROUP_PREFIX = "/aws/bedrock-agentcore/runtimes/${var.agent_name}-"

    # --- AgentCore GenAI Observability ---
    # Master switch; AgentCore injects the ADOT config + managed OTLP endpoint.
    AGENT_OBSERVABILITY_ENABLED = "true"
    # Always sample. The UI path (API Gateway -> BFF Lambda -> InvokeAgentRuntime)
    # propagates an X-Ray trace context with sampled=0; the default parent-based
    # sampler then marks every span not-recording and NOTHING is exported. Direct
    # SDK invokes have no parent, so they sampled fine — which is why UI runs
    # showed no traces while direct tests did. always_on ignores the inherited
    # decision.
    OTEL_TRACES_SAMPLER = "always_on"
    # Disable ADOT's bundled LangChain instrumentation (entry point "aws_langchain").
    # It emits a SECOND "chat <model>" span per LLM call (aws.genai.span_kind=LLM)
    # that carries NO token usage, while the botocore bedrock-runtime span carries
    # the real gen_ai.usage.* counts. The GenAI Observability dashboard reads the
    # LangChain framework span for its token metric, so tokens showed as 0.
    # Disabling it leaves exactly one LLM span per call (botocore, with tokens).
    # Trade-off: also drops the LangChain-only gen_ai.input/output.messages and
    # langgraph.node/step attributes (the prompt/response content view) —
    # app/features/observability captures that itself. Reversible.
    OTEL_PYTHON_DISABLED_INSTRUMENTATIONS = "aws_langchain"

    # Map of agent_id -> its dedicated runtime ARN, consumed by
    # AgentCoreRuntimeAgent to invoke a "dedicated" agent cross-runtime.
    AGENT_RUNTIME_ARNS = jsonencode({
      for id, r in awscc_bedrockagentcore_runtime.subagent : id => r.agent_runtime_arn
    })
    # Map of agent_id -> bearer token for each `runtime = "a2a"` agent with
    # `auth = "bearer"`, consumed by A2AAgent. Projected rather than passed whole so a
    # token for an agent that no longer exists is not shipped to the container. These
    # are credentials for someone else's service, which is why they come from
    # var.a2a_tokens (sensitive) and never from workflow.json.
    A2A_TOKENS = jsonencode({
      for id in local.a2a_agent_ids : id => var.a2a_tokens[id]
      if lower(try(local.workflow_def.agents[id].auth, "none")) == "bearer"
    })
    # Map of agent_id -> endpoint, for a `runtime = "a2a"` agent whose URL only exists
    # AFTER a deploy (the stand-in's Function URL). Injected rather than committed, for
    # the same reason AGENT_RUNTIME_ARNS is. Empty when no agent uses `source`.
    A2A_ENDPOINTS = jsonencode(local.a2a_endpoints)

    # Gateway-backed MCP access (agent -> Gateway via IdP client-credentials).
    # Empty when var.enable_gateway is false, in which case an agent with a `tool`
    # fails loudly rather than inventing an answer (app/common/errors.py).
    GATEWAY_URL           = var.enable_gateway ? aws_bedrockagentcore_gateway.mcp[0].gateway_url : ""
    GATEWAY_TOKEN_URL     = var.enable_gateway ? local.gateway_token_url : ""
    GATEWAY_CLIENT_ID     = local.gateway_client_id
    GATEWAY_CLIENT_SECRET = local.gateway_client_secret
    # Which client-credentials request shape to build ("cognito" | "auth0").
    GATEWAY_AUTH_FLOW = var.enable_gateway ? local.gateway_auth_flow : ""
    # The OAuth2 `scope` the runtime requests (Cognito's client-credentials
    # equivalent of an audience) — see app/features/gateway/client.py.
    GATEWAY_AUDIENCE = local.gateway_audience
    # Cedar policy mode in effect at the Gateway (LOG_ONLY|ENFORCE), so the app
    # can label policy decisions in the observability UI. Enforcement itself is
    # server-side at the Gateway; this is display-only.
    GATEWAY_POLICY_MODE = local.policy_enabled ? local.policy_mode : ""
    # The `tools` block from workflow.json: how each tool is called (its type,
    # and per-type call options like the KB corpora or WebSearch maxResults).
    # Lets an agent invoke its configured tool without hardcoding anything.
    TOOLS_JSON = local.tools_env
  }

  depends_on = [null_resource.build_push, time_sleep.iam_propagation]
}
