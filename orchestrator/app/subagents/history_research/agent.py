"""Prior Run Research — this deployment's own run history (step 2, parallel).

The `type: "lambda"` demonstration. Bound by workflow.json to a tool whose target
is an AWS Lambda function, which is how an agent reaches anything the Gateway
cannot reach directly: a warehouse (Redshift, Snowflake), any RDBMS, an internal
service, or a resource inside a VPC. Here the function fronts the two DynamoDB
tables this deployment already writes, so the demo needs no external system while
still returning REAL rows.

Note what is NOT in this file: no Lambda ARN, no argument names, no tool name, no
IAM. All of it is the tool's entry in workflow.json — `source` (or `lambdaArn`),
`toolSchema`, `call` and `arg`. Swapping the function for one that queries your
warehouse is a config edit, and this file does not change.

Compare the three sibling research agents: same shape, same shared runner, four
different classes of data source. That symmetry is the point.
"""
from app.common import research
from app.common.base import Agent
from app.common.context import AgentContext

from .prompts import SYSTEM_PROMPT


class HistoryResearchAgent(Agent):
    system_prompt = SYSTEM_PROMPT

    async def run(self, ctx: AgentContext) -> str:
        return await research.synthesize(ctx, system_prompt=SYSTEM_PROMPT)


agent = HistoryResearchAgent()
