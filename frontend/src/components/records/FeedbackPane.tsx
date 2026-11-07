import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";
import type { components } from "@/api/schema";
import { Badge, DegradedNote, EmptyNote, ErrorNote, Section, Spinner } from "@/components/ui";
import { Button, TextField, useToast } from "@/components/controls";
import { tr } from "../../lib/i18n";
import { fmtTime } from "./shared";

type FeedbackEntry = components["schemas"]["FeedbackEntry"];

// Feedback list: everything filed from the feedback float.
//
// `GET /api/feedback` is consumed here so filed feedback does not disappear: the
// float itself is write-only, and the per-experiment drill-down filters by
// experiment_id, so the float's own rows (which have none) would appear nowhere.
//
// Without a visible list, timestamps and a processed marker, feedback arrives as
// an undated pile and working out which entries are already closed means digging
// through the git log and issue notes. The list therefore shows the row id (so a
// reference like "#12" is unambiguous), the filing time, and whether the entry
// has been processed and in which version.

const RESOLVED_ROW = "opacity-55";

function useFeedback() {
  return useQuery({
    queryKey: ["feedback", "list"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/feedback", {
        params: { query: { limit: 500 } },
      });
      if (error) throw error;
      return data;
    },
  });
}

export function FeedbackPane() {
  const qc = useQueryClient();
  const { toast, node } = useToast();
  const q = useFeedback();
  // Pre-filled with the version being prepared: typing it per row would get
  // skipped, and a resolved row with no version is barely better than no marker.
  const [version, setVersion] = useState("v6.2.1");
  const [showResolved, setShowResolved] = useState(true);

  const mark = useMutation({
    mutationFn: async (v: { id: number; resolved: boolean }) => {
      const { data, error } = await api.POST("/api/feedback/{feedback_id}/resolved", {
        params: { path: { feedback_id: v.id } },
        body: { resolved: v.resolved, version: v.resolved ? version : "", note: "" },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (d, v) => {
      // `ok=false` means the row was not there — say so rather than showing a
      // success toast over a write that did not happen.
      if (!d?.ok) toast(tr(`第 ${v.id} 条没有更新（这一行可能已不存在）`, `Entry ${v.id} was not updated (the row may no longer exist)`));
      void qc.invalidateQueries({ queryKey: ["feedback", "list"] });
    },
    onError: () => toast(tr("标记失败", "Failed to mark")),
  });

  const items: FeedbackEntry[] = q.data?.items ?? [];
  // Newest first: the reason anyone opens this is the batch that just came in.
  const rows = [...items].reverse().filter((f) => showResolved || !f.resolved_at);
  const nOpen = items.filter((f) => !f.resolved_at).length;

  return (
    <Section
      title={`${tr("用户反馈", "Feedback")}${
        items.length
          ? tr(` (未处理 ${nOpen} / 共 ${items.length})`, ` (${nOpen} open / ${items.length} total)`)
          : ""
      }`}
      actions={
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs text-mast-muted">{tr("标记版本", "Mark as version")}</span>
          <div className="w-28">
            <TextField value={version} onChange={setVersion} placeholder="v6.2.1" mono />
          </div>
          <Button onClick={() => setShowResolved((v) => !v)}>
            {showResolved ? tr("只看未处理", "Open only") : tr("显示全部", "Show all")}
          </Button>
        </div>
      }
    >
      {node}
      {q.isPending && <Spinner />}
      {q.isError && <ErrorNote error={q.error} />}
      {q.data?.degraded && <DegradedNote what={tr("用户反馈", "feedback")} />}
      {q.data && !q.data.degraded && rows.length === 0 && (
        <EmptyNote
          label={
            items.length
              ? tr("没有未处理的反馈——全部已标记。", "No open feedback: everything is marked.")
              : tr("还没有反馈。右下角的反馈浮窗提交的内容会出现在这里。", "No feedback yet. Entries submitted from the feedback float at the bottom right appear here.")
          }
        />
      )}

      {rows.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-mast-muted">
                <th className="py-1 pr-3 font-normal">#</th>
                <th className="py-1 pr-3 font-normal">{tr("时间", "Time")}</th>
                <th className="py-1 pr-3 font-normal">{tr("内容", "Content")}</th>
                <th className="py-1 pr-3 font-normal">{tr("来自", "From")}</th>
                <th className="py-1 font-normal">{tr("状态", "Status")}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((f) => (
                <tr
                  key={f.id}
                  className={
                    "border-t border-mast-border align-top " +
                    (f.resolved_at ? RESOLVED_ROW : "")
                  }
                >
                  {/* The row id, shown, so references to an entry are unambiguous. */}
                  <td className="py-1.5 pr-3 font-mono text-xs text-mast-muted tabular-nums">
                    {f.id}
                  </td>
                  <td className="whitespace-nowrap py-1.5 pr-3 text-xs text-mast-muted tabular-nums">
                    {f.timestamp ? fmtTime(f.timestamp) : "—"}
                  </td>
                  <td className="py-1.5 pr-3 text-mast-text">
                    {f.comment || <span className="text-mast-muted">{tr("（只有评分）", "(rating only)")}</span>}
                    {f.rating && (
                      <span className="ml-2 text-xs text-mast-muted">{tr("评分", "Rating")} {f.rating}</span>
                    )}
                  </td>
                  <td className="py-1.5 pr-3 text-xs text-mast-muted">
                    {String((f.meta as Record<string, unknown>)?.page ?? f.agent ?? "—")}
                  </td>
                  <td className="py-1.5">
                    {f.resolved_at ? (
                      <div className="flex flex-wrap items-center gap-2">
                        <Badge tone="AUTO">
                          {tr(
                            `已在 ${f.resolved_version || "某版本"} 处理`,
                            `Handled in ${f.resolved_version || "an earlier version"}`,
                          )}
                        </Badge>
                        <button
                          type="button"
                          onClick={() => mark.mutate({ id: f.id, resolved: false })}
                          className="text-xs text-mast-muted hover:text-mast-text"
                          title={tr("这个症状又回来了 —— 撤销标记，重新当作未处理", "The symptom is back: remove the mark and treat it as open again")}
                        >
                          {tr("又回来了", "Reopen")}
                        </button>
                      </div>
                    ) : (
                      <button
                        type="button"
                        onClick={() => mark.mutate({ id: f.id, resolved: true })}
                        className="rounded-mast-ctl border border-mast-border px-2 py-0.5 text-xs text-mast-muted hover:text-mast-text"
                      >
                        {tr("标为已处理", "Mark handled")}
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Section>
  );
}
