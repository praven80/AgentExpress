variable "region" {
  type    = string
  default = "us-east-1"
}

variable "agent_name" {
  description = "AgentCore runtime name (letters, numbers, underscores)."
  type        = string
  default     = "multiagent_orchestrator"
}

variable "model_id" {
  type    = string
  default = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
}

variable "container_engine" {
  description = "Container CLI used to build/push images (docker, finch, or podman)."
  type        = string
  default     = "docker"
}

variable "memory_event_expiry_days" {
  description = "Retention for AgentCore Memory short-term events."
  type        = number
  default     = 30
}

# --- Cognito (authentication) ---------------------------------------------
# Leave cognito_user_pool_id empty to deploy with no authentication. When set,
# the API Gateway routes require a valid Cognito JWT and the UI gates on login.
# Alternatively, set create_cognito = true to have Terraform create the pool.

variable "create_cognito" {
  description = "Create a Cognito User Pool, Domain, and App Client automatically."
  type        = bool
  default     = false
}

variable "cognito_user_pool_id" {
  description = "Cognito User Pool ID (empty disables auth). Set in terraform.tfvars."
  type        = string
  default     = ""
}

variable "cognito_user_pool_client_id" {
  description = "Cognito User Pool App Client ID (public, for the SPA). Set in terraform.tfvars."
  type        = string
  default     = ""
}

variable "cognito_domain_prefix" {
  description = "Cognito Hosted UI domain prefix (the part before .auth.<region>.amazoncognito.com). Set in terraform.tfvars."
  type        = string
  default     = ""
}

# --- AgentCore Gateway (MCP tool plane) -----------------------------------

variable "enable_gateway" {
  description = "Create an AgentCore Gateway fronting an MCP server. Set false to skip all Gateway/Cognito resources."
  type        = bool
  default     = true
}

variable "gateway_mcp_endpoint" {
  description = "Remote MCP server endpoint the Gateway fronts. Default is the public AWS Knowledge MCP (no key). Point at a different MCP server as needed."
  type        = string
  default     = "https://knowledge-mcp.global.api.aws"
}

# Cognito machine-to-machine (client-credentials) identity for agent -> Gateway.
# Create a Cognito App Client with a secret + custom scopes on a Resource Server.
# Required when enable_gateway = true.

variable "cognito_gateway_client_id" {
  description = "Cognito App Client ID (with secret) used by the runtime to obtain a Gateway token. Set in terraform.tfvars."
  type        = string
  default     = ""
}

variable "cognito_gateway_client_secret" {
  description = "Cognito App Client Secret. Pass via TF_VAR_cognito_gateway_client_secret; never commit it."
  type        = string
  default     = ""
  sensitive   = true
}

variable "cognito_gateway_scope" {
  description = "OAuth2 scope the runtime requests when fetching a Gateway token (e.g. gateway/invoke). Set in terraform.tfvars."
  type        = string
  default     = ""
}


