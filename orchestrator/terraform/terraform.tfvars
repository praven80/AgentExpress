# Local deploy config (gitignored). Generic sample deploy into a sandbox account.
#
# No Cognito User Pool wired here, so:
#   * cognito_* are left empty  -> UI/API deploy OPEN (no login). Fine for a sandbox demo.
#   * enable_gateway = false  -> no AgentCore Gateway / Knowledge Base. LLM inference
#                                is real (Bedrock); MCP + RAG calls run simulated.
# To enable auth + the Gateway/KB later, set the cognito_* values and enable_gateway = true.

region         = "us-west-2"
create_cognito = true
enable_gateway = false
