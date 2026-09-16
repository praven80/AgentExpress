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

variable "transaction_search_indexing_percentage" {
  description = "Percentage of spans indexed for CloudWatch Transaction Search (0-100). Enabling Transaction Search is what delivers agent spans to the aws/spans log group, which AgentCore Observability and the Evaluations/Insights features read. 1% is free; the default 100 gives full trace coverage for a low-volume deployment — lower it to reduce cost at higher volume."
  type        = number
  default     = 100
}

# ===========================================================================
# IDENTITY PROVIDER (IdP) — one switch selects the whole auth stack
# ===========================================================================
# `idp` picks the provider for BOTH boundaries at once:
#   * end-user login on the UI + the API Gateway JWT authorizer on /api/*
#   * the machine-to-machine (client-credentials) token agents use to call the
#     AgentCore Gateway
#
# Everything downstream (authorizer issuer/audience, the Gateway's CUSTOM_JWT
# config, the OAuth token URL, the shape of auth-config.js the SPA reads) is
# derived in identity.tf — you set ONE value here.
#
#   "cognito" — Amazon Cognito. Terraform can create the whole pool for you
#               (cognito.create = true), including the confidential M2M client.
#   "auth0"   — an existing Auth0 tenant. Terraform creates nothing in Auth0;
#               you supply the tenant domain + client ids.
#   "none"    — NO authentication. The UI and /api/* are OPEN. Local trials and
#               throwaway sandboxes only — never anywhere shared.
#
# See identity.tf for the per-provider requirements (it fails the plan with a
# precise message if something required for your chosen provider is missing).

variable "idp" {
  description = "Identity provider for UI login + API auth + agent->Gateway M2M: \"cognito\", \"auth0\", or \"none\"."
  type        = string
  default     = "cognito"

  validation {
    condition     = contains(["cognito", "auth0", "none"], var.idp)
    error_message = "idp must be one of: \"cognito\", \"auth0\", \"none\"."
  }
}

variable "cognito" {
  description = "Cognito settings (used when idp = \"cognito\"). Set create = true to have Terraform provision the User Pool, Hosted UI domain, SPA client and — when enable_gateway is true — the confidential M2M client + resource server. Otherwise supply your existing ids."
  type = object({
    create        = optional(bool, true)
    user_pool_id  = optional(string, "")
    client_id     = optional(string, "")
    domain_prefix = optional(string, "")
  })
  default = {}
}

variable "auth0" {
  description = "Auth0 settings (used when idp = \"auth0\"). Terraform creates nothing in Auth0 — set these from your tenant. `client_id` is the SPA application (its id is also the ID-token audience the API authorizer checks)."
  type = object({
    domain    = optional(string, "")
    client_id = optional(string, "")
  })
  default = {}
}

# Machine-to-machine identity for agent -> AgentCore Gateway. Required when
# enable_gateway = true, EXCEPT for cognito with create = true (Terraform makes
# the client itself and reads the secret from state).
#
#   Cognito: a Resource Server defines the scope; `audience` is that OAuth2
#            scope, e.g. "gateway/invoke". The token has a `client_id` claim.
#   Auth0:   an API's Identifier is the `audience`. The token has an `aud` claim
#            and no `client_id`, so the Gateway pins the caller on `azp` instead.
variable "gateway_identity" {
  description = "M2M client the runtime uses to obtain a Gateway token. `audience` = the OAuth2 scope (Cognito) or the API identifier (Auth0)."
  type = object({
    client_id = optional(string, "")
    audience  = optional(string, "")
  })
  default = {}
}

variable "gateway_client_secret" {
  description = "M2M client secret. Pass via TF_VAR_gateway_client_secret; never commit it. Not needed when idp = \"cognito\" and cognito.create = true."
  type        = string
  default     = ""
  sensitive   = true
}

# --- AgentCore Gateway (MCP tool plane) -----------------------------------

variable "enable_gateway" {
  description = "Create an AgentCore Gateway fronting an MCP server. Set false to skip all Gateway/Cognito resources."
  type        = bool
  default     = true
}

# --- Tool secrets ----------------------------------------------------------
# API keys for tools declared in app/workflow.json, keyed by the TOOL NAME
# (the key in the workflow.json `tools` block). Only needed for a tool whose
# endpoint requires a key; everything else about the tool is declared in
# workflow.json, which stays free of secrets.
#
# Pass at apply time rather than committing:
#   export TF_VAR_tool_api_keys='{"billing":"sk-live-..."}'
#
# Each key is vaulted in an AgentCore API-key credential provider and sent by
# the Gateway as an X-API-Key header, so it never reaches the agent.
variable "tool_api_keys" {
  description = "Map of workflow.json tool name -> API key, for tools that need one."
  type        = map(string)
  default     = {}
  sensitive   = true
}
