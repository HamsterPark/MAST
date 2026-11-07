// Figure jobs: starting one, the header chip (which also watches the running -> finished edge),
// and the job bar on the Figures page.
//
// There is a single job slot. A job can be started from the lightbox, the series page, the bottom
// selection bar or the Marked page, so the "figures are ready" notification lives at the header
// level: the user must learn of it even when not on the Figures page.

import { useEffect, useRef } from "react";
import { Link } from "react-router-dom";
import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";
import { ErrorNote } from "@/components/ui";
import { viewSearch } from "@/lib/gallery/route";
import { JOB_PHASE, KIND_LABEL, describeStart, type FigureJobStatus, type FigureKind } from "@/lib/gallery/figures";
import { toast } from "../toast";
import { SMALL_BTN } from "../bits";
import { FIGURE_KEYS, useFigureStatus } from "./useFigures";
import { tr } from "@/lib/i18n";

const FIGURES_LINK = { search: `?${viewSearch({ v: "figures" })}` };

export interface FigureJobRequest {
  kind: FigureKind;
  ids?: string[];
  series?: string[];
  options?: Record<string, unknown>;
}

/** Start a figure job and report the outcome in a toast (including the case where the request succeeded but nothing started because a job is already running). */
export async function startFigureJob(qc: QueryClient, req: FigureJobRequest): Promise<boolean> {
  const prev = qc.getQueryData<FigureJobStatus>(FIGURE_KEYS.status);
  try {
    const { data, error } = await api.POST("/api/gallery/figures/run", {
      body: { kind: req.kind, ids: req.ids ?? [], series: req.series ?? [], options: req.options ?? {} },
    });
    if (error || !data) throw new Error(tr("请求失败", "Request failed"));
    if (!data.degraded) qc.setQueryData(FIGURE_KEYS.status, data);
    const d = describeStart(prev, data, req.kind);
    if (d.ok) {
      toast(
        <>
          {d.text} · <Link to={FIGURES_LINK}>{tr("去出图页", "Go to Figures")}</Link>
        </>,
      );
    } else {
      toast(d.text, "err");
    }
    return d.ok;
  } catch (e) {
    toast(tr(`出图没有开始：${(e as Error).message}`, `Figure job did not start: ${(e as Error).message}`), "err");
    return false;
  }
}

export async function cancelFigureJob(qc: QueryClient): Promise<void> {
  try {
    const { data } = await api.POST("/api/gallery/figures/cancel");
    if (data && !data.degraded) qc.setQueryData(FIGURE_KEYS.status, data);
  } catch {
    /* The next poll shows the real state */
  }
}

/** The "exporting n/N" chip in the header; when a job goes from running to not running, refresh the artifact list and notify. */
export function FigureJobChip() {
  const qc = useQueryClient();
  const st = useFigureStatus();
  const running = st.data?.running;
  const was = useRef<boolean | null>(null);
  useEffect(() => {
    if (running === undefined) return;
    if (was.current && !running) {
      void qc.invalidateQueries({ queryKey: FIGURE_KEYS.list });
      const d = st.data;
      if (d?.phase === "error") {
        toast(tr(`出图出错：${d.message || d.detail || ""}`, `Figure job failed: ${d.message || d.detail || ""}`), "err");
      } else if (d?.phase === "cancelled") {
        toast(tr("出图已取消", "Figure job cancelled"));
      } else {
        const nErr = d?.errors?.length ?? 0;
        toast(
          <>
            {tr(
              `出图完成：${d?.made?.length ?? 0} 张${nErr ? `，失败 ${nErr} 个` : ""}`,
              `Figures done: ${d?.made?.length ?? 0} made${nErr ? `, ${nErr} failed` : ""}`,
            )}{" "}
            · <Link to={FIGURES_LINK}>{tr("去出图页", "Go to Figures")}</Link>
          </>,
          nErr ? "err" : "ok",
        );
      }
    }
    was.current = running;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [running]);

  if (!running || !st.data) return null;
  const kind = st.data.kind;
  return (
    <Link
      to={FIGURES_LINK}
      className="rounded-mast-badge bg-mast-info-bg px-1.5 text-xs text-mast-info hover:no-underline"
      title={tr(`正在出图${kind ? `：${KIND_LABEL[kind]}` : ""}`, `Exporting figures${kind ? `: ${KIND_LABEL[kind]}` : ""}`)}
    >
      {tr("出图中", "Exporting")} {st.data.done}/{st.data.total}
    </Link>
  );
}

/** The job bar on the Figures page: phase, progress, count made, failures, recent log, cancel. */
export function FigureJobBar() {
  const qc = useQueryClient();
  const q = useFigureStatus();
  const st = q.data;
  if (q.isError) return <ErrorNote error={q.error} />;
  if (!st) return null;
  const pct = st.total ? Math.round((100 * st.done) / st.total) : 0;
  const errors = st.errors ?? [];
  const log = st.log ?? [];
  return (
    <div className="mt-2.5 rounded-[3px] border border-mast-border bg-mast-panel px-3 py-2.5 text-[13px]">
      {st.degraded && (
        <div className="mb-1 text-mast-warn">
          {tr("出图后端不可用：", "Figure backend unavailable: ")}
          {st.detail || tr("未知原因", "unknown reason")}
          {tr("。发起的出图会提示「没有开始」。", ". Starting a job will report that it did not start.")}
        </div>
      )}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span>
          {tr("出图任务：", "Figure job: ")}
          <b>{JOB_PHASE[st.phase] ?? st.phase}</b>
          {st.kind ? ` · ${KIND_LABEL[st.kind]}` : ""}
          {st.started ? tr(` · 开始 ${st.started}`, ` · started ${st.started}`) : ""}
          {st.finished ? tr(` · 结束 ${st.finished}`, ` · finished ${st.finished}`) : ""}
        </span>
        <span className="font-mono text-xs text-mast-muted">
          {tr(`已出 ${st.made?.length ?? 0} 张`, `${st.made?.length ?? 0} made`)}
        </span>
        <button type="button" className={SMALL_BTN} disabled={!st.running} onClick={() => void cancelFigureJob(qc)}>
          {tr("取消", "Cancel")}
        </button>
      </div>
      {st.total > 0 && (
        <div className="mt-1.5">
          <div className="h-1.5 overflow-hidden rounded bg-mast-panel-2">
            <div className="h-full bg-mast-accent transition-[width]" style={{ width: `${pct}%` }} />
          </div>
          <div className="mt-0.5 font-mono text-xs text-mast-muted">
            {st.done} / {st.total}（{pct}%）
          </div>
        </div>
      )}
      {st.message && <div className="mt-1">{st.message}</div>}
      {errors.length > 0 && (
        <details className="mt-1.5">
          <summary className="cursor-pointer text-mast-danger">
            {tr(`失败 ${errors.length} 个`, `${errors.length} failed`)}
          </summary>
          <ul className="mt-1 max-h-48 overflow-auto font-mono text-xs">
            {errors.map((e, i) => (
              <li key={`${e.id}:${i}`}>
                {e.id} — {e.why}
              </li>
            ))}
          </ul>
        </details>
      )}
      {log.length > 0 && (
        <pre className="mt-1.5 max-h-44 overflow-auto whitespace-pre-wrap rounded bg-mast-code-bg p-2 font-mono text-xs text-mast-text">
          {log.join("\n")}
        </pre>
      )}
    </div>
  );
}
