// Line-scan figure dialog.
//
// Which series are blocks of the same line, which station is labelled V/D, and from which
// spectrum on the tip changed are chosen by the user here: the default grouping is suggested from
// the series names (suggestLineGroups in lib/gallery/figures.ts), and the line name, station
// labels and each block's "tip changed from" index can all be edited. "Dry-run stations" runs the
// server's station inference using only the coordinates in the index, so you can see how many
// stations each block maps to and whether anything fails to match before exporting.
//
// When the ticked set is exactly the same as a previous export (order-independent), that
// export's parameters pre-fill the form.

import { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { api } from "@/api/client";
import type { GalleryItem } from "@/lib/gallery/types";
import { RLAB, num } from "@/lib/gallery/format";
import { seriesMembers } from "@/lib/gallery/series";
import {
  allEntries,
  blockLabel,
  commonLineName,
  findPrefill,
  fmtNumber,
  formFromOptions,
  groupFor,
  stsLinesOptions,
  suggestLineGroups,
  type StsLinePlan,
  type StsLinesForm,
} from "@/lib/gallery/figures";
import type { GalleryModel } from "../useGalleryData";
import { useMarksStore } from "../marksStore";
import { SMALL_BTN, SMALL_SELECT } from "../bits";
import { startFigureJob } from "./figureJob";
import { useGalleryFigures } from "./useFigures";
import { tr } from "@/lib/i18n";

const ACC = "!border-mast-accent !bg-mast-accent !text-mast-accent-ink";
const CELL = "border-b border-mast-border px-1.5 py-1";

export function StsLinesDialog({ sid, model, onClose }: { sid: string; model: GalleryModel; onClose: () => void }) {
  const qc = useQueryClient();
  const series = useMarksStore((s) => s.doc.series);
  const figures = useGalleryFigures();
  const entries = useMemo(() => allEntries(figures.data?.categories), [figures.data]);

  // Participating series: all members in the index are spectra; when the index has no members at all, fall back to the kind the series records itself.
  const members = useMemo(() => {
    const m = new Map<string, GalleryItem[]>();
    for (const [id, s] of Object.entries(series)) m.set(id, seriesMembers(s, model.items));
    return m;
  }, [series, model.items]);
  const groups = useMemo(
    () =>
      suggestLineGroups(series, (id, s) => {
        const mem = members.get(id) ?? [];
        return mem.length ? mem.every((it) => it.k === "s") : s.k === "s";
      }),
    [series, members],
  );
  const ordered = useMemo(() => groups.flatMap((g) => g.sids), [groups]);

  const [checked, setChecked] = useState<string[]>(() => groupFor(groups, sid).sids);
  const [form, setForm] = useState<StsLinesForm>(() => formFromOptions(null, commonLineName(series, groupFor(groups, sid).sids)));
  const [lineEdited, setLineEdited] = useState(false);
  const [prefillFrom, setPrefillFrom] = useState<string | null>(null);
  const [plan, setPlan] = useState<StsLinePlan | null>(null);
  const [busy, setBusy] = useState<"" | "plan" | "run">("");

  const checkedKey = [...checked].sort().join("\n");
  useEffect(() => {
    const hit = findPrefill(entries, checked);
    if (hit) {
      setForm(formFromOptions(hit.options, commonLineName(series, checked)));
      setPrefillFrom(hit.created || hit.base);
      setLineEdited(false);
    } else {
      setPrefillFrom(null);
      if (!lineEdited) setForm((f) => ({ ...f, lineName: commonLineName(series, checked) }));
    }
    setPlan(null);
    // Recompute only when the ticked set (by content) or the existing figure list changes; the series object changes reference on every mark edit.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [checkedKey, entries]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const toggle = (id: string) =>
    setChecked((c) => (c.includes(id) ? c.filter((x) => x !== id) : ordered.filter((x) => x === id || c.includes(x))));
  const setMarkRow = (i: number, patch: Partial<{ station: string; label: string }>) =>
    setForm((f) => ({ ...f, marks: f.marks.map((r, k) => (k === i ? { ...r, ...patch } : r)) }));

  const doPlan = async () => {
    if (!checked.length) return;
    setBusy("plan");
    try {
      const { data, error } = await api.POST("/api/gallery/figures/sts_lines/plan", {
        body: { series: checked, options: stsLinesOptions(form, checked) },
      });
      if (error || !data) throw new Error(tr("请求失败", "Request failed"));
      setPlan(data);
    } catch (e) {
      setPlan({ ok: false, degraded: true, detail: (e as Error).message, line_name: "", n_stations: 0, reference: "" });
    } finally {
      setBusy("");
    }
  };

  const doRun = async () => {
    if (!checked.length) return;
    setBusy("run");
    const ok = await startFigureJob(qc, { kind: "sts_lines", series: checked, options: stsLinesOptions(form, checked) });
    setBusy("");
    if (ok) onClose();
  };

  const node = (
    <div
      className="fixed inset-0 z-[65] flex items-start justify-center overflow-auto overscroll-contain bg-black/60 p-6"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        role="dialog"
        aria-label={tr("拉线谱出图", "STS line figure")}
        className="mt-6 w-full max-w-4xl rounded-mast-card border border-mast-border bg-mast-panel text-mast-text shadow-mast"
      >
        <div className="flex items-center justify-between border-b border-mast-border bg-mast-panel-2 px-4 py-2.5">
          <h3 className="text-base font-semibold">{tr("拉线谱出图", "STS line figure")}</h3>
          <button type="button" onClick={onClose} className="text-mast-muted hover:text-mast-text" title={tr("关闭（Esc）", "Close (Esc)")}>
            ✕
          </button>
        </div>

        <div className="space-y-3 p-4 text-[13px]">
          <p className="text-mast-muted">
            {tr(
              "一条线 = 若干个谱系列，每个系列是这条线的一个区组。站位由服务端按谱的位置沿线投影推断，区组之间的漂移自动对齐；默认勾上了与本系列「同一条线」的系列（系列名去掉「区组k」与末尾括号后相同）。出四张图：分区组比较的热图与瀑布、站位均值的热图与瀑布。",
              "A line is several spectrum series, each series being one block of the line. The server infers stations by projecting the spectrum positions along the line and aligns drift between blocks automatically. Series on the \"same line\" as this one are ticked by default (same series name once the block label \"区组k\" and trailing parentheses are removed). Four figures are produced: per-block heatmap and waterfall, and per-station-average heatmap and waterfall.",
            )}
          </p>
          {prefillFrom && (
            <div className="text-mast-info">
              {tr(`已按 ${prefillFrom} 那次出图的参数预填。`, `Pre-filled from the parameters of the export at ${prefillFrom}.`)}
            </div>
          )}

          <div className="max-h-72 overflow-auto rounded-[3px] border border-mast-border">
            <table className="w-full border-collapse">
              <thead>
                <tr className="text-left">
                  {[
                    "",
                    tr("系列", "Series"),
                    tr("区组", "Block"),
                    tr("条数", "Count"),
                    tr("评级", "Rating"),
                    tr("从第几条起针尖在变", "Tip changed from #"),
                  ].map((h, i) => (
                    <th key={i} className={`${CELL} sticky top-0 bg-mast-panel-2 font-semibold`}>
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {ordered.map((id) => {
                  const s = series[id];
                  if (!s) return null;
                  const on = checked.includes(id);
                  return (
                    <tr key={id} className={clsx(on && "bg-mast-accent-soft")}>
                      <td className={CELL}>
                        <input type="checkbox" checked={on} onChange={() => toggle(id)} />
                      </td>
                      <td className={CELL}>
                        <button type="button" className="text-left hover:underline" onClick={() => toggle(id)}>
                          {s.name}
                        </button>
                      </td>
                      <td className={`${CELL} whitespace-nowrap text-mast-muted`}>{blockLabel(s.name)}</td>
                      <td className={`${CELL} font-mono`}>{members.get(id)?.length ?? 0}</td>
                      <td className={`${CELL} whitespace-nowrap`}>{s.r ? RLAB[String(s.r)] : ""}</td>
                      <td className={CELL}>
                        {on && (
                          <input
                            className={`${SMALL_SELECT} w-16 font-mono`}
                            value={form.badFrom[id] ?? ""}
                            placeholder={tr("无", "none")}
                            title={tr("按采集顺序、从 0 数；从这一条起画红虚线 / 红字，不进均值", "Counted from 0 in acquisition order; from this spectrum on, drawn as red dashes / red text and excluded from the average")}
                            onChange={(e) => setForm((f) => ({ ...f, badFrom: { ...f.badFrom, [id]: e.target.value } }))}
                          />
                        )}
                      </td>
                    </tr>
                  );
                })}
                {!ordered.length && (
                  <tr>
                    <td colSpan={6} className={`${CELL} text-mast-muted`}>
                      {tr("没有谱系列。", "No spectrum series.")}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <label className="flex items-center gap-1">
              {tr("线名", "Line name")}
              <input
                className={`${SMALL_SELECT} w-[26rem] max-w-full`}
                value={form.lineName}
                onChange={(e) => {
                  setForm((f) => ({ ...f, lineName: e.target.value }));
                  setLineEdited(true);
                }}
              />
            </label>
            <label className="flex items-center gap-1">
              <input
                type="checkbox"
                checked={form.excludeRejected}
                onChange={(e) => setForm((f) => ({ ...f, excludeRejected: e.target.checked }))}
              />
              {tr("均值里剔除评级为「排除」的系列", "Exclude series rated \"Exclude\" from the average")}
            </label>
          </div>

          <div>
            <div className="mb-1 text-mast-muted">
              {tr("站位标注（图上黄圈与纵轴文字，例如 5 → V）：", "Station labels (yellow circles and y-axis text on the figure, e.g. 5 → V):")}
            </div>
            <div className="space-y-1">
              {form.marks.map((r, i) => (
                <div key={i} className="flex items-center gap-1.5">
                  {tr("站位", "Station")}
                  <input
                    className={`${SMALL_SELECT} w-14 font-mono`}
                    value={r.station}
                    onChange={(e) => setMarkRow(i, { station: e.target.value })}
                  />
                  →
                  <input className={`${SMALL_SELECT} w-28`} value={r.label} onChange={(e) => setMarkRow(i, { label: e.target.value })} />
                  <button
                    type="button"
                    className={SMALL_BTN}
                    onClick={() => setForm((f) => ({ ...f, marks: f.marks.filter((_, k) => k !== i) }))}
                  >
                    {tr("删除", "Delete")}
                  </button>
                </div>
              ))}
              <button
                type="button"
                className={SMALL_BTN}
                onClick={() => setForm((f) => ({ ...f, marks: [...f.marks, { station: "", label: "" }] }))}
              >
                {tr("+ 加一行", "+ Add row")}
              </button>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2 border-t border-mast-border pt-3">
            <button type="button" className={SMALL_BTN} disabled={!checked.length || !!busy} onClick={() => void doPlan()}>
              {busy === "plan" ? tr("预演中…", "Dry-running…") : tr("预演站位", "Dry-run stations")}
            </button>
            <button type="button" className={clsx(SMALL_BTN, ACC)} disabled={!checked.length || !!busy} onClick={() => void doRun()}>
              {busy === "run" ? tr("发起中…", "Starting…") : tr(`出图（${checked.length} 个区组）`, `Export (${checked.length} blocks)`)}
            </button>
            <button type="button" className={SMALL_BTN} onClick={onClose}>
              {tr("取消", "Cancel")}
            </button>
          </div>

          {plan && <PlanTable plan={plan} model={model} names={series} />}
        </div>
      </div>
    </div>
  );
  return createPortal(node, document.body);
}

function PlanTable({
  plan,
  model,
  names,
}: {
  plan: StsLinePlan;
  model: GalleryModel;
  names: Record<string, { name: string }>;
}) {
  const warnings = plan.warnings ?? [];
  if (plan.degraded || !plan.ok) {
    return (
      <div className="rounded-[3px] border border-mast-warn-border bg-mast-warn-bg px-3 py-2 text-mast-warn">
        {tr("预演没有结果：", "Dry run produced no result: ")}
        {plan.detail || warnings.join(tr("；", "; ")) || tr("未知原因", "unknown reason")}
      </div>
    );
  }
  const short = (id: string) => {
    const it = model.byId.get(id);
    return it ? num(it) : (id.split("/").pop() ?? id);
  };
  const dir = plan.direction ?? [];
  return (
    <div className="rounded-[3px] border border-mast-border bg-mast-bg px-3 py-2">
      <div>
        {tr("线名", "Line name")} <b>{plan.line_name || "—"}</b> · {tr("站位", "stations")} <b>{plan.n_stations}</b>
        {tr(" 个", "")} · {tr("步长", "step")}{" "}
        {plan.step_nm != null ? `${fmtNumber(plan.step_nm)} nm` : "—"} · {tr("参照", "reference")}{" "}
        {names[plan.reference]?.name ?? (plan.reference || "—")}
        {dir.length === 2
          ? tr(` · 方向 (${fmtNumber(dir[0] ?? 0)}, ${fmtNumber(dir[1] ?? 0)})`, ` · direction (${fmtNumber(dir[0] ?? 0)}, ${fmtNumber(dir[1] ?? 0)})`)
          : ""}
      </div>
      <table className="mt-1.5 w-full border-collapse">
        <thead>
          <tr className="text-left">
            {[
              tr("区组", "Block"),
              tr("条数", "Count"),
              tr("分到站位", "Assigned"),
              tr("对不上", "Unmatched"),
              tr("漂移偏移 nm", "Drift offset nm"),
            ].map((h) => (
              <th key={h} className={`${CELL} font-semibold`}>
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {(plan.blocks ?? []).map((b) => {
            const unmatched = b.unmatched ?? [];
            return (
              <tr key={b.series}>
                <td className={CELL}>{b.label || names[b.series]?.name || b.series}</td>
                <td className={`${CELL} font-mono`}>{b.n}</td>
                <td className={`${CELL} font-mono`}>{b.assigned?.length ?? 0}</td>
                <td className={clsx(CELL, "font-mono", unmatched.length > 0 && "text-mast-danger")}>
                  {unmatched.length ? `${unmatched.length}${tr("：", ": ")}${unmatched.map(short).join(" · ")}` : "0"}
                </td>
                <td className={`${CELL} font-mono`}>{fmtNumber(b.offset_nm)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {warnings.length > 0 && (
        <ul className="mt-1.5 list-disc pl-5 text-mast-warn">
          {warnings.map((w, i) => (
            <li key={i}>{w}</li>
          ))}
        </ul>
      )}
    </div>
  );
}
