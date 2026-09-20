/** The workflow graph, laid out the way Step Functions draws an execution.
 *
 *  Cloudscape has no graph component — the console's own workflow views are custom
 *  canvases too — so this is bespoke markup INSIDE a Cloudscape Container, using
 *  Cloudscape's design tokens for every colour and radius so it sits in the page
 *  rather than on it. What is borrowed from Step Functions: a tinted dot-grid canvas,
 *  zoom controls, a vertical flow, state boxes whose border carries status, connectors
 *  with arrowheads, and a Parallel state drawn as a frame around its branches.
 */

import Badge from "@cloudscape-design/components/badge";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Popover from "@cloudscape-design/components/popover";
import SpaceBetween from "@cloudscape-design/components/space-between";
import { useState } from "react";

import type { AgentMeta, NodeState, NodeStatus, Step, Workflow } from "../types";
import { statusIndicator } from "../lib/status";
import "./graph.css";

const BRANCH_OPS = ["equals", "notEquals", "in", "contains", "exists", "gt", "gte", "lt", "lte"];

/** The rules of a step's `branch`, as prose. The graph can only draw the CONFIGURED
 *  order of steps; with a branch the real path depends on the output, so a reader has
 *  to be able to see what decides it. */
function branchSummary(b: NonNullable<Step["branch"]>): string[] {
  const lines = (b.when ?? []).map((r) => {
    const cmp = BRANCH_OPS.filter((o) => o in r)
      .map((o) => `${o} ${JSON.stringify(r[o])}`)
      .join(" and ");
    return `if ${r.field ?? "output"} ${cmp} → ${r.goto}`;
  });
  lines.push(b.default ? `otherwise → ${b.default}` : "otherwise → next step");
  return lines;
}

function monogram(name: string): string {
  const words = (name || "?").trim().split(/\s+/).filter(Boolean);
  if (words.length === 0) return "?";
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (words[0][0] + words[1][0]).toUpperCase();
}

function shortModel(model?: string): string | null {
  if (!model) return null;
  return model.split(".").pop()!.replace(/-\d+-v\d+:\d+$/, "");
}

/** What an agent reads, when it is not bound to a declared tool. */
function sourceLabel(meta: AgentMeta): string | null {
  if (meta.tool) return null;
  if (meta.runtime === "a2a") return meta.source ? `a2a · ${meta.source}` : "remote agent";
  if (meta.access) return meta.access;
  return null;
}

function StateBox({
  agentId, meta, state, selected, onSelect,
}: {
  agentId: string;
  meta: AgentMeta;
  state: NodeState;
  selected: boolean;
  onSelect: (id: string) => void;
}) {
  const status: NodeStatus = state.status ?? "pending";
  const model = shortModel(meta.model);
  const src = sourceLabel(meta);
  return (
    <div
      className={`sfn-state sfn-${status}${selected ? " sfn-selected" : ""}`}
      data-testid={`state-${agentId}`}
      role="button"
      tabIndex={0}
      aria-label={`${meta.name ?? agentId} — ${status}`}
      onClick={(e) => { e.stopPropagation(); onSelect(agentId); }}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onSelect(agentId); }
      }}
    >
      <div className="sfn-state-head">
        <span className="sfn-mono" aria-hidden="true">{monogram(meta.name ?? agentId)}</span>
        <span className="sfn-state-name">{meta.name ?? agentId}</span>
      </div>
      <div className="sfn-state-tags">
        {meta.runtime === "dedicated" ? <Badge color="blue">dedicated</Badge> : null}
        {meta.runtime === "a2a" ? <Badge color="grey">a2a</Badge> : null}
        {meta.tool ? <Badge color="green">{`tool: ${meta.tool}`}</Badge> : null}
        {meta.corpus ? <Badge color="severity-low">{`corpus: ${meta.corpus}`}</Badge> : null}
        {src ? <Badge color="grey">{src}</Badge> : null}
        {model ? <Badge color="grey">{model}</Badge> : null}
      </div>
      {status === "running" ? (
        <div className="sfn-bar"><i style={{ width: `${state.pct || 6}%` }} /></div>
      ) : null}
      <div className="sfn-state-status">{statusIndicator(status)}</div>
    </div>
  );
}

function Connector() {
  return <div className="sfn-connector" aria-hidden="true" />;
}

function StageAnnotations({ step }: { step: Step }) {
  if (!step.hitl && !step.branch) return null;
  return (
    <div className="sfn-annotations">
      {step.branch ? (
        <Popover
          dismissButton={false}
          position="top"
          size="medium"
          triggerType="custom"
          header="Branch rules"
          content={
            <SpaceBetween size="xxs">
              {branchSummary(step.branch).map((l, i) => (
                <Box key={i} variant="code" fontSize="body-s">{l}</Box>
              ))}
            </SpaceBetween>
          }
        >
          <span className="sfn-pill sfn-pill-branch" role="note">Branch</span>
        </Popover>
      ) : null}
      {step.hitl ? (
        <Popover
          dismissButton={false}
          position="top"
          size="small"
          triggerType="custom"
          content={<Box variant="p">A human approves, revises or denies this stage before the run continues.</Box>}
        >
          <span className="sfn-pill sfn-pill-gate" role="note">Review gate</span>
        </Popover>
      ) : null}
    </div>
  );
}

function Stage({
  step, index, workflow, nodes, selected, onSelect,
}: {
  step: Step;
  index: number;
  workflow: Workflow;
  nodes: Record<string, NodeState>;
  selected: string | null;
  onSelect: (id: string) => void;
}) {
  const box = (id: string) => (
    <StateBox
      key={id}
      agentId={id}
      meta={workflow.agents[id] ?? { name: id }}
      state={nodes[id] ?? { status: "pending" }}
      selected={selected === id}
      onSelect={onSelect}
    />
  );

  let body: JSX.Element;
  if (step.parallel) {
    const cols = Math.min(2, step.parallel.length);
    body = (
      <div className="sfn-frame">
        <span className="sfn-frame-label">
          {`${step.gateName ?? "Parallel"} · ${step.parallel.length} branches in parallel`}
        </span>
        <div className="sfn-frame-grid" style={{ gridTemplateColumns: `repeat(${cols}, 240px)` }}>
          {step.parallel.map(box)}
        </div>
      </div>
    );
  } else if (step.sequence) {
    body = (
      <div className="sfn-frame">
        <span className="sfn-frame-label">
          {`${step.gateName ?? "Sequence"} · ${step.sequence.length} in order`}
        </span>
        <div className="sfn-frame-row">
          {step.sequence.map((id, i) => (
            <div key={id} className="sfn-seq-item">
              {i > 0 ? <div className="sfn-seq-arrow" aria-hidden="true" /> : null}
              {box(id)}
            </div>
          ))}
        </div>
      </div>
    );
  } else {
    body = box(step.agent!);
  }

  return (
    <div className="sfn-stage">
      <div className="sfn-stage-num" aria-hidden="true">{index + 1}</div>
      <StageAnnotations step={step} />
      {body}
    </div>
  );
}

const ZOOMS = [0.6, 0.75, 0.9, 1, 1.15, 1.3];

export function Graph({
  workflow, nodes, selected, onSelect,
}: {
  workflow: Workflow;
  nodes: Record<string, NodeState>;
  selected: string | null;
  onSelect: (id: string | null) => void;
}) {
  const [zoomIdx, setZoomIdx] = useState(3);
  const zoom = ZOOMS[zoomIdx];
  const steps = workflow.steps ?? [];

  return (
    <div className="sfn-canvas" onClick={() => onSelect(null)}>
      <div className="sfn-toolbar" onClick={(e) => e.stopPropagation()}>
        <Button
          variant="icon" iconName="zoom-out" ariaLabel="Zoom out"
          disabled={zoomIdx === 0} onClick={() => setZoomIdx((i) => Math.max(0, i - 1))}
        />
        <span className="sfn-zoom-label">{Math.round(zoom * 100)}%</span>
        <Button
          variant="icon" iconName="zoom-in" ariaLabel="Zoom in"
          disabled={zoomIdx === ZOOMS.length - 1}
          onClick={() => setZoomIdx((i) => Math.min(ZOOMS.length - 1, i + 1))}
        />
        <Button
          variant="icon" iconName="refresh" ariaLabel="Reset zoom"
          onClick={() => setZoomIdx(3)}
        />
      </div>
      <div className={`sfn-flow${selected ? " sfn-has-selection" : ""}`}
           style={{ transform: `scale(${zoom})` }}>
        {steps.map((step, i) => (
          <div key={step.gateId ?? step.agent ?? i} className="sfn-stage-wrap">
            {i > 0 ? <Connector /> : null}
            <Stage
              step={step} index={i} workflow={workflow} nodes={nodes}
              selected={selected} onSelect={onSelect}
            />
          </div>
        ))}
      </div>
    </div>
  );
}

export { branchSummary, monogram };
