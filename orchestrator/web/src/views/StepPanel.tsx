/** What goes in the SplitPanel when a step is selected: Details and Output, the way
 *  Step Functions splits a selected state's panel.
 *
 *  Version history lives under Output rather than in a tab of its own, because a
 *  revision is a version OF the output — and a reviewer comparing v1 with v2 wants
 *  them adjacent. */

import Box from "@cloudscape-design/components/box";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Tabs from "@cloudscape-design/components/tabs";

import { AssetView } from "../assets/AssetView";
import { fmtET } from "../lib/clock";
import { statusIndicator } from "../lib/status";
import type { SessionSnapshot, Workflow } from "../types";

export function StepPanel({
  agentId, snap, workflow,
}: {
  agentId: string;
  snap: SessionSnapshot;
  workflow: Workflow;
}) {
  const meta = workflow.agents[agentId] ?? { name: agentId };
  const state = snap.nodes?.[agentId] ?? { status: "pending" as const };
  const history = snap.history?.[agentId] ?? [];

  const dataSource = meta.tool
    ? meta.tool + (meta.corpus ? ` · corpus ${meta.corpus}` : "")
    : meta.runtime === "a2a"
      ? `remote agent${meta.source ? ` · ${meta.source}` : ""}`
      : (meta.access ?? "—");

  return (
    <Tabs
      variant="container"
      tabs={[
        {
          id: "details",
          label: "Details",
          content: (
            <KeyValuePairs
              columns={2}
              items={[
                { label: "Status", value: statusIndicator(state.status) },
                { label: "Step ID", value: <Box variant="code" fontSize="body-s">{agentId}</Box> },
                { label: "Produces", value: meta.produces ?? "—" },
                { label: "Placement", value: meta.runtime ?? "main" },
                { label: "Data source", value: dataSource },
                { label: "Model", value: meta.model ?? "deployment default" },
                { label: "Output budget", value: meta.maxTokens ? `${meta.maxTokens} tokens` : "—" },
                { label: "Versions", value: String(history.length || 1) },
                ...(meta.skill ? [{ label: "Remote skill", value: meta.skill }] : []),
                ...(meta.access ? [{ label: "Access", value: meta.access }] : []),
              ]}
            />
          ),
        },
        {
          id: "output",
          label: "Output",
          content:
            history.length > 1
              ? (
                <SpaceBetween size="m">
                  {history.slice().reverse().map((h) => (
                    <ExpandableSection
                      key={h.version}
                      defaultExpanded={h.version === history.length}
                      headerText={`v${h.version} — ${h.version === 1 ? "initial" : "revision"}`}
                      headerDescription={fmtET(h.at)}
                    >
                      <SpaceBetween size="s">
                        {h.comment ? (
                          <Box variant="p">
                            <Box variant="awsui-key-label">Reviewer note</Box>
                            {h.comment}
                          </Box>
                        ) : null}
                        <AssetView text={h.output ?? ""} />
                      </SpaceBetween>
                    </ExpandableSection>
                  ))}
                </SpaceBetween>
              )
              : <AssetView text={state.output ?? ""} />,
        },
      ]}
    />
  );
}
