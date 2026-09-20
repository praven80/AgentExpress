/** Rewind and re-run.
 *
 *  Two flavours, both POST /api/sessions/{id}/rerun:
 *    1. one agent + everything downstream;
 *    2. a SUBSET of one gated parallel stage, which re-runs those agents together and
 *       re-pauses at that stage's gate.
 *  The second is only offered for a gated parallel step, because that gate is what
 *  re-reviews the subset — anywhere else there is nothing to review them together. */

import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Checkbox from "@cloudscape-design/components/checkbox";
import Container from "@cloudscape-design/components/container";
import FormField from "@cloudscape-design/components/form-field";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Textarea from "@cloudscape-design/components/textarea";
import { useState } from "react";

import type { Action, SessionSnapshot, Workflow } from "../types";

const SETTLED = ["done", "denied", "failed", "waiting_human", "cancelled"];

export function RerunPanel({
  snap, workflow, can, onRerun,
}: {
  snap: SessionSnapshot;
  workflow: Workflow;
  can: (a: Action) => boolean;
  onRerun: (agents: string[], comment: string) => Promise<void>;
}) {
  const [picked, setPicked] = useState<Record<string, boolean>>({});
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);

  const settled = SETTLED.includes(String(snap.overall ?? ""));
  const nodes = snap.nodes ?? {};
  const gatedParallel = (workflow.steps ?? []).filter((s) => s.parallel && s.hitl);
  const allIds = (workflow.steps ?? []).flatMap((s) => s.parallel ?? s.sequence ?? [s.agent!]);
  const ran = (id: string) =>
    Boolean(nodes[id]?.output) || nodes[id]?.status === "done";

  const chosen = Object.keys(picked).filter((k) => picked[k]);

  if (!settled) {
    return (
      <Container header={<Header variant="h2">Re-run</Header>}>
        <Alert type="info">
          Available once the run settles. It is currently{" "}
          <Box variant="strong" display="inline">{String(snap.overall)}</Box>.
        </Alert>
      </Container>
    );
  }

  async function submit(agents: string[]) {
    setBusy(true);
    try {
      await onRerun(agents, comment);
      setPicked({});
      setComment("");
    } finally {
      setBusy(false);
    }
  }

  return (
    <SpaceBetween size="l">
      {gatedParallel.map((step) => (
        <Container
          key={step.gateId ?? step.gateName}
          header={
            <Header
              variant="h2"
              description="These agents run concurrently, so any subset can be re-run together and re-reviewed at this stage's gate."
            >
              {`Re-run agents — ${step.gateName ?? step.gateId}`}
            </Header>
          }
        >
          <SpaceBetween size="s">
            {step.parallel!.map((id) => (
              <Checkbox
                key={id}
                checked={Boolean(picked[id])}
                disabled={!ran(id)}
                onChange={({ detail }) => setPicked((p) => ({ ...p, [id]: detail.checked }))}
              >
                {workflow.agents[id]?.name ?? id}
                {ran(id) ? null : <Box variant="small" color="text-status-inactive"> — no output to revise</Box>}
              </Checkbox>
            ))}
            <FormField label="Feedback" description="Injected as the feedback for every agent you picked." stretch>
              <Textarea value={comment} rows={2} placeholder="What should they change?"
                        onChange={({ detail }) => setComment(detail.value)} />
            </FormField>
            <Box float="right">
              <Button
                variant="primary" loading={busy}
                disabled={!can("rerun") || chosen.length === 0}
                onClick={() => void submit(chosen.filter((id) => step.parallel!.includes(id)))}
              >
                {`Re-run ${chosen.length || ""} selected`.trim()}
              </Button>
            </Box>
          </SpaceBetween>
        </Container>
      ))}

      <Container
        header={
          <Header
            variant="h2"
            description="Re-runs one step and every stage after it. Each downstream review pauses again."
          >
            Re-run from a single step
          </Header>
        }
      >
        <SpaceBetween size="s">
          <SpaceBetween direction="horizontal" size="xs">
            {allIds.filter(ran).map((id) => (
              <Button
                key={id} disabled={!can("rerun") || busy}
                onClick={() => void submit([id])}
              >
                {workflow.agents[id]?.name ?? id}
              </Button>
            ))}
          </SpaceBetween>
          {allIds.filter(ran).length === 0 ? (
            <Box color="text-status-inactive">No step has produced output yet.</Box>
          ) : null}
        </SpaceBetween>
      </Container>
    </SpaceBetween>
  );
}
