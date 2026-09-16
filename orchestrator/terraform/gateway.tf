# --- AgentCore Gateway (the tool plane's front door) -----------------------
#
# ONE Gateway fronts every tool your agents can call. Inbound auth is a
# CUSTOM_JWT authorizer backed by the configured IdP (see identity.tf): the agent
# runtime fetches a short-lived client-credentials token and calls the Gateway's
# MCP URL.
#
# The TARGETS behind it are generated from the `tools` block in
# app/workflow.json — see terraform/tools.tf. Add a data source there, not here.
#
# A Cedar policy engine (policy.tf) is attached below, so every tool call is
# authorized server-side against permits generated from that same config.

# --- Gateway service role -------------------------------------------------

resource "aws_iam_role" "gateway" {
  count = var.enable_gateway ? 1 : 0
  name  = "AgentCoreGateway-${var.agent_name}"

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

resource "aws_iam_role_policy" "gateway" {
  count = var.enable_gateway ? 1 : 0
  name  = "GatewayPolicy-${var.agent_name}"
  role  = aws_iam_role.gateway[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${var.region}:${local.account_id}:*"
      },
      {
        # Needed when a target uses an API-key / OAuth credential provider.
        # Harmless when every target is public or SigV4.
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = "arn:aws:secretsmanager:${var.region}:${local.account_id}:secret:bedrock-agentcore*"
      },
      {
        # Lets the Gateway read + evaluate the attached Cedar Policy Engine on
        # each tool call (required to attach a policy_engine_configuration).
        Sid    = "PolicyEngineEvaluate"
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:GetPolicyEngine",
          "bedrock-agentcore:ListPolicies",
          "bedrock-agentcore:GetPolicy",
          "bedrock-agentcore:*Authorize*"
        ]
        # AuthorizeAction is checked against BOTH the policy engine and the
        # gateway resource, so grant on both ARNs.
        Resource = [
          "arn:aws:bedrock-agentcore:${var.region}:${local.account_id}:policy-engine/*",
          "arn:aws:bedrock-agentcore:${var.region}:${local.account_id}:gateway/*"
        ]
      }
      # OUTBOUND permission for the managed web-search connector. The connector
      # runs inside AWS and the Gateway reaches it as ITSELF, so without this a
      # call fails at INVOKE time (not at apply time) with
      #   -32002 "Execution role is not authorized for connector web-search"
      # Generated from config: present only when a tools entry declares
      # type=websearch.
      ], length(local.websearch_tools) == 0 ? [] : [
      {
        # Scoped to the AWS-OWNED tool ARN exactly as documented (note the
        # literal "aws" where the account id would normally be — authorization is
        # enforced per invocation against that ARN). This was "*" before, which
        # worked but granted more than the docs call for.
        Sid      = "InvokeWebSearch"
        Effect   = "Allow"
        Action   = ["bedrock-agentcore:InvokeWebSearch"]
        Resource = "arn:aws:bedrock-agentcore:${var.region}:aws:tool/web-search.v1"
      },
      {
        # The documented Web Search service-role policy pairs InvokeWebSearch with
        # InvokeGateway on the gateway.
        #
        # Scoped to gateway/* rather than the concrete ARN, to stay identical to
        # the CDK path: there, referencing the Gateway from this role's policy is a
        # CloudFormation circular dependency (the Gateway waits for the policy).
        # The PolicyEngineEvaluate statement above is scoped the same way.
        Sid      = "InvokeGateway"
        Effect   = "Allow"
        Action   = ["bedrock-agentcore:InvokeGateway"]
        Resource = "arn:aws:bedrock-agentcore:${var.region}:${local.account_id}:gateway/*"
      }
    ])
  })
}

# --- The Gateway ----------------------------------------------------------

resource "aws_bedrockagentcore_gateway" "mcp" {
  count           = var.enable_gateway ? 1 : 0
  name            = "${replace(var.agent_name, "_", "-")}-gw"
  role_arn        = aws_iam_role.gateway[0].arn
  protocol_type   = "MCP"
  authorizer_type = "CUSTOM_JWT"

  # Inbound auth is provider-specific, because the two token formats differ:
  #
  #   Cognito client-credentials tokens carry `client_id` + `scope` and NO `aud`
  #   claim, so the caller must be pinned with allowed_clients — an
  #   allowed_audience check could never match.
  #
  #   Auth0 M2M tokens are the mirror image: they carry `aud` (the API
  #   identifier) and no `client_id`, so we pin the audience and additionally
  #   constrain the calling application via its `azp` claim.
  authorizer_configuration {
    custom_jwt_authorizer {
      discovery_url = local.gateway_discovery_url

      allowed_clients  = local.is_cognito ? [local.gateway_client_id] : null
      allowed_audience = local.is_auth0 ? [local.gateway_audience] : null

      dynamic "custom_claim" {
        for_each = local.is_auth0 ? [1] : []
        content {
          inbound_token_claim_name       = "azp"
          inbound_token_claim_value_type = "STRING"
          authorizing_claim_match_value {
            claim_match_operator = "EQUALS"
            claim_match_value {
              match_value_string = local.gateway_client_id
            }
          }
        }
      }
    }
  }

  # AgentCore Policy: attach the Cedar policy engine (terraform/policy.tf) so the
  # Gateway evaluates policies on every tool call. `mode` comes from workflow.json
  # (orchestrator.policy.mode): LOG_ONLY = log decisions only; ENFORCE = block.
  # When policy.enabled is false, local.policy_enabled is false and this block is
  # omitted entirely — the Gateway does no policy evaluation.
  dynamic "policy_engine_configuration" {
    for_each = local.policy_enabled ? [1] : []
    content {
      arn  = aws_bedrockagentcore_policy_engine.main[0].policy_engine_arn
      mode = local.policy_mode
    }
  }
}
