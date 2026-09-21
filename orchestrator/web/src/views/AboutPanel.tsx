/** "What is this?" — the AppLayout tools drawer.
 *
 *  A console explains itself in a HelpPanel rather than in a wall of text on the page,
 *  so that is where this lives. Two halves, and the split is the point:
 *
 *    1. WHAT THE FRAMEWORK DOES. Fixed prose, because these capabilities are the
 *       framework's, not a deployment's — durable state, review gates, re-run from any
 *       step, grounding checks, RBAC, cost and token observability.
 *    2. WHAT THIS DEPLOYMENT DOES. Derived from the workflow that was actually loaded:
 *       the stage count, the agents, where each one runs, which stages are gated. Nobody
 *       has to keep it in sync with workflow.json, because it IS workflow.json — and a
 *       customer who swaps in their own workflow gets an accurate description for free.
 *
 *  Writing part 2 as prose would have made it a lie the first time someone edited a
 *  step. */

import Box from "@cloudscape-design/components/box";
import HelpPanel from "@cloudscape-design/components/help-panel";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import Link from "@cloudscape-design/components/link";
import SpaceBetween from "@cloudscape-design/components/space-between";
import { useMemo } from "react";

import { REPO, WORKFLOW_JSON, WORKFLOW_REFERENCE } from "../lib/links";
import type { Workflow } from "../types";

/** The capabilities a reader would otherwise have to discover by clicking. */
const FEATURES: { title: string; body: string }[] = [
  {
    title: "A workflow you declare, not code you write",
    body: "Stages, the agents in them, which run in parallel, and which pause for a "
      + "human all come from one file — workflow.json. Adding a step is an edit to that "
      + "file plus a prompt; the graph, the tables and this panel follow automatically.",
  },
  {
    title: "Human review gates",
    body: "A stage can pause for a person to approve, revise or deny. Revise sends your "
      + "note back to the agent as feedback and re-runs just that agent. On a parallel "
      + "stage you decide each agent separately, so four findings can be accepted while "
      + "one goes back.",
  },
  {
    title: "Durable state, so a pause can last",
    body: "A paused run is checkpointed, not held in memory. Close the tab, come back "
      + "tomorrow, and the run is still waiting at its gate with every output intact.",
  },
  {
    title: "Re-run from any step",
    body: "Select a step in the graph and re-run it together with everything downstream, "
      + "or restart the whole workflow from the graph toolbar. Each version of an output "
      + "is kept, so you can compare a revision against what it replaced.",
  },
  {
    title: "Grounded output, checked",
    body: "An agent that states a figure its sources never supplied is flagged and sent "
      + "back before a reviewer ever sees it. The check is skipped for agents bound to a "
      + "tool, whose figures come from the tool.",
  },
  {
    title: "Cost, tokens and latency per call",
    body: "Observability shows spend by agent, by model and by day, the exact prompt and "
      + "response for every call, and evaluation scores. Costs are estimates against a "
      + "published price book, not your bill.",
  },
  {
    title: "Permissions that are enforced twice",
    body: "Starting a run, deciding a gate, re-running, stopping and deleting can each "
      + "be restricted to an identity-provider group. The UI hides what you cannot do, "
      + "and the backend refuses it again — so hiding a button is not the control.",
  },
  {
    title: "An assistant that can act",
    body: "The bubble in the corner answers questions about a run and can take the same "
      + "actions the buttons take. It is held to the same permissions: it cannot approve "
      + "a gate you are not allowed to approve.",
  },
];

export function AboutPanel({ workflow, heading }: { workflow: Workflow; heading: string }) {
  const facts = useMemo(() => {
    const steps = workflow.steps ?? [];
    const agents = workflow.agents ?? {};
    const ids = steps.flatMap((s) => s.parallel ?? s.sequence ?? (s.agent ? [s.agent] : []));
    const gates = steps.filter((s) => s.hitl);
    const branches = steps.filter((s) => s.branch);
    const placements = new Map<string, number>();
    for (const id of ids) {
      const where = agents[id]?.runtime ?? "main";
      placements.set(where, (placements.get(where) ?? 0) + 1);
    }
    const tools = new Set(ids.map((id) => agents[id]?.tool).filter(Boolean) as string[]);
    return { steps, ids, gates, branches, placements, tools };
  }, [workflow]);

  const placementText = [...facts.placements.entries()]
    .map(([where, n]) => `${n} ${where}`)
    .join(", ") || "—";

  return (
    <HelpPanel header={<h2>{`About ${heading}`}</h2>}>
      <SpaceBetween size="l">
        <Box variant="p">
          This is a multi-agent orchestrator. You give it one request; it runs a sequence
          of AI agents over that request, pauses where a human has to sign off, and keeps
          every output, cost and decision along the way. What the agents are and how they
          are wired together is configuration, not code.
        </Box>

        <div>
          <Box variant="h3">This deployment</Box>
          <KeyValuePairs
            columns={1}
            items={[
              { label: "Stages", value: String(facts.steps.length) },
              { label: "Agents", value: String(facts.ids.length) },
              { label: "Placement", value: placementText },
              {
                label: "Review gates",
                // A gate on a single-agent step has no gateName, so it is named by the
                // agent it guards. The bare fallback printed the literal word "gate".
                value: facts.gates.length
                  ? facts.gates
                    .map((g) => g.gateName
                      ?? (g.agent ? (workflow.agents[g.agent]?.name ?? g.agent) : null)
                      ?? g.gateId
                      ?? "a stage")
                    .join(", ")
                  : "none",
              },
              {
                label: "Conditional stages",
                value: facts.branches.length ? String(facts.branches.length) : "none",
              },
              {
                label: "Data sources",
                value: facts.tools.size ? [...facts.tools].join(", ") : "none declared",
              },
            ]}
          />
        </div>

        <div>
          <Box variant="h3">Key features</Box>
          <SpaceBetween size="m">
            {FEATURES.map((f) => (
              <div key={f.title}>
                <Box variant="awsui-key-label">{f.title}</Box>
                <Box variant="p">{f.body}</Box>
              </div>
            ))}
          </SpaceBetween>
        </div>

        <div>
          <Box variant="h3">Getting started</Box>
          <Box variant="p">
            Choose <b>Start run</b> on the Runs table and describe what you want. Open the
            run to watch the graph fill in. When a stage turns amber it is waiting for
            you — read the outputs and approve, revise or deny. Use <b>Observability</b>
            {" "}for what each call cost and what was actually sent to the model.
          </Box>
        </div>

        <div>
          <Box variant="h3">Learn more</Box>
          <SpaceBetween size="xxs">
            <Link external href={REPO} externalIconAriaLabel="Opens in a new tab">
              This project on GitHub
            </Link>
            <Link external href={WORKFLOW_JSON} externalIconAriaLabel="Opens in a new tab">
              workflow.json — the file everything above is read from
            </Link>
            <Link external href={WORKFLOW_REFERENCE} externalIconAriaLabel="Opens in a new tab">
              workflow.json reference — every key, and what reads it
            </Link>
          </SpaceBetween>
        </div>
      </SpaceBetween>
    </HelpPanel>
  );
}
