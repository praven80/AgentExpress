/** Starting a run, as a console Modal with a Form.
 *
 *  The topic field used to sit in the top navigation bar, where a console puts global
 *  utilities and nothing page-specific. A collection page offers a primary action that
 *  opens a form, so that is what this is. Every label, placeholder and hint comes from
 *  workflow.json `ui`, so a customer's wording appears here without a code change. */

import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Form from "@cloudscape-design/components/form";
import FormField from "@cloudscape-design/components/form-field";
import Input from "@cloudscape-design/components/input";
import Modal from "@cloudscape-design/components/modal";
import SpaceBetween from "@cloudscape-design/components/space-between";
import { useEffect, useState } from "react";

import type { UiConfig } from "../types";

export function StartRunModal({
  visible, ui, onDismiss, onStart,
}: {
  visible: boolean;
  ui: UiConfig;
  onDismiss: () => void;
  onStart: (topic: string, subject: string) => Promise<void>;
}) {
  const [topic, setTopic] = useState("");
  const [subject, setSubject] = useState("");
  const [busy, setBusy] = useState(false);

  // Seed the default topic when the modal opens, not on every render, so a reader's
  // edit is not overwritten.
  useEffect(() => {
    if (visible) setTopic(ui.defaultTopic ?? "");
  }, [visible, ui.defaultTopic]);

  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      header="Start run"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss} disabled={busy}>Cancel</Button>
            <Button
              variant="primary" loading={busy} disabled={!topic.trim()}
              onClick={async () => {
                setBusy(true);
                try { await onStart(topic.trim(), subject.trim()); } finally { setBusy(false); }
              }}
            >
              Start run
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <Form variant="embedded">
        <SpaceBetween size="l">
          <FormField
            label="Request"
            description="What the workflow should work on. This becomes the run's topic."
            stretch
          >
            <Input
              value={topic}
              placeholder={ui.topicPlaceholder ?? "What should the workflow work on?"}
              onChange={({ detail }) => setTopic(detail.value)}
              autoFocus
            />
          </FormField>
          <FormField
            label="Subject (optional)"
            description={
              ui.subjectHint
              ?? "Scopes long-term memory, so agents recall insights from earlier runs on the same subject."
            }
            stretch
          >
            <Input
              value={subject}
              placeholder={ui.subjectPlaceholder ?? "A customer, product or project"}
              onChange={({ detail }) => setSubject(detail.value)}
            />
          </FormField>
        </SpaceBetween>
      </Form>
    </Modal>
  );
}
