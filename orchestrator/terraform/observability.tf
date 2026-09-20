# --- Observability UI asset (served from the UI bucket) --------------------
resource "aws_s3_object" "observability_js" {
  bucket        = aws_s3_bucket.ui.id
  key           = "observability.js"
  source        = "${path.module}/../web/observability.js"
  content_type  = "application/javascript"
  cache_control = "no-cache"
  etag          = filemd5("${path.module}/../web/observability.js")
}

# --- Observability: telemetry store + access -------------------------------
# One row per model / tool / memory / guardrail / policy / eval / compute event
# (written by app/features/observability). PK is the session_id so a whole run
# reads back with one query; the by_date GSI powers the by-date / by-model /
# by-user aggregation (grouped in the BFF).

resource "aws_dynamodb_table" "telemetry" {
  name         = "${var.agent_name}_telemetry"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "session_id"
  range_key    = "sk"

  attribute {
    name = "session_id"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }
  attribute {
    name = "date"
    type = "S"
  }

  global_secondary_index {
    name = "by_date"
    key_schema {
      attribute_name = "date"
      key_type       = "HASH"
    }
    key_schema {
      attribute_name = "sk"
      key_type       = "RANGE"
    }
    projection_type = "ALL"
  }

  # Auto-expire old telemetry. The writer stamps `ttl` on every row
  # (app/features/observability/store.py, TELEMETRY_TTL_DAYS, default 90 days) —
  # this was enabled with nothing writing the attribute, so rows carrying captured
  # prompts and model output were kept forever.
  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
}

# Orchestrator runtime writes telemetry, and also READS it back: AgentCore
# Evaluations scores each prompt from its persisted model-call I/O, which means
# querying this table.
resource "aws_iam_role_policy" "runtime_telemetry" {
  name = "TelemetryReadWrite-${var.agent_name}"
  role = aws_iam_role.runtime.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["dynamodb:PutItem", "dynamodb:Query", "dynamodb:GetItem"]
      Resource = [aws_dynamodb_table.telemetry.arn, "${aws_dynamodb_table.telemetry.arn}/index/*"]
    }]
  })
}

# Dedicated per-agent runtimes write telemetry too (only when any exist).
# One per dedicated agent, following the per-agent roles in subagent_runtimes.tf. Every
# dedicated container writes a telemetry row per model call, so unlike the feature-gated
# statements there this one is unconditional — it is just no longer shared.
resource "aws_iam_role_policy" "subagent_telemetry" {
  for_each = local.dedicated_agents
  name     = "TelemetryWrite-${var.agent_name}-${each.key}"
  role     = aws_iam_role.subagent[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["dynamodb:PutItem"]
      Resource = [aws_dynamodb_table.telemetry.arn]
    }]
  })
}

# BFF reads telemetry (session detail query + by_date GSI for aggregation).
resource "aws_iam_role_policy" "bff_telemetry" {
  name = "TelemetryRead-${var.agent_name}"
  role = aws_iam_role.bff.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["dynamodb:Query"]
      Resource = [aws_dynamodb_table.telemetry.arn, "${aws_dynamodb_table.telemetry.arn}/index/*"]
    }]
  })
}


