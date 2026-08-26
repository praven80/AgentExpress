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

  depends_on = [aws_ecr_repository.orchestrator]
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
        Sid    = "AgentCoreMemoryDataPlane"
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:CreateEvent",
          "bedrock-agentcore:ListEvents",
          "bedrock-agentcore:GetEvent",
          "bedrock-agentcore:ListSessions",
          "bedrock-agentcore:RetrieveMemories"
        ]
        Resource = [
          awscc_bedrockagentcore_memory.orchestrator.memory_arn,
          "${awscc_bedrockagentcore_memory.orchestrator.memory_arn}/*"
        ]
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
        Resource = [
          "arn:aws:bedrock:*::foundation-model/*",
          "arn:aws:bedrock:${var.region}:${local.account_id}:*"
        ]
      },
      {
        Sid      = "ProgressStoreWrite"
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:GetItem"]
        Resource = [aws_dynamodb_table.status.arn, aws_dynamodb_table.events.arn]
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
    AWS_REGION       = var.region
    BEDROCK_MODEL_ID = var.model_id
    MEMORY_ID        = awscc_bedrockagentcore_memory.orchestrator.memory_id
    STATUS_TABLE     = aws_dynamodb_table.status.name
    EVENTS_TABLE     = aws_dynamodb_table.events.name
    TELEMETRY_TABLE  = aws_dynamodb_table.telemetry.name

    # Map of agent_id -> its dedicated runtime ARN, consumed by
    # AgentCoreRuntimeAgent to invoke a "dedicated" agent cross-runtime.
    AGENT_RUNTIME_ARNS = jsonencode({
      for id, r in awscc_bedrockagentcore_runtime.subagent : id => r.agent_runtime_arn
    })

    # Gateway-backed MCP access (agent -> Gateway via Cognito client-credentials).
    # Empty when var.enable_gateway is false, which keeps MCP calls simulated.
    GATEWAY_URL           = var.enable_gateway ? aws_bedrockagentcore_gateway.mcp[0].gateway_url : ""
    GATEWAY_TOKEN_URL     = var.enable_gateway ? "https://${var.cognito_domain_prefix}.auth.${var.region}.amazoncognito.com/oauth2/token" : ""
    GATEWAY_CLIENT_ID     = var.cognito_gateway_client_id
    GATEWAY_CLIENT_SECRET = var.cognito_gateway_client_secret
    GATEWAY_AUDIENCE      = var.cognito_gateway_scope
  }

  depends_on = [null_resource.build_push, time_sleep.iam_propagation]
}
