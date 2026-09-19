# The stand-in A2A agent: a real Agent2Agent server, in its own Lambda, behind its own
# Function URL.
#
# It exists so `runtime: "a2a"` ships with something live. That placement needs an agent
# THIS DEPLOYMENT DOES NOT OWN, and a committed placeholder URL would have failed every
# run — so the framework deploys a stand-in and the orchestrator reaches it over the
# protocol exactly as it would reach a third party's agent. See a2a_lambda/handler.py.
#
# Created ONLY when at least one agent asks for it with `source = "a2a_lambda"`. Point
# your agents at a real partner's `agentCard` instead and none of this is provisioned —
# the same deal a `tools` entry gets with `lambdaArn` vs `source`.
#
# Mirrored by cdk/lib/orchestrator-stack.ts. Kept in its own file because it is a
# self-contained demonstration a customer may well delete.

locals {
  # Agents that want the shipped stand-in, and which skill each one is.
  a2a_lambda_agents = {
    for id in local.a2a_agent_ids : id => lower(try(
    local.workflow_def.agents[id].skill, "compliance"))
    if try(local.workflow_def.agents[id].source, "") == "a2a_lambda"
  }
  a2a_lambda_enabled = length(local.a2a_lambda_agents) > 0 ? 1 : 0

  # agent_id -> the URL to reach it at. The skill is a PATH SEGMENT, not a query
  # string: a client appends `/.well-known/agent-card.json` to whatever base URL it is
  # given, and `https://host/?skill=x` + that path resolves to nothing. One function
  # therefore backs several agents that are genuinely different reviewers.
  #
  # The Function URL already ends in "/", hence trimsuffix.
  # `one()` rather than [0]: with the function disabled the list is empty, and indexing
  # it would fail evaluating a map that is about to be empty anyway.
  #
  # trimsuffix then "/" + skill, which is the same result the CDK path gets by appending
  # "/<skill>" without trimming. Either way a doubled slash would only be an empty path
  # segment that the handler skips, so neither path can produce "https://hostcompliance".
  a2a_function_url = trimsuffix(try(one(aws_lambda_function_url.a2a[*].function_url), ""), "/")
  a2a_endpoints = {
    for id, skill in local.a2a_lambda_agents : id => "${local.a2a_function_url}/${skill}"
  }
}

data "archive_file" "a2a_lambda" {
  count       = local.a2a_lambda_enabled
  type        = "zip"
  source_dir  = "${path.module}/../a2a_lambda"
  output_path = "${path.module}/.build/a2a_lambda.zip"
}

resource "aws_iam_role" "a2a_lambda" {
  count = local.a2a_lambda_enabled
  name  = "A2AAgent-${var.agent_name}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "a2a_lambda" {
  count = local.a2a_lambda_enabled
  name  = "A2AAgentModelAndLogs"
  role  = aws_iam_role.a2a_lambda[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # It is an agent, so it calls a model. Scoped to the deployment's model rather
        # than bedrock:* — it has exactly one reason to talk to Bedrock.
        Effect = "Allow"
        Action = ["bedrock:InvokeModel"]
        Resource = [
          "arn:aws:bedrock:*::foundation-model/*",
          "arn:aws:bedrock:*:${local.account_id}:inference-profile/*",
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.a2a_lambda[0].arn}:*"
      },
    ]
  })
}

# Stack-owned, with retention. Left implicit, Lambda creates the group itself with
# NEVER-EXPIRE retention and no stack ownership, so a destroy orphans it forever.
resource "aws_cloudwatch_log_group" "a2a_lambda" {
  count             = local.a2a_lambda_enabled
  name              = "/aws/lambda/A2AAgent-${var.agent_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "a2a" {
  count = local.a2a_lambda_enabled
  # Prefixed like every other framework-owned function (ToolLambda-…,
  # AgentCoreBFF-…, AgentCoreKBRetrieve-…) so a scoped deploy policy can name it
  # without granting lambda:* account-wide.
  function_name    = "A2AAgent-${var.agent_name}"
  description      = "Stand-in A2A (Agent2Agent) agent for runtime = \"a2a\""
  role             = aws_iam_role.a2a_lambda[0].arn
  runtime          = "python3.12"
  handler          = "handler.lambda_handler"
  filename         = data.archive_file.a2a_lambda[0].output_path
  source_code_hash = data.archive_file.a2a_lambda[0].output_base64sha256
  # It makes a model call, so it needs more than the 3s default. Below the
  # orchestrator's own a2aInvoke.timeoutSeconds, so the client's timeout is the one
  # that fires and the failure says which side gave up.
  timeout     = 25
  memory_size = 512

  environment {
    variables = {
      BEDROCK_MODEL_ID = var.model_id
      A2A_AGENT_NAME   = "Independent Review Agent"
      A2A_MAX_TOKENS   = "2000"
    }
  }

  depends_on = [aws_cloudwatch_log_group.a2a_lambda]
}

resource "aws_lambda_function_url" "a2a" {
  count         = local.a2a_lambda_enabled
  function_name = aws_lambda_function.a2a[0].function_name
  # AWS_IAM, never NONE. This is the whole reason the client has an `auth = "sigv4"`
  # mode: the endpoint is not public, a caller must present a SigV4 signature from a
  # principal allowed to invoke it, and no bearer token exists to leak or rotate. A
  # public URL guarded by a shared secret would have been less code and materially
  # worse.
  authorization_type = "AWS_IAM"
}

# The orchestrator is the only principal allowed to call it.
resource "aws_iam_role_policy" "runtime_invoke_a2a" {
  count = local.a2a_lambda_enabled
  name  = "InvokeA2AAgentUrl"
  role  = aws_iam_role.runtime.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["lambda:InvokeFunctionUrl"]
      Resource = aws_lambda_function.a2a[0].arn
    }]
  })
}
