# --- Cognito User Pool (optional, created when create_cognito = true) ------

resource "aws_cognito_user_pool" "this" {
  count = var.create_cognito ? 1 : 0
  name  = "${var.agent_name}-users"

  auto_verified_attributes = ["email"]
  username_attributes      = ["email"]

  admin_create_user_config {
    allow_admin_create_user_only = false
  }

  password_policy {
    minimum_length    = 8
    require_uppercase = true
    require_numbers   = true
    require_symbols   = false
  }

  schema {
    name                = "email"
    attribute_data_type = "String"
    required            = true
    mutable             = true
    string_attribute_constraints {
      min_length = 1
      max_length = 256
    }
  }
}

resource "aws_cognito_user_pool_domain" "this" {
  count        = var.create_cognito ? 1 : 0
  domain       = "${replace(var.agent_name, "_", "-")}-${local.account_id}"
  user_pool_id = aws_cognito_user_pool.this[0].id
}

resource "aws_cognito_user_pool_client" "spa" {
  count        = var.create_cognito ? 1 : 0
  name         = "${var.agent_name}-spa"
  user_pool_id = aws_cognito_user_pool.this[0].id

  generate_secret              = false
  explicit_auth_flows          = ["ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
  allowed_oauth_flows          = ["code"]
  allowed_oauth_scopes         = ["openid", "email", "profile"]
  supported_identity_providers = ["COGNITO"]

  allowed_oauth_flows_user_pool_client = true
  callback_urls                        = ["https://localhost"]
  logout_urls                          = ["https://localhost"]
}

locals {
  cognito_pool_id     = var.create_cognito ? aws_cognito_user_pool.this[0].id : var.cognito_user_pool_id
  cognito_client_id   = var.create_cognito ? aws_cognito_user_pool_client.spa[0].id : var.cognito_user_pool_client_id
  cognito_domain      = var.create_cognito ? aws_cognito_user_pool_domain.this[0].domain : var.cognito_domain_prefix
}

output "cognito_user_pool_id" {
  value = local.cognito_pool_id
}

output "cognito_client_id" {
  value = local.cognito_client_id
}

output "cognito_domain_prefix" {
  value = local.cognito_domain
}
