# --- Static UI on S3 + CloudFront -----------------------------------------

resource "aws_s3_bucket" "ui" {
  # S3 bucket names must be lowercase and cannot contain underscores.
  bucket        = "agentcore-${replace(var.agent_name, "_", "-")}-ui-${local.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "ui" {
  bucket                  = aws_s3_bucket.ui.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_object" "index" {
  bucket        = aws_s3_bucket.ui.id
  key           = "index.html"
  source        = "${path.module}/../web/index.html"
  content_type  = "text/html"
  cache_control = "no-cache" # HTML must revalidate so UI updates land immediately
  etag          = filemd5("${path.module}/../web/index.html")
}

# Non-secret Cognito settings injected into the SPA at deploy time. Rendered from
# a template so index.html stays static (its JS uses ${...} literals that must
# not be interpreted by Terraform).
# Non-secret IdP settings the SPA reads at load time. local.ui_auth is built in
# identity.tf and already carries the right fields for the selected provider, so
# this stays provider-agnostic.
resource "aws_s3_object" "auth_config" {
  bucket        = aws_s3_bucket.ui.id
  key           = "auth-config.js"
  content_type  = "application/javascript"
  cache_control = "no-cache"
  content       = templatefile("${path.module}/../web/auth-config.js.tftpl", local.ui_auth)
  etag          = md5(templatefile("${path.module}/../web/auth-config.js.tftpl", local.ui_auth))
}

resource "aws_cloudfront_origin_access_control" "ui" {
  name                              = "agentcore-${var.agent_name}-oac"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

locals {
  api_host = replace(aws_apigatewayv2_api.bff.api_endpoint, "https://", "")
  # AWS managed policies
  cache_disabled_id   = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
  cache_optimized_id  = "658327ea-f89d-4fab-a63d-7e88639e58f6"
  orp_all_except_host = "b689b0a8-53d0-40ab-baf2-68738e2966ac"
}

resource "aws_cloudfront_distribution" "ui" {
  enabled             = true
  default_root_object = "index.html"
  comment             = "Multi-agent orchestrator UI"

  origin {
    origin_id                = "s3-ui"
    domain_name              = aws_s3_bucket.ui.bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.ui.id
  }

  origin {
    origin_id   = "api-bff"
    domain_name = local.api_host
    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }

  default_cache_behavior {
    target_origin_id       = "s3-ui"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    cache_policy_id        = local.cache_optimized_id
  }

  ordered_cache_behavior {
    path_pattern             = "/api/*"
    target_origin_id         = "api-bff"
    viewer_protocol_policy   = "https-only"
    allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods           = ["GET", "HEAD"]
    cache_policy_id          = local.cache_disabled_id
    origin_request_policy_id = local.orp_all_except_host
  }

  restrictions {
    geo_restriction { restriction_type = "none" }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }
}

resource "aws_s3_bucket_policy" "ui" {
  bucket = aws_s3_bucket.ui.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowCloudFrontRead"
      Effect    = "Allow"
      Principal = { Service = "cloudfront.amazonaws.com" }
      Action    = "s3:GetObject"
      Resource  = "${aws_s3_bucket.ui.arn}/*"
      Condition = {
        StringEquals = { "AWS:SourceArn" = aws_cloudfront_distribution.ui.arn }
      }
    }]
  })
}


