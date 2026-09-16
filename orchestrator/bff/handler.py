"""
BFF Lambda for the orchestrator UI.

Two modes in one function:
  * API mode (API Gateway HTTP API): serves the UI's REST calls, reads live
    progress from DynamoDB, and serves the workflow definition so the UI renders
    dynamically.
  * Runner mode (async self-invoke): calls the AgentCore runtime
    (InvokeAgentRuntime) for start/resume so the browser never blocks.

Every MUTATING endpoint below is additionally checked against bff/authz.py, which
maps a JWT group claim to the actions a caller may take (workflow.json ->
`authorization`). The API Gateway authorizer only proves WHO the caller is; authz
decides what they may DO. Actions not named in that block stay open to any
authenticated caller, so an absent block behaves as it did before.

Endpoints:
  GET    /api/workflow                           -> workflow definition (agents+steps)
  GET    /api/me                                 -> {user, groups, permittedActions}
  POST   /api/sessions               {topic}     -> {session_id}
  GET    /api/sessions                           -> [session summaries]
  GET    /api/sessions/{id}                      -> full snapshot (+ timeline)
  DELETE /api/sessions/{id}                      -> {ok} (remove a previous run)
  POST   /api/sessions/{id}/decision {decision, comment}  -> {ok}
        decision: approve | deny | revise; comment: feedback used on revise
  POST   /api/sessions/{id}/cancel               -> {ok}
  POST   /api/sessions/{id}/rerun    {agentId|agents, comment} -> {ok}
  POST   /api/sessions/{id}/evaluate {agentId, prompt} -> {ok}  (AgentCore Evaluations)
  POST   /api/insights/run           {lookbackHours}   -> {ok}  (cross-run Insights)
  GET    /api/insights                           -> latest Insights findings
  GET    /api/sessions/{id}/telemetry            -> per-run cost/token drilldown
  GET    /api/telemetry/aggregate                -> by date / model / user rollup
  POST   /api/chat                   {message, history} -> {reply, actions}
"""

import decimal
import json
import os
import uuid

import boto3
from boto3.dynamodb.conditions import Key

import authz
import chatbot
import clock

STATUS_TABLE = os.environ["STATUS_TABLE"]
EVENTS_TABLE = os.environ["EVENTS_TABLE"]
RUNTIME_ARN = os.environ["RUNTIME_ARN"]
REGION = os.environ.get("AWS_REGION", "us-east-1")
WORKFLOW = json.loads(os.environ.get("WORKFLOW_JSON", '{"agents":{},"steps":[]}'))
NODE_IDS = list(WORKFLOW.get("agents", {}).keys())
# The topic a run starts with when the caller sends none. From workflow.json
# (ui.defaultTopic) via the projection, never a literal — the sample's topic has
# nothing to do with a customer's use case.
DEFAULT_TOPIC = str((WORKFLOW.get("ui") or {}).get("defaultTopic") or "")

ddb = boto3.resource("dynamodb")
status_tbl = ddb.Table(STATUS_TABLE)
events_tbl = ddb.Table(EVENTS_TABLE)
TELEMETRY_TABLE = os.environ.get("TELEMETRY_TABLE", "")
telemetry_tbl = ddb.Table(TELEMETRY_TABLE) if TELEMETRY_TABLE else None
lambda_client = boto3.client("lambda")
agentcore = boto3.client("bedrock-agentcore", region_name=REGION)


class _DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, decimal.Decimal):
            return int(o) if o % 1 == 0 else float(o)
        return super().default(o)


def _resp(status: int, body) -> dict:
    return {"statusCode": status, "headers": {"content-type": "application/json"},
            "body": json.dumps(body, cls=_DecimalEncoder)}


def _skeleton(session_id: str, topic: str) -> dict:
    return {
        "session_id": session_id, "topic": topic, "overall": "running",
        "nodes": {n: {"status": "pending", "pct": 0, "output": ""} for n in NODE_IDS},
        "history": {}, "hitl": None, "result": None,
        "created": clock.now_str(), "updated_at": clock.now_str(),
    }


def _self_invoke(fn_name: str, payload: dict) -> None:
    lambda_client.invoke(FunctionName=fn_name, InvocationType="Event",
                         Payload=json.dumps(payload).encode())


def _run(event: dict) -> dict:
    action = event["action"]
    session_id = event["session_id"]
    payload = {"action": action, "session_id": session_id}
    payload["user"] = event.get("user", "")  # for observability attribution
    if action == "start":
        payload["topic"] = event.get("topic", "")
        payload["subject_id"] = event.get("subject_id", "")
    elif action == "resume":
        payload["decision"] = event.get("decision", "approve")
        payload["comment"] = event.get("comment", "")
        payload["decisions"] = event.get("decisions")  # per-agent map for group gates
    elif action == "rerun_from":
        # Rewind to a chosen agent and re-run it + everything downstream, with the
        # reviewer comment injected as that agent's feedback. `agents` (a list of
        # {agent_id, comment}) re-runs a SUBSET of one parallel stage.
        payload["agent_id"] = event.get("agent_id", "")
        payload["comment"] = event.get("comment", "")
        payload["agents"] = event.get("agents")
    elif action == "evaluate":
        # On-demand AgentCore Evaluation of one agent's run. An optional prompt
        # name scopes to one named model call; empty = all the agent's prompts.
        payload["agent_id"] = event.get("agent_id", "")
        payload["prompt"] = event.get("prompt", "")
    elif action == "run_insights":
        payload["lookback_hours"] = int(event.get("lookback_hours") or 168)
    # Control-plane actions (evaluate/insights) are stateless — they only
    # read/write DynamoDB + CloudWatch and emit logs. Route them to a UNIQUE
    # runtimeSessionId so they always run on a fresh container with the LATEST
    # image, never pinned to a run's warm container that may still be executing an
    # older build. (The workflow actions keep the session-derived id so they
    # resume the same run.)
    if action in ("evaluate", "run_insights"):
        runtime_session = f"{action[:6]}{uuid.uuid4().hex}".ljust(33, "0")[:33]
    else:
        runtime_session = session_id.ljust(33, "0")[:33]
    try:
        agentcore.invoke_agent_runtime(
            agentRuntimeArn=RUNTIME_ARN,
            runtimeSessionId=runtime_session,
            payload=json.dumps(payload).encode())
    except Exception as e:  # noqa: BLE001
        status_tbl.update_item(
            Key={"session_id": session_id},
            UpdateExpression="SET overall = :o, updated_at = :u",
            ExpressionAttributeValues={":o": "failed", ":u": clock.now_str()})
        print(f"[runner] invoke failed: {type(e).__name__}: {e}")
    return {"ok": True}


def _user(event: dict) -> str:
    """Authenticated user from the JWT claims (empty when idp = "none").

    Provider-agnostic: API Gateway puts the validated claims in the same place
    for any JWT authorizer. `email` and `sub` are standard OIDC and cover both
    Cognito and Auth0; the last two fallbacks are the provider-specific ones
    (`name` on Auth0, `cognito:username` on Cognito).
    """
    claims = (event.get("requestContext", {}).get("authorizer", {})
              .get("jwt", {}).get("claims", {}))
    return (claims.get("email") or claims.get("sub")
            or claims.get("name") or claims.get("cognito:username") or "")


def _forbidden(action: str, event: dict) -> dict | None:
    """A 403 response when the caller may not perform `action`, else None.

    Returned rather than raised so each route reads as a single guard line, and
    logged because a denial is nearly always a group-mapping mistake rather than an
    attack — without the log you only see a 403 in the browser.
    """
    if authz.permitted(action, event):
        return None
    body = authz.denial(action, event)
    print(f"[authz] DENY {action} user={_user(event)!r} groups={body['yourGroups']} "
          f"required={body['requiredGroups']} claim={body['groupsClaim']}")
    return _resp(403, body)


def _delete_session(sid: str) -> dict:
    """Delete a previously-run pipeline: its event timeline rows, then its status
    row. (Telemetry rows in the separate cost table are left as historical data.)"""
    if not sid:
        return _resp(400, {"error": "missing session id"})
    try:
        for e in _query_all(events_tbl,
                            KeyConditionExpression=Key("session_id").eq(sid)):
            events_tbl.delete_item(Key={"session_id": sid, "ts": e["ts"]})
    except Exception as e:  # noqa: BLE001 - never let timeline cleanup block the delete
        print(f"[delete] events cleanup failed: {type(e).__name__}: {e}")
    status_tbl.delete_item(Key={"session_id": sid})
    return _resp(200, {"ok": True})


def _insights_latest() -> dict:
    """Read the latest cross-run Insights findings from the runtime (synchronous;
    the runtime holds the batch-evaluation state)."""
    try:
        resp = agentcore.invoke_agent_runtime(
            agentRuntimeArn=RUNTIME_ARN,
            runtimeSessionId="insights-latest".ljust(33, "0")[:33],
            payload=json.dumps({"action": "get_insights"}).encode())
        stream = resp.get("response")
        raw = stream.read() if hasattr(stream, "read") else stream
        data = json.loads(raw)
        # Tolerate a runtime body that is a JSON-encoded string wrapping the JSON.
        if isinstance(data, str):
            data = json.loads(data)
    except Exception as e:  # noqa: BLE001
        print(f"[insights] {type(e).__name__}: {e}")
        return _resp(502, {"error": "insights read failed"})
    return _resp(200, data if isinstance(data, dict) else {"status": "none"})


def _session_user(sid: str) -> str:
    """The user a run was started by, for cost attribution on follow-up actions."""
    sess = status_tbl.get_item(Key={"session_id": sid},
                               ProjectionExpression="#u",
                               ExpressionAttributeNames={"#u": "user"}).get("Item") or {}
    return sess.get("user", "")


# --- Observability reads ---------------------------------------------------
# Cost is computed and stored per row by the runtime
# (app/features/observability), so the BFF only SUMS pre-computed numbers here —
# no pricing logic lives in the BFF.

def _query_all(table, **kwargs) -> list:
    out = []
    while True:
        page = table.query(**kwargs)
        out.extend(page.get("Items", []))
        lek = page.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek
    return out


def _telemetry_session(sid: str) -> dict:
    """Per-run detail: every LLM/tool/compute row for the session, plus a
    per-agent rollup and session totals."""
    if telemetry_tbl is None or not sid:
        return _resp(200, {"session_id": sid, "calls": [], "byAgent": [], "totals": {}})
    items = _query_all(telemetry_tbl, KeyConditionExpression=Key("session_id").eq(sid))
    agents: dict = {}
    tot = {"costUsd": decimal.Decimal(0), "inputTokens": 0, "outputTokens": 0,
           "calls": 0, "latencyMs": 0}
    for it in items:
        aid = it.get("agent_id", "?")
        a = agents.setdefault(aid, {"agentId": aid, "costUsd": decimal.Decimal(0),
                                    "inputTokens": 0, "outputTokens": 0, "latencyMs": 0,
                                    "calls": 0, "tools": 0, "models": set()})
        c = it.get("cost_usd", 0) or 0
        a["costUsd"] += c
        a["inputTokens"] += int(it.get("input_tokens", 0) or 0)
        a["outputTokens"] += int(it.get("output_tokens", 0) or 0)
        a["latencyMs"] += int(it.get("latency_ms", 0) or 0)
        a["calls"] += 1
        if it.get("kind") == "tool":
            a["tools"] += 1
        if it.get("kind") == "llm" and it.get("label"):
            a["models"].add(it["label"])
        tot["costUsd"] += c
        tot["inputTokens"] += int(it.get("input_tokens", 0) or 0)
        tot["outputTokens"] += int(it.get("output_tokens", 0) or 0)
        tot["latencyMs"] += int(it.get("latency_ms", 0) or 0)
        tot["calls"] += 1
    by_agent = []
    for a in agents.values():
        a["models"] = sorted(a["models"])
        by_agent.append(a)
    return _resp(200, {"session_id": sid, "calls": items, "byAgent": by_agent, "totals": tot})


def _telemetry_aggregate(by: str, frm: str | None, to: str | None) -> dict:
    """Roll up by date / model / user over a date range (defaults to last 30 days).
    Uses the by_date GSI, one query per day in range, grouped in-memory."""
    if telemetry_tbl is None:
        return _resp(200, {"by": by, "buckets": []})
    import datetime as _dt
    today = _dt.date.fromisoformat(clock.today_str())  # Eastern "today"
    to = to or today.isoformat()
    frm = frm or (today - _dt.timedelta(days=30)).isoformat()
    try:
        d, d1 = _dt.date.fromisoformat(frm), _dt.date.fromisoformat(to)
    except ValueError:
        return _resp(400, {"error": "from/to must be YYYY-MM-DD"})
    buckets: dict = {}
    bucket_sessions: dict = {}   # key -> set(session_id), for distinct run counts
    all_sessions: set = set()
    while d <= d1:
        for it in _query_all(telemetry_tbl, IndexName="by_date",
                             KeyConditionExpression=Key("date").eq(d.isoformat())):
            if by == "model":
                key = it.get("label") if it.get("kind") == "llm" else "(tool/agentcore)"
            elif by == "user":
                key = it.get("user") or "(unattributed)"
            else:
                key = it.get("date")
            b = buckets.setdefault(key, {"key": key, "costUsd": decimal.Decimal(0),
                                         "inputTokens": 0, "outputTokens": 0,
                                         "calls": 0, "latencyMs": 0, "sessions": 0,
                                         "embedTokensEst": 0,
                                         "inRate": decimal.Decimal(0),
                                         "outRate": decimal.Decimal(0)})
            b["costUsd"] += it.get("cost_usd", 0) or 0
            b["inputTokens"] += int(it.get("input_tokens", 0) or 0)
            b["outputTokens"] += int(it.get("output_tokens", 0) or 0)
            b["embedTokensEst"] += int(it.get("embed_tokens_est", 0) or 0)
            b["latencyMs"] += int(it.get("latency_ms", 0) or 0)
            b["calls"] += 1
            sid = it.get("session_id")
            if sid:
                bucket_sessions.setdefault(key, set()).add(sid)
                all_sessions.add(sid)
            # Representative per-1M rates for the by-model view (llm rows only).
            if by == "model" and it.get("kind") == "llm":
                if it.get("in_rate"):
                    b["inRate"] = it["in_rate"]
                if it.get("out_rate"):
                    b["outRate"] = it["out_rate"]
        d += _dt.timedelta(days=1)
    for key, b in buckets.items():
        b["sessions"] = len(bucket_sessions.get(key, ()))
    return _resp(200, {"by": by, "from": frm, "to": to,
                       "totalSessions": len(all_sessions),
                       "buckets": sorted(buckets.values(), key=lambda x: str(x["key"]))})


def _api(event: dict, context) -> dict:
    method = event["requestContext"]["http"]["method"]
    path = event.get("rawPath", "")
    params = event.get("pathParameters") or {}
    route_key = event.get("routeKey", "")
    body = {}
    if event.get("body"):
        try:
            body = json.loads(event["body"])
        except Exception:
            body = {}

    if method == "GET" and path == "/api/workflow":
        return _resp(200, WORKFLOW)

    if method == "GET" and path == "/api/me":
        # Who am I and what may I do — the UI disables the controls it isn't
        # allowed to use instead of offering a button that 403s. Advisory only:
        # every action is still checked server-side on its own route.
        return _resp(200, {"user": _user(event), "groups": authz.groups_of(event),
                           "permittedActions": authz.permitted_actions(event),
                           "authzEnabled": authz.ENABLED})

    if method == "POST" and path == "/api/sessions":
        topic = body.get("topic") or DEFAULT_TOPIC
        # Optional grouping key; scopes long-term memory (insights/{agentId}-{subject}).
        subject_id = body.get("subjectId", "")
        session_id = uuid.uuid4().hex[:12]
        item = _skeleton(session_id, topic)
        item["user"] = _user(event)
        if subject_id:
            item["subject_id"] = subject_id  # so the UI can show it after a refresh
        try:
            status_tbl.put_item(Item=item,
                                ConditionExpression="attribute_not_exists(session_id)")
        except Exception:
            pass
        _self_invoke(context.function_name,
                     {"action": "start", "session_id": session_id, "topic": topic,
                      "subject_id": subject_id, "user": item["user"]})
        return _resp(200, {"session_id": session_id})

    if method == "GET" and path == "/api/sessions":
        # Paginate: a Scan returns at most 1MB of data READ per page (before the
        # ProjectionExpression trims it). Status items are large (they embed every
        # node's output + history + result), so even a few sessions exceed one
        # page — without following LastEvaluatedKey, sessions silently disappear.
        items = []
        scan_kwargs = {"ProjectionExpression": "session_id, topic, overall, hitl, created"}
        while True:
            page = status_tbl.scan(**scan_kwargs)
            items.extend(page.get("Items", []))
            last = page.get("LastEvaluatedKey")
            if not last:
                break
            scan_kwargs["ExclusiveStartKey"] = last
        # `created` is an ET string ("YYYY-MM-DD HH:MM:SS") for new sessions and a
        # legacy epoch-ms int for old ones. Coerce to str so the sort never mixes
        # types; both forms still order newest-first (ET strings sort above ints).
        items.sort(key=lambda x: str(x.get("created", "")), reverse=True)
        return _resp(200, items)

    if method == "GET" and params.get("id") and path.endswith(params["id"]):
        sid = params["id"]
        item = status_tbl.get_item(Key={"session_id": sid}).get("Item")
        if not item:
            return _resp(404, {"error": "unknown session"})
        evs = events_tbl.query(
            KeyConditionExpression=boto3.dynamodb.conditions.Key("session_id").eq(sid),
            Limit=40, ScanIndexForward=False).get("Items", [])
        evs.reverse()
        item["logs"] = [{"ts": e["ts"], "node": e.get("node"), "msg": e.get("msg", "")} for e in evs]
        return _resp(200, item)

    if method == "DELETE" and params.get("id") and path.endswith(params["id"]):
        denied = _forbidden("delete", event)
        if denied:
            return denied
        return _delete_session(params["id"])

    if method == "POST" and params.get("id") and path.endswith("/cancel"):
        denied = _forbidden("cancel", event)
        if denied:
            return denied
        sid = params["id"]
        item = status_tbl.get_item(Key={"session_id": sid}).get("Item")
        if not item:
            return _resp(404, {"error": "unknown session"})
        if item.get("overall") in ("done", "denied", "failed", "cancelled"):
            return _resp(200, {"ok": True, "overall": item.get("overall")})
        # Set the durable flag (the running workflow polls it) and reflect the
        # stop immediately in the UI by clearing any pending human gate.
        status_tbl.update_item(
            Key={"session_id": sid},
            UpdateExpression="SET cancel_requested = :c, overall = :o, hitl = :z, updated_at = :u",
            ExpressionAttributeValues={":c": True, ":o": "cancelled", ":z": None, ":u": clock.now_str()})
        return _resp(200, {"ok": True, "overall": "cancelled"})

    if method == "POST" and params.get("id") and path.endswith("/decision"):
        denied = _forbidden("decision", event)
        if denied:
            return denied
        sid = params["id"]
        decision = body.get("decision")
        # Per-agent decisions for a parallel-group gate; when present, the single
        # decision need not be one of the three.
        decisions = body.get("decisions")
        if not decisions and decision not in ("approve", "deny", "revise"):
            return _resp(400, {"error": "decision must be 'approve', 'deny' or 'revise', "
                                        "or provide a per-agent 'decisions' map"})
        _self_invoke(context.function_name,
                     {"action": "resume", "session_id": sid, "decision": decision or "approve",
                      "comment": body.get("comment", ""), "decisions": decisions,
                      "user": _session_user(sid)})
        return _resp(200, {"ok": True})

    if method == "POST" and params.get("id") and path.endswith("/rerun"):
        # Rewind and re-run downstream. Two shapes:
        #   {"agentId": "x", "comment": "..."}                       single agent
        #   {"agents": [{"agentId": "x", "comment": "..."}, ...]}    subset of a
        #     parallel stage (re-runs those agents in parallel, then re-reviews).
        denied = _forbidden("rerun", event)
        if denied:
            return denied
        sid = params["id"]
        agents = body.get("agents")
        agent_id = body.get("agentId") or body.get("agent_id")
        if not agents and not agent_id:
            return _resp(400, {"error": "provide 'agentId' or a non-empty 'agents' list"})
        _self_invoke(context.function_name,
                     {"action": "rerun_from", "session_id": sid, "agent_id": agent_id,
                      "comment": body.get("comment", ""), "agents": agents,
                      "user": _session_user(sid)})
        return _resp(200, {"ok": True})

    if method == "POST" and params.get("id") and path.endswith("/evaluate"):
        # On-demand AgentCore Evaluation: score one agent's run now.
        denied = _forbidden("evaluate", event)
        if denied:
            return denied
        sid = params["id"]
        agent_id = body.get("agentId") or body.get("agent_id")
        if not agent_id:
            return _resp(400, {"error": "agentId is required"})
        _self_invoke(context.function_name,
                     {"action": "evaluate", "session_id": sid, "agent_id": agent_id,
                      "prompt": body.get("prompt", ""), "user": _session_user(sid)})
        return _resp(200, {"ok": True})

    # AgentCore Insights: run a cross-run batch analysis / read the latest
    # findings. Not tied to a session (spans every run in the lookback window).
    if method == "POST" and path == "/api/insights/run":
        denied = _forbidden("insights", event)
        if denied:
            return denied
        _self_invoke(context.function_name,
                     {"action": "run_insights", "session_id": "insights",
                      "lookback_hours": int(body.get("lookbackHours") or 168),
                      "user": _user(event)})
        return _resp(200, {"ok": True})
    if method == "GET" and path == "/api/insights":
        return _insights_latest()

    if method == "POST" and path == "/api/chat":
        # In-app assistant: a Bedrock tool-use loop that answers questions about
        # runs and triggers the same runtime actions the UI buttons use.
        if not chatbot.is_enabled():
            return _resp(403, {"error": "assistant disabled"})
        # The assistant can approve gates, re-run agents and start evaluations, so
        # it is an alternative path to the guarded routes above. Pass the caller's
        # permitted actions in and let it drop the tools they may not use —
        # otherwise RBAC would be enforced on the buttons and bypassable by asking.
        result = chatbot.handle_chat(
            body, body.get("session_id", ""),
            lambda payload: _self_invoke(context.function_name, payload),
            authz.permitted_actions(event))
        return _resp(400 if result.get("error") else 200, result)

    # --- Observability (telemetry) read endpoints -------------------------
    if route_key == "GET /api/sessions/{id}/telemetry":
        return _telemetry_session(params.get("id", ""))
    if route_key == "GET /api/telemetry/aggregate":
        qs = event.get("queryStringParameters") or {}
        return _telemetry_aggregate(qs.get("by", "date"), qs.get("from"), qs.get("to"))

    return _resp(404, {"error": f"no route for {method} {path}"})


def handler(event, context):
    if "action" in event and "requestContext" not in event:
        return _run(event)
    return _api(event, context)
