import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";
import { Badge, Card, DegradedNote, EmptyNote, ErrorNote, Spinner } from "@/components/ui";
import { Button, useToast } from "@/components/controls";
import { tr } from "@/lib/i18n";

/** Override history: every override category's current payload plus timestamped
 *  backups, with restore-by-timestamp (ConfigOverrideRegistry history).
 *  GET /api/admin/overrides/{category}, /history; POST .../restore/{ts}. */

const CATEGORIES: { id: string; label: string }[] = [
  { id: "safety_limits", label: tr("安全限值", "Safety limits") },
  { id: "checks", label: tr("全局检查规则", "Global check rules") },
  { id: "constraints", label: tr("材料约束", "Material constraints") },
  { id: "skill", label: tr("技能覆盖", "Skill overrides") },
  { id: "knowledge", label: tr("知识库", "Knowledge base") },
  { id: "guidance", label: tr("技能指导", "Skill guidance") },
  { id: "encyclopedia", label: tr("百科配置", "Encyclopedia config") },
  { id: "agent", label: tr("智能体", "Agent") },
];

export function OverrideHistory() {
  const queryClient = useQueryClient();
  const { toast, node } = useToast();
  const [category, setCategory] = useState(CATEGORIES[0]!.id);

  const ovrQ = useQuery({
    queryKey: ["override", category],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/admin/overrides/{category}", {
        params: { path: { category } },
      });
      if (error) throw error;
      return data;
    },
  });

  const histQ = useQuery({
    queryKey: ["override", category, "history"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/admin/overrides/{category}/history", {
        params: { path: { category } },
      });
      if (error) throw error;
      return data;
    },
  });

  const restore = useMutation({
    mutationFn: async (ts: string) => {
      const { data, error } = await api.POST("/api/admin/overrides/{category}/restore/{ts}", {
        params: { path: { category, ts } },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (res) => {
      queryClient.invalidateQueries({ queryKey: ["override", category] });
      queryClient.invalidateQueries({ queryKey: ["override", category, "history"] });
      queryClient.invalidateQueries({ queryKey: ["safety", "limits"] });
      res?.degraded ? toast(tr("恢复未生效（内核未接入）。", "Restore not applied (core not attached)."), "err")
        : toast(res?.reloaded ? tr("已恢复并热重载。", "Restored and hot-reloaded.") : tr("已恢复。", "Restored."), "ok");
    },
    onError: (e) => toast(`${tr("恢复失败：", "Restore failed: ")}${String((e as Error)?.message ?? e)}`, "err"),
  });

  return (
    <div>
      <div className="mb-3 flex flex-wrap gap-2">
        {CATEGORIES.map((c) => (
          <button
            key={c.id}
            onClick={() => setCategory(c.id)}
            className={
              "rounded-md border px-3 py-1.5 text-sm " +
              (category === c.id
                ? "border-mast-accent bg-mast-accent/15 text-mast-accent"
                : "border-mast-border text-mast-muted hover:text-mast-text")
            }
          >
            {c.label}
          </button>
        ))}
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card>
          <div className="mb-2 flex items-center gap-2">
            <span className="text-sm font-medium text-mast-text">{tr("当前覆盖内容", "Current override")}</span>
            {ovrQ.data && !ovrQ.data.degraded &&
              (ovrQ.data.has_override ? <Badge tone="INFO">{tr("已覆盖", "Overridden")}</Badge> : <Badge tone="AUTO">{tr("使用默认", "Using default")}</Badge>)}
          </div>
          {ovrQ.isPending && <Spinner />}
          {ovrQ.error && <ErrorNote error={ovrQ.error} />}
          {ovrQ.data?.degraded && <DegradedNote what={tr("覆盖读取", "Override read")} />}
          {ovrQ.data && !ovrQ.data.degraded && (
            <pre className="max-h-96 overflow-auto rounded-md bg-mast-bg p-3 text-xs text-mast-text">
              {JSON.stringify(ovrQ.data.data ?? {}, null, 2)}
            </pre>
          )}
        </Card>

        <Card>
          <span className="mb-2 block text-sm font-medium text-mast-text">{tr("历史备份", "Backup history")}</span>
          {histQ.isPending && <Spinner />}
          {histQ.error && <ErrorNote error={histQ.error} />}
          {histQ.data?.degraded && <DegradedNote what={tr("历史读取", "History read")} />}
          {histQ.data && !histQ.data.degraded && (
            (histQ.data.entries ?? []).length === 0 ? (
              <EmptyNote label={tr("暂无历史备份", "No backups yet")} />
            ) : (
              <ul className="space-y-1">
                {histQ.data.entries!.map((e) => (
                  <li key={e.timestamp} className="flex items-center justify-between gap-2 rounded-md border border-mast-border px-2 py-1.5 text-sm">
                    <div className="min-w-0">
                      <div className="font-mono text-xs text-mast-text">{e.timestamp}</div>
                      <div className="truncate text-xs text-mast-muted">{e.filename}</div>
                    </div>
                    <Button variant="primary" disabled={restore.isPending} onClick={() => restore.mutate(e.timestamp)}>
                      {tr("恢复", "Restore")}
                    </Button>
                  </li>
                ))}
              </ul>
            )
          )}
        </Card>
      </div>
      {node}
    </div>
  );
}
