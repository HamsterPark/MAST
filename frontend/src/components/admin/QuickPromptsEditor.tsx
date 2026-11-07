import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";
import { Badge, Card, DegradedNote, ErrorNote, Spinner } from "@/components/ui";
import { Button, TextField, useToast } from "@/components/controls";
import { tr } from "@/lib/i18n";

/** Chat quick-prompt list editor (label + prompt rows). GET/POST
 *  /api/admin/quick-prompts. Empty list ⇒ reset to code defaults. No Dataframe. */

type Prompt = { label: string; prompt: string };

export function QuickPromptsEditor() {
  const queryClient = useQueryClient();
  const { toast, node } = useToast();

  const q = useQuery({
    queryKey: ["quick-prompts"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/admin/quick-prompts");
      if (error) throw error;
      return data;
    },
  });

  const stored: Prompt[] = ((q.data?.prompts ?? []) as any[]).map((p) => ({
    label: String(p.label ?? p.name ?? ""),
    prompt: String(p.prompt ?? p.text ?? ""),
  }));

  const [rows, setRows] = useState<Prompt[] | null>(null);
  useEffect(() => { setRows(null); }, [q.data]);
  const view = rows ?? stored;

  const save = useMutation({
    mutationFn: async (prompts: Prompt[]) => {
      const { data, error } = await api.POST("/api/admin/quick-prompts", {
        body: { prompts: prompts as never },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (res) => {
      setRows(null);
      queryClient.invalidateQueries({ queryKey: ["quick-prompts"] });
      res?.degraded ? toast(tr("写入未生效（内核未接入）。", "Write not applied (core not attached)."), "err") : toast(tr("已保存快速提示。", "Quick prompts saved."), "ok");
    },
    onError: (e) => toast(`${tr("保存失败：", "Save failed: ")}${String((e as Error)?.message ?? e)}`, "err"),
  });

  const persist = (next: Prompt[]) => {
    const clean = next.filter((r) => r.label.trim() || r.prompt.trim());
    save.mutate(clean);
  };

  return (
    <Card>
      <div className="mb-2 flex items-center gap-2">
        <span className="text-sm font-medium text-mast-text">{tr("聊天快速提示", "Chat quick prompts")}</span>
        {q.data?.has_override ? <Badge tone="INFO">{tr("已覆盖", "Overridden")}</Badge> : <Badge tone="AUTO">{tr("默认", "Default")}</Badge>}
      </div>
      <p className="mb-3 text-xs text-mast-muted">
        {tr(
          "编辑主聊天界面的快捷提示按钮（按钮名称 + 发送内容）。留空保存=恢复代码默认。",
          "Edit the quick-prompt buttons of the main chat (button label + text sent). Saving an empty list restores the code defaults.",
        )}
      </p>
      {q.isPending && <Spinner />}
      {q.error && <ErrorNote error={q.error} />}
      {q.data?.degraded && <DegradedNote what={tr("快速提示", "Quick prompts")} />}
      {q.data && !q.data.degraded && (
        <div className="space-y-2">
          <div className="grid grid-cols-[1fr_2fr_auto] gap-2 text-xs text-mast-muted">
            <span>{tr("按钮名称", "Button label")}</span><span>{tr("发送内容", "Text sent")}</span><span />
          </div>
          {view.map((r, i) => (
            <div key={i} className="grid grid-cols-[1fr_2fr_auto] items-center gap-2">
              <TextField value={r.label} onChange={(v) => { const n = [...view]; n[i] = { ...r, label: v }; setRows(n); }} />
              <TextField value={r.prompt} onChange={(v) => { const n = [...view]; n[i] = { ...r, prompt: v }; setRows(n); }} />
              <Button variant="danger" onClick={() => setRows(view.filter((_, j) => j !== i))}>{tr("删除", "Delete")}</Button>
            </div>
          ))}
          <Button variant="default" onClick={() => setRows([...view, { label: "", prompt: "" }])}>{tr("+ 新增提示", "+ Add prompt")}</Button>
          <div className="flex flex-wrap items-center gap-2 pt-1">
            <Button variant="primary" disabled={save.isPending} onClick={() => persist(view)}>
              {save.isPending ? tr("保存中…", "Saving…") : tr("保存", "Save")}
            </Button>
            {rows && <Button variant="ghost" onClick={() => setRows(null)}>{tr("撤销编辑", "Discard edits")}</Button>}
            <Button variant="danger" disabled={save.isPending} onClick={() => persist([])}>{tr("恢复默认", "Reset to default")}</Button>
          </div>
        </div>
      )}
      {node}
    </Card>
  );
}
