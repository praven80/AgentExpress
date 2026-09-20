/** One mapping from a run/node status onto Cloudscape's StatusIndicator.
 *
 *  Kept in one file because the status vocabulary appears in the graph, the runs
 *  table, the detail header and the split panel, and four copies would drift. The
 *  indicator pairs a shape with a colour, so state is never carried by hue alone. */

import StatusIndicator, {
  type StatusIndicatorProps,
} from "@cloudscape-design/components/status-indicator";

type Type = StatusIndicatorProps.Type;

const TYPES: Record<string, Type> = {
  done: "success",
  running: "in-progress",
  waiting_human: "pending",
  revise: "pending",
  cancelling: "pending",
  failed: "error",
  denied: "error",
  cancelled: "stopped",
  skipped: "stopped",
  pending: "pending",
};

/** The label a reviewer reads. `waiting_human` is the one worth rewording: the raw
 *  value describes the machine's state, not what the reader has to do. */
const LABELS: Record<string, string> = {
  waiting_human: "Awaiting review",
  done: "Succeeded",
  running: "Running",
  failed: "Failed",
  denied: "Denied",
  cancelled: "Cancelled",
  cancelling: "Cancelling",
  skipped: "Skipped",
  pending: "Not started",
  revise: "Revising",
};

export function statusType(status?: string): Type {
  return TYPES[status ?? "pending"] ?? "pending";
}

export function statusLabel(status?: string): string {
  const s = status ?? "pending";
  return LABELS[s] ?? s.replace(/_/g, " ");
}

export function statusIndicator(status?: string) {
  return <StatusIndicator type={statusType(status)}>{statusLabel(status)}</StatusIndicator>;
}
