"""
Gateway Lambda target: retrieves relevant chunks from a Bedrock Knowledge Base.

The AgentCore Gateway invokes this Lambda with the tool arguments as the `event`
and the tool name in `context.client_context.custom['bedrockAgentCoreToolName']`
(format: "<target>___<tool>"). We return a JSON object the agent can use.
"""

import contextlib
import os

import boto3

KB_ID = os.environ["KB_ID"]
REGION = os.environ.get("AWS_REGION", "us-east-1")
NUM_RESULTS = int(os.environ.get("KB_NUM_RESULTS", "5"))

_rt = boto3.client("bedrock-agent-runtime", region_name=REGION)


def lambda_handler(event, context):
    tool = ""
    with contextlib.suppress(Exception):
        tool = context.client_context.custom.get("bedrockAgentCoreToolName", "")

    query = (event or {}).get("query", "").strip()
    if not query:
        return {"error": "missing required 'query' argument", "tool": tool}

    # Optional per-agent corpus scoping: when a doc_type is supplied, restrict
    # the vector search to chunks tagged with that doc_type metadata attribute
    # so an agent only retrieves from its own corpus.
    vector_cfg = {"numberOfResults": NUM_RESULTS}
    doc_type = (event or {}).get("filter", "").strip()
    if doc_type:
        vector_cfg["filter"] = {"equals": {"key": "doc_type", "value": doc_type}}

    resp = _rt.retrieve(
        knowledgeBaseId=KB_ID,
        retrievalQuery={"text": query},
        retrievalConfiguration={"vectorSearchConfiguration": vector_cfg},
    )
    results = [
        {
            "text": r.get("content", {}).get("text", ""),
            "score": r.get("score"),
            "source": r.get("location", {}).get("s3Location", {}).get("uri"),
        }
        for r in resp.get("retrievalResults", [])
    ]
    return {"query": query, "count": len(results), "results": results}
