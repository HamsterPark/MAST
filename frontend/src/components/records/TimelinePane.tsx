import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import type { components } from "../../api/schema";
import { Card, DegradedNote, EmptyNote, ErrorNote, Section, Spinner } from "../ui";
import { SelectField } from "../controls";
import { tr } from "../../lib/i18n";
import { RECORDS_DETAIL_POLL_MS, RECORDS_LIST_POLL_MS } from "../../lib/pollRates";
import { newestFirst } from "../../lib/recordsOrder";
import { JsonBlock, fmtTime } from "./shared";

// Log sub-tab (history viewer):
//   experiment dropdown + sample dropdown -> action timeline with expandable
//   per-action TCP calls and before/after state diff.
//   GET /api/experiments  (dropdown choices)
//   GET /api/experiments/{id}/timeline  (entries + tcp_calls + state_diff)

type ExperimentSummary = components["schemas"]["ExperimentSummary"];
type TimelineEntry = components["schemas"]["TimelineEntry"];

function useExperiments() {
  return useQuery({
    queryKey: ["experiments"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/experiments");
      if (error) throw error;
      return data;
    },
    refetchInterval: RECORDS_LIST_POLL_MS,
  });
}

function useTimeline(experimentId: string | null) {
  return useQuery({
    enabled: !!experimentId,
    queryKey: ["experiment", experimentId, "timeline"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/experiments/{experiment_id}/timeline", {
        params: { path: { experiment_id: experimentId as string } },
      });
      if (error) throw error;
      return data;
    },
    refetchInterval: RECORDS_DETAIL_POLL_MS,
  });
}

export function TimelinePane() {
  const expQ = useExperiments();
  const [expId, setExpId] = useState<string>("");
  const [sampleId, setSampleId] = useState<string>("");
  const [oldestFirst, setOldestFirst] = useState(false);

  const experiments: ExperimentSummary[] = expQ.data?.experiments ?? [];
  const effectiveExpId = expId || experiments[0]?.id || "";
  const tlQ = useTimeline(effectiveExpId || null);

  const expOptions = [
    { value: "", label: experiments.length ? tr("（选择实验）", "(select experiment)") : tr("（无实验）", "(no experiments)") },
    ...experiments.map((e) => ({
      value: e.id,
      label: `${e.name || tr("未命名", "unnamed")} · ${fmtTime(e.start_time)} [${e.status || "—"}]`,
    })),
  ];

  const entries: TimelineEntry[] = tlQ.data?.entries ?? [];
  // Sample-id values present on the entries — feed the sample dropdown.
  const sampleIds = Array.from(
    new Set(entries.map((e) => e.sample_id).filter((s): s is string => !!s)),
  );
  const sampleOptions = [
    { value: "", label: tr("全部样品", "All samples") },
    ...sampleIds.map((s) => ({ value: s, label: s.slice(0, 8) + "…" })),
  ];
  const filtered = sampleId ? entries.filter((e) => e.sample_id === sampleId) : entries;
  // Step numbers count in execution order (1 = first action) in either display order.
  const latestFirst = newestFirst(filtered);
  const shown = latestFirst.map((entry, i) => ({ entry, step: latestFirst.length - i }));
  if (oldestFirst) shown.reverse();

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-4">
        <div className="min-w-64">
          <div className="mb-1 text-xs text-mast-muted">{tr("Experiment", "Experiment")}</div>
          <SelectField
            value={effectiveExpId}
            onChange={(v) => {
              setExpId(v);
              setSampleId("");
            }}
            options={expOptions}
          />
        </div>
        <div className="min-w-48">
          <div className="mb-1 text-xs text-mast-muted">{tr("Sample", "Sample")}</div>
          <SelectField value={sampleId} onChange={setSampleId} options={sampleOptions} />
        </div>
        <button
          type="button"
          onClick={() => setOldestFirst((v) => !v)}
          className="rounded border border-mast-border px-3 py-1 text-sm text-mast-muted hover:text-mast-text"
        >
          {oldestFirst ? tr("当前：最早在前", "Showing: oldest first") : tr("当前：最新在前", "Showing: newest first")}
        </button>
        <span className="pb-1 text-xs text-mast-faint">
          {tr("自动更新", "Updates automatically")}
        </span>
      </div>

      {expQ.isError && <ErrorNote error={expQ.error} />}
      {tlQ.isPending && effectiveExpId && <Spinner />}
      {tlQ.isError && <ErrorNote error={tlQ.error} />}
      {tlQ.data?.degraded && <DegradedNote what={tr("实验时间线", "experiment timeline")} />}

      {tlQ.data && !tlQ.data.degraded && (
        <>
          <Section
            title={`${tr("时间线", "Timeline")} — ${tlQ.data.name || effectiveExpId.slice(0, 8) + "…"}`}
          >
            <Card>
              <div className="flex flex-wrap gap-x-6 gap-y-1 text-sm text-mast-muted">
                <span>{tr("状态：", "Status: ")}{tlQ.data.status || "—"}</span>
                <span className="tabular-nums">{tr("动作：", "Actions: ")}{tlQ.data.action_count}</span>
                <span className="tabular-nums text-mast-auto">{tr("成功：", "Succeeded: ")}{tlQ.data.succeeded}</span>
                <span className="tabular-nums text-mast-danger">{tr("失败：", "Failed: ")}{tlQ.data.failed}</span>
                <span className="tabular-nums">
                  {tr("总耗时：", "Total time: ")}{tlQ.data.total_duration_s.toFixed(1)}s
                </span>
              </div>
              {tlQ.data.goal_text && (
                <div className="mt-2 text-sm text-mast-text">{tlQ.data.goal_text}</div>
              )}
            </Card>
          </Section>

          {shown.length === 0 ? (
            <EmptyNote label={tr("暂无动作", "No actions")} />
          ) : (
            <ol className="relative ml-1 space-y-2.5 border-l border-mast-border pl-0">
              {shown.map(({ entry, step }) => (
                <TimelineRow key={entry.id} entry={entry} step={step} />
              ))}
            </ol>
          )}
        </>
      )}
    </div>
  );
}

function fmtParams(params?: { [k: string]: unknown }): string {
  if (!params) return "";
  return Object.entries(params)
    .map(([k, v]) => `${k}=${typeof v === "string" ? `"${v}"` : String(v)}`)
    .join(", ");
}

// tiny inline lucide-style glyphs (no runtime dep) ──────────────────────────
function IconCheck({ size = 11 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="3"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M20 6 9 17l-5-5" />
    </svg>
  );
}
function IconX({ size = 11 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="3"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M18 6 6 18M6 6l12 12" />
    </svg>
  );
}
// IC wrench — the instrument-control agent these timeline actions belong to.
function IconWrench({ size = 11 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18l3 3 6.3-6.3a4 4 0 0 0 5.4-5.4l-2.5 2.5-2.9-.6-.6-2.9z" />
    </svg>
  );
}

function TimelineRow({ entry, step }: { entry: TimelineEntry; step: number }) {
  const [open, setOpen] = useState(false);
  const ok = entry.success !== false;
  const tcp = entry.tcp_calls ?? [];
  const diff = entry.state_diff;
  const hasDetail = tcp.length > 0 || !!diff?.before || !!diff?.after || !!entry.context;
  const ts =
    entry.timestamp && entry.timestamp.length >= 19 ? entry.timestamp.slice(11, 19) : "—";

  return (
    <li className="relative pl-6">
      {/* rail node — agent-colored (IC) badge sitting on the vertical rail */}
      <span className="absolute -left-[9px] top-2.5 flex h-[18px] w-[18px] items-center justify-center rounded-full border border-mast-ag-ic bg-mast-panel text-mast-ag-ic">
        <IconWrench size={10} />
      </span>

      <div className="overflow-hidden rounded-mast-card border border-mast-border bg-mast-panel shadow-mast">
        <button
          className="flex w-full items-center gap-2 px-3.5 py-2.5 text-left text-sm"
          onClick={() => hasDetail && setOpen((o) => !o)}
        >
          {/* step number badge */}
          <span className="flex h-[18px] min-w-[18px] items-center justify-center rounded-full bg-mast-panel-2 px-1 font-mono text-[10.5px] tabular-nums text-mast-faint">
            {step}
          </span>
          {/* mono timestamp */}
          <span className="w-16 font-mono text-xs tabular-nums text-mast-muted">{ts}</span>
          {/* semantic status pill (triple + icon) */}
          <span
            className={
              "inline-flex items-center gap-1 rounded-mast-badge border px-2 py-0.5 text-xs " +
              (ok
                ? "border-mast-auto-border bg-mast-auto-bg text-mast-auto"
                : "border-mast-danger-border bg-mast-danger-bg text-mast-danger")
            }
          >
            {ok ? <IconCheck /> : <IconX />}
            {ok ? tr("成功", "Success") : tr("失败", "Failed")}
          </span>
          <code className="truncate font-mono text-mast-text">
            <span className="text-mast-ag-ic">{entry.skill_name || tr("(未知)", "(unknown)")}</span>
            <span className="text-mast-faint">({fmtParams(entry.parameters)})</span>
          </code>
          {entry.error && (
            <span className="truncate text-mast-danger">{entry.error.slice(0, 60)}</span>
          )}
          <span className="ml-auto whitespace-nowrap font-mono text-xs tabular-nums text-mast-muted">
            {entry.duration_s.toFixed(2)}s
          </span>
          {hasDetail && (
            <span className="w-3 shrink-0 text-center text-mast-faint">{open ? "−" : "+"}</span>
          )}
        </button>

        {open && hasDetail && (
          <div className="space-y-2 border-t border-mast-border bg-mast-panel-2/30 px-3.5 py-2.5 text-xs">
            {tcp.length > 0 && (
              <div>
                <div className="mb-1 font-medium text-mast-faint">
                  {tr("TCP 调用", "TCP calls")} <span className="font-mono tabular-nums">({tcp.length})</span>
                </div>
                <div className="overflow-hidden rounded-mast-ctl border border-mast-border bg-mast-code-bg">
                  {tcp.map((c, i) => (
                    <div
                      key={i}
                      className="border-t border-mast-border px-2.5 py-1.5 font-mono first:border-t-0"
                    >
                      <code className="text-mast-accent">{c.method}</code>
                      <code className="text-mast-text">({c.args})</code>
                      <span className="ml-2 tabular-nums text-mast-muted">
                        {c.elapsed_s.toFixed(3)}s
                      </span>
                      {c.error && <span className="ml-2 text-mast-danger">ERR: {c.error}</span>}
                    </div>
                  ))}
                </div>
              </div>
            )}
            {(diff?.before || diff?.after) && (
              <div className="grid gap-2 sm:grid-cols-2">
                <div>
                  <div className="mb-1 font-medium text-mast-faint">before</div>
                  <JsonBlock value={diff?.before ?? null} />
                </div>
                <div>
                  <div className="mb-1 font-medium text-mast-faint">after</div>
                  <JsonBlock value={diff?.after ?? null} />
                </div>
              </div>
            )}
            {entry.context && (
              <div>
                <div className="mb-1 font-medium text-mast-faint">context</div>
                <div className="text-mast-text">{entry.context}</div>
              </div>
            )}
          </div>
        )}
      </div>
    </li>
  );
}
