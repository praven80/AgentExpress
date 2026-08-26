# --- Per-agent (dedicated) AgentCore Runtimes -----------------------------
# Any agent marked `"runtime": "dedicated"` in workflow.json gets its OWN
# AgentCore Runtime here (same image; AGENT_ID selects which agent it hosts).
# Agents left as "main" run in-process in the orchestrator runtime. This is
# purely config-driven: add/remove a dedicated agent by editing workflow.json.

locals {
  workflow_def    = jsondecode(file("${path.module}/../app/workflow.json"))
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

  agent_runtime_name = "${var.agent_name}_${lookup(each.value, "module", each.key)}"
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
    AWS_REGION       = var.region
    BEDROCK_MODEL_ID = var.model_id
    AGENT_ID         = each.key
    TELEMETRY_TABLE  = aws_dynamodb_table.telemetry.name

    # Gateway-backed MCP access (same as the orchestrator) for agents that use it.
    GATEWAY_URL           = var.enable_gateway ? aws_bedrockagentcore_gateway.mcp[0].gateway_url : ""
    GATEWAY_TOKEN_URL     = var.enable_gateway ? "https://${var.cognito_domain_prefix}.auth.${var.region}.amazoncognito.com/oauth2/token" : ""
    GATEWAY_CLIENT_ID     = var.cognito_gateway_client_id
    GATEWAY_CLIENT_SECRET = var.cognito_gateway_client_secret
    GATEWAY_AUDIENCE      = var.cognito_gateway_scope
  }

  depends_on = [null_resource.build_push, time_sleep.subagent_iam_propagation]
}
