import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { api } from "@/api/client";
import { useCurrentScope, useSwitchExperiment } from "@/api/scope";
import { NewExperimentDialog } from "@/components/scope/NewScopeDialogs";
import type { components } from "@/api/schema";
import { DegradedNote, ErrorNote, Section, Spinner } from "@/components/ui";
import { DataTable } from "@/components/DataTable";
import { Button, SubTabs } from "@/components/controls";
import { fmtTime } from "@/components/records/shared";
import { ExperimentDetailPane } from "@/components/records/ExperimentDetailPane";
import { ActionDetailPane } from "@/components/records/ActionDetailPane";
import { TimelinePane } from "@/components/records/TimelinePane";
import { ScanDataPane } from "@/components/records/ScanDataPane";
import { GroupChatHistoryPane } from "@/components/records/GroupChatHistoryPane";
import { DocumentsPane } from "@/components/records/DocumentsPane";
import { FeedbackPane } from "@/components/records/FeedbackPane";
import { DiagnosticsPane } from "@/components/records/DiagnosticsPane";
import { TrajectoryExportPane } from "@/components/records/TrajectoryExportPane";
import { ExportAllPane } from "@/components/records/ExportAllPane";
import {
  FeedbackButton,
} from "@/components/records/ExperimentLifecycle";
import { useStickyTab } from "@/hooks/useStickyTab";
import { useLegacyFeatures } from "@/hooks/useLegacyFeatures";
import { RECORDS_LIST_POLL_MS } from "@/lib/pollRates";
import { tr } from "@/lib/i18n";

// Records page. Flat in-page SubTabs (no nested tab containers, which froze the
// old Gradio build).
//
// Sub-tabs:
//   Experiments  list (GET /api/experiments) -> detail -> action detail,
//                with switch-to-experiment and feedback actions.
//   Data         recent .sxm/.dat/.3ds scans with a PNG preview
//                (GET /api/scans/latest + /api/scans/preview).
//   Log          experiment + sample selectors -> action timeline incl. per-action
//                TCP calls and state diff (GET /api/experiments/{id}/timeline).
//   Feedback     what was sent through the feedback float (always on).
//   Group chats, Training log: built-in agent features, shown only when legacy
//                features are enabled.
//   Export all   full-history ZIP (POST /api/experiments/export).
//
// Every read renders loading / error / degraded / empty states; edits toast.

type ExperimentSummary = components["schemas"]["ExperimentSummary"];

type TopTab =
  | "experiments"
  | "data"
  | "timeline"
  | "diagnostics"
  | "groupchats"
  | "documents"
  | "feedback"
  | "training"
  | "export";

// `legacy` tabs belong to the built-in agents and are hidden unless legacy
// features are enabled.
const TABS: { id: TopTab; label: string; legacy?: boolean }[] = [
  { id: "experiments", label: tr("实验记录", "Experiments") },
  { id: "data", label: tr("数据", "Data") },
  { id: "timeline", label: tr("日志", "Log") },
  { id: "groupchats", label: tr("群聊记录", "Group chats"), legacy: true },
  { id: "diagnostics", label: tr("诊断", "Diagnostics") },
  { id: "documents", label: tr("报告", "Reports") },
  { id: "feedback", label: tr("反馈", "Feedback") },
  { id: "training", label: tr("训练日志", "Training log"), legacy: true },
  { id: "export", label: tr("导出全部（仅限实验室内部）", "Export all (lab-internal only)") },
];

// Selection state machine for the experiments drill-down.
type Selection =
  | { kind: "list" }
  | { kind: "experiment"; experimentId: string }
  | { kind: "action"; experimentId: string; actionId: string };

export default function RecordsPage() {
  // Remember the last sub-tab: several of them are places people return to, and
  // resetting to the first one on every visit means searching for them again.
  const [stickyTab, setTab] = useStickyTab<TopTab>(
    "records", TABS.map((t) => t.id), "experiments");
  const [sel, setSel] = useState<Selection>({ kind: "list" });
  const { enabled: legacy } = useLegacyFeatures();
  const tabs = TABS.filter((t) => legacy || !t.legacy);
  // A remembered tab that is currently hidden falls back to the first one.
  const tab: TopTab = tabs.some((t) => t.id === stickyTab) ? stickyTab : "experiments";

  return (
    <div className="space-y-6">
      <SubTabs tabs={tabs} value={tab} onChange={setTab} />

      {tab === "experiments" && <ExperimentsView sel={sel} setSel={setSel} />}
      {tab === "data" && <ScanDataPane />}
      {tab === "timeline" && <TimelinePane />}
      {tab === "groupchats" && <GroupChatHistoryPane />}
      {tab === "diagnostics" && <DiagnosticsPane />}
      {tab === "documents" && <DocumentsPane />}
      {tab === "feedback" && <FeedbackPane />}
      {tab === "training" && <TrajectoryExportPane />}
      {tab === "export" && <ExportAllPane />}
    </div>
  );
}

function ExperimentsView({
  sel,
  setSel,
}: {
  sel: Selection;
  setSel: (s: Selection) => void;
}) {
  if (sel.kind === "action") {
    return (
      <ActionDetailPane
        actionId={sel.actionId}
        onBack={() => setSel({ kind: "experiment", experimentId: sel.experimentId })}
      />
    );
  }
  if (sel.kind === "experiment") {
    return (
      <div className="space-y-4">
        <div className="flex flex-wrap items-center gap-2">
          {/* Experiments have no terminal state, so there is no "end experiment"
              action; the primary action is switching back to an older one. */}
          <ActivateExperimentButton experimentId={sel.experimentId} />
          <FeedbackButton experimentId={sel.experimentId} />
        </div>
        <ExperimentDetailPane
          experimentId={sel.experimentId}
          onBack={() => setSel({ kind: "list" })}
          onSelectAction={(actionId) =>
            setSel({ kind: "action", experimentId: sel.experimentId, actionId })
          }
        />
      </div>
    );
  }
  return (
    <ExperimentsList onSelect={(id) => setSel({ kind: "experiment", experimentId: id })} />
  );
}

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

/** Relative time; last activity answers "which one do I switch back to" better than start time. */
function relTime(iso?: string | null): string {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "—";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 90) return tr("刚刚", "just now");
  if (s < 3600) return tr(`${Math.round(s / 60)} 分钟前`, `${Math.round(s / 60)} min ago`);
  if (s < 86400) return tr(`${Math.round(s / 3600)} 小时前`, `${Math.round(s / 3600)} h ago`);
  return tr(`${Math.round(s / 86400)} 天前`, `${Math.round(s / 86400)} d ago`);
}


/** Primary action of the experiment detail view: make this experiment the active one. */
function ActivateExperimentButton({ experimentId }: { experimentId: string }) {
  const scope = useCurrentScope();
  const switchExp = useSwitchExperiment();
  const isCurrent = scope.data?.experiment?.id === experimentId;
  if (isCurrent) {
    return (
      <span className="rounded-mast-badge bg-mast-accent-soft px-2 py-1 text-xs text-mast-accent">
        {tr("当前实验", "Current experiment")}
      </span>
    );
  }
  return (
    <Button
      variant="primary"
      loading={switchExp.isPending}
      onClick={() => switchExp.mutate({ experimentId })}
    >
      {tr("切换到此实验", "Switch to this experiment")}
    </Button>
  );
}

/** New experiment (confirmation dialog with duplicate-name detection). */
function NewExperimentLauncher() {
  const [open, setOpen] = useState(false);
  const scope = useCurrentScope();
  return (
    <>
      <Button onClick={() => setOpen(true)}>{tr("新建实验", "New experiment")}</Button>
      <NewExperimentDialog
        open={open}
        onClose={() => setOpen(false)}
        currentName={scope.data?.experiment?.name}
      />
    </>
  );
}

function ExperimentsList({ onSelect }: { onSelect: (id: string) => void }) {
  const q = useExperiments();
  const scope = useCurrentScope();
  const switchExp = useSwitchExperiment();
  const currentId = scope.data?.experiment?.id;

  const columns: ColumnDef<ExperimentSummary, any>[] = [
    {
      accessorKey: "name",
      header: tr("名称", "Name"),
      cell: (c) => (
        <span className="flex items-center gap-2">
          <button
            className="text-mast-accent hover:underline"
            onClick={() => onSelect(c.row.original.id)}
          >
            {c.getValue() || tr("(未命名)", "(unnamed)")}
          </button>
          {c.row.original.id === currentId && (
            <span className="rounded-mast-badge bg-mast-accent-soft px-1.5 py-px text-[10.5px] text-mast-accent">
              {tr("当前", "Current")}
            </span>
          )}
        </span>
      ),
    },
    { accessorKey: "goal", header: tr("目标", "Goal"), cell: (c) => c.getValue() || "—" },
    {
      // Replaces the former status column: experiments have no lifecycle state;
      // which one is current is answered by the server's single scope pointer.
      // Legacy status values are shown on the detail page only.
      id: "switch",
      header: "",
      cell: (c) =>
        c.row.original.id === currentId ? (
          <span className="text-xs text-mast-faint">—</span>
        ) : (
          <button
            className="rounded-mast-ctl border border-mast-border-strong px-2 py-1 text-xs hover:bg-mast-panel-2 disabled:opacity-50"
            disabled={switchExp.isPending}
            onClick={() => switchExp.mutate({ experimentId: c.row.original.id })}
          >
            {tr("切换到此实验", "Switch to this experiment")}
          </button>
        ),
    },
    {
      accessorKey: "start_time",
      header: tr("开始时间", "Started"),
      cell: (c) => fmtTime(c.getValue() as string | null),
    },
    {
      id: "last_active",
      header: tr("最近活动", "Last active"),
      cell: (c) => (
        <span className="text-xs text-mast-muted">
          {relTime(
            (c.row.original as { last_active_at?: string | null }).last_active_at ??
              c.row.original.start_time,
          )}
        </span>
      ),
    },
    {
      accessorKey: "id",
      header: "ID",
      cell: (c) => (
        <span className="font-mono text-xs text-mast-muted">{c.getValue() as string}</span>
      ),
    },
  ];

  return (
    <>
      <p className="mb-3 text-sm text-mast-muted">
        <strong className="text-mast-text">
          {tr("实验记录 Provenance", "Experiment records (provenance)")}
        </strong>{" "}
        {tr(
          "— campaigns / actions / observations / claims 溯源图。数据来自 ",
          "— provenance graph of campaigns / actions / observations / claims. Data comes from ",
        )}
        <code className="font-mono">mast_experiments_v2.db</code>
        {tr("；列表自动更新。", "; the list updates by itself.")}
      </p>
    <Section
      title={`${tr("实验列表", "Experiment list")}${q.data?.count ? ` (${q.data.count})` : ""}`}
      actions={
        <div className="flex items-center gap-2">
          <NewExperimentLauncher />
        </div>
      }
    >
      {q.isPending && <Spinner />}
      {q.isError && <ErrorNote error={q.error} />}
      {q.data?.degraded && <DegradedNote what={tr("实验记录", "experiment records")} />}
      {q.data && !q.data.degraded && (
        <DataTable
          data={q.data.experiments ?? []}
          columns={columns}
          empty={tr("暂无实验记录", "No experiment records yet")}
        />
      )}
    </Section>
    </>
  );
}
