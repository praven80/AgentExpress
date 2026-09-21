# AgentExpress config contract

The exact shapes, taken from the shipped `orchestrator/app/workflow.json` and the agent
base class. `orchestrator/app/keys.json` is the authoritative source; this is the working
subset an author needs.

## Top-level keys

`$schema`, `$comment`, `orchestrator`, `ui`, `guardrail`, `authorization`, `tools`,
`agents`, `steps`.

## Tool types and their keys

Observed in the shipped config. `description` is required on every type; `type` selects
the rest.

| type | Keys used | Reaches |
|---|---|---|
| `kb` | `corpora`, `maxResults`, `policy` | A Bedrock Knowledge Base. Each top-level folder in `kb_docs/` is a corpus |
| `websearch` | `maxResults` | Web search |
| `mcp` | `endpoint`, `call`, `arg`, `args`, `listingMode` | An existing MCP server |
| `openapi` | `source`, `call`, `arg`, `rowFields`, `rowPath` | A REST API. Spec at `app/tools/<source>/openapi.json` |
| `lambda` | `lambdaArn` **or** `source`, `call`, `arg`, `toolSchema`, `rowFields` | Anything else. See the warning below — for your own function you want `lambdaArn` |

`rowFields` maps the source's own field names onto the ROLES an agent reads, so pointing a
tool at a different backend is a config edit rather than a code change.

### `lambda`: `lambdaArn` for your function, `source` only for the shipped demo

**`source` is CLOSED for `type: "lambda"`.** It is enumerated by `builtinLambdaSource` in
`app/vocabulary.json` and permits only the one folder the repo ships, because a
framework-deployed function needs an execution role that config cannot express — the role
that exists grants logs plus read-only on the public AWS price list, which is right for
that function and wrong for a connector needing VPC config or a secret.

So for a customer's own Lambda tool:

```json
"partsPricing": {
  "type": "lambda",
  "description": "Unit prices for repair parts and labour rates.",
  "lambdaArn": "arn:aws:lambda:us-east-1:111122223333:function:claims-parts-pricing",
  "call": "price_parts",
  "arg": "items",
  "toolSchema": [ … ],
  "rowFields": { … }
}
```

The customer deploys and owns the function; the framework registers it as a Gateway target
and grants the Gateway permission to invoke it. It does not manage the code or the role.

**`source` for `type: "openapi"` is deliberately OPEN** — the framework only uploads a
file, which needs no permissions — so `app/tools/<source>/openapi.json` is the right home
for a customer's own spec.

### `toolSchema` is an ARRAY of tool descriptors

Not a JSON Schema object. One entry per tool the function publishes, and `required` sits on
each property rather than in a sibling list:

```json
"toolSchema": [
  {
    "name": "price_parts",
    "description": "Unit rates for the named parts or trades. Rates only, never a total.",
    "properties": {
      "items": {
        "type": "string",
        "required": true,
        "description": "Comma-separated part or trade names."
      }
    }
  }
]
```

### The Lambda handler contract

A function reached by `lambdaArn` is the customer's to write, but the Gateway calls it the
same way the shipped one is called:

```python
def price_parts(event: dict) -> dict:
    return {"results": [ … ], "unmatchedItems": [ … ], "note": "…"}

def _tool_name(context) -> str:
    """The Gateway passes the invoked tool's name on the client context; it arrives
    prefixed as <target>___<tool>, so split on the delimiter and take the last part."""

_TOOLS = {"price_parts": price_parts}

def lambda_handler(event, context):     # the entry point, not `handler`
    fn = _TOOLS.get(_tool_name(context) or next(iter(_TOOLS)))
    if fn is None:
        return {"error": "unknown tool", "availableTools": sorted(_TOOLS)}
    return fn(event or {})
```

Rows go under `results`, and `rowFields` maps their field names onto agent roles. Name every
shortfall — unmatched inputs, truncation — in its own field rather than dropping it, so the
calling agent can state what was *not* answered.

**Tool keys must match `^[A-Za-z][A-Za-z0-9]*$`** — the key builds both the Gateway target
name (no underscores allowed) and the Cedar policy name `permit_<key>` (no hyphens
allowed), so neither separator survives both. Agent ids are unconstrained this way and may
contain underscores.

## Agent entry

```json
"cost_research": {
  "name": "Cost Research",
  "runtime": "main",
  "produces": "research-finding",
  "maxTokens": 1500,
  "tool": "pricing",
  "agentcore": {
    "evaluations": {
      "enabled": true,
      "auto": false,
      "evaluators": ["Builtin.Faithfulness", "Builtin.ResponseRelevance"]
    },
    "policy": { "enabled": true }
  }
}
```

`runtime` is `main` (in-process), `dedicated` (its own AgentCore Runtime), or `a2a` (an
agent you do not operate — no folder is written).

## Step forms

Three shapes, and a gate follows the shape it is attached to:

```json
{ "agent": "intake", "hitl": true, "branch": { "when": [ … ], "default": "…" } }

{ "parallel": ["a", "b", "c"], "hitl": true,
  "gateId": "research", "gateName": "Research" }

{ "sequence": ["analysis", "recommendation"], "hitl": true,
  "gateId": "analysis_reco", "gateName": "Analysis & Recommendation" }
```

- A `parallel` gate takes **one decision per agent**, so a reviewer can accept four
  findings and send one back. It is also the only place a subset re-run is offered.
- A `sequence` gate takes **one decision for the chain**.
- `parallel` and `sequence` stages **MUST** have `gateId` and `gateName`.
- `branch.when` rules are tried in order, first match wins; operators are `equals`,
  `notEquals`, `in`, `contains`, `exists`, `gt`, `gte`, `lt`, `lte`. `goto` takes an agent
  id, a `gateId`, or `END`.

## The agent module

`scaffold.py agent <id>` writes all three files. `__init__.py` must keep its
`from .agent import agent` line, which is how the loader finds the instance.

```python
class MyAgent(Agent):
    system_prompt = SYSTEM_PROMPT

    async def run(self, ctx: AgentContext) -> str:
        ...
        return json.dumps(asset.model_dump(by_alias=True, mode="json"), indent=2)

agent = MyAgent()
```

## The `ctx` API — the only route to anything external

```
ctx.input(agent_id)                                  an upstream agent's output
await ctx.llm(system, user, model, max_tokens, name)  a model call
await ctx.call_tool(tool_key, query)                  a declared tool
await ctx.call_tool_rows(tool_key, query)             tabular result + rowFields map
await ctx.retrieve(query, doc_type)                   Knowledge Base retrieval
await ctx.memory_recall(query)                        long-term memory, scoped by subject
await ctx.memory_store(content)
await ctx.guardrail(text, source)                     explicit guardrail application
await ctx.policy_check(action)                        Cedar check (fail-open, secondary)
await ctx.get_identity_token(provider)
await ctx.log(msg)                                    a line on the run timeline
await ctx.heartbeat(pct)                              progress for the graph
```

Also on `ctx`: `ctx.topic`, `ctx.agent_id`, `ctx.tool`, `ctx.truncated_calls`.

Errors are deliberate and **MUST** propagate: `ModelUnavailable`, `ToolUnavailable`,
`ToolDenied`. A fabricated fallback value is indistinguishable from real evidence once it
reaches the report.

## `scaffold.py` CLI

```
python3 scaffold.py agent <agent_id>
    [--produces PRODUCES]            deliverable name, default <id>-output
    [--runtime {main,dedicated}]
    [--max-tokens MAX_TOKENS]
    [--tool TOOL]                    a key in the tools block
    [--remote --skill SKILL]         runtime "a2a"; writes no folder
    [--dry-run]                      print, write nothing
```

## `ui` keys

`title`, `heading`, `defaultTopic`, `topicPlaceholder`, `subjectPlaceholder`,
`subjectHint`, `assistantTitle`, `assistantSubtitle`.

`title` and `heading` drive the browser tab, top navigation, side navigation, breadcrumb
root and info-panel heading. A `ui` key nothing renders fails
`tests/test_config_keys.py`, because a presentation key that does nothing is worse than an
absent one.

## `authorization`

Gates the mutating actions: `start`, `decision`, `rerun`, `cancel`, `evaluate`,
`insights`, `delete`. `groupsClaim` names the JWT claim carrying groups — Cognito issues
`cognito:groups`; Auth0 needs a namespaced custom claim added by a post-login Action.

The UI hides what a caller cannot do; `bff/authz.py` refuses it again server-side. Hiding
a button is not the control.
