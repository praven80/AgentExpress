/** The in-app assistant.
 *
 *  A Bedrock Converse tool-use loop lives in the BFF (bff/chatbot.py); this is just its
 *  surface. It can TAKE ACTIONS — approve a gate, re-run an agent, start an evaluation —
 *  through the same routes the buttons use, and it is held to the same `authorization`
 *  rules: the BFF withholds the action tools from a caller who lacks the group, so the
 *  assistant is not a way around a gate.
 *
 *  Rendered as a Cloudscape Modal from a floating trigger, which is how the console
 *  surfaces an assistant panel. Every string comes from workflow.json `ui` /
 *  `orchestrator.chatbot`, so a customer's wording appears with no code change. */

import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import Form from "@cloudscape-design/components/form";
import Header from "@cloudscape-design/components/header";
import Input from "@cloudscape-design/components/input";
import Modal from "@cloudscape-design/components/modal";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Spinner from "@cloudscape-design/components/spinner";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import { useEffect, useRef, useState } from "react";

import { api } from "../api";
import type { ChatbotConfig, UiConfig } from "../types";

interface Turn { role: "user" | "assistant"; content: string }
interface ChatReply { reply?: string; actions?: string[] | null }

export function Assistant({
  ui, chatbot, sessionId, onActed,
}: {
  ui: UiConfig;
  chatbot: ChatbotConfig | null | undefined;
  sessionId: string | null;
  onActed: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [actions, setActions] = useState<string[]>([]);
  const endRef = useRef<HTMLDivElement>(null);

  // The assistant is a config flag, so a deployment can ship without it.
  const enabled = chatbot?.enabled !== false && chatbot != null;

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [turns, busy]);

  if (!enabled) return null;

  const title = ui.assistantTitle || "Assistant";
  const subtitle = ui.assistantSubtitle
    || "Ask about a run, or tell me to do something.";

  async function send() {
    const message = draft.trim();
    if (!message || busy) return;
    setDraft("");
    const history = turns;
    setTurns([...history, { role: "user", content: message }]);
    setBusy(true);
    try {
      const data = await api.post<ChatReply>("/api/chat", {
        message,
        history,
        session_id: sessionId ?? "",
      });
      setTurns((t) => [...t, { role: "assistant", content: data.reply || "(no response)" }]);
      if (data.actions?.length) {
        setActions(data.actions);
        // An action changed server state, so the page has to catch up.
        onActed();
      }
    } catch (e) {
      setTurns((t) => [...t, {
        role: "assistant",
        content: `I could not reach the assistant: ${e instanceof Error ? e.message : String(e)}`,
      }]);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      {/* The floating trigger. `position: fixed` because AppLayout owns the scroll
          containers, so a normally-positioned button would scroll away. */}
      <div style={{ position: "fixed", right: 24, bottom: 24, zIndex: 1000 }}>
        <Button
          variant="primary" iconName="contact" ariaLabel={title}
          onClick={() => setOpen(true)}
        >
          {title}
        </Button>
      </div>

      <Modal
        visible={open}
        onDismiss={() => setOpen(false)}
        size="large"
        header={<Header variant="h2" description={subtitle}>{title}</Header>}
        footer={
          <Form variant="embedded">
            <SpaceBetween direction="horizontal" size="xs">
              <div style={{ flex: 1, minWidth: 360 }}>
                <Input
                  value={draft}
                  placeholder={chatbot?.placeholder || "Ask about a run…"}
                  disabled={busy}
                  onChange={({ detail }) => setDraft(detail.value)}
                  onKeyDown={({ detail }) => { if (detail.key === "Enter") void send(); }}
                />
              </div>
              <Button variant="primary" loading={busy} disabled={!draft.trim()}
                      onClick={() => void send()}>
                Send
              </Button>
            </SpaceBetween>
          </Form>
        }
      >
        <SpaceBetween size="m">
          {turns.length === 0 ? (
            <Box color="text-status-inactive" variant="p">
              {chatbot?.greeting
                || "Ask about status, cost, latency, outputs, guardrails or evaluations — "
                   + "or tell me to approve a gate, re-run an agent, or run an evaluation."}
            </Box>
          ) : null}

          {turns.map((t, i) => (
            <Container
              key={i}
              variant={t.role === "user" ? "stacked" : "default"}
              header={
                <Box variant="small" color="text-status-inactive">
                  {t.role === "user" ? "You" : title}
                </Box>
              }
            >
              <Box variant="p">
                <span style={{ whiteSpace: "pre-wrap" }}>{t.content}</span>
              </Box>
            </Container>
          ))}

          {busy ? <Box><Spinner /> Thinking…</Box> : null}

          {actions.length > 0 ? (
            <StatusIndicator type="success">
              {`Actions taken: ${actions.join(", ")}`}
            </StatusIndicator>
          ) : null}

          <div ref={endRef} />
        </SpaceBetween>
      </Modal>
    </>
  );
}
