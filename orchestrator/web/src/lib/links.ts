/** Outbound links, in one place.
 *
 *  Two screens link to this project on GitHub — the side navigation and the info panel —
 *  and they pointed at two different repositories, one of which was not this one. A
 *  constant makes "which repo is this?" a single edit for anyone who forks it. */

export const REPO = "https://github.com/praven80/sample-multi-agent-orchestrator";

/** `blob/main` rather than `raw`: the point of the link is to READ the file with GitHub's
 *  JSON folding and line anchors, not to download it. */
export const WORKFLOW_JSON = `${REPO}/blob/main/orchestrator/app/workflow.json`;

/** Every key the file may contain, what reads it, and what it does. */
export const WORKFLOW_REFERENCE = `${REPO}/blob/main/orchestrator/docs/WORKFLOW_REFERENCE.md`;
