import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";
import { Badge } from "@/components/ui";
import { useToast } from "@/components/controls";
import { useWsEvent } from "@/hooks/useWsEvents";
import { marketWriteProblem, type MarketWriteResult } from "@/lib/skillMarket";
import { tr } from "@/lib/i18n";

// ── Agent skill recommendations, confirmed in place ──────────────────────────
//
// This is a banner rather than a card inside the chat bubble: bubble HTML is pre-rendered by the
// backend (`chat/render.py`) and injected with dangerouslySetInnerHTML, so React event handlers
// cannot be attached to it. The banner sits above the input box, so the user can confirm without
// leaving the conversation.
//
// The data source is `GET /api/skill-market/recommendations`, not WS frames. Frames only act as
// a trigger: the event bus replays just 100 frames, so a client that accumulates a list from
// frames would eventually show a wrong one.

type Rec = {
  id: string;
  skill: string;
  by_agent: string;
  reason: string;
  status: string;
};

export function PendingRecommendations({ compact = false }: { compact?: boolean }) {
  const qc = useQueryClient();
  const { toast, node: toastNode } = useToast();

  const q = useQuery({
    queryKey: ["skill-market", "recommendations"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/skill-market/recommendations");
      if (error) throw error;
      return data as unknown as { pending: Rec[]; degraded: boolean };
    },
  });

  useWsEvent("skill_recommendation", () => {
    qc.invalidateQueries({ queryKey: ["skill-market", "recommendations"] });
  });

  const resolve = useMutation({
    mutationFn: async (v: { id: string; accept: boolean }) => {
      const { data, error } = await api.POST("/api/skill-market/recommendations/{rec_id}/resolve", { params: { path: { rec_id: v.id } }, body: { accept: v.accept } });
      if (error) throw error;
      return { res: data as unknown as MarketWriteResult, accept: v.accept };
    },
    onSuccess: ({ res, accept }) => {
      qc.invalidateQueries({ queryKey: ["skill-market"] });
      qc.invalidateQueries({ queryKey: ["agents", "tools"] });
      qc.invalidateQueries({ queryKey: ["builder", "catalog"] });
      const problem = marketWriteProblem(res);
      if (problem) {
        // A three-state result is folded into a two-level toast by always using "err": reading
        // "queued" as "in effect" costs far more than a slightly heavy-looking message. The
        // authoritative status is shown in the Skills → Market status bar.
        toast(problem.message, "err");
        return;
      }
      toast(accept ? (res.rebuild_note || tr("已加入你的技能面", "Added to your skill set")) : tr("已拒绝，记录保留", "Declined; the record is kept"), "ok");
    },
    onError: (e: any) => toast(String(e?.message || e), "err"),
  });

  const pending = q.data?.pending ?? [];
  if (q.isPending || q.data?.degraded || pending.length === 0) return null;

  return (
    <div className="flex flex-col gap-1.5">
      {toastNode}
      {pending.map((r) => (
        <div
          key={r.id}
          className="flex flex-wrap items-center gap-2 rounded border border-mast-warn-border bg-mast-warn-bg px-2.5 py-1.5 text-xs"
        >
          <Badge tone="WARN">{tr("技能推荐", "Skill suggestion")}</Badge>
          <span className="text-mast-text">
            <b>{r.by_agent || "agent"}</b> {tr("想用", "wants to use")}{" "}
            <code className="font-mono">{r.skill}</code>
            {r.reason ? <span className="text-mast-muted">{tr("：", ": ")}{r.reason}</span> : null}
          </span>
          <span className="flex-1" />
          <button
            type="button"
            disabled={resolve.isPending}
            onClick={() => resolve.mutate({ id: r.id, accept: true })}
            className="rounded border border-mast-accent bg-mast-accent px-2 py-0.5 text-mast-accent-ink disabled:opacity-50"
          >
            {tr("加入我的技能面", "Add to my skill set")}
          </button>
          <button
            type="button"
            disabled={resolve.isPending}
            onClick={() => resolve.mutate({ id: r.id, accept: false })}
            className="rounded border border-mast-border px-2 py-0.5 text-mast-muted disabled:opacity-50"
          >
            {tr("不用", "Dismiss")}
          </button>
          {!compact && (
            <span className="text-[10px] text-mast-faint">
              {tr("在「技能 → 市场」可以看到全部", "See all under Skills → Market")}
            </span>
          )}
        </div>
      ))}
    </div>
  );
}
