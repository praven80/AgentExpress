"""
Gateway Lambda target: the framework's own run history, as a tool.

This is the BUILT-IN demo function for `type: "lambda"` in workflow.json — the
tool type that lets an agent reach anything the Gateway cannot reach directly (a
warehouse, an RDBMS, an internal service, something inside a VPC). Every other
tool type ships with a live agent proving it works; this exists so that one does
too, without asking you to stand up a database first.

It queries the two DynamoDB tables this deployment already writes:

  prior_runs — past workflow runs whose topic matches a phrase, from the status
               table. Institutional memory: what has this system already been
               asked, and how did it end up?
  run_costs  — the per-agent cost / latency / token breakdown for one run, from
               the telemetry table.

The rows are REAL. Nothing here is generated, sampled or stubbed: if there are no
prior runs, the tool says so and the calling agent records that as a named data
limitation rather than inventing history. That is the same contract every other
tool in this framework is held to (see app/common/errors.py).

Replacing this with your own function is the point. Change the `tools` entry to
`"lambdaArn": "<your function>"` and the framework stops deploying anything —
it registers yours and grants the Gateway permission to invoke it. Nothing here
is on the path of a customer's own tool.

Invocation contract (identical to kb_lambda/handler.py): the Gateway passes the
tool arguments as `event`, and the tool name as
`context.client_context.custom["bedrockAgentCoreToolName"]`, formatted
"<targetName>___<toolName>". Dispatching on that is what lets ONE function publish
several tools.
"""

import decimal
import os

import boto3
from boto3.dynamodb.conditions import Key

REGION = os.environ.get("AWS_REGION", "us-east-1")
STATUS_TABLE = os.environ["STATUS_TABLE"]
TELEMETRY_TABLE = os.environ.get("TELEMETRY_TABLE", "")

# Hard ceilings, so a large deployment cannot return a payload that blows the
# agent's context window or the Gateway's response limit.
MAX_RUNS = 25
MAX_OUTPUT_CHARS = 400

_ddb = boto3.resource("dynamodb", region_name=REGION)
_status = _ddb.Table(STATUS_TABLE)
_telemetry = _ddb.Table(TELEMETRY_TABLE) if TELEMETRY_TABLE else None


def _num(x):
    """DynamoDB Decimal -> int/float, so the result is JSON-serialisable."""
    if isinstance(x, decimal.Decimal):
        return int(x) if x == x.to_integral_value() else float(x)
    return x


def _tool_name(context) -> str:
    """The tool the Gateway is asking for, minus the "<target>___" prefix.

    Note the name may be doubly prefixed when the underlying server prefixes its
    own tools; splitting on the LAST "___" is what makes that safe.
    """
    try:
        full = context.client_context.custom.get("bedrockAgentCoreToolName", "")
    except Exception:  # noqa: BLE001 - a direct test invoke has no client_context
        return ""
    return full.rsplit("___", 1)[-1] if full else ""


def _scan_all(table, **kwargs) -> list:
    """Every page of a Scan.

    A Scan returns at most 1 MB of data READ per page, before the projection
    trims it. Status items are large (they embed every agent's output), so even a
    handful of runs exceeds one page — without following LastEvaluatedKey, runs
    silently disappear from the result, which for a history tool means quietly
    answering "no prior runs" when there are plenty.
    """
    out = []
    while True:
        page = table.scan(**kwargs)
        out.extend(page.get("Items", []))
        last = page.get("LastEvaluatedKey")
        if not last:
            return out
        kwargs["ExclusiveStartKey"] = last


# A run still in flight has no outcome to learn from, and — the reason this filter
# exists — the CALLING run is itself always in flight. Without it, the agent was
# handed its own session as a "prior run": it reported
#   "Run 7e7380d13b07 is currently running ... started 6 seconds before this
#    request brief was created"
# and the pipeline then produced "Coordinate with concurrent run 7e7380d13b07 to
# avoid duplicate effort" as a high-priority recommendation in the final report.
# The system was advising the reader to coordinate with itself.
#
# `waiting_human` is deliberately NOT excluded: a paused run is a real, useful
# state ("someone is mid-review on this topic"), and it cannot be the caller,
# because the caller is executing.
IN_FLIGHT = ("running", "cancelling")


def prior_runs(args: dict) -> dict:
    """Past runs whose topic matches `topic`, newest first."""
    topic = str(args.get("topic") or "").strip().lower()
    try:
        limit = max(1, min(int(args.get("limit") or 5), MAX_RUNS))
    except (TypeError, ValueError):
        limit = 5

    scanned = _scan_all(
        _status,
        ProjectionExpression="session_id, topic, overall, created, subject_id",
    )
    items = [it for it in scanned if str(it.get("overall", "")) not in IN_FLIGHT]
    in_flight = len(scanned) - len(items)
    # Substring match rather than a DynamoDB filter: the corpus is small, and
    # doing it here keeps the tool's behaviour identical whatever the table holds.
    if topic:
        words = [w for w in topic.split() if len(w) > 3]
        matched = [
            it for it in items
            if topic in str(it.get("topic", "")).lower()
            or any(w in str(it.get("topic", "")).lower() for w in words)
        ]
    else:
        matched = items
    matched.sort(key=lambda x: str(x.get("created", "")), reverse=True)

    results = [
        {
            "text": (
                f"Run {it.get('session_id')} \u2014 topic: {it.get('topic', '(none)')} "
                f"\u2014 outcome: {it.get('overall', 'unknown')} "
                f"\u2014 started: {it.get('created', 'unknown')}"
                + (f" \u2014 subject: {it['subject_id']}" if it.get("subject_id") else "")
            )[:MAX_OUTPUT_CHARS],
            "title": f"Prior run {it.get('session_id')}",
            "sessionId": it.get("session_id"),
            "outcome": it.get("overall"),
        }
        for it in matched[:limit]
    ]
    return {
        "query": topic,
        "count": len(results),
        # An empty result set is REAL information, not a failure. The research
        # runner turns it into a named data limitation so the model does not fill
        # the gap with something plausible.
        "completedRunsInSystem": len(items),
        # Reported rather than hidden, so the agent can see that runs were excluded
        # and why — one of them is always the run doing the asking.
        "inFlightRunsExcluded": in_flight,
        "results": results,
    }


def run_costs(args: dict) -> dict:
    """Per-agent cost / latency / tokens for one run, from the telemetry table."""
    sid = str(args.get("session_id") or "").strip()
    if not sid:
        return {"error": "missing required 'session_id' argument"}
    if _telemetry is None:
        return {"error": "this deployment has no telemetry table, so per-run cost is unavailable"}

    rows = []
    kwargs = {"KeyConditionExpression": Key("session_id").eq(sid)}
    while True:
        page = _telemetry.query(**kwargs)
        rows.extend(page.get("Items", []))
        last = page.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last

    by_agent: dict = {}
    for r in rows:
        a = by_agent.setdefault(
            r.get("agent_id", "?"),
            {"agentId": r.get("agent_id", "?"), "costUsd": decimal.Decimal(0),
             "inputTokens": 0, "outputTokens": 0, "latencyMs": 0, "calls": 0},
        )
        a["costUsd"] += r.get("cost_usd", 0) or 0
        a["inputTokens"] += int(r.get("input_tokens", 0) or 0)
        a["outputTokens"] += int(r.get("output_tokens", 0) or 0)
        a["latencyMs"] += int(r.get("latency_ms", 0) or 0)
        a["calls"] += 1

    results = [
        {
            "text": (
                f"{a['agentId']}: ${_num(a['costUsd']):.6f} over {a['calls']} call(s), "
                f"{a['inputTokens']}+{a['outputTokens']} tokens, {a['latencyMs']} ms"
            )[:MAX_OUTPUT_CHARS],
            "title": f"{a['agentId']} cost",
            **{k: _num(v) for k, v in a.items() if k != "agentId"},
        }
        for a in sorted(by_agent.values(), key=lambda x: -x["costUsd"])
    ]
    return {"query": sid, "count": len(results), "results": results}


# Tool name -> implementation. The Gateway publishes both; `call` in
# workflow.json says which one the bound agent invokes.
_TOOLS = {"prior_runs": prior_runs, "run_costs": run_costs}


def lambda_handler(event, context):
    tool = _tool_name(context)
    fn = _TOOLS.get(tool)
    if fn is None:
        # Named explicitly rather than defaulting to one of them: silently running
        # the wrong tool would return real-looking data for a question nobody asked.
        return {
            "error": f"unknown tool {tool!r}",
            "availableTools": sorted(_TOOLS),
        }
    try:
        return fn(event or {})
    except Exception as e:  # noqa: BLE001 - surface the reason, never a fake answer
        print(f"[tool_lambda] {tool} failed: {type(e).__name__}: {e}")
        return {"error": f"{tool} failed: {type(e).__name__}: {e}"}
