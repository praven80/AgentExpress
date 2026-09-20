/** The shapes the BFF actually returns. Kept in one file so a projection change
 *  surfaces as a type error rather than as an undefined at runtime.
 *
 *  Note how little the framework asserts about an AGENT'S OUTPUT: it is a string,
 *  because the contract inside it is the customer's. Everything that reads it does so
 *  by SHAPE (see assets/AssetView.tsx), which is what lets a workflow define its own
 *  asset types without touching this file. */

export type NodeStatus =
  | "pending" | "running" | "done" | "waiting_human"
  | "failed" | "denied" | "cancelled" | "cancelling" | "skipped" | "revise";

export interface AgentMeta {
  name?: string;
  kind?: string;
  runtime?: "main" | "dedicated" | "a2a" | string;
  tool?: string;
  corpus?: string;
  model?: string;
  produces?: string;
  access?: string;
  maxTokens?: number;
  source?: string;
  skill?: string;
}

export interface BranchRule {
  field?: string;
  goto: string;
  [operator: string]: unknown;
}

export interface Step {
  agent?: string;
  parallel?: string[];
  sequence?: string[];
  hitl?: boolean;
  gateId?: string;
  gateName?: string;
  branch?: { when?: BranchRule[]; default?: string };
}

export interface UiConfig {
  title?: string;
  heading?: string;
  defaultTopic?: string;
  topicPlaceholder?: string;
  subjectPlaceholder?: string;
  subjectHint?: string;
  assistantTitle?: string;
  assistantSubtitle?: string;
}

export interface ChatbotConfig {
  enabled?: boolean;
  greeting?: string;
  placeholder?: string;
  tools?: Record<string, boolean>;
}

export interface Workflow {
  agents: Record<string, AgentMeta>;
  steps: Step[];
  ui?: UiConfig;
  authorization?: { actions?: string[] };
  chatbot?: ChatbotConfig | null;
  evalAgents?: string[];
}

export interface NodeState {
  status: NodeStatus;
  pct?: number;
  output?: string;
}

export interface HistoryEntry {
  version: number;
  at: string;
  comment?: string;
  output?: string;
}

export interface LogLine {
  ts: string;
  node?: string;
  msg: string;
}

export interface SessionSummary {
  session_id: string;
  topic: string;
  overall: NodeStatus | string;
  created?: string;
}

export interface SessionSnapshot extends SessionSummary {
  updated_at?: string;
  user?: string;
  subject_id?: string;
  nodes?: Record<string, NodeState>;
  history?: Record<string, HistoryEntry[]>;
  logs?: LogLine[];
  result?: string;
  hitl?: { node: string; question?: string } | null;
}

export interface Me {
  user: string;
  groups: string[];
  /** null means "not loaded yet or unrestricted" — everything is allowed. */
  permittedActions: string[] | null;
  authzEnabled: boolean;
}

/** The mutating actions `authorization` in workflow.json can gate. */
export type Action =
  | "start" | "decision" | "rerun" | "cancel" | "evaluate" | "insights" | "delete";
