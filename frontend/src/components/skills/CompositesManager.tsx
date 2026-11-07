import { useMemo, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { type ColumnDef } from "@tanstack/react-table";
import { api } from "@/api/client";
import { DataTable } from "@/components/DataTable";
import {
  Section,
  Card,
  Badge,
  Spinner,
  ErrorNote,
  DegradedNote,
  EmptyNote,
} from "@/components/ui";
import { Field, TextField, Button, Modal, useToast } from "@/components/controls";
import { CompositeDag } from "./CompositeDag";
import type { SpecNode } from "./compositeGraph";
import { tr } from "@/lib/i18n";

// ── Composites — list + react-flow DAG + version history + clone/restore ──
//   composite list (table) · metadata + flow chart (DAG) + parameters ·
//   version history + one-click restore · clone as new skill.
// NOTE: the API has no delete endpoint (no DELETE /api/composites/{name}), so delete is not offered.

const SAFETY_TONE: Record<string, string> = {
  auto: "AUTO",
  confirm: "WARN",
  dangerous: "DANGEROUS",
  AUTO: "AUTO",
  WARN: "WARN",
  DANGEROUS: "DANGEROUS",
};

type CompRow = {
  name: string;
  version: number;
  description: string;
  safety_level: string;
  n_nodes: number;
  tags?: unknown[];
};

function useComposites() {
  return useQuery({
    queryKey: ["composites"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/composites");
      if (error) throw error;
      return data;
    },
  });
}

function useComposite(name: string | null) {
  return useQuery({
    enabled: !!name,
    queryKey: ["composite", name],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/composites/{name}", {
        params: { path: { name: name! } },
      });
      if (error) throw error;
      return data;
    },
  });
}

export function CompositesManager() {
  const qc = useQueryClient();
  const { toast, node: toastNode } = useToast();
  const q = useComposites();

  const [text, setText] = useState("");
  const [selected, setSelected] = useState<string | null>(null);

  // clone modal
  const [cloneOpen, setCloneOpen] = useState(false);
  const [cloneName, setCloneName] = useState("");
  const [cloneAuthor, setCloneAuthor] = useState("");

  const rows = (q.data?.composites ?? []) as CompRow[];

  const cloneMut = useMutation({
    mutationFn: async () => {
      if (!selected) throw new Error(tr("未选择复合技能", "No composite selected"));
      if (!cloneName.trim()) throw new Error(tr("请填写新技能名称", "Enter a name for the new skill"));
      const { data, error } = await api.POST("/api/composites/{name}/clone", {
        params: { path: { name: selected } },
        body: { new_name: cloneName.trim(), author: cloneAuthor.trim() },
      });
      if (error) throw error;
      if (!data?.ok) throw new Error(data?.error || tr("克隆失败", "Clone failed"));
      return data;
    },
    onSuccess: (d) => {
      qc.invalidateQueries({ queryKey: ["composites"] });
      setCloneOpen(false);
      setCloneName("");
      setCloneAuthor("");
      toast(d?.message || tr("已克隆", "Cloned"), "ok");
      if (d?.name) setSelected(d.name);
    },
    onError: (e) => toast(e instanceof Error ? e.message : tr("克隆失败", "Clone failed"), "err"),
  });

  const restoreMut = useMutation({
    mutationFn: async (version: number) => {
      if (!selected) throw new Error(tr("未选择复合技能", "No composite selected"));
      const { data, error } = await api.POST(
        "/api/composites/{name}/restore/{version}",
        { params: { path: { name: selected, version } } },
      );
      if (error) throw error;
      if (!data?.ok) throw new Error(data?.error || tr("回滚失败", "Restore failed"));
      return data;
    },
    onSuccess: (d) => {
      qc.invalidateQueries({ queryKey: ["composites"] });
      qc.invalidateQueries({ queryKey: ["composite", selected] });
      toast(d?.message || tr("已回滚", "Restored"), "ok");
    },
    onError: (e) => toast(e instanceof Error ? e.message : tr("回滚失败", "Restore failed"), "err"),
  });

  const columns = useMemo<ColumnDef<CompRow, any>[]>(
    () => [
      {
        accessorKey: "name",
        header: tr("名称", "Name"),
        cell: (c) => <span className="font-medium text-mast-text">{c.getValue() as string}</span>,
      },
      { accessorKey: "version", header: tr("版本", "Version"), cell: (c) => `v${c.getValue() as number}` },
      {
        accessorKey: "safety_level",
        header: tr("安全等级", "Safety level"),
        cell: (c) => (
          <Badge tone={SAFETY_TONE[c.getValue() as string] ?? "default"}>{c.getValue() as string}</Badge>
        ),
      },
      { accessorKey: "n_nodes", header: tr("节点数", "Nodes") },
      { accessorKey: "description", header: tr("说明", "Description") },
      {
        id: "open",
        header: "",
        cell: (c) => (
          <button
            onClick={() => setSelected(c.row.original.name)}
            className="rounded border border-mast-border px-2 py-0.5 text-xs text-mast-accent hover:bg-mast-accent/10"
          >
            {tr("查看", "View")}
          </button>
        ),
      },
    ],
    [],
  );

  if (q.isPending) return <Spinner />;
  if (q.isError) return <ErrorNote error={q.error} />;
  if (q.data?.degraded) return <DegradedNote what={tr("复合技能列表", "Composite list")} />;

  return (
    <div className="space-y-4">
      {toastNode}
      <div className="flex flex-wrap items-center gap-2">
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={tr("搜索复合技能…", "Search composites…")}
          className="w-64 rounded border border-mast-border bg-mast-bg px-3 py-1.5 text-sm text-mast-text placeholder:text-mast-muted"
        />
        <span className="text-xs text-mast-muted">{tr(`共 ${rows.length} 个`, `${rows.length} total`)}</span>
      </div>

      <DataTable data={rows} columns={columns} globalFilter={text} empty={tr("暂无复合技能", "No composite skills")} />

      {selected && (
        <CompositeDetailPanel
          name={selected}
          onClose={() => setSelected(null)}
          onClone={() => {
            setCloneName("");
            setCloneAuthor("");
            setCloneOpen(true);
          }}
          onRestore={(v) => restoreMut.mutate(v)}
          restoring={restoreMut.isPending}
        />
      )}

      <Modal open={cloneOpen} onClose={() => setCloneOpen(false)} title={tr(`克隆为新复合技能（源：${selected}）`, `Clone as new composite (source: ${selected})`)}>
        <div className="space-y-3">
          <Field label={tr("新技能名称", "New skill name")} hint={tr("将作为新蓝本（v1），可在列表中选择并编辑", "Becomes a new blueprint (v1); select and edit it from the list")}>
            <TextField value={cloneName} onChange={setCloneName} placeholder="MyNewWorkflow" />
          </Field>
          <Field label={tr("作者（可选）", "Author (optional)")}>
            <TextField value={cloneAuthor} onChange={setCloneAuthor} placeholder="" />
          </Field>
          <div className="flex justify-end gap-2 pt-1">
            <Button variant="ghost" onClick={() => setCloneOpen(false)}>{tr("取消", "Cancel")}</Button>
            <Button variant="primary" disabled={cloneMut.isPending} onClick={() => cloneMut.mutate()}>
              {cloneMut.isPending ? tr("克隆中…", "Cloning…") : tr("克隆", "Clone")}
            </Button>
          </div>
        </div>
      </Modal>
    </div>
  );
}

function CompositeDetailPanel({
  name,
  onClose,
  onClone,
  onRestore,
  restoring,
}: {
  name: string;
  onClose: () => void;
  onClone: () => void;
  onRestore: (version: number) => void;
  restoring: boolean;
}) {
  const qc = useQueryClient();
  const { toast, node: toastNode } = useToast();
  const q = useComposite(name);

  const validateMut = useMutation({
    mutationFn: async () => {
      const spec = (q.data?.spec ?? {}) as Record<string, unknown>;
      const { data, error } = await api.POST("/api/composites/{name}/validate", {
        params: { path: { name } },
        body: { spec },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (d) => {
      if (d?.degraded) toast(tr("校验后端不可用（degraded）", "Validation backend unavailable (degraded)"), "err");
      else if (d?.ok) toast(tr("校验通过，无问题", "Validation passed, no problems"), "ok");
      else toast(tr(`校验发现 ${d?.problems?.length ?? 0} 个问题`, `Validation found ${d?.problems?.length ?? 0} problem(s)`), "err");
      qc.setQueryData(["composite", name, "validate"], d);
    },
    onError: (e) => toast(e instanceof Error ? e.message : tr("校验失败", "Validation failed"), "err"),
  });

  return (
    <Section
      title={tr(`复合技能详情：${name}`, `Composite details: ${name}`)}
      actions={
        <button onClick={onClose} className="text-xs text-mast-muted hover:text-mast-text">{tr("关闭", "Close")}</button>
      }
    >
      {toastNode}
      {q.isPending ? (
        <Spinner />
      ) : q.isError ? (
        <ErrorNote error={q.error} />
      ) : q.data?.degraded ? (
        <DegradedNote what={tr("复合技能详情", "Composite details")} />
      ) : !q.data?.found ? (
        <EmptyNote label={tr(`未找到复合技能：${name}`, `Composite not found: ${name}`)} />
      ) : (
        (() => {
          const d = q.data!;
          const spec = (d.spec ?? null) as SpecNode | null;
          const version = spec ? Number(spec.version ?? 0) : 0;
          const safety = spec ? String(spec.safety_level ?? "") : "";
          const description = spec ? String(spec.description ?? "") : "";
          const tags = spec && Array.isArray(spec.tags) ? (spec.tags as unknown[]) : [];
          const params = spec && Array.isArray(spec.params) ? (spec.params as Record<string, unknown>[]) : [];
          const valReport = qc.getQueryData(["composite", name, "validate"]) as
            | { ok?: boolean; problems?: string[]; steps?: { id: string; skill: string; errors: string[]; warnings: string[] }[]; degraded?: boolean }
            | undefined;

          return (
            <div className="space-y-4">
              <Card className="space-y-3">
                <div className="flex flex-wrap items-center gap-2">
                  <h3 className="text-base font-semibold text-mast-text">{d.name}</h3>
                  {version > 0 && <Badge>v{version}</Badge>}
                  {safety && <Badge tone={SAFETY_TONE[safety] ?? "default"}>{safety}</Badge>}
                </div>
                {description && <p className="text-sm text-mast-text/90">{description}</p>}
                {!!tags.length && (
                  <div className="flex flex-wrap gap-1.5">
                    {tags.map((t, i) => (
                      <span key={i} className="rounded bg-mast-bg px-1.5 py-0.5 text-xs text-mast-muted">{String(t)}</span>
                    ))}
                  </div>
                )}
                {!!params.length && (
                  <div>
                    <h4 className="mb-1 text-sm font-medium text-mast-text">{tr("输入参数", "Input parameters")}</h4>
                    <div className="flex flex-wrap gap-2 text-xs text-mast-muted">
                      {params.map((p, i) => (
                        <span key={i} className="rounded border border-mast-border px-2 py-1">
                          {String(p.name ?? "")}
                          <span className="opacity-60"> : {String(p.type ?? "")}</span>
                          {p.default != null && <span className="opacity-60"> = {String(p.default)}</span>}
                          {p.required ? <span className="ml-1 text-mast-danger">*</span> : null}
                        </span>
                      ))}
                    </div>
                  </div>
                )}
                <div className="flex flex-wrap gap-2 pt-1">
                  <Button variant="primary" onClick={onClone}>{tr("克隆为新技能", "Clone as new skill")}</Button>
                  <Button variant="default" disabled={validateMut.isPending} onClick={() => validateMut.mutate()}>
                    {validateMut.isPending ? tr("校验中…", "Validating…") : tr("校验流程", "Validate workflow")}
                  </Button>
                </div>
              </Card>

              {valReport && (
                <Card className="space-y-2">
                  <h4 className="text-sm font-medium text-mast-text">
                    {tr("校验结果", "Validation result")} {valReport.ok ? <Badge tone="AUTO">{tr("通过", "Passed")}</Badge> : <Badge tone="WARN">{tr("有问题", "Problems found")}</Badge>}
                  </h4>
                  {!!valReport.problems?.length && (
                    <ul className="list-disc space-y-0.5 pl-5 text-xs text-mast-danger">
                      {valReport.problems.map((p, i) => <li key={i}>{p}</li>)}
                    </ul>
                  )}
                  {!!valReport.steps?.length && (
                    <div className="overflow-auto rounded border border-mast-border">
                      <table className="w-full text-xs">
                        <thead className="bg-mast-bg text-mast-muted">
                          <tr>
                            <th className="px-2 py-1.5 text-left">{tr("节点", "Node")}</th>
                            <th className="px-2 py-1.5 text-left">{tr("技能", "Skill")}</th>
                            <th className="px-2 py-1.5 text-left">{tr("错误", "Errors")}</th>
                            <th className="px-2 py-1.5 text-left">{tr("警告", "Warnings")}</th>
                          </tr>
                        </thead>
                        <tbody>
                          {valReport.steps.map((s) => (
                            <tr key={s.id} className="border-t border-mast-border">
                              <td className="px-2 py-1.5 font-mono">{s.id}</td>
                              <td className="px-2 py-1.5 font-mono">{s.skill}</td>
                              <td className="px-2 py-1.5 text-mast-danger">{s.errors.join("; ") || "—"}</td>
                              <td className="px-2 py-1.5 text-mast-warn">{s.warnings.join("; ") || "—"}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </Card>
              )}

              <Card>
                <h4 className="mb-2 text-sm font-medium text-mast-text">{tr("控制流 DAG", "Control-flow DAG")}</h4>
                <CompositeDag spec={spec} />
              </Card>

              <Card>
                <h4 className="mb-2 text-sm font-medium text-mast-text">{tr("版本历史", "Version history")}</h4>
                {!d.versions?.length ? (
                  <EmptyNote label={tr("暂无版本历史", "No version history")} />
                ) : (
                  <div className="overflow-auto rounded border border-mast-border">
                    <table className="w-full text-xs">
                      <thead className="bg-mast-bg text-mast-muted">
                        <tr>
                          <th className="px-2 py-1.5 text-left">{tr("版本", "Version")}</th>
                          <th className="px-2 py-1.5 text-left">{tr("保存时间", "Saved at")}</th>
                          <th className="px-2 py-1.5 text-left">{tr("节点数", "Nodes")}</th>
                          <th className="px-2 py-1.5 text-left">{tr("说明", "Description")}</th>
                          <th className="px-2 py-1.5 text-left">{tr("操作", "Action")}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {d.versions.map((v) => (
                          <tr key={v.version} className="border-t border-mast-border">
                            <td className="px-2 py-1.5 font-medium">v{v.version}</td>
                            <td className="px-2 py-1.5 text-mast-muted">{v.saved_at || "—"}</td>
                            <td className="px-2 py-1.5 text-mast-muted tabular-nums">{v.n_nodes}</td>
                            <td className="px-2 py-1.5 text-mast-muted">{v.description || "—"}</td>
                            <td className="px-2 py-1.5">
                              <button
                                disabled={restoring}
                                onClick={() => onRestore(v.version)}
                                className="rounded border border-mast-border px-2 py-0.5 text-xs text-mast-accent hover:bg-mast-accent/10 disabled:opacity-50"
                              >
                                {tr("回滚到此版本", "Restore this version")}
                              </button>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </Card>
            </div>
          );
        })()
      )}
    </Section>
  );
}
