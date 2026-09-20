# --- BFF Lambda + API Gateway (HTTP API) ----------------------------------

# The BFF's deployment package: bff/*.py PLUS app/workflow.json, so the API reads
# the same file the customer edits and the same file the runtime reads.
#
# It used to be source_dir = ../bff, with a projection of the workflow built here in
# HCL (and a second time in TypeScript for the CDK path) and shipped in a
# WORKFLOW_JSON environment variable. That was a CEILING: Lambda caps the whole
# environment at 4 KB and the quota cannot be raised, so this file carried a
# 3400-byte precondition and the shipped ten-agent workflow measured 3153 bytes —
# about eleven agents before a customer's deploy failed with "shorten your agent
# names". bff/workflow.py now does the projection, once, in Python.
#
# `source` blocks rather than `source_dir` because the two inputs live in different
# directories. bff/ is pure UTF-8 Python, which is why `file()` is safe here; a
# binary asset would need `filebase64` and a different archive strategy.
#
# `**/*.py` rather than `*.py` so a future subpackage under bff/ is included, and
# rather than `**` so __pycache__ is not: source_dir used to ship every stale .pyc
# in the working tree — including ones built by a different Python minor version —
# into the deployment package.
data "archive_file" "bff" {
  type        = "zip"
  output_path = "${path.module}/.build/bff.zip"

  dynamic "source" {
    for_each = fileset("${path.module}/../bff", "**/*.py")
    content {
      content  = file("${path.module}/../bff/${source.value}")
      filename = source.value
    }
  }

  source {
    content  = file("${path.module}/../app/workflow.json")
    filename = "workflow.json"
  }

  # The framework's closed value sets. bff/authz.py reads the RBAC action names from
  # here rather than keeping a third copy of them.
  source {
    content  = file("${path.module}/../app/vocabulary.json")
    filename = "vocabulary.json"
  }
  # Every key's DEFAULT (generated from app/keys.json by build_schema.py). bff/authz.py
  # reads groupsClaim's default from here rather than keeping a fourth copy of it.
  source {
    content  = file("${path.module}/../app/defaults.json")
    filename = "defaults.json"
  }
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
      # No WORKFLOW_JSON. The workflow is in the deployment package (see the
      # archive_file above) because a 4 KB environment could not hold more than
      # about eleven agents' worth of it.
    }
  }
  # The log group must exist BEFORE the function, or Lambda creates
  # /aws/lambda/<name> itself and Terraform's CreateLogGroup then fails with
  # ResourceAlreadyExistsException. Nothing in the function's arguments references the
  # group, so without this they are created in parallel and the apply is a race — which
  # is exactly how it failed on the first real apply, for two of the four functions.
  # (The CDK path gets this ordering for free by passing the group as `logGroup:`.)
  depends_on = [aws_cloudwatch_log_group.bff]
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
