# --- Per-agent (dedicated) AgentCore Runtimes -----------------------------
# Any agent marked `"runtime": "dedicated"` in workflow.json gets its OWN
# AgentCore Runtime here (same image; AGENT_ID selects which agent it hosts).
# Agents left as "main" run in-process in the orchestrator runtime. This is
# purely config-driven: add/remove a dedicated agent by editing workflow.json.

locals {
  workflow_def = jsondecode(file("${path.module}/../app/workflow.json"))
  dedicated_agents = {
    for id, a in local.workflow_def.agents : id => a
    if lookup(a, "runtime", "main") == "dedicated"
  }

  subagent_runtime_arns = [for r in awscc_bedrockagentcore_runtime.subagent : r.agent_runtime_arn]
  # Non-empty resource list even when there are no dedicated agents (an empty
  # IAM Resource is invalid).
  invoke_runtime_resources = length(local.subagent_runtime_arns) > 0 ? concat(
    local.subagent_runtime_arns,
    [for a in local.subagent_runtime_arns : "${a}/*"],
  ) : ["arn:aws:bedrock-agentcore:${var.region}:${local.account_id}:runtime/none"]
}

# Shared execution role for the dedicated agent runtimes. Narrower than the
# orchestrator's: no Memory checkpointer, no DynamoDB progress store — a
# dedicated agent just runs its model (and any Gateway MCP calls, which use
# Cognito env credentials, not IAM).
resource "aws_iam_role" "subagent" {
  count = length(local.dedicated_agents) > 0 ? 1 : 0
  name  = "AgentCoreSubagent-${var.agent_name}"

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

resource "aws_iam_role_policy" "subagent" {
  count = length(local.dedicated_agents) > 0 ? 1 : 0
  name  = "AgentCoreSubagentPolicy-${var.agent_name}"
  role  = aws_iam_role.subagent[0].id

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
        # Required for AgentCore "unified" telemetry span delivery to the agent's
        # own log group (see main.tf for details).
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
        Effect    = "Allow"
        Action    = "cloudwatch:PutMetricData"
        Resource  = "*"
        Condition = { StringEquals = { "cloudwatch:namespace" = "bedrock-agentcore" } }
      },
      {
        # Long-term memory: a dedicated agent recalls/stores exactly like an
        # in-process one when memory is enabled for it in workflow.json.
        Sid    = "AgentCoreLongTermMemory"
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:CreateEvent",
          "bedrock-agentcore:RetrieveMemories",
          "bedrock-agentcore:RetrieveMemoryRecords",
          "bedrock-agentcore:ListMemoryRecords",
          "bedrock-agentcore:GetMemoryRecord"
        ]
        Resource = [
          awscc_bedrockagentcore_memory.semantic.memory_arn,
          "${awscc_bedrockagentcore_memory.semantic.memory_arn}/*"
        ]
      },
      {
        Sid    = "AgentCoreWorkloadIdentity"
        Effect = "Allow"
        Action = ["bedrock-agentcore:GetWorkloadAccessToken", "bedrock-agentcore:GetWorkloadAccessTokenForJWT", "bedrock-agentcore:GetWorkloadAccessTokenForUserId"]
        Resource = [
          "arn:aws:bedrock-agentcore:${var.region}:${local.account_id}:workload-identity-directory/default",
          "arn:aws:bedrock-agentcore:${var.region}:${local.account_id}:workload-identity-directory/default/workload-identity/${var.agent_name}*"
        ]
      },
      {
        Sid    = "BedrockModelInvocation"
        Effect = "Allow"
        Action = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:CountTokens"]
        Resource = [
          "arn:aws:bedrock:*::foundation-model/*",
          "arn:aws:bedrock:${var.region}:${local.account_id}:*"
        ]
      },
      {
        # Content safety for dedicated agents that enable guardrails (matches the
        # orchestrator role in main.tf). Without this the GUARDRAIL_ID env would
        # resolve but ApplyGuardrail would be denied.
        Sid      = "BedrockGuardrails"
        Effect   = "Allow"
        Action   = ["bedrock:ApplyGuardrail"]
        Resource = [aws_bedrock_guardrail.main.guardrail_arn]
      }
    ]
  })
}

resource "time_sleep" "subagent_iam_propagation" {
  count           = length(local.dedicated_agents) > 0 ? 1 : 0
  depends_on      = [aws_iam_role_policy.subagent]
  create_duration = "25s"
}

resource "awscc_bedrockagentcore_runtime" "subagent" {
  for_each = local.dedicated_agents

  # The agent id is the module name — one convention, no override (see
  # app/orchestrator/registry.py). app/features/evaluations/service.py finds this
  # runtime's traces by matching the "_<agent id>" suffix, so the two must agree.
  agent_runtime_name = "${var.agent_name}_${each.key}"
  description        = "Dedicated runtime for agent ${each.key} (${each.value.name})"
  role_arn           = aws_iam_role.subagent[0].arn

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
    AGENT_ID           = each.key
    TELEMETRY_TABLE    = aws_dynamodb_table.telemetry.name
    SEMANTIC_MEMORY_ID = awscc_bedrockagentcore_memory.semantic.memory_id

    # --- AgentCore GenAI Observability ---
    # Master switch; AgentCore injects the ADOT config + managed OTLP endpoint.
    AGENT_OBSERVABILITY_ENABLED = "true"
    # Always sample (ignore the inherited sampled=0 from the cross-runtime parent
    # context) so spans are exported. See the note in main.tf.
    OTEL_TRACES_SAMPLER = "always_on"
    # Drop ADOT's duplicate, token-less LangChain LLM span so tokens show. See main.tf.
    OTEL_PYTHON_DISABLED_INSTRUMENTATIONS = "aws_langchain"

    # Default guardrail, same as the orchestrator runtime (main.tf), so a
    # dedicated agent that enables guardrails in workflow.json enforces the SAME
    # account-provisioned guardrail. Injected per account; nothing hardcoded.
    GUARDRAIL_ID      = aws_bedrock_guardrail.main.guardrail_id
    GUARDRAIL_VERSION = "DRAFT"

    # Gateway-backed MCP access (same as the orchestrator) for agents that use it.
    GATEWAY_URL           = var.enable_gateway ? aws_bedrockagentcore_gateway.mcp[0].gateway_url : ""
    GATEWAY_TOKEN_URL     = var.enable_gateway ? local.gateway_token_url : ""
    GATEWAY_CLIENT_ID     = local.gateway_client_id
    GATEWAY_CLIENT_SECRET = local.gateway_client_secret
    # Which client-credentials request shape to build ("cognito" | "auth0").
    GATEWAY_AUTH_FLOW = var.enable_gateway ? local.gateway_auth_flow : ""
    GATEWAY_AUDIENCE  = local.gateway_audience
    # Cedar policy mode (display-only, so the observability UI can label decisions).
    GATEWAY_POLICY_MODE = local.policy_enabled ? local.policy_mode : ""
    # The `tools` block from workflow.json: how each tool is called (its type,
    # and per-type call options like the KB corpora or WebSearch maxResults).
    TOOLS_JSON = local.tools_env
  }

  depends_on = [null_resource.build_push, time_sleep.subagent_iam_propagation]
}
