import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "../../api/client";
import { tr } from "../../lib/i18n";
import { Card, DegradedNote, Section } from "../ui";
import { Button, Toggle, useToast } from "../controls";
import { Field as KV, fmtTime } from "./shared";

// Export-all sub-tab — POST /api/experiments/export.
//   include_heavy (model weights / caches / manuals) · dest (optional override).
//   Returns the export_all manifest roll-up. The archive layout is defined in
//   MASTv2/mast/logging/export_all.py (databases/ snapshots + experiments/ and
//   artifacts/ trees + MANIFEST.json); API keys and settings are not part of it.

function fmtBytes(b: number): string {
  if (b < 1024) return `${b} B`;
  if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`;
  if (b < 1024 * 1024 * 1024) return `${(b / 1024 / 1024).toFixed(1)} MB`;
  return `${(b / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

export function ExportAllPane() {
  const { toast, node } = useToast();
  const [includeHeavy, setIncludeHeavy] = useState(false);

  const m = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/experiments/export", {
        body: { include_heavy: includeHeavy, dest: null },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (d) => {
      if (d.ok) toast(tr(`导出完成：${d.file_count} 个文件`, `Export complete: ${d.file_count} files`));
      else toast(d.detail || tr("导出降级", "Export degraded"), "err");
    },
    onError: () => toast(tr("导出失败", "Export failed"), "err"),
  });

  return (
    <div className="space-y-4">
      <div
        role="alert"
        className="rounded-mast-ctl border border-mast-warn bg-mast-warn-bg px-4 py-3 text-sm text-mast-warn"
      >
        <p className="font-semibold">
          {tr("⚠ 仅限实验室内部使用", "⚠ For use inside your own lab only")}
        </p>
        <p className="mt-1">
          {tr(
            "导出的压缩包包含完整的实验数据、日志、对话记录、反馈和记忆数据库，不做任何隐私过滤。它不包含 API 密钥和设置。请勿在实验室之外分享，也不要公开上传。",
            "The archive contains complete experiment data, logs, conversation transcripts, feedback and memory databases, with no privacy filtering. It does not include API keys or settings. Do not share it outside the lab or upload it publicly.",
          )}
        </p>
      </div>
      <p className="text-sm text-mast-muted">
        <strong className="text-mast-text">
          {tr("📦 一键导出全部历史数据", "📦 Export all history in one click")}
        </strong>{" "}
        {tr(
          "— 把全部实验记录、实验数据、所有对话交互、评价/反馈、训练轨迹、日志、计划与快照打包成一个 ZIP（含 v1+v2 数据库的一致快照）。",
          "— packs all experiment records, experiment data, conversations, ratings/feedback, training trajectories, logs, plans and snapshots into one ZIP (including consistent snapshots of the v1 and v2 databases).",
        )}
      </p>
      <Section title={tr("导出全部", "Export all")}>
        <Card>
          <div className="flex items-start gap-2">
            <Toggle checked={includeHeavy} onChange={setIncludeHeavy} label="include_heavy" />
            <span className="text-sm text-mast-muted">
              {tr(
                "同时包含大型模型权重 / 文献索引 / 缓存（体积可达 GB 级，通常不需要）",
                "Also include large model weights / literature index / caches (can reach GB size; usually not needed)",
              )}
            </span>
          </div>
          <div className="mt-4">
            <Button variant="primary" onClick={() => m.mutate()} disabled={m.isPending}>
              {m.isPending ? tr("打包中…", "Packing…") : tr("打包导出全部历史数据", "Pack all history")}
            </Button>
          </div>
        </Card>
      </Section>

      {m.data?.degraded && <DegradedNote what={tr("全量导出", "full export")} />}

      {m.data && !m.data.degraded && (
        <Section title={tr("导出清单", "Export manifest")}>
          <Card>
            <KV label={tr("目标", "Destination")}>
              <span className="break-all font-mono text-xs">{m.data.dest || "—"}</span>
            </KV>
            <KV label={tr("文件数", "Files")}>
              <span className="tabular-nums">{m.data.file_count}</span>
            </KV>
            <KV label={tr("数据库数", "Databases")}>
              <span className="tabular-nums">{m.data.database_count}</span>
            </KV>
            <KV label={tr("总大小", "Total size")}>
              <span className="tabular-nums">{fmtBytes(m.data.total_bytes)}</span>
            </KV>
            <KV label={tr("跳过", "Skipped")}>
              <span className="tabular-nums">{m.data.skipped_count}</span>
            </KV>
            <KV label={tr("错误", "Errors")}>
              <span className="tabular-nums text-mast-danger">{m.data.error_count}</span>
            </KV>
            <KV label={tr("包含大文件", "Includes large files")}>
              {m.data.include_heavy ? tr("是", "Yes") : tr("否", "No")}
            </KV>
            <KV label={tr("创建时间", "Created")}>{fmtTime(m.data.created_at)}</KV>
            {m.data.detail && <KV label={tr("详情", "Details")}>{m.data.detail}</KV>}
          </Card>
        </Section>
      )}
      {node}
    </div>
  );
}
