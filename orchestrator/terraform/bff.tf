# --- BFF Lambda + API Gateway (HTTP API) ----------------------------------

# The BFF/UI only need a trimmed view of the workflow (agent display fields +
# steps) to render the DAG — not the module names, orchestrator block, or
# descriptions. Trimmed + compact (jsonencode) so the Lambda env stays under the
# 4 KB limit; the runtime reads the FULL workflow.json from the image, not env.
locals {
  bff_workflow = {
    agents = { for id, a in local.workflow_def.agents : id => {
      name    = a.name
      kind    = lookup(a, "kind", "sync")
      runtime = lookup(a, "runtime", "main")
      mcp     = lookup(a, "mcp", null)
      rag     = lookup(a, "rag", null)
      model   = lookup(a, "model", null)
      # Compact, display-ready data-source label for the UI (avoids shipping the
      # full access[] array in the 4 KB env). Mirrors the UI's agentSource order.
      source = (
        lookup(a, "mcp", null) != null ? "MCP · ${lookup(a, "mcp", "")}" :
        lookup(a, "rag", null) != null ? "Knowledge Base · ${lookup(a, "rag", "")}" :
        can(regex("(?i)session", try(a.access[0], ""))) ? "Session input" :
        can(regex("(?i)upstream", try(a.access[0], ""))) ? "Upstream agent outputs" :
        "—"
      )
    } }
    steps = local.workflow_def.steps
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
      }
    ]
  })
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

# Cognito JWT authorizer — created only when cognito_user_pool_id is set.
# The UI sends the Cognito ID token; its audience is the App Client ID
# and the issuer is the User Pool endpoint.
locals {
  auth_enabled = var.create_cognito || var.cognito_user_pool_id != ""
}

resource "aws_apigatewayv2_authorizer" "cognito" {
  count            = local.auth_enabled ? 1 : 0
  api_id           = aws_apigatewayv2_api.bff.id
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]
  name             = "cognito-${var.agent_name}"

  jwt_configuration {
    audience = [local.cognito_client_id]
    issuer   = "https://cognito-idp.${var.region}.amazonaws.com/${local.cognito_pool_id}"
  }
}

resource "aws_apigatewayv2_route" "routes" {
  for_each = toset([
    "GET /api/workflow",
    "POST /api/sessions",
    "GET /api/sessions",
    "GET /api/sessions/{id}",
    "DELETE /api/sessions/{id}",
    "POST /api/sessions/{id}/decision",
    "POST /api/sessions/{id}/cancel",
    "GET /api/sessions/{id}/telemetry",
    "GET /api/telemetry/aggregate",
  ])
  api_id             = aws_apigatewayv2_api.bff.id
  route_key          = each.value
  target             = "integrations/${aws_apigatewayv2_integration.bff.id}"
  authorization_type = local.auth_enabled ? "JWT" : "NONE"
  authorizer_id      = local.auth_enabled ? aws_apigatewayv2_authorizer.cognito[0].id : null
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
