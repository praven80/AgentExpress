/** A run, as a console resource-detail page: breadcrumbs, a page header with the
 *  resource's actions, a KeyValuePairs summary, then Tabs.
 *
 *  The tab set mirrors a Step Functions execution: the graph, the same information as
 *  a table, the event timeline, and the final output. */

import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import Header from "@cloudscape-design/components/header";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Table from "@cloudscape-design/components/table";
import Tabs from "@cloudscape-design/components/tabs";
import { useMemo } from "react";

import { AssetView } from "../assets/AssetView";
import { duration, fmtET } from "../lib/clock";
import { statusIndicator } from "../lib/status";
import type { Action, SessionSnapshot, Workflow } from "../types";
import { Graph } from "./Graph";
import { RerunPanel } from "./RerunPanel";

export function RunDetail({
  snap, workflow, selected, onSelect, can, onCancel, activeTab, onTabChange, onRerun,
}: {
  snap: SessionSnapshot;
  workflow: Workflow;
  selected: string | null;
  onSelect: (id: string | null) => void;
  can: (a: Action) => boolean;
  onCancel: () => void;
  activeTab: string;
  onTabChange: (id: string) => void;
  onRerun: (agents: string[], comment: string) => Promise<void>;
}) {
  const nodes = snap.nodes ?? {};
  const overall = String(snap.overall ?? "");
  const running = ["running", "waiting_human", "cancelling"].includes(overall);
  const ids = useMemo(
    () => (workflow.steps ?? []).flatMap((s) => s.parallel ?? s.sequence ?? [s.agent!]),
    [workflow]);
  const doneCount = ids.filter((i) => nodes[i]?.status === "done").length;

  const stageOf = (id: string): string => {
    const i = (workflow.steps ?? []).findIndex(
      (s) => (s.parallel ?? s.sequence ?? [s.agent]).includes(id));
    return i < 0 ? "—" : String(i + 1);
  };

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description={`Run ${snap.session_id}`}
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            {running ? (
              <Button iconName="close" disabled={!can("cancel")} onClick={onCancel}>
                Stop run
              </Button>
            ) : null}
          </SpaceBetween>
        }
      >
        {snap.topic || "(no topic)"}
      </Header>

      <Container header={<Header variant="h2">Details</Header>}>
        <KeyValuePairs
          columns={4}
          items={[
            { label: "Status", value: statusIndicator(overall) },
            { label: "Progress", value: `${doneCount} of ${ids.length} steps complete` },
            { label: "Started (ET)", value: fmtET(snap.created) || "—" },
            { label: "Duration", value: duration(snap.created, snap.updated_at) },
            { label: "Run ID", value: <Box variant="code" fontSize="body-s">{snap.session_id}</Box> },
            { label: "Started by", value: snap.user || "—" },
            { label: "Subject", value: snap.subject_id || "—" },
            {
              label: "Awaiting",
              value: snap.hitl?.node
                ? (workflow.agents[snap.hitl.node]?.name ?? snap.hitl.node)
                : "—",
            },
          ]}
        />
      </Container>

      <Tabs
        activeTabId={activeTab}
        onChange={({ detail }) => onTabChange(detail.activeTabId)}
        tabs={[
          {
            id: "graph",
            label: "Graph view",
            content: (
              <Container
                header={
                  <Header
                    variant="h2"
                    description="Select a step to see its configuration and output in the panel."
                  >
                    Workflow
                  </Header>
                }
                disableContentPaddings
              >
                <Graph workflow={workflow} nodes={nodes} selected={selected} onSelect={onSelect} />
              </Container>
            ),
          },
          {
            id: "table",
            label: "Table view",
            content: (
              <Table
                variant="container"
                items={ids.map((id) => ({ id, ...(nodes[id] ?? { status: "pending" }) }))}
                trackBy="id"
                onRowClick={({ detail }) => onSelect(detail.item.id)}
                selectedItems={selected ? ids.filter((i) => i === selected).map((id) => ({
                  id, ...(nodes[id] ?? { status: "pending" as const }),
                })) : []}
                header={<Header variant="h2" counter={`(${ids.length})`}>Steps</Header>}
                columnDefinitions={[
                  {
                    id: "stage", header: "Stage", width: 90,
                    cell: (r) => stageOf(r.id),
                  },
                  {
                    id: "name", header: "Step", isRowHeader: true, minWidth: 220,
                    cell: (r) => workflow.agents[r.id]?.name ?? r.id,
                  },
                  {
                    id: "status", header: "Status", width: 170,
                    cell: (r) => statusIndicator(r.status),
                  },
                  {
                    id: "placement", header: "Placement", width: 130,
                    cell: (r) => workflow.agents[r.id]?.runtime ?? "main",
                  },
                  {
                    id: "tool", header: "Data source", width: 180,
                    cell: (r) => workflow.agents[r.id]?.tool ?? "—",
                  },
                  {
                    id: "produces", header: "Produces", width: 170,
                    cell: (r) => workflow.agents[r.id]?.produces ?? "—",
                  },
                  {
                    id: "size", header: "Output", width: 120,
                    cell: (r) => (r.output ? `${(r.output.length / 1024).toFixed(1)} KB` : "—"),
                  },
                ]}
              />
            ),
          },
          {
            id: "timeline",
            label: "Timeline",
            content: (
              <Table
                variant="container"
                items={(snap.logs ?? []).slice().reverse().map((l, i) => ({ ...l, key: i }))}
                trackBy="key"
                header={
                  <Header variant="h2" counter={`(${(snap.logs ?? []).length})`}
                          description="Newest first. Branch decisions, guardrail blocks and ungrounded-figure warnings appear here.">
                    Events
                  </Header>
                }
                empty={<Box textAlign="center" padding={{ vertical: "l" }}>No events yet.</Box>}
                columnDefinitions={[
                  {
                    id: "ts", header: "Time (ET)", width: 190,
                    cell: (l) => <Box fontSize="body-s" variant="code">{fmtET(l.ts)}</Box>,
                  },
                  {
                    id: "node", header: "Step", width: 190,
                    cell: (l) => (l.node ? (workflow.agents[l.node]?.name ?? l.node) : "—"),
                  },
                  { id: "msg", header: "Message", cell: (l) => l.msg, minWidth: 320 },
                ]}
              />
            ),
          },
          {
            id: "outputs",
            label: "Outputs",
            content: (
              <Container
                header={
                  <Header variant="h2" counter={`(${ids.filter((i) => nodes[i]?.output).length})`}>
                    Step outputs
                  </Header>
                }
              >
                <SpaceBetween size="s">
                  {ids.filter((i) => nodes[i]?.output).map((id) => (
                    <ExpandableSection
                      key={id}
                      variant="container"
                      headerText={workflow.agents[id]?.name ?? id}
                    >
                      <AssetView text={nodes[id]?.output ?? ""} />
                    </ExpandableSection>
                  ))}
                  {ids.every((i) => !nodes[i]?.output) ? (
                    <Box color="text-status-inactive">No outputs yet.</Box>
                  ) : null}
                </SpaceBetween>
              </Container>
            ),
          },
          {
            id: "rerun",
            label: "Re-run",
            content: (
              <RerunPanel
                snap={snap} workflow={workflow} can={can} onRerun={onRerun}
              />
            ),
          },
        ]}
      />
    </SpaceBetween>
  );
}
