# Config-plane tests (CDK path)

```bash
cd orchestrator/cdk
npm install
npm test
```

No AWS credentials and no container builder. `Template.fromStack` renders the
template without staging the cloud assembly, which is the step that would bundle the
Docker image — verified by running the suite with `CDK_DOCKER=/nonexistent/builder`.
The whole thing is under ten seconds.

This is the TypeScript half of the config-plane suite. The Python half is
[`orchestrator/tests/`](../../tests/README.md), which covers the runtime; this covers
the IaC.

## The files

| File | Covers |
|---|---|
| `config-plane.test.ts` | the pure projections and validators: `validateTools`, `validateWorkflow`, `authzGroups`, `buildBffWorkflow`, `buildGuardrail`, `stripForEnv`, `cedarStatement` |
| `parity.test.ts` | Terraform ↔ CDK agreement: the BFF projection's key set, the API route list, the byte budget, and the constants that are necessarily duplicated across HCL / TypeScript / Python |
| `stack.test.ts` | the synthesized template: Cognito groups and self-signup, the route set and its authorizer, the BFF environment, the Gateway targets and the Cedar policies |

## Why parity.test.ts exists

The project promises both IaC paths deploy the same thing from the same
`workflow.json`, and nothing structural enforces that — they are two independent
implementations of the same projection. They have already drifted once:
`buildBffWorkflow` kept emitting `mcp`/`rag` after `workflow.json` renamed those keys
to `tool`/`corpus`, and omitted `evalAgents` and `chatbot` entirely. CDK deployments
silently lost the data-source chips, the Evaluate buttons and the in-app assistant
while Terraform deployments kept them. It was found by comparing two live
deployments, which is not a repeatable way to find that class of bug.

So `parity.test.ts` reads `terraform/*.tf` as text and compares the shapes. Regex
over HCL is crude; the mitigation is that every extraction asserts it found something
before comparing, so a regex that stops matching fails loudly instead of passing
vacuously. (That guard has already earned its place — the first version of the
per-agent key check silently extracted zero keys, because `agents` is a `for`
comprehension and the keys sit one brace deeper than expected.)

The duplicated-constant checks cover the lists that cannot be shared: the six RBAC
action names live in `bff/authz.py` (which enforces them), `terraform/identity.tf`
and `lib/orchestrator-stack.ts` (which both need them at plan/synth time to reject a
typo'd action key). Terraform cannot read the Python and TypeScript cannot read
either, so this test is the link between them.

## Verified by mutation, not just by being green

Each of these was reintroduced and the suite confirmed to fail, then reverted:

| Mutation | Tests that caught it |
|---|---|
| emit `mcp`/`rag` instead of `tool`/`corpus` | 2 |
| drop `evalAgents` + `chatbot` from the projection | 5 |
| drop `authorization` from the projection | 3 |
| remove `/api/me` from the CDK route list only | 3 |
| typo the RBAC action list in TypeScript only | 19 |
| change the 3000-byte budget on one path only | 1 |
| re-enable Cognito self-signup | 1 |
| stop creating the Cognito groups | 2 |
| permit web search at target level instead of by name | 2 |

The last one is the live `ToolDenied` bug: a target-level Cedar permit does not
authorize the managed web-search connector's tool, so it has to be permitted by name.
An MCP target is the opposite — its tool names are unknown at deploy time, so it must
stay target-level.

## Adding a test

Put projection and validator assertions in `config-plane.test.ts`, anything that
compares against the HCL in `parity.test.ts`, and anything that needs the rendered
template in `stack.test.ts`. `stack.test.ts` synthesizes once in `beforeAll` and
shares the `Template` — keep it that way, since synth is the only slow part.
