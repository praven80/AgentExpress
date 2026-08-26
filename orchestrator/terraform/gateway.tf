# --- AgentCore Gateway in front of an MCP server --------------------------
#
# One Gateway fronts a remote MCP server (default: the public AWS Knowledge MCP,
# which needs no egress credentials). Inbound auth is a CUSTOM_JWT authorizer
# backed by Cognito (client-credentials): the agent runtime fetches a short-lived
# Cognito access token and calls the Gateway's MCP URL.
#
# To front a DIFFERENT MCP server later:
#   * point var.gateway_mcp_endpoint at the provider's MCP endpoint, and
#   * if it requires a key, add a `credential_provider_configuration { api_key { ... } }`
#     block on the target below referencing an
#     aws_bedrockagentcore_api_key_credential_provider that holds the key.
# A REST API can instead be attached as an OpenAPI target (see the note below).
#
# The Gateway target `name` MUST equal the `mcp` label in app/workflow.json
# ("knowledge" for the web_research agent). A separate KB retrieve target ("kb",
# in kb.tf) backs the RAG agent.

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
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${var.region}:${local.account_id}:*"
      },
      {
        # Needed when a target uses an API-key / OAuth credential provider.
        # Harmless for the public Knowledge MCP.
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = "arn:aws:secretsmanager:${var.region}:${local.account_id}:secret:bedrock-agentcore*"
      }
    ]
  })
}

# --- Gateway + MCP-server target ------------------------------------------
#
# Inbound auth: Cognito client-credentials. The runtime presents a Cognito
# access token whose scope matches the Resource Server; the Gateway validates
# via the Cognito User Pool's OIDC discovery endpoint.

resource "aws_bedrockagentcore_gateway" "mcp" {
  count           = var.enable_gateway ? 1 : 0
  name            = "${replace(var.agent_name, "_", "-")}-gw"
  role_arn        = aws_iam_role.gateway[0].arn
  protocol_type   = "MCP"
  authorizer_type = "CUSTOM_JWT"

  authorizer_configuration {
    custom_jwt_authorizer {
      discovery_url    = "https://cognito-idp.${var.region}.amazonaws.com/${var.cognito_user_pool_id}/.well-known/openid-configuration"
      allowed_audience = [var.cognito_gateway_client_id]

      # Cognito client-credentials tokens carry the client_id claim.
      allowed_clients = [var.cognito_gateway_client_id]
    }
  }
}

resource "aws_bedrockagentcore_gateway_target" "mcp" {
  count              = var.enable_gateway ? 1 : 0
  gateway_identifier = aws_bedrockagentcore_gateway.mcp[0].gateway_id
  name               = "knowledge"
  description        = "Remote MCP server fronted by the Gateway"

  target_configuration {
    mcp {
      mcp_server {
        endpoint = var.gateway_mcp_endpoint
      }
    }
  }
}

# ==========================================================================
# ADDING YOUR OWN MCP / REST PROVIDER
# ==========================================================================
# The Gateway can front additional providers alongside the default "knowledge"
# target. In short:
#
#   * Another MCP server: add a second `aws_bedrockagentcore_gateway_target`
#     with `target_configuration { mcp { mcp_server { endpoint = "..." } } }`,
#     naming it to match a new `mcp` label in app/workflow.json. If it needs a
#     key, attach a `credential_provider_configuration { api_key { ... } }`
#     referencing an `aws_bedrockagentcore_api_key_credential_provider` (its
#     name MUST start with "bedrock-agentcore" so the Gateway role can read it).
#
#   * A REST API: attach it as an OpenAPI target
#     (`target_configuration { mcp { open_api_schema { s3 { uri = "..." } } } }`).
#     The operation the agent calls must accept a parameter named `query`.
#
# CRITICAL: a target `name` MUST exactly equal the `mcp` label in
# app/workflow.json, or the agent silently falls back to simulated data.
