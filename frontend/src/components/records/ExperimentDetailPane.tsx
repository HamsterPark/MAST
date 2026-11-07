import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import clsx from "clsx";
import { api } from "../../api/client";
import type { components } from "../../api/schema";
import { Card, DegradedNote, EmptyNote, ErrorNote, Section, Spinner } from "../ui";
import { DataTable } from "../DataTable";
import { Field, StatusPill, SuccessPill, fmtTime } from "./shared";
import { ExperimentDocuments } from "./ExperimentDocuments";
import { useCurrentScope, useSwitchSample } from "@/api/scope";
import { TipHistoryDialog } from "@/components/scope/TipControls";
import { RECORDS_DETAIL_POLL_MS } from "@/lib/pollRates";
import { newestFirst } from "@/lib/recordsOrder";
import { tr } from "@/lib/i18n";

type ExperimentDetail = components["schemas"]["ExperimentDetail"];
type SampleSummary = components["schemas"]["SampleSummary"];
type ActionSummary = components["schemas"]["ActionSummary"];
type MapMarker = components["schemas"]["MapMarker"];
type FeedbackEntry = components["schemas"]["FeedbackEntry"];
type TipInService = components["schemas"]["TipInService"];

function useExperimentDetail(id: string) {
  return useQuery({
    queryKey: ["experiment", id],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/experiments/{experiment_id}", {
        params: { path: { experiment_id: id } },
      });
      if (error) throw error;
      return data;
    },
    refetchInterval: RECORDS_DETAIL_POLL_MS,
  });
}

const MARKER_COLUMNS: ColumnDef<MapMarker, any>[] = [
  { accessorKey: "kind", header: tr("类型", "Type") },
  { accessorKey: "skill_name", header: tr("技能", "Skill"), cell: (c) => c.getValue() || "—" },
  { accessorKey: "label", header: tr("标签", "Label"), cell: (c) => c.getValue() || "—" },
  {
    accessorKey: "x_m",
    header: "X (nm)",
    cell: (c) => {
      const v = c.getValue() as number | null;
      return v == null ? "—" : (v * 1e9).toFixed(1);
    },
  },
  {
    accessorKey: "y_m",
    header: "Y (nm)",
    cell: (c) => {
      const v = c.getValue() as number | null;
      return v == null ? "—" : (v * 1e9).toFixed(1);
    },
  },
  {
    accessorKey: "status",
    header: tr("状态", "Status"),
    cell: (c) => <StatusPill status={c.getValue() as string | null} />,
  },
  { accessorKey: "source", header: tr("来源", "Source") },
];

const FEEDBACK_COLUMNS: ColumnDef<FeedbackEntry, any>[] = [
  { accessorKey: "kind", header: tr("类别", "Category") },
  { accessorKey: "rating", header: tr("评分", "Rating"), cell: (c) => c.getValue() || "—" },
  { accessorKey: "comment", header: tr("评论", "Comment"), cell: (c) => c.getValue() || "—" },
  { accessorKey: "agent", header: "Agent", cell: (c) => c.getValue() || "—" },
  {
    accessorKey: "timestamp",
    header: tr("时间", "Time"),
    cell: (c) => fmtTime(c.getValue() as string | null),
  },
];

export function ExperimentDetailPane({
  experimentId,
  onBack,
  onSelectAction,
}: {
  experimentId: string;
  onBack: () => void;
  onSelectAction: (actionId: string) => void;
}) {
  const q = useExperimentDetail(experimentId);

  const actionColumns: ColumnDef<ActionSummary, any>[] = [
    {
      accessorKey: "skill_name",
      header: tr("技能", "Skill"),
      cell: (c) => (
        <button
          className="text-mast-accent hover:underline"
          onClick={() => onSelectAction(c.row.original.id)}
        >
          {c.getValue() || tr("(未知)", "(unknown)")}
        </button>
      ),
    },
    {
      accessorKey: "success",
      header: tr("结果", "Result"),
      cell: (c) => <SuccessPill success={c.getValue() as boolean | null} />,
    },
    {
      accessorKey: "duration_s",
      header: tr("耗时(s)", "Duration (s)"),
      cell: (c) => (c.getValue() as number)?.toFixed(2) ?? "0.00",
    },
    {
      accessorKey: "error",
      header: tr("错误", "Error"),
      cell: (c) => {
        const v = c.getValue() as string | null;
        return v ? <span className="text-mast-danger">{v}</span> : "—";
      },
    },
    {
      accessorKey: "timestamp",
      header: tr("时间", "Time"),
      cell: (c) => fmtTime(c.getValue() as string | null),
    },
  ];

  return (
    <div className="space-y-6">
      <button onClick={onBack} className="text-sm text-mast-accent hover:underline">
        {tr("← 返回实验列表", "← Back to experiment list")}
      </button>

      {q.isPending && <Spinner />}
      {q.isError && <ErrorNote error={q.error} />}
      {q.data && <DetailBody data={q.data} actionColumns={actionColumns} />}
    </div>
  );
}

// Sentinel for the "all samples" navigation option — empty string,
// distinct from any real sample id, so the scope key stays a plain string.
const ALL_SAMPLES = "";

const sampleLabel = (s: SampleSummary) => s.name || `${s.id.slice(0, 8)}…`;

/** Compact "start → end" time range for a sample. Prefers the sample's own
 *  start/end columns; falls back to the min/max timestamp of its actions so a
 *  sample with no recorded range still shows a meaningful span. */
function sampleTimeRange(
  s: SampleSummary,
  actions: ActionSummary[],
): { start: string | null; end: string | null } {
  let start = s.start_time ?? null;
  let end = s.end_time ?? null;
  if (start && end) return { start, end };
  const ts = actions
    .filter((a) => a.sample_id === s.id && a.timestamp)
    .map((a) => a.timestamp as string)
    .sort();
  if (ts.length) {
    start = start ?? ts[0] ?? null;
    end = end ?? ts[ts.length - 1] ?? null;
  }
  return { start, end };
}

/** One sample navigation card — the prominent, clickable per-sample entry that
 *  scopes the timeline / markers / feedback below. Shows name + type + status +
 *  action count + time range so samples read as a real navigation dimension. */

/** "Set as current sample". Samples can be switched back and forth; switching does not end the previous one. */
function SetCurrentSample({
  experimentId,
  sampleId,
  name,
}: {
  experimentId: string;
  sampleId: string;
  name: string;
}) {
  const scope = useCurrentScope();
  const switchSmp = useSwitchSample();
  const isCurrent = scope.data?.sample?.id === sampleId;
  if (isCurrent) {
    return <span className="text-mast-accent">{tr("当前样品", "Current sample")}</span>;
  }
  return (
    <button
      className="rounded-mast-ctl border border-mast-border-strong px-2 py-0.5 text-xs hover:bg-mast-panel-2 disabled:opacity-50"
      disabled={switchSmp.isPending}
      onClick={() => switchSmp.mutate({ experimentId, sampleId })}
      title={tr(`把「${name}」设为当前样品`, `Set "${name}" as the current sample`)}
    >
      {switchSmp.isPending ? tr("切换中…", "Switching…") : tr("设为当前", "Set as current")}
    </button>
  );
}

function SampleNavCard({
  active,
  title,
  subtitle,
  status,
  count,
  range,
  onClick,
}: {
  active: boolean;
  title: string;
  subtitle?: string | null;
  status?: string | null;
  count: number;
  range?: { start: string | null; end: string | null };
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={clsx(
        "flex w-full flex-col gap-1.5 rounded-lg border p-3 text-left transition-colors",
        active
          ? "border-mast-accent bg-mast-accent/10"
          : "border-mast-border bg-mast-bg hover:border-mast-accent/50",
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <span
          className={clsx(
            "min-w-0 truncate text-sm font-medium",
            active ? "text-mast-accent" : "text-mast-text",
          )}
        >
          {title}
        </span>
        <span className="shrink-0 rounded bg-mast-border/40 px-1.5 py-0.5 text-xs text-mast-muted">
          {tr(`${count} 动作`, `${count} actions`)}
        </span>
      </div>
      <div className="flex flex-wrap items-center gap-2 text-xs text-mast-muted">
        {subtitle ? <span className="truncate">{subtitle}</span> : null}
        {status ? <StatusPill status={status} /> : null}
      </div>
      {range && (range.start || range.end) ? (
        <div className="text-xs text-mast-muted">
          {fmtTime(range.start)} → {fmtTime(range.end)}
        </div>
      ) : null}
    </button>
  );
}

function DetailBody({
  data,
  actionColumns,
}: {
  data: ExperimentDetail;
  actionColumns: ColumnDef<ActionSummary, any>[];
}) {
  // Hooks must run unconditionally — declare before the early returns below.
  const [sampleFilter, setSampleFilter] = useState<string>(ALL_SAMPLES);

  const samples = data.samples ?? [];
  const actions = data.actions ?? [];
  const markers = data.map_markers ?? [];
  const feedback = data.feedback ?? [];

  // Guard against a stale selection (sample disappeared after a refetch) so the
  // scope and the displayed data always agree.
  const effectiveFilter = samples.some((s) => s.id === sampleFilter)
    ? sampleFilter
    : ALL_SAMPLES;

  // Per-sample action counts derived from the action list — authoritative for
  // the nav cards even when a SampleSummary.action_count is stale/0.
  const countBySample = useMemo(() => {
    const m = new Map<string, number>();
    for (const a of actions) {
      if (a.sample_id) m.set(a.sample_id, (m.get(a.sample_id) ?? 0) + 1);
    }
    return m;
  }, [actions]);
  const sampleCount = (s: SampleSummary) =>
    s.action_count || countBySample.get(s.id) || 0;
  // Actions with no sample attribution are surfaced under "all samples" so nothing is lost.
  const unscopedActions = actions.filter((a) => !a.sample_id).length;

  // Filter actions / map markers / feedback by sample; "all samples" returns them unchanged.
  const filtered = useMemo(() => {
    if (!effectiveFilter) return { actions, markers, feedback };
    return {
      actions: actions.filter((a) => a.sample_id === effectiveFilter),
      markers: markers.filter((m) => m.sample_id === effectiveFilter),
      feedback: feedback.filter((f) => f.sample_id === effectiveFilter),
    };
  }, [effectiveFilter, actions, markers, feedback]);

  if (data.degraded) return <DegradedNote what={tr("实验详情", "experiment details")} />;
  if (data.found === false) {
    return <EmptyNote label={tr(`未找到实验 ${data.id}`, `Experiment ${data.id} not found`)} />;
  }

  const selectedSample =
    effectiveFilter ? samples.find((s) => s.id === effectiveFilter) : undefined;
  const scopeSuffix = effectiveFilter
    ? ` · ${tr("样品", "Sample")} ${selectedSample ? sampleLabel(selectedSample) : effectiveFilter.slice(0, 8) + "…"}`
    : "";

  return (
    <>
      <Section title={data.name || tr("(未命名实验)", "(unnamed experiment)")}>
        <Card>
          <Field label="ID">{data.id}</Field>
          <Field label={tr("目标", "Goal")}>{data.goal_text || "—"}</Field>
          {/* Status is a legacy field: experiments have no lifecycle state (the
              server's scope pointer says which one is current). Values already
              stored in old databases (superseded/completed) are still shown so
              historical data stays readable. */}
          {data.status && data.status !== "running" && (
            <Field label={tr("历史状态（旧版）", "Legacy status")}>
              <StatusPill status={data.status} />
            </Field>
          )}
          <Field label={tr("开始", "Started")}>{fmtTime(data.start_time)}</Field>
          {data.end_time && <Field label={tr("结束", "Ended")}>{fmtTime(data.end_time)}</Field>}
          {data.notes && <Field label={tr("备注", "Notes")}>{data.notes}</Field>}
        </Card>
      </Section>

      {/* Tips in service during this experiment. Placed before the samples: when
          reading a record, "which tip was used" and "what the sample was" are
          premises of the same level. */}
      <TipsInService tips={data.tips ?? []} />

      {/* Sample = first-level navigation inside an experiment. Selecting a sample
          limits the action timeline / map markers / feedback below to it; "all
          samples" shows the whole experiment. */}
      <Section title={`${tr("样品", "Samples")} (${samples.length})`}>
        {samples.length === 0 ? (
          <EmptyNote label={tr("该实验暂无样品", "This experiment has no samples")} />
        ) : (
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
            <SampleNavCard
              active={effectiveFilter === ALL_SAMPLES}
              title={tr("全部样品", "All samples")}
              subtitle={`${tr(`${samples.length} 个样品`, `${samples.length} samples`)}${
                unscopedActions
                  ? ` · ${tr(`${unscopedActions} 个未归属动作`, `${unscopedActions} unassigned actions`)}`
                  : ""
              }`}
              count={actions.length}
              range={{ start: data.start_time ?? null, end: data.end_time ?? null }}
              onClick={() => setSampleFilter(ALL_SAMPLES)}
            />
            {samples.map((s) => {
              const subtitle = [s.sample_type, s.sample_subtype]
                .filter(Boolean)
                .join(" / ") || null;
              return (
                <SampleNavCard
                  key={s.id}
                  active={effectiveFilter === s.id}
                  title={sampleLabel(s)}
                  subtitle={subtitle}
                  status={s.status}
                  count={sampleCount(s)}
                  range={sampleTimeRange(s, actions)}
                  onClick={() => setSampleFilter(s.id)}
                />
              );
            })}
          </div>
        )}
        {effectiveFilter ? (
          <div className="mt-3 flex items-center gap-2 text-sm text-mast-muted">
            <span>
              {tr("当前查看：", "Viewing: ")}
              <span className="text-mast-text">
                {selectedSample ? sampleLabel(selectedSample) : effectiveFilter}
              </span>
            </span>
            <button
              type="button"
              onClick={() => setSampleFilter(ALL_SAMPLES)}
              className="rounded border border-mast-border px-2 py-0.5 text-xs text-mast-muted hover:text-mast-text"
            >
              {tr("查看全部样品", "View all samples")}
            </button>
          </div>
        ) : null}
      </Section>

      {selectedSample && (
        <Section title={`${tr("样品详情", "Sample details")} · ${sampleLabel(selectedSample)}`}>
          <Card>
            <Field label="ID">{selectedSample.id}</Field>
            <Field label={tr("类型", "Type")}>{selectedSample.sample_type ?? "—"}</Field>
            <Field label={tr("子类型", "Subtype")}>{selectedSample.sample_subtype ?? "—"}</Field>
            {selectedSample.status && selectedSample.status !== "active" && (
              <Field label={tr("历史状态（旧版）", "Legacy status")}>
                <StatusPill status={selectedSample.status} />
              </Field>
            )}
            <Field label={tr("动作数", "Actions")}>{sampleCount(selectedSample)}</Field>
            <Field label={tr("开始", "Started")}>{fmtTime(selectedSample.start_time)}</Field>
            <Field label={tr("当前", "Current")}>
              <SetCurrentSample
                experimentId={String(data.id)}
                sampleId={String(selectedSample.id)}
                name={sampleLabel(selectedSample)}
              />
            </Field>
            {selectedSample.description && (
              <Field label={tr("描述", "Description")}>{selectedSample.description}</Field>
            )}
          </Card>
        </Section>
      )}

      <ExperimentDocuments experimentId={String(data.id)} />

      <Section title={`${tr("动作时间线", "Action timeline")} (${filtered.actions.length})${scopeSuffix}`}>
        <DataTable
          data={newestFirst(filtered.actions)}
          columns={actionColumns}
          empty={effectiveFilter ? tr("该样品暂无动作", "No actions for this sample") : tr("暂无动作", "No actions")}
        />
      </Section>

      <Section title={`${tr("地图标记", "Map markers")} (${filtered.markers.length})${scopeSuffix}`}>
        <DataTable
          data={filtered.markers}
          columns={MARKER_COLUMNS}
          empty={effectiveFilter ? tr("该样品暂无地图标记", "No map markers for this sample") : tr("暂无地图标记", "No map markers")}
        />
      </Section>

      <Section title={`${tr("用户反馈", "Feedback")} (${filtered.feedback.length})${scopeSuffix}`}>
        <DataTable
          data={filtered.feedback}
          columns={FEEDBACK_COLUMNS}
          empty={effectiveFilter ? tr("该样品暂无反馈", "No feedback for this sample") : tr("暂无反馈", "No feedback")}
        />
      </Section>
    </>
  );
}


// ── Tips in service during this experiment ──────────────────────────────────
//
// Tips are owned by the instrument domain (the `tips` table): one tip spans many
// experiments and one experiment may use several tips, so tips are siblings of
// experiments/samples, not children. What was missing was this side: when
// reading an experiment record, "which tip was used" could not be answered.
//
// This is a display-layer aggregation: no writes, no new tables. The time-window
// overlap is computed on the server (SQL; both tables live in the same
// database) because the NULL semantics (experiment not ended / tip still in
// service / install time back-filled as a plain date) fail silently as an empty
// block, which reads as "no tip change during this experiment". Those rules
// belong in one place, on the side that writes the data.

function TipsInService({ tips }: { tips: TipInService[] }) {
  const [historyFor, setHistoryFor] = useState<string | null>(null);

  return (
    <Section title={`${tr("在役针尖", "Tips in service")} (${tips.length})`}>
      {tips.length === 0 ? (
        // Worded so "nothing found" is distinguishable from "no tip change": no
        // row of the tips table covers this period, most often because tip
        // registration had not started yet.
        <EmptyNote
          label={tr(
            "这段时间里没有登记过在役的针尖(可能是登记功能启用之前的实验)",
            "No tip was registered as in service during this period (possibly an experiment from before tip registration existed)",
          )}
        />
      ) : (
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {tips.map((t) => (
            <Card key={t.id} className="space-y-1 px-3 py-2 text-xs">
              <div className="flex items-baseline justify-between gap-2">
                <span className="font-semibold text-mast-text">
                  {t.tip_index ? `T${String(t.tip_index).padStart(2, "0")} ` : ""}
                  {t.name || tr("未命名", "unnamed")}
                </span>
                <span className={t.in_service_now ? "text-mast-auto" : "text-mast-faint"}>
                  {t.in_service_now ? tr("在用", "In use") : tr("已取出", "Removed")}
                </span>
              </div>
              <div className="text-mast-muted">
                {[t.material, t.fabrication, t.form].filter(Boolean).join(" · ") || "—"}
              </div>
              <div className="text-mast-faint">
                {t.installed_at
                  ? tr(`${t.installed_at.slice(0, 10)} 装入`, `installed ${t.installed_at.slice(0, 10)}`)
                  : tr("装入日期未记录", "install date not recorded")}
                {t.removed_at
                  ? tr(` → ${t.removed_at.slice(0, 10)} 取出`, ` → removed ${t.removed_at.slice(0, 10)}`)
                  : ""}
              </div>
              {!t.overlap_exact && (
                // "Certainly in service" and "probably in service" must look
                // different. When the install time is a back-filled plain date
                // (00:00 that day) or missing (falls back to the record creation
                // time), the overlapping end is a lower bound: it errs early, so a
                // tip may be listed that was not used but none is missed.
                <div className="text-mast-warn">
                  {tr(
                    "装入时刻只精确到天(或未记录),与本实验的重叠是按下界推的",
                    "Install time is only accurate to the day (or not recorded); the overlap with this experiment is estimated as a lower bound",
                  )}
                </div>
              )}
              <button
                type="button"
                onClick={() => setHistoryFor(t.id)}
                className="text-mast-accent hover:underline"
              >
                {tr("在换针史中查看 →", "View in tip history →")}
              </button>
            </Card>
          ))}
        </div>
      )}
      {/* Link to the real tip card (the only renderer of it) instead of drawing a
          look-alike here: the card states material / fabrication / service period,
          facts that get used for decisions, and two copies would drift apart. Tips
          have no per-tip route, so the link opens the tip history and highlights
          this row. */}
      <TipHistoryDialog
        open={historyFor !== null}
        highlightId={historyFor}
        onClose={() => setHistoryFor(null)}
      />
    </Section>
  );
}
