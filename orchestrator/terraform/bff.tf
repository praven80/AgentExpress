# --- BFF Lambda + API Gateway (HTTP API) ----------------------------------

# The BFF/UI only need a trimmed view of the workflow (agent display fields +
# steps) to render the DAG — not the module names, orchestrator block, or
# descriptions. Trimmed + compact (jsonencode) so the Lambda env stays under the
# 4 KB limit; the runtime reads the FULL workflow.json from the image, not env.
locals {
  # tool name -> declared type, for the UI's data-source chip. Reads the raw
  # workflow.json block (not local.tools) so the label is correct even when the
  # Gateway is disabled and no targets are provisioned.
  tool_types = {
    for n, t in try(local.workflow_def.tools, {}) : n => lower(try(t.type, "mcp"))
  }

  bff_workflow = {
    agents = { for id, a in local.workflow_def.agents : id => {
      name    = a.name
      kind    = lookup(a, "kind", "sync")
      runtime = lookup(a, "runtime", "main")
      tool    = lookup(a, "tool", null)
      corpus  = lookup(a, "corpus", null)
      model   = lookup(a, "model", null)
      # Compact, display-ready data-source label for the UI (avoids shipping the
      # full access[] array in the 4 KB env). Derived from the tool's declared
      # TYPE, so a new tool type shows a sensible chip with no UI change.
      source = (
        lookup(a, "tool", null) == null ? (
          can(regex("(?i)session", try(a.access[0], ""))) ? "Session input" :
          can(regex("(?i)upstream", try(a.access[0], ""))) ? "Upstream agent outputs" :
          "—"
          ) : (
          local.tool_types[a.tool] == "kb" ? "Knowledge Base · ${lookup(a, "corpus", "all")}" :
          local.tool_types[a.tool] == "websearch" ? "Web Search" :
          local.tool_types[a.tool] == "openapi" ? "REST API · ${a.tool}" :
          local.tool_types[a.tool] == "lambda" ? "Function · ${a.tool}" :
          "MCP · ${a.tool}"
        )
      )
    } }
    steps = local.workflow_def.steps
    # Compact feature-flag list the UI uses to gate the "Evaluate" button (kept as
    # a short id array to stay under Lambda's 4 KB env limit).
    evalAgents = [for id, a in local.workflow_def.agents : id if try(a.agentcore.evaluations.enabled, false)]
    # In-app assistant config for the BFF tool loop + the UI. Kept lean for the 4 KB
    # Lambda env: enabled + model + only the DISABLED tool flags (the backend
    # defaults any missing tool to ON). greeting/placeholder ARE shipped — they were
    # not, which made those two workflow.json keys decorative: a customer edited
    # them and the UI kept showing its own hardcoded strings.
    chatbot = try(local.workflow_def.orchestrator.chatbot.enabled, null) == null ? null : {
      enabled     = local.workflow_def.orchestrator.chatbot.enabled
      model       = try(local.workflow_def.orchestrator.chatbot.model, null)
      greeting    = try(local.workflow_def.orchestrator.chatbot.greeting, null)
      placeholder = try(local.workflow_def.orchestrator.chatbot.placeholder, null)
      tools       = { for k, v in try(local.workflow_def.orchestrator.chatbot.tools, {}) : k => v if v == false }
    }
    # RBAC rules for bff/authz.py. Shipped ONLY when something is actually
    # restricted: with no `actions` map the module is a no-op, and null keeps those
    # bytes out of the 4 KB env. The *Note key is dropped for the same reason.
    authorization = length(local.authz_actions) == 0 ? null : {
      groupsClaim = try(local.workflow_def.authorization.groupsClaim, null)
      actions     = local.authz_actions
    }
    # Presentation strings (title / heading / default topic / assistant title), so
    # re-branding is a workflow.json edit rather than an index.html edit. The
    # explanatory *Note keys are dropped — they are for whoever edits the config,
    # and every byte counts in the 4 KB Lambda env.
    ui = {
      for k, v in try(local.workflow_def.ui, {}) : k => v
      if !endswith(k, "Note") && v != null && v != ""
    }
  }
}

# Lambda caps the TOTAL environment at 4 KB. The other variables are ARNs and
# table names (~1 KB together), so fail at PLAN with an actionable message rather
# than letting a large workflow surface as an opaque Lambda error mid-apply.
# The CDK path has the same guard in lib/orchestrator-stack.ts.
resource "terraform_data" "bff_workflow_size" {
  input = length(jsonencode(local.bff_workflow))

  lifecycle {
    precondition {
      condition     = length(jsonencode(local.bff_workflow)) <= 3000
      error_message = "The workflow projection shipped to the BFF is ${length(jsonencode(local.bff_workflow))} bytes, over the 3000-byte budget (Lambda's whole environment is capped at 4 KB). Shorten agent \"name\" values in app/workflow.json, or move the projection to S3/SSM and have bff/handler.py read it from there."
    }
  }
}

data "archive_file" "bff" {
  type        = "zip"
  source_dir  = "${path.module}/../bff"
  output_path = "${path.module}/.build/bff.zip"
}

resource "aws_iam_role" "bff" {
  name = "AgentCoreBFF-${var.agent_name}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "bff" {
  name = "AgentCoreBFFPolicy-${var.agent_name}"
  role = aws_iam_role.bff.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${var.region}:${local.account_id}:*"
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:Query", "dynamodb:Scan"]
        Resource = [aws_dynamodb_table.status.arn, aws_dynamodb_table.events.arn]
      },
      {
        Effect = "Allow"
        Action = ["bedrock-agentcore:InvokeAgentRuntime"]
        Resource = [
          awscc_bedrockagentcore_runtime.orchestrator.agent_runtime_arn,
          "${awscc_bedrockagentcore_runtime.orchestrator.agent_runtime_arn}/*"
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = "arn:aws:lambda:${var.region}:${local.account_id}:function:AgentCoreBFF-${var.agent_name}"
      },
      {
        # In-app assistant: the chat tool loop calls Bedrock Converse.
        # (Telemetry Query for its read tools is already granted by the
        # bff_telemetry policy in observability.tf.)
        Sid    = "BedrockChatModel"
        Effect = "Allow"
        Action = ["bedrock:InvokeModel"]
        Resource = [
          "arn:aws:bedrock:*::foundation-model/*",
          "arn:aws:bedrock:${var.region}:${local.account_id}:inference-profile/*"
        ]
      }
    ]
  })
}

# Owned explicitly so `destroy` removes it. See var.log_retention_days.
resource "aws_cloudwatch_log_group" "bff" {
  name              = "/aws/lambda/AgentCoreBFF-${var.agent_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "bff" {
  function_name    = "AgentCoreBFF-${var.agent_name}"
  role             = aws_iam_role.bff.arn
  runtime          = "python3.13"
  handler          = "handler.handler"
  filename         = data.archive_file.bff.output_path
  source_code_hash = data.archive_file.bff.output_base64sha256
  timeout          = 60
  memory_size      = 256

  environment {
    variables = {
      # The assistant's tool-use loop runs in this Lambda, so it needs the
      # deployment's model rather than its own copy of the id.
      MODEL_ID        = var.model_id
      STATUS_TABLE    = aws_dynamodb_table.status.name
      EVENTS_TABLE    = aws_dynamodb_table.events.name
      TELEMETRY_TABLE = aws_dynamodb_table.telemetry.name
      RUNTIME_ARN     = awscc_bedrockagentcore_runtime.orchestrator.agent_runtime_arn
      WORKFLOW_JSON   = jsonencode(local.bff_workflow)
    }
  }
}

resource "aws_apigatewayv2_api" "bff" {
  name          = "AgentCoreBFF-${var.agent_name}"
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "bff" {
  api_id                 = aws_apigatewayv2_api.bff.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.bff.invoke_arn
  payload_format_version = "2.0"
}

# JWT authorizer on /api/* — provider-agnostic. The UI sends its ID token; the
# issuer + audience for the configured IdP are derived in identity.tf. Skipped
# entirely when idp = "none", which leaves the API OPEN.
resource "aws_apigatewayv2_authorizer" "jwt" {
  count            = local.auth_enabled ? 1 : 0
  api_id           = aws_apigatewayv2_api.bff.id
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]
  # Deliberately NOT derived from var.idp: the name is an identity, not config.
  # Keeping it stable means switching provider updates issuer/audience in place
  # instead of replacing an authorizer that live routes still reference.
  name = "jwt-${var.agent_name}"

  jwt_configuration {
    audience = [local.jwt_audience]
    issuer   = local.jwt_issuer
  }

  # If a future change ever does force replacement, the new authorizer must exist
  # before the old one goes away — API Gateway refuses to delete an authorizer
  # that any route still references (409 ConflictException).
  lifecycle {
    create_before_destroy = true
  }
}

# Renamed from .cognito when auth became provider-agnostic. Without this,
# Terraform would try to create a second authorizer and delete the in-use one.
moved {
  from = aws_apigatewayv2_authorizer.cognito
  to   = aws_apigatewayv2_authorizer.jwt
}

resource "aws_apigatewayv2_route" "routes" {
  for_each = toset([
    "GET /api/workflow",
    # Who the caller is and which run actions they may take, so the UI can disable
    # controls instead of offering buttons that 403.
    "GET /api/me",
    "POST /api/sessions",
    "GET /api/sessions",
    "GET /api/sessions/{id}",
    "DELETE /api/sessions/{id}",
    "POST /api/sessions/{id}/decision",
    "POST /api/sessions/{id}/cancel",
    "POST /api/sessions/{id}/rerun",
    "POST /api/sessions/{id}/evaluate",
    "POST /api/insights/run",
    "GET /api/insights",
    "GET /api/sessions/{id}/telemetry",
    "GET /api/telemetry/aggregate",
    "POST /api/chat",
  ])
  api_id             = aws_apigatewayv2_api.bff.id
  route_key          = each.value
  target             = "integrations/${aws_apigatewayv2_integration.bff.id}"
  authorization_type = local.auth_enabled ? "JWT" : "NONE"
  authorizer_id      = local.auth_enabled ? aws_apigatewayv2_authorizer.jwt[0].id : null
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.bff.id
  name        = "$default"
  auto_deploy = true
}

resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.bff.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.bff.execution_arn}/*/*"
}
