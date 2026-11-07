import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "../../api/client";
import { Card, DegradedNote, Section } from "../ui";
import { Button, Field, SelectField, useToast } from "../controls";
import { tr } from "../../lib/i18n";
import { JsonBlock } from "./shared";

// Training-log export sub-tab (built-in agent trajectories) — POST /api/trajectories/export.
//   format: jsonl | sft | dpo | failure_mining
//   thread_id (jsonl only) · limit · inline · max_inline

type Fmt = "jsonl" | "sft" | "dpo" | "failure_mining";

// Export type options, mapped onto the /api/trajectories/export `format` enum
// (failure -> failure_mining).
const FORMAT_OPTIONS: { value: Fmt; label: string }[] = [
  { value: "jsonl", label: tr("全量 JSONL（每行一条完整轨迹）", "Full JSONL (one complete trajectory per line)") },
  { value: "sft", label: tr("SFT 样本（指令+上下文→步骤序列）", "SFT samples (instruction + context → step sequence)") },
  { value: "dpo", label: tr("DPO 偏好对（HITL 改参：模型 vs 人）", "DPO preference pairs (HITL parameter edits: model vs human)") },
  { value: "failure_mining", label: tr("失败挖掘（失败/中止/回滚轨迹）", "Failure mining (failed / aborted / rolled-back trajectories)") },
];

export function TrajectoryExportPane() {
  const { toast, node } = useToast();
  const [format, setFormat] = useState<Fmt>("jsonl");

  const m = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/trajectories/export", {
        body: {
          format,
          // Only the export type is user-facing; limit / max_inline use fixed
          // values here to satisfy the request schema.
          limit: 10000,
          inline: true,
          max_inline: 500,
        },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (d) => {
      if (d.ok) toast(tr(`导出完成：${d.count} 条`, `Export complete: ${d.count} items`));
      else toast(d.detail || tr("导出降级", "Export degraded"), "err");
    },
    onError: () => toast(tr("导出失败", "Export failed"), "err"),
  });

  function download() {
    const rows = m.data?.rows ?? [];
    if (!rows.length) return;
    const text = rows.map((r) => JSON.stringify(r)).join("\n");
    const blob = new Blob([text], { type: "application/x-ndjson" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `trajectories_${format}.jsonl`;
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div className="space-y-4">
      <p className="text-sm text-mast-muted">
        <strong className="text-mast-text">
          {tr("训练日志 Agent Trajectories", "Training log: agent trajectories")}
        </strong>{" "}
        {tr(
          "— 把记录的「用户指令 → 路由 → agent 思考 → skill 调用 → 安全门控 → HITL → 结果」因果轨迹导出成训练集（SFT / DPO 偏好对 / 失败挖掘）。数据来自 ",
          "— exports the recorded causal trajectories (instruction → routing → agent reasoning → skill call → safety gate → HITL → result) as training sets (SFT / DPO preference pairs / failure mining). Data comes from the trajectories / trajectory_steps tables of ",
        )}
        <code className="font-mono">mast_experiments_v2.db</code>
        {tr("的 trajectories / trajectory_steps 表，自有产权、本地存储。", ", stored locally.")}
      </p>
      <Section title={tr("训练日志", "Training log")}>
        <Card>
          <div className="grid items-end gap-4 sm:grid-cols-[3fr_1fr]">
            <Field label={tr("导出类型", "Export type")}>
              <SelectField value={format} onChange={setFormat} options={FORMAT_OPTIONS} />
            </Field>
            <Button variant="primary" onClick={() => m.mutate()} disabled={m.isPending}>
              {m.isPending ? tr("导出中…", "Exporting…") : tr("导出训练集", "Export training set")}
            </Button>
          </div>
          {m.data?.rows && m.data.rows.length > 0 && (
            <div className="mt-4">
              <Button onClick={download}>{tr("下载导出文件", "Download export")} ({m.data.rows.length})</Button>
            </div>
          )}
        </Card>
      </Section>

      {m.data?.degraded && <DegradedNote what={tr("训练日志导出", "training-log export")} />}

      {m.data && !m.data.degraded && (
        <Section title={tr("导出结果", "Export result")}>
          <Card>
            <div className="flex flex-wrap gap-x-6 gap-y-1 text-sm text-mast-muted">
              <span>{tr("格式：", "Format: ")}{m.data.format}</span>
              <span className="tabular-nums">{tr("总数：", "Total: ")}{m.data.count}</span>
              <span className="tabular-nums">{tr("内联：", "Inline: ")}{m.data.rows?.length ?? 0}</span>
              {m.data.truncated && <span className="text-mast-warn">{tr("已截断", "Truncated")}</span>}
            </div>
            {m.data.rows && m.data.rows.length > 0 && (
              <div className="mt-3 space-y-2">
                {m.data.rows.slice(0, 20).map((r, i) => (
                  <JsonBlock key={i} value={r} />
                ))}
                {m.data.rows.length > 20 && (
                  <div className="text-xs text-mast-muted">
                    {tr(
                      `仅预览前 20 条（共 ${m.data.rows.length} 条内联）；点击“下载导出文件”获取全部。`,
                      `Previewing the first 20 only (${m.data.rows.length} inline); click "Download export" to get all.`,
                    )}
                  </div>
                )}
              </div>
            )}
          </Card>
        </Section>
      )}
      {node}
    </div>
  );
}
