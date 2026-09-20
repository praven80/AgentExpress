# --- AgentCore Policy (Cedar authorization at the Gateway) -----------------
#
# Enforcement is SERVER-SIDE: when an agent calls a tool through the Gateway, the
# attached policy engine evaluates these Cedar rules and allows or denies it.
#
# YOU DO NOT WRITE CEDAR. Every statement below is generated from the `tools`
# block in app/workflow.json (see terraform/tools.tf → local.cedar_statements):
#
#   * declaring a tool permits that tool
#   * a tool with `policy.tool` + `policy.restrictTo` gets a fine-grained permit
#     with an argument restriction (the KB uses this to pin retrieval to its
#     declared corpora)
#   * anything NOT declared — including a tool name a prompt-injected instruction
#     invents — matches no permit and is refused by Cedar's DEFAULT-DENY
#
# orchestrator.policy in workflow.json controls the whole feature:
#   enabled = true  -> engine created + attached, decisions OBEYED (DENY blocks)
#   enabled = false -> no engine created or attached; no policy evaluation at all
#   mode    = "ENFORCE" (default) | "LOG_ONLY" (evaluate + log without blocking)

locals {
  # Policy only applies when there is a Gateway to attach it to.
  policy_enabled = var.enable_gateway && try(local.workflow_def.orchestrator.policy.enabled, local.key_defaults.orchestrator.policy.enabled)
  policy_mode    = upper(try(local.workflow_def.orchestrator.policy.mode, local.key_defaults.orchestrator.policy.mode))

  # The Gateway ARN every generated statement is scoped to. A tool-specific
  # policy REQUIRES a specific Gateway ARN — AgentCore rejects a wildcard
  # resource or a bare `resource is AgentCore::Gateway` type constraint.
  gateway_arn_for_policy = var.enable_gateway ? aws_bedrockagentcore_gateway.mcp[0].gateway_arn : ""
}

resource "aws_bedrockagentcore_policy_engine" "main" {
  count       = local.policy_enabled ? 1 : 0
  name        = "${var.agent_name}_policy"
  description = "Cedar policy engine governing AgentCore Gateway tool access"
}

# One policy per declared tool. Named after the tool so a denial in CloudWatch
# points straight at the config entry that governs it.
resource "aws_bedrockagentcore_policy" "tool" {
  for_each = local.policy_enabled ? local.cedar_statements : {}

  name             = "permit_${each.key}"
  description      = "Generated from workflow.json tools.${each.key}. Anything not permitted here is denied by Cedar's default-deny."
  policy_engine_id = aws_bedrockagentcore_policy_engine.main[0].policy_engine_id

  definition {
    cedar {
      statement = each.value
    }
  }

  # The Cedar engine validates action names against the gateway's REGISTERED
  # targets, so a policy can only be created after the target it governs exists.
  # Without this a fresh single apply can order the policy first and fail with
  # "unrecognized action".
  depends_on = [
    aws_bedrockagentcore_gateway_target.kb,
    aws_bedrockagentcore_gateway_target.websearch,
    aws_bedrockagentcore_gateway_target.mcp_server,
    aws_bedrockagentcore_gateway_target.openapi,
    # A type="lambda" tool produces a Cedar statement like any other, so it belongs
    # here too. Omitting it left the exact "unrecognized action" ordering failure
    # this list exists to prevent reachable on a fresh single apply.
    aws_bedrockagentcore_gateway_target.lambda_fn,
  ]
}
