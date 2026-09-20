/** The Runs collection, as a console Table.
 *
 *  This is the change that makes the app read as a service screen: a collection lives
 *  in a Table in the content area with a header count, a filter and row actions — it
 *  does not live in the side navigation, which a console reserves for navigating.
 */

import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Header from "@cloudscape-design/components/header";
import Link from "@cloudscape-design/components/link";
import Pagination from "@cloudscape-design/components/pagination";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Table from "@cloudscape-design/components/table";
import TextFilter from "@cloudscape-design/components/text-filter";
import { useMemo, useState } from "react";

import { fmtET } from "../lib/clock";
import { statusIndicator } from "../lib/status";
import type { SessionSummary } from "../types";

const PAGE_SIZE = 12;

export function RunsTable({
  runs, loading, can, onOpen, onDelete, onStartNew,
}: {
  runs: SessionSummary[];
  loading: boolean;
  can: (a: "delete" | "start") => boolean;
  onOpen: (id: string) => void;
  onDelete: (id: string) => void;
  onStartNew: () => void;
}) {
  const [filter, setFilter] = useState("");
  const [page, setPage] = useState(1);

  const matched = useMemo(() => {
    const q = filter.trim().toLowerCase();
    if (!q) return runs;
    return runs.filter((r) =>
      r.topic.toLowerCase().includes(q)
      || r.session_id.toLowerCase().includes(q)
      || String(r.overall).toLowerCase().includes(q));
  }, [runs, filter]);

  const pages = Math.max(1, Math.ceil(matched.length / PAGE_SIZE));
  const current = Math.min(page, pages);
  const visible = matched.slice((current - 1) * PAGE_SIZE, current * PAGE_SIZE);

  return (
    <Table
      variant="container"
      loading={loading}
      loadingText="Loading runs"
      items={visible}
      trackBy="session_id"
      onRowClick={({ detail }) => onOpen(detail.item.session_id)}
      header={
        <Header
          counter={`(${matched.length})`}
          description="Each run is one execution of the workflow defined in workflow.json."
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Button iconName="add-plus" variant="primary" disabled={!can("start")}
                      onClick={onStartNew}>
                Start run
              </Button>
            </SpaceBetween>
          }
        >
          Runs
        </Header>
      }
      filter={
        <TextFilter
          filteringText={filter}
          filteringPlaceholder="Find runs"
          filteringAriaLabel="Filter runs"
          countText={filter ? `${matched.length} matches` : ""}
          onChange={({ detail }) => { setFilter(detail.filteringText); setPage(1); }}
        />
      }
      pagination={
        pages > 1
          ? <Pagination currentPageIndex={current} pagesCount={pages}
                        onChange={({ detail }) => setPage(detail.currentPageIndex)} />
          : undefined
      }
      empty={
        <Box textAlign="center" color="inherit" padding={{ vertical: "l" }}>
          <SpaceBetween size="s">
            <b>No runs</b>
            <Box variant="p" color="inherit">
              Start a run to see it here.
            </Box>
            <Button disabled={!can("start")} onClick={onStartNew}>Start run</Button>
          </SpaceBetween>
        </Box>
      }
      columnDefinitions={[
        {
          id: "topic",
          header: "Request",
          cell: (r) => (
            <Link
              href="#"
              onFollow={(e) => { e.preventDefault(); onOpen(r.session_id); }}
            >
              {r.topic || "(no topic)"}
            </Link>
          ),
          isRowHeader: true,
          minWidth: 320,
        },
        {
          id: "status",
          header: "Status",
          cell: (r) => statusIndicator(String(r.overall)),
          width: 170,
        },
        {
          id: "id",
          header: "Run ID",
          cell: (r) => <Box fontSize="body-s" variant="code">{r.session_id}</Box>,
          width: 180,
        },
        {
          id: "created",
          header: "Started (ET)",
          cell: (r) => fmtET(r.created) || "—",
          width: 190,
        },
        {
          id: "actions",
          header: "",
          cell: (r) =>
            can("delete")
              ? (
                <Button
                  variant="inline-icon" iconName="remove" ariaLabel={`Delete ${r.session_id}`}
                  onClick={() => onDelete(r.session_id)}
                />
              )
              : null,
          width: 70,
        },
      ]}
    />
  );
}
