import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/api/client";
import { DegradedNote, ErrorNote, Section, Spinner } from "@/components/ui";
import { DocList, type DocRow } from "@/components/records/ExperimentDocuments";
import { useCurrentScope } from "@/api/scope";
import { tr } from "@/lib/i18n";

// Reports: the written outputs of the autonomous pipeline (reports, reviews,
// plans, paper drafts).
//
// Documents live in the experiment folder (reports/ and plans/) with real
// version history and ownership. This panel is the cross-experiment overview;
// the documents of a single experiment are in Experiments -> experiment detail.
// Bodies are rendered as markdown.

const KIND_TABS: { value: string; label: string }[] = [
  { value: "", label: tr("全部", "All") },
  { value: "experiment_report", label: tr("实验报告", "Experiment reports") },
  { value: "paper_draft", label: tr("论文草稿", "Paper drafts") },
  { value: "literature_report", label: tr("文献报告", "Literature reports") },
  { value: "experiment_plan", label: tr("实验计划", "Experiment plans") },
  { value: "review", label: tr("评审", "Reviews") },
];

export function DocumentsPane() {
  const [kind, setKind] = useState("");
  const [scopedOnly, setScopedOnly] = useState(false);
  const [showDiscarded, setShowDiscarded] = useState(false);
  const scope = useCurrentScope();
  const currentExperimentId = scope.data?.experiment?.id ?? null;

  const q = useQuery({
    queryKey: ["documents", kind, scopedOnly ? currentExperimentId : null, showDiscarded],
    queryFn: async () => {
      const query: Record<string, string | boolean> = {};
      if (kind) query.kind = kind;
      if (scopedOnly && currentExperimentId) query.experiment_id = currentExperimentId;
      if (showDiscarded) query.include_discarded = true;
      const { data, error } = await api.GET("/api/documents", {
        params: { query },
      });
      if (error) throw error;
      return data;
    },
    refetchInterval: 30_000,
  });

  if (q.isLoading) return <Spinner label={tr("加载文档…", "Loading documents…")} />;
  if (q.isError) return <ErrorNote error={q.error} />;
  const payload = q.data as
    | { documents?: DocRow[]; degraded?: boolean | null }
    | undefined;
  if (payload?.degraded) return <DegradedNote what={tr("文档库", "document library")} />;

  const docs = (payload?.documents ?? []) as DocRow[];

  return (
    <Section
      title={`${tr("文档", "Documents")} (${docs.length})`}
      subtitle={tr(
        "文献报告 / 实验计划 / 实验报告 / 论文草稿 / 评审 —— 每次修订都是新版本，永不覆盖。点击展开全文。",
        "Literature reports / experiment plans / experiment reports / paper drafts / reviews. Every revision is a new version; nothing is overwritten. Click to expand.",
      )}
    >
      <div className="mb-3 flex flex-wrap items-center gap-2">
        {KIND_TABS.map((t) => (
          <button
            key={t.value}
            onClick={() => setKind(t.value)}
            // `accent-ink` is the foreground for a solid accent fill; on the
            // `accent-soft` tint (almost the page background) it would be
            // unreadable in light mode, so the active chip pairs the tint with
            // `text-mast-accent` like every other accent-soft surface.
            className={
              "rounded-full border px-2.5 py-1 text-xs " +
              (kind === t.value
                ? "border-mast-accent-line bg-mast-accent-soft font-medium text-mast-accent"
                : "border-mast-border text-mast-muted hover:bg-mast-bg/40")
            }
          >
            {t.label}
          </button>
        ))}
        <label
          className="ml-auto flex cursor-pointer items-center gap-1.5 text-xs text-mast-muted"
          title={tr("废弃的文档没有被删除，只是搬进了 _discarded 区，随时可恢复", "Discarded documents are not deleted; they are moved to the _discarded area and can be restored at any time")}
        >
          <input
            type="checkbox"
            checked={showDiscarded}
            onChange={(e) => setShowDiscarded(e.target.checked)}
          />
          {tr("含已废弃", "Include discarded")}
        </label>
        {currentExperimentId && (
          <label className="flex cursor-pointer items-center gap-1.5 text-xs text-mast-muted">
            <input
              type="checkbox"
              checked={scopedOnly}
              onChange={(e) => setScopedOnly(e.target.checked)}
            />
            {tr("只看当前实验", "Current experiment only")}
          </label>
        )}
      </div>

      {docs.length === 0 ? (
        <div className="space-y-1 text-sm text-mast-muted">
          <p>{tr("没有匹配的文档。", "No matching documents.")}</p>
          <p className="text-xs">
            {tr(
              "智能体写出的报告、综述、计划和论文草稿会保存到各自实验文件夹的",
              "Reports, reviews, plans and paper drafts written by agents are saved under ",
            )}{" "}
            <code className="font-mono text-[11px]">reports/</code> {tr("与", "and")}{" "}
            <code className="font-mono text-[11px]">plans/</code>
            {tr(" 下。没有活跃实验时保存的文档落在", " in their experiment folder. Documents saved with no active experiment go to")}{" "}
            <code className="font-mono text-[11px]">_unfiled/</code>
            {tr("，可以稍后认领。", " and can be claimed later.")}
          </p>
        </div>
      ) : (
        <DocList rows={docs} showOwner />
      )}
    </Section>
  );
}
