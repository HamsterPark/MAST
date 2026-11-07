// Experiment / sample pickers: the one click to go back to an earlier experiment.
//
// Sorted by last_active_at descending (not creation time): the experiment to return
// to may have been created last month but touched yesterday.
// /api/experiments/recent already returns them in that order.

import { useState } from "react";
import { Modal, TextField } from "../controls";
import { Spinner } from "../ui";
import { tr } from "@/lib/i18n";
import { useRecentExperiments, useSamplesOf } from "@/api/scope";

function relTime(iso?: string | null): string {
  if (!iso) return tr("从未活动", "never active");
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 90) return tr("刚刚", "just now");
  if (s < 3600) return tr(`${Math.round(s / 60)} 分钟前`, `${Math.round(s / 60)} min ago`);
  if (s < 86400) return tr(`${Math.round(s / 3600)} 小时前`, `${Math.round(s / 3600)} h ago`);
  return tr(`${Math.round(s / 86400)} 天前`, `${Math.round(s / 86400)} d ago`);
}

const rowCls =
  "flex w-full items-center justify-between gap-3 rounded-mast-ctl px-3 py-2 text-left " +
  "hover:bg-mast-panel-2 disabled:cursor-not-allowed disabled:opacity-60";

export function ExperimentPicker({
  open,
  onClose,
  onPick,
  onCreate,
  currentId,
}: {
  open: boolean;
  onClose: () => void;
  onPick: (id: string, name: string) => void;
  onCreate: () => void;
  currentId?: string | null;
}) {
  const [q, setQ] = useState("");
  const list = useRecentExperiments(q, open);

  return (
    <Modal open={open} onClose={onClose} title={tr("切换实验", "Switch experiment")}>
      <div className="space-y-3">
        <TextField value={q} onChange={setQ} placeholder={tr("按名称筛选…", "Filter by name…")} />
        <button
          className={rowCls + " border border-dashed border-mast-border-strong text-mast-accent"}
          onClick={onCreate}
        >
          {tr("＋ 新建实验", "+ New experiment")}
        </button>
        <div className="max-h-[46vh] space-y-1 overflow-auto">
          {list.isLoading && <Spinner />}
          {list.data?.experiments?.length === 0 && (
            <p className="px-3 py-6 text-center text-sm text-mast-muted">{tr("没有匹配的实验", "No matching experiments")}</p>
          )}
          {list.data?.experiments?.map((e) => {
            const isCur = e.id === currentId;
            return (
              <button
                key={e.id}
                className={rowCls + (isCur ? " bg-mast-accent-soft" : "")}
                onClick={() => onPick(e.id, e.name)}
                disabled={isCur}
                title={e.id}
              >
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-sm text-mast-text">
                    {e.name || tr("(未命名)", "(unnamed)")}
                    {isCur && <span className="ml-2 text-xs text-mast-accent">{tr("当前", "Current")}</span>}
                  </span>
                  <span className="block truncate text-xs text-mast-faint">
                    {tr(`${e.sample_count} 个样品 · ${e.action_count} 条动作`, `${e.sample_count} samples · ${e.action_count} actions`)} ·{" "}
                    {relTime(e.last_active_at)}
                  </span>
                </span>
              </button>
            );
          })}
        </div>
        <p className="text-xs text-mast-faint">
          {tr("切换实验不会结束任何实验 —— 实验永久存在，随时可以再切回来。", "Switching experiments ends none of them; they persist and you can switch back at any time.")}
        </p>
      </div>
    </Modal>
  );
}

export function SamplePicker({
  open,
  onClose,
  onPick,
  onCreate,
  experimentId,
  experimentName,
  currentId,
}: {
  open: boolean;
  onClose: () => void;
  onPick: (id: string, name: string) => void;
  onCreate: () => void;
  experimentId?: string | null;
  experimentName?: string;
  currentId?: string | null;
}) {
  const list = useSamplesOf(experimentId, open);

  return (
    <Modal open={open} onClose={onClose} title={tr(`选择样品 — ${experimentName || "当前实验"}`, `Select sample: ${experimentName || "current experiment"}`)}>
      <div className="space-y-3">
        <button
          className={rowCls + " border border-dashed border-mast-border-strong text-mast-accent"}
          onClick={onCreate}
        >
          {tr("＋ 新建样品", "+ New sample")}
        </button>
        <div className="max-h-[46vh] space-y-1 overflow-auto">
          {list.isLoading && <Spinner />}
          {list.data?.samples?.length === 0 && (
            <p className="px-3 py-6 text-center text-sm text-mast-muted">
              {tr("这个实验下还没有样品", "No samples in this experiment yet")}
            </p>
          )}
          {list.data?.samples?.map((s) => {
            const isCur = s.id === currentId;
            return (
              <button
                key={s.id}
                className={rowCls + (isCur ? " bg-mast-accent-soft" : "")}
                onClick={() => onPick(s.id, s.name)}
                disabled={isCur}
                title={s.id}
              >
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-sm text-mast-text">
                    {s.index ? `S${String(s.index).padStart(2, "0")} · ` : ""}
                    {s.name || tr("(未命名)", "(unnamed)")}
                    {isCur && <span className="ml-2 text-xs text-mast-accent">{tr("当前", "Current")}</span>}
                  </span>
                  <span className="block truncate text-xs text-mast-faint">
                    {s.sample_type || tr("未分类", "Uncategorized")} · {relTime(s.last_active_at)}
                  </span>
                </span>
              </button>
            );
          })}
        </div>
        <p className="text-xs text-mast-faint">
          {tr("样品可以来回切换 —— 换样品不会结束上一个，之前那块随时能切回来。", "Samples can be switched back and forth; changing sample does not end the previous one.")}
        </p>
      </div>
    </Modal>
  );
}
