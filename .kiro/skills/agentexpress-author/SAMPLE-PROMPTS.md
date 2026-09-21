# Sample prompts

Open Kiro in this repository and paste one of these. The `agentexpress-author` skill
loads automatically because your request matches its description, then follows
[author-workflow.sop.md](author-workflow.sop.md): it confirms the topology with you,
writes the config, scaffolds the agents, writes the prompts, and runs the gates.

You can also just describe what you want in your own words — "build me a claims
settlement workflow with AgentExpress" — and it will ask for whatever it still needs in a
single prompt.

---

## 1. Rebuild this project's own workflow

Useful as a rehearsal: you can diff the result against the shipped
`orchestrator/app/workflow.json` and see exactly where it drifts.

```
Use the agentexpress-author skill to build this workflow.

use_case_description: Take a high-level AWS architecture request from a user,
research it from multiple sources in parallel, analyse the findings, produce a
recommendation, and assemble a final sectioned report. A human reviews the request
brief before research starts, reviews all research findings together, and reviews the
analysis and recommendation before the report is written.

domain_vocabulary: request-brief, research-finding, analysis, recommendation-package,
final-report

data_sources:
- internal reference docs as a Bedrock Knowledge Base corpus
- web search
- AWS documentation via a remote MCP server
- AWS pricing via the framework's built-in pricing Lambda
- product lifecycle dates via a REST API we have an OpenAPI spec for

review_points: a gate after intake, a gate on the parallel research group with a
decision per agent, and a gate on the analysis and recommendation stage

Notes: intake should branch — if the request has no clear objective, end the run; if it
has fewer than one key question, skip straight to the analysis stage. Analysis and
recommendation run as a sequence and are agents we do not operate, so use A2A.
```

---

## 2. Insurance claims settlement

A different domain, exercising a Knowledge Base corpus and a customer-owned Lambda.
This is the one that was used to test the skill.

```
Use the agentexpress-author skill to build this workflow.

use_case_description: Settle household insurance claims. Read the FNOL, check the
policy wording for coverage, price the repair, then produce a settlement decision a
human approves.

domain_vocabulary: fnol-summary, coverage-finding, repair-estimate, settlement-decision

data_sources:
- household policy wordings as a Bedrock Knowledge Base corpus
- parts and labour rates from a Lambda we deploy ourselves

review_points: a gate after intake, a gate on the parallel assessment group, and a gate
before the settlement is issued
```

Produces four agents — `intake`, then `coverage_check` and `repair_pricing` in parallel
behind an Assessment gate, then `settle`.

---

## 3. Minimal — reasoning only, no external data

Touches two surfaces: `workflow.json` and the agent folders. No tools, no KB.

```
Use the agentexpress-author skill to build this workflow.

use_case_description: Review an architecture proposal against our internal engineering
standards and produce a decision record a principal engineer signs off.

domain_vocabulary: proposal-summary, standards-finding, decision-record

review_points: one gate before the decision record is written
```

---

## 4. Add one agent to an existing workflow

You do not have to start over to change something.

```
Use the agentexpress-author skill to add an agent.

I want a new agent called `regulatory_check` in the parallel research stage. It should
produce a `compliance-finding` and read our regulatory guidance, which I will add as a
new Knowledge Base corpus called `regulations`. It must be covered by the existing
review gate on that stage.
```

---

## What it will ask you

If you leave anything out, it asks for all of it at once, using these names:

| Parameter | Required | What it wants |
|---|---|---|
| `use_case_description` | yes | What the workflow accomplishes, in prose |
| `domain_vocabulary` | yes | Your names for the things produced — these become `produces` values and asset types |
| `data_sources` | no | Each system the agents read, and how it is reached |
| `review_points` | no | Where a human must approve, revise or deny |
| `workspace_root` | no | Which checkout to modify |

## What it will NOT do

It stops and asks you to confirm the topology before writing any file, because every
later step derives from that shape.

It will not deploy. When the gates pass it tells you the next command —
`terraform apply` or `cdk deploy` — and stops there, because passing gates prove the
config is valid, not that it runs.
