# --- Bedrock Knowledge Base (S3 Vectors) fronted by the Gateway -----------
#
# The RAG research agent (knowledge_research) retrieves from a Bedrock Knowledge
# Base backed by S3 Vectors (serverless, no OCU floor). Retrieval is exposed as a
# tool through the
# same Cognito-authed AgentCore Gateway via a Lambda target. All declarative — no
# vector index bootstrap script required (S3 Vectors index is a native resource).

locals {
  kb_enabled  = var.enable_gateway
  kb_dims     = 1024 # Titan Text Embeddings v2 default
  embed_model = "arn:aws:bedrock:${var.region}::foundation-model/amazon.titan-embed-text-v2:0"

  # Corpus files, recursive (subfolders included), excluding macOS noise that
  # Bedrock ingestion would reject.
  kb_files = [
    for f in fileset("${path.module}/../kb_docs", "**") : f
    if !endswith(f, ".DS_Store")
  ]
}

# --- S3 Vectors store -----------------------------------------------------

resource "aws_s3vectors_vector_bucket" "kb" {
  count              = local.kb_enabled ? 1 : 0
  vector_bucket_name = "agentcore-${replace(var.agent_name, "_", "-")}-kb-${local.account_id}"
  force_destroy      = true
}

resource "aws_s3vectors_index" "kb" {
  count              = local.kb_enabled ? 1 : 0
  vector_bucket_name = aws_s3vectors_vector_bucket.kb[0].vector_bucket_name
  index_name         = "kb-index"
  data_type          = "float32"
  dimension          = local.kb_dims
  distance_metric    = "cosine"

  # Bedrock stores the chunk text in this metadata key; it must be non-filterable
  # because chunk text exceeds the filterable-metadata size limit.
  metadata_configuration {
    non_filterable_metadata_keys = ["AMAZON_BEDROCK_TEXT"]
  }
}

# --- Documents bucket + sample corpus -------------------------------------

resource "aws_s3_bucket" "kb_docs" {
  count         = local.kb_enabled ? 1 : 0
  bucket        = "agentcore-${replace(var.agent_name, "_", "-")}-kbdocs-${local.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "kb_docs" {
  count                   = local.kb_enabled ? 1 : 0
  bucket                  = aws_s3_bucket.kb_docs[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_object" "kb_docs" {
  for_each = local.kb_enabled ? toset(local.kb_files) : []
  bucket   = aws_s3_bucket.kb_docs[0].id
  key      = each.value
  source   = "${path.module}/../kb_docs/${each.value}"
  etag     = filemd5("${path.module}/../kb_docs/${each.value}")
}

# Per-document metadata sidecars. Bedrock reads "<key>.metadata.json" next to
# each source object and attaches its attributes to every chunk (it does not
# ingest the sidecar itself as a document). We tag each doc with a filterable
# `doc_type` derived from its top-level folder (e.g. "reference") so a RAG agent
# can restrict retrieval to its own corpus instead of searching the whole shared
# index. Add more folders to add more corpora.
resource "aws_s3_object" "kb_docs_metadata" {
  for_each     = local.kb_enabled ? toset(local.kb_files) : []
  bucket       = aws_s3_bucket.kb_docs[0].id
  key          = "${each.value}.metadata.json"
  content      = jsonencode({ metadataAttributes = { doc_type = split("/", each.value)[0] } })
  content_type = "application/json"
}

# --- KB service role ------------------------------------------------------

resource "aws_iam_role" "kb" {
  count = local.kb_enabled ? 1 : 0
  name  = "AgentCoreKB-${var.agent_name}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "bedrock.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = local.account_id } }
    }]
  })
}

resource "aws_iam_role_policy" "kb" {
  count = local.kb_enabled ? 1 : 0
  name  = "KBPolicy-${var.agent_name}"
  role  = aws_iam_role.kb[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "Embeddings"
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel"]
        Resource = [local.embed_model]
      },
      {
        Sid      = "S3VectorsData"
        Effect   = "Allow"
        Action   = ["s3vectors:*"]
        Resource = [aws_s3vectors_vector_bucket.kb[0].vector_bucket_arn, "${aws_s3vectors_vector_bucket.kb[0].vector_bucket_arn}/*"]
      },
      {
        Sid      = "DocsRead"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:ListBucket"]
        Resource = [aws_s3_bucket.kb_docs[0].arn, "${aws_s3_bucket.kb_docs[0].arn}/*"]
      }
    ]
  })
}

# --- Knowledge Base + data source -----------------------------------------

resource "aws_bedrockagent_knowledge_base" "kb" {
  count    = local.kb_enabled ? 1 : 0
  name     = "${replace(var.agent_name, "_", "-")}-kb"
  role_arn = aws_iam_role.kb[0].arn

  knowledge_base_configuration {
    type = "VECTOR"
    vector_knowledge_base_configuration {
      embedding_model_arn = local.embed_model
      embedding_model_configuration {
        bedrock_embedding_model_configuration {
          dimensions          = local.kb_dims
          embedding_data_type = "FLOAT32"
        }
      }
    }
  }

  storage_configuration {
    type = "S3_VECTORS"
    s3_vectors_configuration {
      index_arn = aws_s3vectors_index.kb[0].index_arn
    }
  }

  depends_on = [aws_iam_role_policy.kb]
}

resource "aws_bedrockagent_data_source" "kb" {
  count             = local.kb_enabled ? 1 : 0
  knowledge_base_id = aws_bedrockagent_knowledge_base.kb[0].id
  name              = "docs"

  data_source_configuration {
    type = "S3"
    s3_configuration {
      bucket_arn = aws_s3_bucket.kb_docs[0].arn
    }
  }
}

# Trigger ingestion when the corpus changes. No declarative equivalent exists for
# StartIngestionJob; this mirrors the existing build_push local-exec pattern.
resource "null_resource" "kb_ingest" {
  count = local.kb_enabled ? 1 : 0
  triggers = {
    docs_hash = sha1(join(",", [for f in sort(local.kb_files) : filemd5("${path.module}/../kb_docs/${f}")]))
    meta_hash = sha1(jsonencode({ for f in local.kb_files : f => split("/", f)[0] }))
    ds_id     = aws_bedrockagent_data_source.kb[0].data_source_id
  }
  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      aws bedrock-agent start-ingestion-job --region ${var.region} \
        --knowledge-base-id ${aws_bedrockagent_knowledge_base.kb[0].id} \
        --data-source-id ${aws_bedrockagent_data_source.kb[0].data_source_id}
    EOT
  }
  depends_on = [aws_s3_object.kb_docs, aws_s3_object.kb_docs_metadata]
}

# --- Retrieve Lambda (Gateway Lambda target) ------------------------------

data "archive_file" "kb_lambda" {
  count       = local.kb_enabled ? 1 : 0
  type        = "zip"
  source_dir  = "${path.module}/../kb_lambda"
  output_path = "${path.module}/.build/kb_lambda.zip"
}

resource "aws_iam_role" "kb_lambda" {
  count = local.kb_enabled ? 1 : 0
  name  = "AgentCoreKBLambda-${var.agent_name}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "kb_lambda" {
  count = local.kb_enabled ? 1 : 0
  name  = "KBLambdaPolicy-${var.agent_name}"
  role  = aws_iam_role.kb_lambda[0].id
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
        Action   = ["bedrock:Retrieve"]
        Resource = [aws_bedrockagent_knowledge_base.kb[0].arn]
      }
    ]
  })
}

resource "aws_lambda_function" "kb_retrieve" {
  count            = local.kb_enabled ? 1 : 0
  function_name    = "AgentCoreKBRetrieve-${var.agent_name}"
  role             = aws_iam_role.kb_lambda[0].arn
  runtime          = "python3.13"
  handler          = "handler.lambda_handler"
  filename         = data.archive_file.kb_lambda[0].output_path
  source_code_hash = data.archive_file.kb_lambda[0].output_base64sha256
  timeout          = 30
  memory_size      = 256

  environment {
    variables = {
      KB_ID = aws_bedrockagent_knowledge_base.kb[0].id
    }
  }
}

# Allow the Gateway (via its execution role) to invoke the retrieve Lambda.
resource "aws_iam_role_policy" "gateway_invoke_kb" {
  count = local.kb_enabled ? 1 : 0
  name  = "GatewayInvokeKB-${var.agent_name}"
  role  = aws_iam_role.gateway[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["lambda:InvokeFunction"]
      Resource = [aws_lambda_function.kb_retrieve[0].arn]
    }]
  })
}

resource "aws_lambda_permission" "gateway_invoke_kb" {
  count          = local.kb_enabled ? 1 : 0
  statement_id   = "AllowAgentCoreGatewayInvoke"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.kb_retrieve[0].function_name
  principal      = "bedrock-agentcore.amazonaws.com"
  source_account = local.account_id
}

# --- Gateway target: KB retrieve tool -------------------------------------

resource "aws_bedrockagentcore_gateway_target" "kb" {
  count              = local.kb_enabled ? 1 : 0
  gateway_identifier = aws_bedrockagentcore_gateway.mcp[0].gateway_id
  name               = "kb"
  description        = "Bedrock Knowledge Base retrieval tool"

  target_configuration {
    mcp {
      lambda {
        lambda_arn = aws_lambda_function.kb_retrieve[0].arn
        tool_schema {
          inline_payload {
            name        = "retrieve"
            description = "Retrieve relevant document chunks from the knowledge base for a query."
            input_schema {
              type = "object"
              property {
                name        = "query"
                type        = "string"
                required    = true
                description = "The natural-language search query."
              }
              property {
                name        = "filter"
                type        = "string"
                required    = false
                description = "Optional doc_type to restrict retrieval to one corpus (e.g. reference)."
              }
            }
          }
        }
      }
    }
  }

  credential_provider_configuration {
    gateway_iam_role {}
  }
}
