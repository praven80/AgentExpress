# ===========================================================================
# TOOLS — every Gateway target and every Cedar permit, derived from ONE config
# ===========================================================================
# This file reads the `tools` block in app/workflow.json and generates:
#
#   * one AgentCore Gateway target per entry (kb / websearch / mcp / openapi)
#   * one vaulted API-key credential provider per entry that needs a key
#   * the Cedar policy statements that permit exactly those tools
#
# A customer adds a data source by adding a JSON block to workflow.json — no
# Terraform edits. Nothing here is specific to this sample's three tools.
#
# The map KEY is the Gateway target name AND the label an agent references via
# its `tool` field. AgentCore names every tool "<targetName>___<toolName>", and
# that string is also the Cedar action id.

locals {
  # ---- Parse + normalise the tools block ---------------------------------
  # NOT gated on the Gateway: bff.tf reads this to build the UI's data-source
  # labels even when no tool plane is deployed. The Gateway gate is on `tools` below.
  tools_raw = try(local.workflow_def.tools, {})

  # Normalise every entry to ONE shape, so the result is a homogeneous map that
  # HCL can reason about (the raw entries differ per type). The `if` filter also
  # zeroes the whole map when there is no Gateway to host the targets — done here
  # rather than with a conditional, which would compare mismatched object types.
  tools = {
    for name, t in local.tools_raw : name => {
      type = lower(try(t.type, ""))
      # workflow.json descriptions explain the tool to whoever edits the config,
      # so they can run long; the Gateway target Description field caps at 200.
      description   = substr(try(t.description, "Tool target ${name}"), 0, 190)
      corpora       = try(t.corpora, [])
      endpoint      = try(t.endpoint, "")
      schema_s3_uri = try(t.schemaS3Uri, "")
      # type=lambda: an EXISTING function's ARN plus the tool schema to publish
      # for it. This is the general-purpose escape hatch — a Lambda can front a
      # database (Redshift, Snowflake, any RDBMS), an internal service, or a
      # private VPC resource, none of which the Gateway can reach directly. The
      # framework does not create or deploy the function; it registers it.
      lambda_arn = try(t.lambdaArn, "")
      # type=lambda alternative to lambdaArn: name a function the FRAMEWORK ships
      # and deploys, so the committed config stays account-neutral (a real ARN
      # would pin workflow.json to one AWS account). Exactly one built-in exists —
      # "tool_lambda", the run-history demo under orchestrator/tool_lambda/ — and
      # the validator accepts only that value. It is not a general "deploy any
      # function" feature: a function of YOUR OWN goes in via lambdaArn, and the
      # framework then touches neither its code nor its execution role.
      lambda_source = try(t.source, "")
      # A list of tool definitions, because ONE Lambda may publish several tools
      # (the Gateway passes the tool name through so the handler can dispatch).
      # Each entry: { name, description, properties: { <arg>: { type, required,
      # description } } }. Normalised to the provider's flattened `property`
      # blocks below.
      tool_schema = [
        for s in try(t.toolSchema, []) : {
          name        = try(s.name, "")
          description = substr(try(s.description, try(s.name, "tool")), 0, 190)
          properties = [
            for pname, p in try(s.properties, {}) : {
              name        = pname
              type        = lower(try(p.type, "string"))
              required    = try(p.required, false)
              description = substr(try(p.description, pname), 0, 190)
            }
          ]
        }
      ]
      max_results     = try(t.maxResults, 10)
      exclude_domains = try(t.excludeDomains, [])
      include_domains = try(t.includeDomains, [])
      # TARGET-level domain lists: hidden from the agent, enforced on every
      # request. The stronger control — see the notes on the websearch resource.
      target_include_domains = try(t.targetIncludeDomains, [])
      target_exclude_domains = try(t.targetExcludeDomains, [])
      # Optional connector version pin, e.g. "1.2.0" (request-level filters need
      # 1.2.0 or later). Empty tracks the latest.
      connector_version = try(t.connectorVersion, "")
      # Request-level published-date bounds, inclusive, ISO-8601 UTC.
      published_from = try(t.publishedFrom, "")
      published_to   = try(t.publishedTo, "")
      # Which tool on the target to invoke, the parameter the query goes into, and
      # any fixed extra arguments. A target may publish several tools, and every
      # MCP server names its parameters differently — these three make an
      # arbitrary server reachable from config alone, with no code change.
      # Consumed by app/features/gateway/client.py via TOOLS_JSON.
      call = try(t.call, "")
      arg  = try(t.arg, "")
      args = try(t.args, {})
      # Which result-row field carries which role, for an agent that consumes the
      # tool's DATA instead of its prose rendering (ctx.call_tool_rows). Optional:
      # only a deterministic agent needs it.
      row_fields = try(t.rowFields, {})

      # How the Gateway discovers the server's tools:
      #   DEFAULT — the Gateway synchronises the catalog when the target is
      #             created/updated and caches it (needed for semantic search).
      #   DYNAMIC — the Gateway forwards tools/list to the server at invocation
      #             time instead of pre-indexing. Avoids a stale catalogue when a
      #             server's tool set changes.
      #
      # Either works. When verifying with tools/list, remember the response is
      # PAGINATED — follow nextCursor, or a target whose tools sit on page two
      # looks empty.
      listing_mode = upper(try(t.listingMode, "DEFAULT"))

      # OUTBOUND auth from the Gateway to the tool's endpoint:
      #   none   — a public endpoint (the default)
      #   apikey — a vaulted key sent as X-API-Key (set it in var.tool_api_keys)
      #   sigv4  — the Gateway signs with SigV4 using its own execution role.
      #            Only works for servers hosted behind a service that verifies
      #            SigV4: AgentCore Runtime/Gateway, API Gateway, Lambda Function
      #            URLs. `service` is the signing name (auto-detected from the
      #            hostname when omitted).
      auth         = lower(try(t.auth, ""))
      auth_service = try(t.service, "")
      # Secrets never live in workflow.json — they come from tfvars, keyed by
      # the tool name (see var.tool_api_keys).
      api_key = try(var.tool_api_keys[name], "")
      # Cedar shape: a `policy.tool` narrows the permit to ONE tool (and can add
      # an argument restriction); its absence permits the whole target, which is
      # what a remote MCP server needs since its tool names aren't known here.
      # The managed web-search connector always publishes exactly ONE tool, named
      # "WebSearch", so default to permitting it BY NAME. This is not cosmetic: a
      # target-level permit does NOT authorize it. Verified on a live gateway —
      # `action in AgentCore::Action::"websearch"` produced
      # "ToolDenied: websearch___WebSearch was denied by the Cedar policy engine".
      policy_tool = try(t.policy.tool, "") != "" ? try(t.policy.tool, "") : (
        lower(try(t.type, "")) == "websearch" ? "WebSearch" : ""
      )
      policy_restrict = try(t.policy.restrictTo, {})
      # Set policy = false (or policy.permit = false) to register the target but
      # deliberately NOT permit it — useful for demonstrating default-deny.
      policy_permit = try(t.policy.permit, true)
    }
    if var.enable_gateway
  }

  # ---- Split by type -----------------------------------------------------
  # The KB is special: it isn't just a target, it provisions a whole Knowledge
  # Base (kb.tf). At most one is supported, so kb.tf keys off this flag.
  kb_tools = { for n, t in local.tools : n => t if t.type == "kb" }
  # The framework's closed value sets, from app/vocabulary.json — the SAME file
  # app/common/vocabulary.py and cdk/lib/vocabulary.ts read. Every list below used to be
  # written out here as well as in Python and TypeScript, and a copy that drifted meant a
  # value one plane accepted and another rejected.
  vocab = jsondecode(file("${path.module}/../app/vocabulary.json"))

  kb_tool_name = length(local.kb_tools) > 0 ? keys(local.kb_tools)[0] : ""
  # The corpora (doc_type tags) the KB agent(s) may retrieve from.
  kb_corpora = local.kb_tool_name != "" ? local.kb_tools[local.kb_tool_name].corpora : []
  # Retrieval depth for the KB Lambda (KB_NUM_RESULTS in kb.tf). Same `maxResults`
  # key a websearch tool uses, so the two tool types read the same way.
  kb_max_results = local.kb_tool_name != "" ? try(local.kb_tools[local.kb_tool_name].maxResults, 5) : 5

  websearch_tools = { for n, t in local.tools : n => t if t.type == "websearch" }
  mcp_tools       = { for n, t in local.tools : n => t if t.type == "mcp" }
  openapi_tools   = { for n, t in local.tools : n => t if t.type == "openapi" }
  lambda_tools    = { for n, t in local.tools : n => t if t.type == "lambda" }

  # Split by where the function comes from. `source` = one the framework ships and
  # deploys; `lambdaArn` = one you already own, which the framework only registers.
  builtin_lambda_tools  = { for n, t in local.lambda_tools : n => t if t.lambda_source != "" }
  external_lambda_tools = { for n, t in local.lambda_tools : n => t if t.lambda_source == "" }

  # Every declared lambda tool's effective ARN, whichever way it was supplied. The
  # Gateway target and the IAM grant both read this, so neither has to care.
  lambda_tool_arns = merge(
    { for n, t in local.external_lambda_tools : n => t.lambda_arn },
    { for n, t in local.builtin_lambda_tools : n => aws_lambda_function.tool[n].arn },
  )

  # A Lambda's resource policy can only be edited from the account that owns the
  # function, so `aws_lambda_permission` is emitted only for functions in THIS
  # account. Same-account invocation is already authorized by the Gateway role's
  # identity policy; the resource statement is belt-and-braces for the case where
  # the service invokes the function directly rather than by assuming the role.
  # For a cross-account function, the owning account adds the statement.
  # A framework-deployed function is always local, so it is always included.
  # The ARN format precondition below guarantees 7 colon-separated fields, so the
  # field-count guard this used to carry could never fail.
  external_lambda_tools_local = {
    for n, t in local.external_lambda_tools : n => t
    if split(":", t.lambda_arn)[4] == local.account_id
  }

  # Targets that reach a third-party endpoint and were given a key.
  # auth="apikey" without a key is rejected by a precondition below, so a key being
  # present is the only condition needed here.
  keyed_tools = {
    for n, t in local.tools : n => t
    if t.api_key != "" && contains(local.vocab.apiKeyToolTypes.values, t.type)
  }

  # ---- Cedar policy, generated ------------------------------------------
  # Only tools that are actually permitted produce a statement. Everything else
  # — including any tool name a compromised prompt invents — is refused by
  # Cedar's default-deny.
  permitted_tools = { for n, t in local.tools : n => t if t.policy_permit }

  # An argument restriction becomes a `when` clause. Today one key is supported
  # (the KB's `filter`), which is what the corpus scoping needs.
  cedar_statements = {
    for n, t in local.permitted_tools : n => (
      t.policy_tool != ""
      # Fine-grained: one specific tool, optionally argument-restricted.
      #
      # NOTE the `when` clause is NOT optional. The policy engine REFUSES to
      # create a permit that is unconditioned for an unconstrained principal:
      #   "Overly Permissive: Policy Engine will allow every request for the
      #    specified principal (AgentCore::IamEntity), action (...) and resource
      #    ... combination"  (HandlerErrorCode: NotStabilized)
      # So when a tool declares no `policy.restrictTo`, we still require the
      # query argument to be PRESENT. That is a real constraint (a call carrying
      # no query is refused) and it is what makes the permit acceptable.
      ? format(
        "permit(\n  principal,\n  action == AgentCore::Action::\"%s___%s\",\n  resource == AgentCore::Gateway::\"%s\"\n) when {\n%s\n};",
        n, t.policy_tool, local.gateway_arn_for_policy,
        length(keys(t.policy_restrict)) == 0
        # No declared restriction: require the query parameter to be present.
        ? format("  context.input has %s", t.arg != "" ? t.arg : "query")
        : join(" &&\n", [
          for arg, allowed in t.policy_restrict :
          format("  context.input has %s && %s.contains(context.input.%s)",
          arg, jsonencode(allowed), arg)
        ])
      )
      # Target-level fallback, used when the tool names cannot be known at deploy
      # time (an arbitrary remote MCP server whose catalogue we have not seen).
      #
      # What was actually MEASURED on a live gateway, since the two differ:
      #   * mcpServer target — a target-level permit WORKS. The `docs` target has no
      #     policy.tool and its tool calls succeed.
      #   * connector target — it did NOT. A call to websearch___WebSearch under
      #     `action in AgentCore::Action::"websearch"` came back ToolDenied, which is
      #     why the websearch permit is emitted by NAME (see policy_tool above).
      # No theory offered for the difference. If you point a tool at your own MCP
      # server and calls come back ToolDenied, set `policy.tool` to the published
      # name minus the "<target>___" prefix and it becomes a fine-grained permit.
      : format(
        "permit(\n  principal,\n  action in AgentCore::Action::\"%s\",\n  resource == AgentCore::Gateway::\"%s\"\n);",
        n, local.gateway_arn_for_policy
      )
    )
  }

  # ---- Workflow shape, for validation ------------------------------------
  # Every agent id named anywhere in `steps`, whatever the step shape.
  step_agent_ids = distinct(flatten([
    for s in local.workflow_def.steps : (
      try(s.parallel, null) != null ? s.parallel :
      try(s.sequence, null) != null ? s.sequence :
      [s.agent]
    )
  ]))

  # ---- Content-based branching, for validation ---------------------------
  # A step may carry `branch`, letting its own output choose what runs next (see
  # app/common/branching.py). Every mistake below FAILS SILENTLY at runtime: a
  # misspelled operator, a rule with no comparison, or a target that names nothing
  # all evaluate to "no match", so the run quietly takes the default on every
  # request and the branch looks like it is working. These mirror
  # graph_builder.validate_branches and validateBranches in the CDK path.
  #
  # Each local below collects human-readable STRINGS rather than objects, because a
  # list of the branch specs themselves would not type-unify: two steps' rules
  # legitimately have different key sets.

  # The name a step is addressed by, and what a branch target names.
  step_names = [
    for i, s in local.workflow_def.steps : try(s.agent, try(s.gateId, "group${i}"))
  ]
  branch_steps = [for i, s in local.workflow_def.steps : i if try(s.branch, null) != null]
  # Two steps sharing a name make a branch target ambiguous, and the lookups below
  # would silently resolve it to the later one.
  branch_duplicate_step_names = length(local.branch_steps) > 0 && length(distinct(local.step_names)) != length(local.step_names) ? local.step_names : []

  branch_ops       = ["equals", "notEquals", "in", "contains", "exists", "gt", "gte", "lt", "lte"]
  branch_rule_keys = concat(local.branch_ops, ["field", "goto"])

  # A parallel group has no single agent whose output decides.
  branch_on_parallel = [
    for i in local.branch_steps : local.step_names[i]
    if try(local.workflow_def.steps[i].parallel, null) != null
  ]
  # A branch on the last step has nowhere to route.
  branch_on_last = [
    for i in local.branch_steps : local.step_names[i]
    if i == length(local.workflow_def.steps) - 1
  ]
  # `when` alone, `default` alone (an unconditional jump to a later step), or both —
  # but not neither, and not an empty `when`.
  branch_without_rules = [
    for i in local.branch_steps : local.step_names[i]
    if length(try(local.workflow_def.steps[i].branch.when, [])) == 0
    && try(local.workflow_def.steps[i].branch.default, "") == ""
  ]
  branch_with_empty_when = [
    for i in local.branch_steps : local.step_names[i]
    if try(length(local.workflow_def.steps[i].branch.when) == 0, false)
  ]
  branch_bad_rules = flatten([
    for i in local.branch_steps : [
      for ri, r in try(local.workflow_def.steps[i].branch.when, []) :
      "steps[${i}].branch.when[${ri}] has ${join(" and ", compact([
        length(setsubtract(keys(r), local.branch_rule_keys)) > 0
        ? "unknown key(s) ${join(",", setsubtract(keys(r), local.branch_rule_keys))}" : "",
        try(r.goto, "") == "" ? "no `goto`" : "",
        length(setintersection(keys(r), local.branch_ops)) == 0 ? "no comparison" : "",
      ]))}"
      if length(setsubtract(keys(r), local.branch_rule_keys)) > 0
      || try(r.goto, "") == ""
      || length(setintersection(keys(r), local.branch_ops)) == 0
    ]
  ])
  # Every step a branch can route to: each rule's `goto`, plus `default`. "END" is
  # the run itself, so it is not a step name.
  branch_targets = flatten([
    for i in local.branch_steps : [
      for t in concat(
        [for r in try(local.workflow_def.steps[i].branch.when, []) : try(r.goto, "")],
        [try(local.workflow_def.steps[i].branch.default, "")],
      ) : "${i}|${t}" if t != "" && t != "END"
    ]
  ])
  branch_unknown_targets = [
    for t in local.branch_targets : t if !contains(local.step_names, split("|", t)[1])
  ]
  branch_backward_targets = [
    for t in local.branch_targets : t
    if contains(local.step_names, split("|", t)[1])
    && index(local.step_names, split("|", t)[1]) <= tonumber(split("|", t)[0])
  ]

  # ---- Agent placement (`runtime`), for validation ------------------------
  # "main" and "dedicated" are placements of OUR code; "a2a" is a trust boundary —
  # the agent is somebody else's service, reached at its Agent Card URL. Mirrors
  # registry.validate_runtimes and validateRuntimes in the CDK path.
  a2a_runtimes   = local.vocab.runtimes.values
  a2a_auth_modes = local.vocab.a2aAuthModes.values

  # A misspelled runtime falls through to "main" and dies on a missing module.
  bad_runtimes = [
    for id, a in local.workflow_def.agents : "${id}=${try(a.runtime, "main")}"
    if !contains(local.a2a_runtimes, try(a.runtime, "main"))
  ]
  a2a_agent_ids = [
    for id, a in local.workflow_def.agents : id if try(a.runtime, "main") == "a2a"
  ]
  # `agentCard`/`auth` on any other placement read as settings and control nothing.
  a2a_keys_on_local_agents = [
    for id, a in local.workflow_def.agents : id
    if try(a.runtime, "main") != "a2a"
    && (try(a.agentCard, "") != "" || try(a.auth, "") != "")
  ]
  a2a_sources = local.vocab.a2aSources.values
  # EXACTLY ONE of agentCard (an agent that already exists) or source (the stand-in this
  # repo deploys, whose Function URL is generated by the deploy and cannot be committed).
  # Same rule a `tools` entry has for lambdaArn vs source, for the same reason.
  a2a_card_xor_source = [
    for id in local.a2a_agent_ids :
    "${id}=${try(local.workflow_def.agents[id].agentCard, "") != "" ? "both" : "neither"}"
    if(try(local.workflow_def.agents[id].agentCard, "") != "") == (try(local.workflow_def.agents[id].source, "") != "")
  ]
  # The stand-in is behind an AWS_IAM Function URL, so sigv4 is the only auth that can
  # reach it; anything else is a guaranteed 403 that reads like a missing agent.
  a2a_source_without_sigv4 = [
    for id in local.a2a_agent_ids : "${id}=${lower(try(local.workflow_def.agents[id].auth, "none"))}"
    if try(local.workflow_def.agents[id].source, "") == "a2a_lambda"
    && lower(try(local.workflow_def.agents[id].auth, "none")) != "sigv4"
  ]
  a2a_bad_source = [
    for id in local.a2a_agent_ids : "${id}=${try(local.workflow_def.agents[id].source, "")}"
    if try(local.workflow_def.agents[id].source, "") != ""
    && !contains(local.a2a_sources, try(local.workflow_def.agents[id].source, ""))
  ]
  # `skill` picks one of the stand-in's published skills, so it only means anything with
  # `source`. Read from app/vocabulary.json like everything else here: as a literal pair
  # this rejected the shipped `skill = "analysis"` while the CDK plane accepted it, which
  # is exactly the drift that file exists to make impossible.
  a2a_lambda_skills = local.vocab.a2aLambdaSkills.values
  a2a_skill_without_source = [
    for id in local.a2a_agent_ids : id
    if try(local.workflow_def.agents[id].skill, "") != ""
    && try(local.workflow_def.agents[id].source, "") == ""
  ]
  a2a_bad_skill = [
    for id in local.a2a_agent_ids : "${id}=${try(local.workflow_def.agents[id].skill, "")}"
    if try(local.workflow_def.agents[id].source, "") == "a2a_lambda"
    && try(local.workflow_def.agents[id].skill, "") != ""
    && !contains(local.a2a_lambda_skills, lower(try(local.workflow_def.agents[id].skill, "")))
  ]
  # A plaintext card URL would put the bearer token for someone else's agent on the wire.
  a2a_insecure_card = [
    for id in local.a2a_agent_ids : id
    if try(local.workflow_def.agents[id].agentCard, "") != ""
    && !startswith(lower(try(local.workflow_def.agents[id].agentCard, "")), "https://")
  ]
  a2a_bad_auth = [
    for id in local.a2a_agent_ids : "${id}=${try(local.workflow_def.agents[id].auth, "none")}"
    if !contains(local.a2a_auth_modes, lower(try(local.workflow_def.agents[id].auth, "none")))
  ]
  a2a_oauth_without_provider = [
    for id in local.a2a_agent_ids : id
    if lower(try(local.workflow_def.agents[id].auth, "none")) == "oauth2"
    && length(try(local.workflow_def.agents[id].agentcore.identity.outbound, [])) == 0
  ]
  # A remote agent reaches its own data sources, and makes its own model call.
  a2a_with_local_only_keys = flatten([
    for id in local.a2a_agent_ids : [
      for k in ["tool", "corpus", "model", "temperature", "maxTokens", "access"] :
      "${id}.${k}" if can(local.workflow_def.agents[id][k])
    ]
  ])
  # A bearer-auth agent needs its token supplied out of band, never in workflow.json.
  a2a_missing_tokens = [
    for id in local.a2a_agent_ids : id
    if lower(try(local.workflow_def.agents[id].auth, "none")) == "bearer"
    && try(var.a2a_tokens[id], "") == ""
  ]

  # The real corpora available to the KB: the TOP-LEVEL folders under kb_docs/,
  # because kb.tf derives each document's doc_type from its first path segment.
  # Files sitting at the root have no folder and would produce a junk doc_type.
  kb_doc_folders = distinct([
    for f in local.kb_files : split("/", f)[0] if length(split("/", f)) > 1
  ])
  kb_root_files = [for f in local.kb_files : f if length(split("/", f)) == 1]

  # ---- What the app needs ------------------------------------------------
  # Injected as TOOLS_JSON so an agent's ctx can look up how to call its tool
  # (which argument shape, which corpus) without hardcoding anything.
  tools_env = jsonencode({
    for n, t in local.tools : n => merge(
      { type = t.type },
      t.type == "kb" ? { corpora = t.corpora } : {},
      t.type == "websearch" ? merge(
        { maxResults = t.max_results },
        length(t.include_domains) > 0 ? { includeDomains = t.include_domains } : {},
        length(t.exclude_domains) > 0 ? { excludeDomains = t.exclude_domains } : {},
        t.published_from != "" ? { publishedFrom = t.published_from } : {},
        t.published_to != "" ? { publishedTo = t.published_to } : {},
      ) : {},
      t.call != "" ? { call = t.call } : {},
      t.arg != "" ? { arg = t.arg } : {},
      length(keys(t.args)) > 0 ? { args = t.args } : {},
      # rowFields: which field of this target's result rows carries which role, for
      # an agent that reads the tool's DATA rather than its prose (see
      # ctx.call_tool_rows). Repointing the tool at another source is then a config
      # edit — set these to its field names — instead of an agent code change.
      length(keys(t.row_fields)) > 0 ? { rowFields = t.row_fields } : {},
    )
  })
}

# --- Validation ------------------------------------------------------------
# Catch a malformed tools block at PLAN time with a message naming the entry,
# rather than after a 10-minute apply.
resource "terraform_data" "tools_validation" {
  input = keys(local.tools)

  lifecycle {
    precondition {
      condition = alltrue([
        for n, t in local.tools : contains(local.vocab.toolTypes.values, t.type)
      ])
      error_message = "Each workflow.json tools entry needs \"type\" = kb | websearch | mcp | openapi | lambda."
    }
    precondition {
      condition     = alltrue([for n, t in local.mcp_tools : t.endpoint != ""])
      error_message = "A tools entry with type=\"mcp\" requires \"endpoint\" (your MCP server's Streamable HTTP URL)."
    }
    precondition {
      condition     = alltrue([for n, t in local.openapi_tools : t.schema_s3_uri != ""])
      error_message = "A tools entry with type=\"openapi\" requires \"schemaS3Uri\" (s3:// URI of the OpenAPI schema)."
    }
    # --- type=lambda ------------------------------------------------------
    # The Gateway will not discover a Lambda's tools for itself: unlike an MCP
    # server there is no tools/list to call, so the schema has to be declared. A
    # missing or malformed one is accepted by the API and then publishes a tool the
    # agent cannot call, which surfaces as an empty answer rather than an error.
    precondition {
      # Exactly one source of truth for WHICH function to call. Both set is
      # ambiguous; neither means there is nothing to register.
      condition = alltrue([
        for n, t in local.lambda_tools :
        (t.lambda_arn != "") != (t.lambda_source != "")
      ])
      error_message = "A tools entry with type=\"lambda\" needs EXACTLY ONE of \"lambdaArn\" (a function you already own \u2014 the framework only registers it) or \"source\" (a function the framework ships and deploys). Offending: ${join(", ", [for n, t in local.lambda_tools : n if(t.lambda_arn != "") == (t.lambda_source != "")])}."
    }
    precondition {
      # `source` is deliberately NOT a general "deploy any directory" feature —
      # a framework-deployed function needs an execution role the config cannot
      # express, so only the built-in demo function is supported.
      condition = alltrue([for n, t in local.builtin_lambda_tools :
      contains(local.vocab.builtinLambdaSource.values, t.lambda_source)])
      error_message = "A type=\"lambda\" tool's \"source\" must be \"tool_lambda\" \u2014 the run-history demo function the framework ships under orchestrator/tool_lambda/. It is not a general \"deploy any function\" option: a framework-deployed function needs an execution role that config cannot express. To use a function of your own, deploy it yourself and set \"lambdaArn\" instead. Offending: ${join(", ", [for n, t in local.builtin_lambda_tools : n if t.lambda_source != "tool_lambda"])}."
    }
    precondition {
      condition = alltrue([
        for n, t in local.external_lambda_tools :
        can(regex("^arn:aws[a-z-]*:lambda:[a-z0-9-]+:[0-9]{12}:function:[a-zA-Z0-9-_]+(:[a-zA-Z0-9-_$]+)?$", t.lambda_arn))
      ])
      error_message = "A type=\"lambda\" tool's \"lambdaArn\" must be the full ARN of an EXISTING function, e.g. \"arn:aws:lambda:us-east-1:123456789012:function:my-tool\" (an alias or version qualifier is allowed). Offending: ${join(", ", [for n, t in local.external_lambda_tools : n if !can(regex("^arn:aws[a-z-]*:lambda:[a-z0-9-]+:[0-9]{12}:function:[a-zA-Z0-9-_]+(:[a-zA-Z0-9-_$]+)?$", t.lambda_arn))])}."
    }
    precondition {
      condition     = alltrue([for n, t in local.lambda_tools : length(t.tool_schema) > 0])
      error_message = "A tools entry with type=\"lambda\" requires a non-empty \"toolSchema\" list. The Gateway cannot discover a Lambda's tools (there is no tools/list to call), so each tool must be declared: { \"name\": \"...\", \"description\": \"...\", \"properties\": { \"<arg>\": { \"type\": \"string\", \"required\": true, \"description\": \"...\" } } }. Offending: ${join(", ", [for n, t in local.lambda_tools : n if length(t.tool_schema) == 0])}."
    }
    precondition {
      condition = alltrue(flatten([
        for n, t in local.lambda_tools : [for s in t.tool_schema : s.name != "" && length(s.properties) > 0]
      ]))
      error_message = "Every \"toolSchema\" entry needs a \"name\" and at least one entry in \"properties\" — a tool with no arguments has nothing for the agent's query to go into."
    }
    precondition {
      # The Gateway rejects a property type outside the JSON-Schema scalar set it
      # supports, but only at apply time and with a generic message.
      condition = alltrue(flatten([
        for n, t in local.lambda_tools : [
          for s in t.tool_schema : [
            for p in s.properties : contains(local.vocab.toolSchemaPropertyTypes.values, p.type)
          ]
        ]
      ]))
      error_message = "A \"toolSchema\" property \"type\" must be one of string, number, integer, boolean, array, object."
    }
    precondition {
      # Mirrors _select_tool in app/features/gateway/client.py, which REFUSES to
      # guess between several published tools. Without `call`, an agent bound to a
      # multi-tool Lambda fails at runtime with ToolUnavailable.
      condition = alltrue([
        for n, t in local.lambda_tools : length(t.tool_schema) == 1 || t.call != ""
      ])
      error_message = "A type=\"lambda\" tool that declares several \"toolSchema\" entries must also set \"call\" naming which one an agent invokes — the app refuses to guess between them. Offending: ${join(", ", [for n, t in local.lambda_tools : n if length(t.tool_schema) > 1 && t.call == ""])}."
    }
    precondition {
      # A `call` that matches no declared tool is a typo that would otherwise only
      # show up as ToolUnavailable on the first real run.
      condition = alltrue([
        for n, t in local.lambda_tools :
        t.call == "" || contains([for s in t.tool_schema : s.name], t.call)
      ])
      error_message = "A type=\"lambda\" tool's \"call\" must name one of its own \"toolSchema\" entries. Offending: ${join(", ", [for n, t in local.lambda_tools : n if t.call != "" && !contains([for s in t.tool_schema : s.name], t.call)])}."
    }
    precondition {
      # `arg` decides which property the agent's query is written into, so it has to
      # exist — otherwise the query is sent under a name the function ignores and
      # the tool answers as though no query had been supplied.
      condition = alltrue(flatten([
        for n, t in local.lambda_tools : [
          for s in t.tool_schema :
          t.arg == "" || t.call != s.name || contains([for p in s.properties : p.name], t.arg)
        ]
      ]))
      error_message = "A type=\"lambda\" tool's \"arg\" must name a property of the tool named by \"call\" — that is where the agent's query is placed."
    }
    precondition {
      condition     = length(local.kb_tools) <= 1
      error_message = "Only one tools entry may have type=\"kb\" (there is a single Knowledge Base per deployment). Scope agents to different document sets with `corpora` + each agent's `corpus` instead."
    }
    precondition {
      condition     = length(local.websearch_tools) <= 1
      error_message = "Only one tools entry may have type=\"websearch\" (the managed connector is a single target)."
    }
    precondition {
      # A KB with no corpora would provision an index nothing may retrieve from,
      # because the generated Cedar permit restricts on the corpus list.
      condition     = length(local.kb_tools) == 0 || length(local.kb_corpora) > 0
      error_message = "A tools entry with type=\"kb\" requires a non-empty \"corpora\" list naming the top-level folders under kb_docs/."
    }
    precondition {
      # Every agent's `tool` must resolve, or that agent silently runs without data.
      condition = alltrue([
        for id, a in local.workflow_def.agents :
        contains(keys(local.tools_raw), try(a.tool, ""))
        if try(a.tool, "") != ""
      ])
      error_message = "An agent's \"tool\" in workflow.json does not match any key in the \"tools\" block. The label must match exactly."
    }
    precondition {
      # The web-search connector is regional; fail early rather than at apply.
      condition = length(local.websearch_tools) == 0 || contains(
        local.vocab.webSearchRegions.values, var.region
      )
      error_message = "type=\"websearch\" (AgentCore Web Search) is only available in us-east-1, eu-west-1 and ap-northeast-1. Remove the entry or deploy in one of those regions."
    }
    precondition {
      condition     = alltrue([for n, t in local.tools : contains(local.vocab.toolListingModes.values, t.listing_mode)])
      error_message = "A tools entry's \"listingMode\" must be \"DEFAULT\" or \"DYNAMIC\"."
    }
    precondition {
      condition     = alltrue([for n, t in local.tools : contains(local.vocab.toolAuthModes.values, t.auth)])
      error_message = "A tools entry's \"auth\" must be omitted, \"none\", \"apikey\" or \"sigv4\"."
    }
    precondition {
      # An apikey target with no key would silently call the endpoint unauthenticated.
      condition = alltrue([
        for n, t in local.tools : t.api_key != "" if t.auth == "apikey"
      ])
      error_message = "A tools entry with auth=\"apikey\" needs its key in var.tool_api_keys, keyed by the tool name: export TF_VAR_tool_api_keys='{\"<tool>\":\"...\"}'."
    }
    precondition {
      # Only mcp/openapi targets get a credential provider (see keyed_tools). On any
      # other type the key was accepted, vaulted nowhere, and the endpoint called
      # unauthenticated — a silent security downgrade rather than an error.
      condition = alltrue([
        for n, t in local.tools : contains(local.vocab.apiKeyToolTypes.values, t.type)
        if t.auth == "apikey" || t.api_key != ""
      ])
      error_message = "auth=\"apikey\" (or a key in var.tool_api_keys) is only supported for type=\"mcp\" and type=\"openapi\" — those are the target kinds that get a credential provider. Offending: ${join(", ", [for n, t in local.tools : n if(t.auth == "apikey" || t.api_key != "") && !contains(local.vocab.apiKeyToolTypes.values, t.type)])}."
    }
  }
}

# --- Workflow shape validation ---------------------------------------------
# The tools block is validated above. These guard the AGENTS and STEPS, which
# nothing used to check: a bad id previously surfaced as a runtime graph-build
# crash, a deploy-time AgentCore API error, or (worst) silently empty retrievals.

resource "terraform_data" "workflow_validation" {
  input = keys(local.workflow_def.agents)

  lifecycle {
    precondition {
      # Every id named in `steps` must exist in `agents`, or the graph fails to
      # build at RUNTIME with a ModuleNotFoundError/KeyError inside the container.
      condition = alltrue([
        for id in local.step_agent_ids : contains(keys(local.workflow_def.agents), id)
      ])
      error_message = "app/workflow.json `steps` names agent id(s) that are not in `agents`: ${join(", ", setsubtract(local.step_agent_ids, keys(local.workflow_def.agents)))}."
    }
    precondition {
      # Every agent must appear in `steps`, or it is dead config that provisions a
      # runtime nothing ever calls.
      condition = alltrue([
        for id in keys(local.workflow_def.agents) : contains(local.step_agent_ids, id)
      ])
      error_message = "app/workflow.json declares agent(s) that no `steps` entry runs: ${join(", ", setsubtract(keys(local.workflow_def.agents), local.step_agent_ids))}. Add them to `steps` or remove them."
    }
    precondition {
      condition     = length(local.branch_on_parallel) == 0
      error_message = "app/workflow.json: `branch` is not supported on a `parallel` step, because a group has no single agent whose output decides. Put the branch on a single-agent step, or on a `sequence` step (its LAST agent decides). Offending step(s): ${join(", ", local.branch_on_parallel)}."
    }
    precondition {
      condition     = length(local.branch_on_last) == 0
      error_message = "app/workflow.json: `branch` on the LAST step has nowhere to route. Use it on an earlier step, or drop it - \"END\" is already where the last step goes. Offending step(s): ${join(", ", local.branch_on_last)}."
    }
    precondition {
      condition     = length(local.bad_runtimes) == 0
      error_message = "app/workflow.json has agent(s) with an unknown `runtime` (shown as \"<agent>=<value>\"): ${join(", ", local.bad_runtimes)}. Valid values are ${join(", ", local.a2a_runtimes)}. A misspelling would otherwise be treated as \"main\" and fail at container start on a missing module under app/subagents/."
    }
    precondition {
      condition     = length(local.a2a_keys_on_local_agents) == 0
      error_message = "app/workflow.json agent(s) set `agentCard` or `auth` without `runtime = \"a2a\"`: ${join(", ", local.a2a_keys_on_local_agents)}. Those keys are read only for a remote (A2A) agent, so elsewhere they look like settings and control nothing."
    }
    precondition {
      condition     = length(local.a2a_card_xor_source) == 0
      error_message = "app/workflow.json A2A agent(s) need EXACTLY ONE of `agentCard` (an agent that already exists - its base URL, or its card URL) or `source` (the stand-in this repo deploys, whose URL only exists after a deploy). Shown as \"<agent>=<both|neither>\": ${join(", ", local.a2a_card_xor_source)}."
    }
    precondition {
      condition     = length(local.a2a_source_without_sigv4) == 0
      error_message = "app/workflow.json agent(s) use `source = \"a2a_lambda\"` without `auth = \"sigv4\"` (shown as \"<agent>=<auth>\"): ${join(", ", local.a2a_source_without_sigv4)}. That stand-in is deployed behind an AWS_IAM Function URL, so the request must be SigV4-signed with the orchestrator's own role - there is no token, by design."
    }
    precondition {
      condition     = length(local.a2a_bad_source) == 0
      error_message = "app/workflow.json A2A agent(s) have an unknown `source` (shown as \"<agent>=<value>\"): ${join(", ", local.a2a_bad_source)}. The only value is ${join(", ", local.a2a_sources)} - a framework-deployed agent needs infrastructure config cannot express."
    }
    precondition {
      condition     = length(local.a2a_skill_without_source) == 0
      error_message = "app/workflow.json agent(s) set `skill` while pointing at an external `agentCard`: ${join(", ", local.a2a_skill_without_source)}. `skill` selects among the skills of the stand-in this repo deploys; a third party's agent advertises its own in its card."
    }
    precondition {
      condition     = length(local.a2a_bad_skill) == 0
      error_message = "app/workflow.json A2A agent(s) name a skill the stand-in does not publish (shown as \"<agent>=<value>\"): ${join(", ", local.a2a_bad_skill)}. It publishes ${join(", ", local.a2a_lambda_skills)} - see a2a_lambda/handler.py SKILLS. An unrecognised one would silently fall back to the default reviewer."
    }
    precondition {
      # The A2A request may carry a bearer token, so plaintext is not an option.
      condition     = length(local.a2a_insecure_card) == 0
      error_message = "app/workflow.json agent(s) have a non-https `agentCard`: ${join(", ", local.a2a_insecure_card)}. The A2A request may carry a bearer token, so the endpoint must be https://."
    }
    precondition {
      condition     = length(local.a2a_bad_auth) == 0
      error_message = "app/workflow.json has A2A agent(s) with an unknown `auth` (shown as \"<agent>=<value>\"): ${join(", ", local.a2a_bad_auth)}. Valid values are ${join(", ", local.a2a_auth_modes)}."
    }
    precondition {
      condition     = length(local.a2a_oauth_without_provider) == 0
      error_message = "app/workflow.json agent(s) use `auth = \"oauth2\"` with no `agentcore.identity.outbound` provider to get a token from: ${join(", ", local.a2a_oauth_without_provider)}. Add one, or use `auth = \"bearer\"` with a token in var.a2a_tokens."
    }
    precondition {
      condition     = length(local.a2a_with_local_only_keys) == 0
      error_message = "app/workflow.json A2A agent(s) also set keys that only apply to an agent THIS deployment runs: ${join(", ", local.a2a_with_local_only_keys)}. A remote agent reaches its own data sources (tool/corpus) and makes its own model call (model/temperature/maxTokens)."
    }
    precondition {
      # A silent empty token becomes a 401 from a service you do not control, which is
      # a much harder failure to read than this message.
      condition     = length(local.a2a_missing_tokens) == 0
      error_message = "app/workflow.json agent(s) declare `auth = \"bearer\"` but no token was supplied for them: ${join(", ", local.a2a_missing_tokens)}. Tokens never go in workflow.json — pass them keyed by agent id: export TF_VAR_a2a_tokens='{\"<agent>\":\"...\"}'."
    }
    precondition {
      condition     = length(local.branch_duplicate_step_names) == 0
      error_message = "app/workflow.json `steps` has duplicate step name(s), which makes a `branch` target ambiguous. Each step is named by its `agent` id or its `gateId`; give every step a distinct one. Names in order: ${join(", ", local.branch_duplicate_step_names)}."
    }
    precondition {
      condition     = length(local.branch_without_rules) == 0
      error_message = "app/workflow.json: `branch` needs `when` (conditional rules), `default` (an unconditional jump to a later step), or both. Offending step(s): ${join(", ", local.branch_without_rules)}."
    }
    precondition {
      condition     = length(local.branch_with_empty_when) == 0
      error_message = "app/workflow.json: `branch.when` must be a non-empty list of rules. Omit it entirely for an unconditional jump via `default` alone. Offending step(s): ${join(", ", local.branch_with_empty_when)}."
    }
    precondition {
      # A rule with an unknown key, no `goto`, or no comparison never matches, so the
      # branch silently takes `default` forever.
      condition     = length(local.branch_bad_rules) == 0
      error_message = "app/workflow.json has malformed branch rule(s): ${join("; ", local.branch_bad_rules)}. A rule is `goto`, an optional `field`, and one or more of ${join(", ", local.branch_ops)}."
    }
    precondition {
      condition     = length(local.branch_unknown_targets) == 0
      error_message = "app/workflow.json has branch target(s) (shown as \"<step index>|<target>\") naming no step: ${join(", ", local.branch_unknown_targets)}. A target is \"END\", a single-agent step's `agent` id, or a group step's `gateId`. Known step names: ${join(", ", local.step_names)}."
    }
    precondition {
      # A backward edge is a cycle the run could not leave; re-running earlier work is
      # what a review gate's `revise` is for.
      condition     = length(local.branch_backward_targets) == 0
      error_message = "app/workflow.json has branch target(s) (shown as \"<step index>|<target>\") at or before their own step: ${join(", ", local.branch_backward_targets)}. Branch targets must be LATER steps."
    }
    precondition {
      # A dedicated agent's id becomes part of its AgentCore Runtime name, which
      # only accepts [a-zA-Z][a-zA-Z0-9_]* — a hyphen fails at DEPLOY time with an
      # opaque API error. Enforce it for every agent so ids stay portable.
      condition = alltrue([
        for id in keys(local.workflow_def.agents) : can(regex("^[a-zA-Z][a-zA-Z0-9_]*$", id))
      ])
      error_message = "Agent ids in app/workflow.json must match ^[a-zA-Z][a-zA-Z0-9_]*$ (letters, digits and underscores; no hyphens) because the id becomes part of the AgentCore Runtime name. Offending: ${join(", ", [for id in keys(local.workflow_def.agents) : id if !can(regex("^[a-zA-Z][a-zA-Z0-9_]*$", id))])}."
    }
    precondition {
      # var.agent_name + "_" + agent id must fit the runtime name limit (48).
      condition = alltrue([
        for id, a in local.dedicated_agents :
        length("${var.agent_name}_${id}") <= 48
      ])
      error_message = "A dedicated agent's AgentCore Runtime name is \"<agent_name>_<agent id>\" and must be 48 characters or fewer. Shorten var.agent_name or the agent id."
    }
    precondition {
      # Every corpus named in the kb tool must be a real top-level folder under
      # kb_docs/, otherwise the generated Cedar permit filters on a doc_type no
      # chunk carries and retrievals come back EMPTY with no error at all.
      condition     = length(setsubtract(local.kb_corpora, local.kb_doc_folders)) == 0
      error_message = "tools.kb.corpora names folder(s) that do not exist under orchestrator/kb_docs/: ${join(", ", setsubtract(local.kb_corpora, local.kb_doc_folders))}. Found: ${join(", ", local.kb_doc_folders)}."
    }
    precondition {
      # An agent's `corpus` must be one of the declared corpora.
      condition = alltrue([
        for id, a in local.workflow_def.agents :
        contains(local.kb_corpora, try(a.corpus, ""))
        if try(a.corpus, "") != ""
      ])
      error_message = "An agent's \"corpus\" in app/workflow.json is not in tools.<kb>.corpora. Declared corpora: ${join(", ", local.kb_corpora)}."
    }
    precondition {
      # A file at the ROOT of kb_docs/ gets doc_type = <filename>, i.e. a junk
      # corpus, because doc_type is the first path segment.
      condition     = length(local.kb_root_files) == 0
      error_message = "These files sit at the root of orchestrator/kb_docs/: ${join(", ", local.kb_root_files)}. Each document must live in a top-level FOLDER, because that folder name becomes its corpus (doc_type)."
    }
    precondition {
      # RBAC needs an authenticated caller. With idp = "none" there is no
      # authorizer, so no claims, so no groups — every rule in `authorization`
      # would silently evaluate against an empty group set and DENY everyone,
      # locking the UI's own buttons out of the app it just deployed.
      condition     = local.auth_enabled || length(local.authz_actions) == 0
      error_message = "app/workflow.json restricts ${join(", ", keys(local.authz_actions))} in `authorization.actions`, but idp = \"none\" deploys the API with no authorizer, so there are no JWT claims to authorize against and every one of those actions would be denied. Set idp to \"cognito\" or \"auth0\", or remove `authorization.actions`."
    }
    precondition {
      # A typo'd action name is worse than useless: it looks like a restriction in
      # the config but gates nothing, so the real action stays wide open.
      condition     = length(setsubtract(keys(local.authz_actions), local.authz_known_actions)) == 0
      error_message = "app/workflow.json `authorization.actions` names unknown action(s): ${join(", ", setsubtract(keys(local.authz_actions), local.authz_known_actions))}. Valid actions are ${join(", ", local.authz_known_actions)} (see ACTIONS in bff/authz.py). An unrecognised key gates nothing."
    }
  }
}

# ===========================================================================
# Gateway targets
# ===========================================================================
# The KB target lives in kb.tf (it needs the retrieve Lambda + tool schema).
# Everything else is generated here, one resource per type.

# --- Managed AgentCore Web Search connector -------------------------------
# A built-in connector, so there is no endpoint, no credentials and no schema to
# maintain: the Gateway resolves and authenticates to the AWS-owned backend
# itself, and queries never leave AWS. The tool surfaces as "<name>___WebSearch".
#
# The connector supports TWO layers of domain filtering, and they compose on every
# request. Both are config in workflow.json:
#
#   TARGET level  — targetIncludeDomains / targetExcludeDomains. Set on the target
#                   below, HIDDEN from the calling agent, applied to every request.
#                   This is the enforceable layer.
#   REQUEST level — includeDomains / excludeDomains / publishedFrom / publishedTo.
#                   Sent per call by the app (app/features/gateway/client.py).
#                   Supplied by the CALLER, so it is scoping, not a boundary.
#
# Per the docs: a domain is dropped if it appears on EITHER exclude list, and
# returned only if it appears on EVERY include list that is set. A request-level
# filter can therefore never override a target-level exclude or widen results
# beyond a target-level include.
#
# maxResults (1-25, default 10) and the request-level filters follow the published
# WebSearch input schema. Request-level filters need connector v1.2.0 or later —
# set `connectorVersion` to pin it.
resource "aws_bedrockagentcore_gateway_target" "websearch" {
  for_each           = local.websearch_tools
  gateway_identifier = aws_bedrockagentcore_gateway.mcp[0].gateway_id
  name               = each.key
  description        = each.value.description

  target_configuration {
    mcp {
      connector {
        source {
          connector_id = "web-search"
          # Optional pin. Request-level filters require 1.2.0+; leaving it unset
          # tracks the latest connector version.
          version = each.value.connector_version != "" ? each.value.connector_version : null
        }

        # "WebSearch" is the tool this connector publishes; the Gateway exposes it
        # as "<target name>___WebSearch", which is also the Cedar action id.
        configuration {
          name        = "WebSearch"
          description = each.value.description
          # REQUIRED, even when empty. The API DISCARDS a configuration entry
          # that has no parameter_values and then reports "Connector
          # configurations must not be empty" - which reads as if the list were
          # absent rather than its single entry dropped. Verified directly
          # against CreateGatewayTarget: adding "{}" is what makes it count.
          #
          # TARGET-level domain lists go in here too, and they are the enforceable
          # form: hidden from the agent and applied to every request. Populated
          # from targetIncludeDomains / targetExcludeDomains in workflow.json, so
          # `{}` when neither is set (which is what makes the entry count).
          parameter_values = jsonencode(merge(
            length(each.value.target_include_domains) > 0 ? {
              domainFilter = { include = each.value.target_include_domains }
            } : {},
            length(each.value.target_exclude_domains) > 0 ? {
              domainFilter = merge(
                length(each.value.target_include_domains) > 0 ? {
                  include = each.value.target_include_domains
                } : {},
                { exclude = each.value.target_exclude_domains },
              )
            } : {},
          ))
        }
      }
    }
  }

  # REQUIRED for a connector target: without it CreateGatewayTarget fails with
  # "Credential provider configurations is not defined". The connector runs
  # inside AWS, so the Gateway authenticates with its OWN execution role and
  # there is no secret to supply.
  credential_provider_configuration {
    gateway_iam_role {}
  }
}

# --- Remote MCP servers ---------------------------------------------------
# The generic "bring your own MCP server" path: change `endpoint` in
# workflow.json and this target follows.
resource "aws_bedrockagentcore_gateway_target" "mcp_server" {
  for_each           = local.mcp_tools
  gateway_identifier = aws_bedrockagentcore_gateway.mcp[0].gateway_id
  name               = each.key
  description        = each.value.description

  target_configuration {
    mcp {
      mcp_server {
        endpoint     = each.value.endpoint
        listing_mode = each.value.listing_mode
      }
    }
  }

  # Outbound auth to the MCP server, selected by `auth` in workflow.json.
  dynamic "credential_provider_configuration" {
    for_each = (each.value.auth == "apikey" || each.value.api_key != "") ? [1] : []
    content {
      api_key {
        provider_arn              = aws_bedrockagentcore_api_key_credential_provider.tool[each.key].credential_provider_arn
        credential_location       = "HEADER"
        credential_parameter_name = "X-API-Key"
      }
    }
  }

  # SigV4: the Gateway signs with its OWN execution role, so no secret exists at
  # all. This is how you reach an MCP server you host yourself on AgentCore
  # Runtime (or behind API Gateway / a Lambda Function URL).
  dynamic "credential_provider_configuration" {
    for_each = each.value.auth == "sigv4" ? [1] : []
    content {
      gateway_iam_role {
        service = each.value.auth_service != "" ? each.value.auth_service : null
        region  = var.region
      }
    }
  }
}

# --- REST APIs described by an OpenAPI schema ------------------------------
# The Gateway translates MCP tool calls into HTTP requests. Tool names come from
# the schema's operationIds, so you control them (and can avoid the "___"
# delimiter problem entirely).
resource "aws_bedrockagentcore_gateway_target" "openapi" {
  for_each           = local.openapi_tools
  gateway_identifier = aws_bedrockagentcore_gateway.mcp[0].gateway_id
  name               = each.key
  description        = each.value.description

  target_configuration {
    mcp {
      open_api_schema {
        s3 {
          uri = each.value.schema_s3_uri
        }
      }
    }
  }

  dynamic "credential_provider_configuration" {
    for_each = (each.value.auth == "apikey" || each.value.api_key != "") ? [1] : []
    content {
      api_key {
        provider_arn              = aws_bedrockagentcore_api_key_credential_provider.tool[each.key].credential_provider_arn
        credential_location       = "HEADER"
        credential_parameter_name = "X-API-Key"
      }
    }
  }

  dynamic "credential_provider_configuration" {
    for_each = each.value.auth == "sigv4" ? [1] : []
    content {
      gateway_iam_role {
        service = each.value.auth_service != "" ? each.value.auth_service : null
        region  = var.region
      }
    }
  }
}

# --- Your own Lambda functions --------------------------------------------
# The general-purpose escape hatch. Anything the Gateway cannot reach directly —
# Redshift, Snowflake, an RDBMS, an internal service, a resource inside a VPC — is
# reachable by pointing at a Lambda that fronts it. The framework does NOT create
# the function: you deploy it however you already deploy Lambdas, and declare its
# ARN here.
#
# Unlike an MCP server there is no tools/list to call, so the tool schema is
# DECLARED rather than discovered. One function may publish several tools; the
# Gateway passes the tool name through so the handler can dispatch on it.
#
# The Gateway invokes the function with its OWN execution role (the
# gateway_iam_role credential provider), so there is no secret anywhere.
# --- The built-in demo function (`source: "tool_lambda"`) ------------------
# Deployed only when a tools entry asks for it. It exists so `type: "lambda"` is
# demonstrable out of the box without asking you to stand up a database first: it
# answers from the two DynamoDB tables this deployment already writes, so the rows
# it returns are real.
#
# Its execution role is FIXED — logs plus read-only on this deployment's own
# tables — which is exactly why `source` accepts no other value. A function of
# your own is declared with `lambdaArn`, and the framework then touches neither
# its code nor its role.
data "archive_file" "tool_lambda" {
  for_each    = local.builtin_lambda_tools
  type        = "zip"
  source_dir  = "${path.module}/../${each.value.lambda_source}"
  output_path = "${path.module}/.build/${each.key}.zip"
}

resource "aws_iam_role" "tool_lambda" {
  for_each = local.builtin_lambda_tools
  name     = "ToolLambda-${var.agent_name}-${each.key}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "tool_lambda" {
  for_each = local.builtin_lambda_tools
  name     = "ToolLambdaPricingRead"
  role     = aws_iam_role.tool_lambda[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${var.region}:${local.account_id}:*"
      },
      {
        # READ ONLY, and the ONLY thing this function reads: the public AWS price
        # list. It touches no data of yours, which is why `source` can accept a
        # framework-deployed function at all — the execution role is fixed and
        # contains nothing a customer would need to review.
        #
        # The Price List Query API has no resource-level permissions, so "*" is the
        # only grant it accepts. It exposes public list prices, not this account's
        # spend, so there is nothing here to scope down to.
        Sid      = "ReadPublicPriceList"
        Effect   = "Allow"
        Action   = ["pricing:GetProducts", "pricing:DescribeServices"]
        Resource = "*"
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "tool" {
  for_each          = local.builtin_lambda_tools
  name              = "/aws/lambda/ToolLambda-${var.agent_name}-${each.key}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "tool" {
  for_each = local.builtin_lambda_tools
  # Prefixed to match this function's own IAM role (ToolLambda-…) and the other
  # framework-owned functions (AgentCoreBFF-…, AgentCoreKBRetrieve-…). Without a
  # prefix the name is just "<agent_name>-<tool>", which a scoped deploy policy
  # cannot express without granting lambda:* on every function in the account.
  # Mirrors cdk/lib/tool-plane.ts.
  function_name    = "ToolLambda-${var.agent_name}-${each.key}"
  role             = aws_iam_role.tool_lambda[each.key].arn
  runtime          = "python3.12"
  handler          = "handler.lambda_handler"
  filename         = data.archive_file.tool_lambda[each.key].output_path
  source_code_hash = data.archive_file.tool_lambda[each.key].output_base64sha256
  timeout          = 30
  memory_size      = 256

  environment {
    variables = {
      # Where the Price List Query API endpoint lives, which is NOT where prices
      # are being asked about: the API is published in only two regions and
      # describes prices for all of them. Pinned so the tool works on a deployment
      # in any region. The region to price FOR is the tool's own `region` argument,
      # defaulting to this deployment's AWS_REGION.
      PRICING_API_REGION = "us-east-1"
    }
  }
}

# --- Gateway targets for every lambda tool, however the ARN was supplied ---
resource "aws_bedrockagentcore_gateway_target" "lambda_fn" {
  for_each           = local.lambda_tools
  gateway_identifier = aws_bedrockagentcore_gateway.mcp[0].gateway_id
  name               = each.key
  description        = each.value.description

  target_configuration {
    mcp {
      lambda {
        lambda_arn = local.lambda_tool_arns[each.key]
        tool_schema {
          dynamic "inline_payload" {
            for_each = each.value.tool_schema
            content {
              name        = inline_payload.value.name
              description = inline_payload.value.description
              input_schema {
                type = "object"
                dynamic "property" {
                  for_each = inline_payload.value.properties
                  content {
                    name        = property.value.name
                    type        = property.value.type
                    required    = property.value.required
                    description = property.value.description
                  }
                }
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

  # The role policy below must exist before the target is registered, or the first
  # invocation is refused.
  depends_on = [aws_iam_role_policy.gateway_invoke_lambda]
}

# Let the Gateway's execution role invoke every declared function. One statement
# listing all of them, rather than a policy per tool, to stay well inside the
# 10-managed-policy / inline-policy size limits as the tool count grows.
resource "aws_iam_role_policy" "gateway_invoke_lambda" {
  count = length(local.lambda_tools) > 0 ? 1 : 0
  name  = "GatewayInvokeToolLambdas-${var.agent_name}"
  role  = aws_iam_role.gateway[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "InvokeToolLambdas"
      Effect   = "Allow"
      Action   = ["lambda:InvokeFunction"]
      Resource = sort(values(local.lambda_tool_arns))
    }]
  })
}

# Same-account functions only — a Lambda's resource policy can only be edited from
# the account that owns it. Cross-account functions still work; their owner adds
# the equivalent statement on their side. A framework-deployed function is always
# in this account, so it is always covered.
resource "aws_lambda_permission" "gateway_invoke_tool_lambda" {
  for_each       = merge(local.external_lambda_tools_local, local.builtin_lambda_tools)
  statement_id   = "AllowAgentCoreGatewayInvoke-${var.agent_name}"
  action         = "lambda:InvokeFunction"
  function_name  = local.lambda_tool_arns[each.key]
  principal      = "bedrock-agentcore.amazonaws.com"
  source_account = local.account_id
}

# --- Vaulted API keys -----------------------------------------------------
# One per tool that supplied a key. The name MUST start with "bedrock-agentcore"
# so the Gateway role's Secrets Manager statement (gateway.tf) can read it.
resource "aws_bedrockagentcore_api_key_credential_provider" "tool" {
  for_each = local.keyed_tools
  name     = "bedrock-agentcore-${replace(var.agent_name, "_", "-")}-${each.key}"
  api_key  = each.value.api_key
}
