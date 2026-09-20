# Config-plane tests (CDK path)

```bash
cd orchestrator/cdk
npm install
npm test
```

168 tests across four files. No AWS credentials and no container builder:
`Template.fromStack` renders the template without staging the cloud assembly, which is the
step that would bundle the Docker image — verified by running the suite with
`CDK_DOCKER=/nonexistent/builder`.

This is the TypeScript half of the config-plane suite. The Python half is
[`orchestrator/tests/`](../../tests/README.md), which covers the runtime; this covers the
IaC.

## The files

| File | Tests | Covers |
|---|---|---|
| `config-plane.test.ts` | 76 | the pure projections and validators: `validateTools`, `validateRuntimes`, `validateWorkflow`, `validateBranches`, `authzGroups`, `buildGuardrail`, `toolsEnv`, `a2aTokenEnv`, `cedarStatement`, and the `openapi` / `lambda` tool shapes |
| `tool-plane.test.ts` | 38 | the tool plane's projections: one Gateway target per `tools` entry, the Knowledge Base storage naming (the digest that lets an immutable property be replaced rather than fail the deploy), and the generated Cedar statements |
| `stack.test.ts` | 35 | the synthesized template: Cognito groups and self-signup, the route set and its authorizer, the BFF environment, the Gateway targets and the Cedar policies |
| `parity.test.ts` | 19 | Terraform ↔ CDK agreement: the API route list, the framework vocabulary having one home, web-search domain filtering, the `kb` tool's retrieval settings, the IAM grants, and the shipped `workflow.json` being read the same way by both |

## Why parity.test.ts exists

The project promises both IaC paths deploy the same thing from the same `workflow.json`, and
nothing structural enforces that — they are two independent implementations of one
projection. They have drifted more than once, and the failure is silent: a CDK deployment
that lost the data-source chips, the Evaluate buttons and the in-app assistant while
Terraform kept them, and a Terraform deployment that granted only
`lambda:InvokeFunctionUrl` where CDK granted both invoke actions, so every A2A stage
returned 403. Both were found by comparing two live deployments, which is not a repeatable
way to find that class of bug.

So `parity.test.ts` reads `terraform/*.tf` as text and compares shapes. Regex over HCL is
crude; the mitigation is that every extraction asserts it found something before comparing,
so a regex that stops matching fails loudly instead of passing vacuously. Anchor on
declarations rather than bare names, too — prose elsewhere in a file can match first and
silently move the window.

The duplicated-constant checks cover lists that cannot be shared: the seven RBAC action
names live in `bff/authz.py` (which enforces them), `terraform/identity.tf` and
`lib/orchestrator-stack.ts` (which both need them at plan/synth time to reject a typo'd
action key). Terraform cannot read the Python and TypeScript cannot read either, so this
test is the link.

## Verified by mutation

Each of these was reintroduced and the suite confirmed to fail, then reverted:

| Mutation | Tests that caught it |
|---|---|
| emit `mcp`/`rag` instead of `tool`/`corpus` | 2 |
| drop `evalAgents` + `chatbot` from the projection | 5 |
| drop `authorization` from the projection | 3 |
| remove `/api/me` from the CDK route list only | 3 |
| typo the RBAC action list in TypeScript only | 19 |
| re-enable Cognito self-signup | 1 |
| stop creating the Cognito groups | 2 |
| permit web search at target level instead of by name | 2 |

The last is a live `ToolDenied`: a target-level Cedar permit does not authorize the managed
web-search connector's tool, so it must be permitted by name. An MCP target is the opposite
— its tool names are unknown at deploy time, so it must stay target-level.

## Adding a test

Projection and validator assertions go in `config-plane.test.ts`, anything comparing against
the HCL in `parity.test.ts`, anything needing the rendered template in `stack.test.ts`.
`stack.test.ts` synthesizes once in `beforeAll` and shares the `Template` — keep it that
way, since synth is the only slow part.
