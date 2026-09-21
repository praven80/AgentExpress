/** The console shell.
 *
 *  AppLayout + TopNavigation + SideNavigation + BreadcrumbGroup + ContentLayout +
 *  SplitPanel + Flashbar — the structure every AWS service page has. The pieces that
 *  make it read as a console are the ones that are easy to leave out: breadcrumbs that
 *  say where you are, a page header that owns the resource's actions, and a collection
 *  in a Table rather than in the navigation panel.
 *
 *  There is no router. The console uses URLs, but adding one would mean a CloudFront
 *  error-page rule to serve index.html for deep links, and this deployment has none —
 *  so navigation is state, and the tradeoff is written down rather than discovered.
 */

import AppLayout from "@cloudscape-design/components/app-layout";
import BreadcrumbGroup from "@cloudscape-design/components/breadcrumb-group";
import ContentLayout from "@cloudscape-design/components/content-layout";
import Flashbar, { type FlashbarProps } from "@cloudscape-design/components/flashbar";
import SideNavigation from "@cloudscape-design/components/side-navigation";
import SplitPanel from "@cloudscape-design/components/split-panel";
import TopNavigation from "@cloudscape-design/components/top-navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ApiError, api } from "./api";
import { authEnabled, initAuth, logout } from "./auth";
import { isSettled } from "./lib/status";
import type {
  Action, Me, SessionSnapshot, SessionSummary, Workflow,
} from "./types";
import { AboutPanel } from "./views/AboutPanel";
import { Assistant } from "./views/Assistant";
import { HitlGate, type Decision, type GroupDecision } from "./views/HitlGate";
import { Observability } from "./views/Observability";
import { RunDetail } from "./views/RunDetail";
import { RunsTable } from "./views/RunsTable";
import { StartRunModal } from "./views/StartRunModal";
import { StepPanel } from "./views/StepPanel";

const REGION = "us-east-1";
type View = "runs" | "observability";

const EMPTY_WORKFLOW: Workflow = { agents: {}, steps: [] };

export default function App() {
  const [booted, setBooted] = useState(false);
  const [user, setUser] = useState("");
  const [workflow, setWorkflow] = useState<Workflow>(EMPTY_WORKFLOW);
  const [me, setMe] = useState<Me>({
    user: "", groups: [], permittedActions: null, authzEnabled: false,
  });

  const [view, setView] = useState<View>("runs");
  const [runs, setRuns] = useState<SessionSummary[]>([]);
  const [runsLoading, setRunsLoading] = useState(true);
  const [openRun, setOpenRun] = useState<string | null>(null);
  const [snap, setSnap] = useState<SessionSnapshot | null>(null);
  const [selectedStep, setSelectedStep] = useState<string | null>(null);
  const [tab, setTab] = useState("graph");
  const [startOpen, setStartOpen] = useState(false);
  /** The tools drawer. Open on the FIRST visit only: a first-time reader needs to be
   *  told what the application is, and a returning one does not need it in the way. */
  const [toolsOpen, setToolsOpen] = useState(
    () => localStorage.getItem("seen_about") !== "1");
  const [flash, setFlash] = useState<FlashbarProps.MessageDefinition[]>([]);

  const flashId = useRef(0);
  const notify = useCallback((type: FlashbarProps.Type, content: string) => {
    const id = String(++flashId.current);
    setFlash((f) => [
      ...f,
      { id, type, content, dismissible: true, onDismiss: () => setFlash((g) => g.filter((x) => x.id !== id)) },
    ]);
  }, []);

  const fail = useCallback((e: unknown, what: string) => {
    const msg = e instanceof ApiError ? `${what}: ${e.message}` : `${what}: ${String(e)}`;
    notify("error", msg);
  }, [notify]);

  /** RBAC is advisory here — every action is enforced again in bff/authz.py. This only
   *  stops the UI offering a control that would 403. */
  const can = useCallback((a: Action) => {
    const list = me.permittedActions;
    return list === null || list.includes(a);
  }, [me.permittedActions]);

  const denyReason = useCallback((a: Action) => {
    if (can(a)) return null;
    return `You do not have permission to ${a}. Your groups: ${me.groups.join(", ") || "none"}.`;
  }, [can, me.groups]);

  // --- boot ---------------------------------------------------------------
  useEffect(() => {
    void (async () => {
      const state = await initAuth();
      // null means a redirect to the IdP is under way. Stop: anything we fetch now is
      // a guaranteed 401 on the way out of the page.
      if (!state) return;
      setUser(state.user);
      try {
        setWorkflow(await api.get<Workflow>("/api/workflow"));
      } catch (e) {
        fail(e, "Could not load the workflow definition");
      }
      try {
        setMe(await api.get<Me>("/api/me"));
      } catch {
        /* leave permittedActions null: unknown means "do not hide anything" */
      }
      setBooted(true);
    })();
  }, [fail]);

  // --- polling ------------------------------------------------------------
  const refreshRuns = useCallback(async () => {
    try {
      setRuns(await api.get<SessionSummary[]>("/api/sessions"));
    } catch (e) {
      if (booted) fail(e, "Could not list runs");
    } finally {
      setRunsLoading(false);
    }
  }, [booted, fail]);

  useEffect(() => {
    if (!booted) return;
    void refreshRuns();
    const t = setInterval(() => void refreshRuns(), 5000);
    return () => clearInterval(t);
  }, [booted, refreshRuns]);

  useEffect(() => {
    if (!booted || !openRun) { setSnap(null); return; }
    let live = true;
    const pull = async () => {
      try {
        const s = await api.get<SessionSnapshot>(`/api/sessions/${openRun}`);
        if (live) setSnap(s);
      } catch { /* a transient failure should not clear the page */ }
    };
    void pull();
    const t = setInterval(() => void pull(), 2000);
    return () => { live = false; clearInterval(t); };
  }, [booted, openRun]);

  const ui = useMemo(() => workflow.ui ?? {}, [workflow.ui]);
  const heading = ui.heading || ui.title || "Multi-Agent Orchestrator";

  useEffect(() => {
    if (ui.title) document.title = ui.title;
  }, [ui.title]);

  useEffect(() => {
    if (toolsOpen) localStorage.setItem("seen_about", "1");
  }, [toolsOpen]);

  const settled = isSettled(String(snap?.overall ?? ""));

  // --- actions ------------------------------------------------------------
  async function startRun(topic: string, subject: string) {
    try {
      const body: Record<string, string> = { topic };
      if (subject) body.subject_id = subject;
      const r = await api.post<{ session_id: string }>("/api/sessions", body);
      setStartOpen(false);
      setOpenRun(r.session_id);
      setSelectedStep(null);
      setTab("graph");
      notify("success", "Run started.");
      void refreshRuns();
    } catch (e) {
      fail(e, "Could not start the run");
    }
  }

  async function decide(d: Decision, comment: string) {
    if (!openRun) return;
    try {
      await api.post(`/api/sessions/${openRun}/decision`, { decision: d, comment });
      notify("success", `Recorded: ${d}.`);
    } catch (e) {
      fail(e, "Could not submit the decision");
    }
  }

  async function groupDecide(per: Record<string, GroupDecision>, comment: string) {
    if (!openRun) return;
    const decisions: Record<string, { decision: string; comment: string }> = {};
    for (const [id, v] of Object.entries(per)) {
      decisions[id] = { decision: v.decision, comment: v.comment };
    }
    try {
      await api.post(`/api/sessions/${openRun}/decision`, { decisions, comment });
      notify("success", "Decisions recorded.");
    } catch (e) {
      fail(e, "Could not submit the decisions");
    }
  }

  async function rerun(agents: string[], comment: string) {
    if (!openRun || agents.length === 0) return;
    try {
      // Two shapes, and they are not interchangeable (bff/handler.py):
      //   one agent      -> {"agentId": "x", "comment": "..."}
      //   a subset       -> {"agents": [{"agentId": "x", "comment": "..."}, ...]}
      // The subset form takes a list of OBJECTS; sending a list of ids silently matched
      // nothing.
      const body = agents.length === 1
        ? { agentId: agents[0], comment }
        : { agents: agents.map((agentId) => ({ agentId, comment })) };
      await api.post(`/api/sessions/${openRun}/rerun`, body);
      setSelectedStep(null);
      notify("success", agents.length === 1
        ? "Re-running that step and everything after it."
        : `Re-running ${agents.length} agents.`);
    } catch (e) {
      fail(e, "Could not re-run");
    }
  }

  async function cancel() {
    if (!openRun) return;
    try {
      await api.post(`/api/sessions/${openRun}/cancel`);
      notify("info", "Stop requested. The run halts at the next step boundary.");
    } catch (e) {
      fail(e, "Could not stop the run");
    }
  }

  async function del(id: string) {
    if (!window.confirm("Delete this run? Its status and timeline are removed permanently.")) return;
    try {
      await api.del(`/api/sessions/${id}`);
      if (openRun === id) { setOpenRun(null); setSnap(null); }
      notify("success", "Run deleted.");
      void refreshRuns();
    } catch (e) {
      fail(e, "Could not delete the run");
    }
  }

  // --- shell --------------------------------------------------------------
  const crumbs = useMemo(() => {
    const items = [{ text: heading, href: "#runs" }];
    if (view === "observability") {
      items.push({ text: "Observability", href: "#obs" });
    } else {
      items.push({ text: "Runs", href: "#runs" });
      if (openRun) items.push({ text: openRun, href: "#run" });
    }
    return items;
  }, [heading, view, openRun]);

  const content = view === "observability"
    ? <Observability />
    : openRun && snap
      ? (
        <>
          {snap.hitl ? (
            <div style={{ marginBottom: 20 }}>
              <HitlGate
                snap={snap} workflow={workflow}
                canDecide={can("decision")} denyReason={denyReason("decision")}
                onDecide={decide} onGroupDecide={groupDecide}
              />
            </div>
          ) : null}
          <RunDetail
            snap={snap} workflow={workflow} selected={selectedStep}
            onSelect={setSelectedStep} can={can} onCancel={cancel}
            activeTab={tab} onTabChange={setTab} onRerun={rerun}
          />
        </>
      )
      : (
        <RunsTable
          runs={runs} loading={runsLoading}
          can={(a) => can(a as Action)}
          onOpen={(id) => { setOpenRun(id); setSelectedStep(null); setTab("graph"); }}
          onDelete={(id) => void del(id)}
          onStartNew={() => setStartOpen(true)}
        />
      );

  return (
    <>
      <div id="top-nav">
        <TopNavigation
          identity={{ href: "#", title: heading, onFollow: (e) => {
            e.preventDefault(); setView("runs"); setOpenRun(null);
          } }}
          utilities={[
            {
              type: "button",
              iconName: "status-info",
              text: "Info",
              ariaLabel: "About this application",
              disableUtilityCollapse: true,
              onClick: () => setToolsOpen(true),
            },
            { type: "button", text: `${REGION}`, iconName: "map", disableUtilityCollapse: true },
            ...(authEnabled()
              ? [{
                type: "menu-dropdown" as const,
                text: user || "Account",
                iconName: "user-profile" as const,
                items: [
                  { id: "groups", text: `Groups: ${me.groups.join(", ") || "none"}`, disabled: true },
                  { id: "signout", text: "Sign out" },
                ],
                onItemClick: ({ detail }: { detail: { id: string } }) => {
                  if (detail.id === "signout") logout();
                },
              }]
              : []),
          ]}
        />
      </div>

      <AppLayout
        headerSelector="#top-nav"
        breadcrumbs={
          <BreadcrumbGroup
            items={crumbs}
            onFollow={(e) => {
              e.preventDefault();
              const href = e.detail.href;
              if (href === "#runs") { setView("runs"); setOpenRun(null); }
              if (href === "#obs") setView("observability");
            }}
          />
        }
        navigation={
          <SideNavigation
            header={{ href: "#runs", text: heading }}
            activeHref={view === "observability" ? "#obs" : "#runs"}
            onFollow={(e) => {
              // ONLY swallow the internal hash links. This handler used to call
              // preventDefault() unconditionally, which meant the external
              // "workflow.json reference" link did nothing at all when clicked — the
              // navigation was cancelled and nothing replaced it.
              const href = e.detail.href;
              if (!href.startsWith("#")) return;
              e.preventDefault();
              if (href === "#runs") { setView("runs"); setOpenRun(null); }
              if (href === "#obs") setView("observability");
              if (href === "#about") setToolsOpen(true);
            }}
            items={[
              { type: "link", text: "Runs", href: "#runs" },
              { type: "link", text: "Observability", href: "#obs" },
              { type: "divider" },
              {
                type: "section",
                text: "Workflow",
                items: (workflow.steps ?? []).map((s, i) => ({
                  type: "link" as const,
                  text: `${i + 1}. ${s.gateName
                    ?? workflow.agents[s.agent ?? ""]?.name
                    ?? s.agent
                    ?? "Stage"}`,
                  href: "#runs",
                })),
              },
              { type: "divider" },
              { type: "link", text: "About this application", href: "#about" },
              {
                type: "link",
                text: "workflow.json reference",
                href: "https://github.com/awslabs/agentcore-samples",
                external: true,
                externalIconAriaLabel: "Opens in a new tab",
              },
            ]}
          />
        }
        tools={<AboutPanel workflow={workflow} heading={heading} />}
        toolsOpen={toolsOpen}
        onToolsChange={({ detail }) => setToolsOpen(detail.open)}
        /* Without these, AppLayout's own open/close controls ship with no accessible
           name — the info panel could be opened and then not closed by anyone using a
           screen reader, and a test could not find the button either. */
        ariaLabels={{
          navigation: "Navigation",
          navigationToggle: "Open the navigation",
          navigationClose: "Close the navigation",
          tools: "About this application",
          toolsToggle: "Open the information panel",
          toolsClose: "Close the information panel",
          notifications: "Notifications",
        }}
        notifications={<Flashbar items={flash} stackItems />}
        splitPanelOpen={Boolean(selectedStep && snap)}
        onSplitPanelToggle={({ detail }) => { if (!detail.open) setSelectedStep(null); }}
        splitPanelPreferences={{ position: "side" }}
        splitPanel={
          selectedStep && snap
            ? (
              <SplitPanel
                header={workflow.agents[selectedStep]?.name ?? selectedStep}
                closeBehavior="hide"
                i18nStrings={{
                  preferencesTitle: "Split panel preferences",
                  preferencesPositionLabel: "Position",
                  preferencesPositionDescription: "Where the panel opens.",
                  preferencesPositionSide: "Side",
                  preferencesPositionBottom: "Bottom",
                  preferencesConfirm: "Confirm",
                  preferencesCancel: "Cancel",
                  closeButtonAriaLabel: "Close panel",
                  openButtonAriaLabel: "Open panel",
                  resizeHandleAriaLabel: "Resize panel",
                }}
              >
                <StepPanel
                  agentId={selectedStep} snap={snap} workflow={workflow}
                  canRerun={can("rerun")} settled={settled} onRerun={rerun}
                />
              </SplitPanel>
            )
            : undefined
        }
        content={
          <ContentLayout>
            {booted ? content : null}
            <StartRunModal
              visible={startOpen} ui={ui}
              onDismiss={() => setStartOpen(false)}
              onStart={startRun}
            />
            {booted ? (
              <Assistant
                ui={ui} chatbot={workflow.chatbot} sessionId={openRun}
                onActed={() => { void refreshRuns(); }}
              />
            ) : null}
          </ContentLayout>
        }
      />
    </>
  );
}
