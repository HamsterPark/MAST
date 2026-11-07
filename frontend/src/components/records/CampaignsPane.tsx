import { useState } from "react";
import { useQuery, keepPreviousData } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { api } from "../../api/client";
import type { components } from "../../api/schema";
import { DegradedNote, ErrorNote, Section, Spinner } from "../ui";
import { DataTable } from "../DataTable";
import { tr } from "../../lib/i18n";
import { StatusPill, fmtTime } from "./shared";
// Shared with the conduct panel so the three goal states are worded in one place.
import { badgeTone, goalProgressText } from "../../lib/conduct";

type CampaignSummary = components["schemas"]["CampaignSummary"];

const PAGE_SIZE = 50;

interface Filters {
  status: string;
  from: string;
  to: string;
  agent: string;
}

function useCampaigns(filters: Filters, page: number) {
  return useQuery({
    queryKey: ["records", "campaigns", filters, page],
    placeholderData: keepPreviousData,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/records/campaigns", {
        params: {
          query: {
            status: filters.status || null,
            from: filters.from || null,
            to: filters.to || null,
            agent: filters.agent || null,
            page,
            page_size: PAGE_SIZE,
          },
        },
      });
      if (error) throw error;
      return data;
    },
  });
}

const inputCls =
  "rounded border border-mast-border bg-mast-bg px-2 py-1 text-sm text-mast-text placeholder:text-mast-muted";

export function CampaignsPane() {
  const [draft, setDraft] = useState<Filters>({ status: "", from: "", to: "", agent: "" });
  const [filters, setFilters] = useState<Filters>(draft);
  const [page, setPage] = useState(1);
  const q = useCampaigns(filters, page);

  const apply = () => {
    setPage(1);
    setFilters(draft);
  };
  const reset = () => {
    const cleared = { status: "", from: "", to: "", agent: "" };
    setDraft(cleared);
    setFilters(cleared);
    setPage(1);
  };

  const columns: ColumnDef<CampaignSummary, any>[] = [
    { accessorKey: "title", header: tr("标题", "Title"), cell: (c) => c.getValue() || tr("(未命名)", "(unnamed)") },
    {
      accessorKey: "hypothesis",
      header: tr("假设", "Hypothesis"),
      cell: (c) => c.getValue() || "—",
    },
    {
      accessorKey: "status",
      header: tr("状态", "Status"),
      cell: (c) => <StatusPill status={c.getValue() as string | null} />,
    },
    { accessorKey: "created_by", header: "Agent", cell: (c) => c.getValue() || "—" },
    {
      accessorKey: "created_at",
      header: tr("创建时间", "Created"),
      cell: (c) => fmtTime(c.getValue() as string | null),
    },
    {
      id: "goal",
      header: tr("目标判据", "Goal criterion"),
      cell: (c) => {
        const g = goalProgressText(c.row.original.goal_progress);
        return (
          <span
            className={"rounded px-1.5 py-0.5 text-xs " + badgeTone(g.tone)}
            title={c.row.original.goal_progress?.reason || ""}
          >
            {g.text}
          </span>
        );
      },
    },
    {
      id: "experiments",
      header: tr("实验", "Experiments"),
      cell: (c) => c.row.original.stats?.experiments ?? 0,
    },
    {
      id: "actions",
      header: tr("动作", "Actions"),
      cell: (c) => c.row.original.stats?.actions ?? 0,
    },
    {
      id: "observations",
      header: tr("观测", "Observations"),
      cell: (c) => c.row.original.stats?.observations ?? 0,
    },
    {
      id: "scans",
      header: tr("扫描", "Scans"),
      cell: (c) => c.row.original.stats?.scans ?? 0,
    },
  ];

  const total = q.data?.count ?? 0;
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <Section title={tr("Campaigns（研究活动）", "Campaigns")}>
      <div className="mb-3 flex flex-wrap items-end gap-2">
        <label className="flex flex-col text-xs text-mast-muted">
          {tr("状态", "Status")}
          <input
            className={inputCls}
            value={draft.status}
            placeholder="running / completed …"
            onChange={(e) => setDraft({ ...draft, status: e.target.value })}
          />
        </label>
        <label className="flex flex-col text-xs text-mast-muted">
          {tr("起始日期", "From date")}
          <input
            className={inputCls}
            value={draft.from}
            placeholder="YYYY-MM-DD"
            onChange={(e) => setDraft({ ...draft, from: e.target.value })}
          />
        </label>
        <label className="flex flex-col text-xs text-mast-muted">
          {tr("截止日期", "To date")}
          <input
            className={inputCls}
            value={draft.to}
            placeholder="YYYY-MM-DD"
            onChange={(e) => setDraft({ ...draft, to: e.target.value })}
          />
        </label>
        <label className="flex flex-col text-xs text-mast-muted">
          Agent
          <input
            className={inputCls}
            value={draft.agent}
            placeholder="created_by"
            onChange={(e) => setDraft({ ...draft, agent: e.target.value })}
          />
        </label>
        <button
          onClick={apply}
          className="rounded bg-mast-accent/20 px-3 py-1 text-sm text-mast-accent hover:bg-mast-accent/30"
        >
          {tr("筛选", "Filter")}
        </button>
        <button
          onClick={reset}
          className="rounded border border-mast-border px-3 py-1 text-sm text-mast-muted hover:text-mast-text"
        >
          {tr("重置", "Reset")}
        </button>
      </div>

      {q.isPending && <Spinner />}
      {q.isError && <ErrorNote error={q.error} />}
      {q.data?.degraded && <DegradedNote what={tr("Campaigns 记录", "campaign records")} />}
      {q.data && !q.data.degraded && (
        <>
          <DataTable
            data={q.data.campaigns ?? []}
            columns={columns}
            empty={tr("无符合条件的 campaign", "No matching campaigns")}
          />
          <div className="mt-3 flex items-center justify-between text-sm text-mast-muted">
            <span>
              {tr(`共 ${total} 条 · 第 ${page} / ${pageCount} 页`, `${total} total · page ${page} / ${pageCount}`)}
            </span>
            <div className="flex gap-2">
              <button
                disabled={page <= 1}
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                className="rounded border border-mast-border px-3 py-1 disabled:opacity-40 hover:text-mast-text"
              >
                {tr("上一页", "Previous")}
              </button>
              <button
                disabled={page >= pageCount}
                onClick={() => setPage((p) => Math.min(pageCount, p + 1))}
                className="rounded border border-mast-border px-3 py-1 disabled:opacity-40 hover:text-mast-text"
              >
                {tr("下一页", "Next")}
              </button>
            </div>
          </div>
        </>
      )}
    </Section>
  );
}
