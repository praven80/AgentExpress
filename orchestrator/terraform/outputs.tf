output "ui_url" {
  description = "CloudFront URL for the web UI."
  value       = "https://${aws_cloudfront_distribution.ui.domain_name}"
}

output "api_endpoint" {
  description = "API Gateway endpoint (also reachable via CloudFront /api/*)."
  value       = aws_apigatewayv2_api.bff.api_endpoint
}

output "agent_runtime_arn" {
  value = awscc_bedrockagentcore_runtime.orchestrator.agent_runtime_arn
}

output "agent_runtime_id" {
  value = awscc_bedrockagentcore_runtime.orchestrator.agent_runtime_id
}

output "agent_runtime_status" {
  value = awscc_bedrockagentcore_runtime.orchestrator.status
}

output "memory_id" {
  value = awscc_bedrockagentcore_memory.orchestrator.memory_id
}

output "ecr_repository_url" {
  value = aws_ecr_repository.orchestrator.repository_url
}

output "image_uri" {
  value = local.image_uri
}

output "gateway_url" {
  description = "AgentCore Gateway MCP endpoint (empty when the Gateway is disabled)."
  value       = var.enable_gateway ? aws_bedrockagentcore_gateway.mcp[0].gateway_url : ""
}

output "gateway_id" {
  value = var.enable_gateway ? aws_bedrockagentcore_gateway.mcp[0].gateway_id : ""
}
output "knowledge_base_id" {
  description = <<-EOT
    Bedrock Knowledge Base id (empty when no tool declares type="kb").

    Worth watching after a corpus change: the id CHANGES whenever an immutable
    property of the vector store changes, because the KB and its index are named
    from a digest of those properties (see kb.tf). A new id means a new, empty KB
    and a fresh ingest, so re-check the ingestion job.
  EOT
  value       = local.kb_enabled ? aws_bedrockagent_knowledge_base.kb[0].id : ""
}
