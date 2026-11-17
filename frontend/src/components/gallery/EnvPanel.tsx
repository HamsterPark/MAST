// Environment section of the lightbox side panel (docs/v2/design/data_env_tags.md §6.3).
//
// Shows every value of the record with its source sensor, reading time and offset from the save,
// the acquisition window (min–max per sensor, how it was computed, whether it covers the whole
// window), sensors that had no usable reading, and where the record came from (sidecar, central
// record, or inferred). Fetched on demand; the index only carries the short summary.
//
// The lightbox always has a dark background, so this uses the same fixed dark colours as the rest
// of the side panel rather than --mast-* tokens.

import clsx from "clsx";
import type { GalleryItem } from "@/lib/gallery/types";
import { fmtTs, dur } from "@/lib/gallery/format";
import {
  HOW_LABEL,
  KIND_LABEL,
  KIND_ORDER,
  ORIGIN_LABEL,
  SOURCE_LABEL,
  fmt3,
  fmtExp,
  offsetText,
} from "@/lib/gallery/env";
import { useGalleryEnv, type GalleryEnvDetail } from "./useGalleryData";
import { CopyButton } from "./clipboard";
import { tr } from "@/lib/i18n";

type Rec = NonNullable<GalleryEnvDetail["record"]>;
type Val = NonNullable<Rec["values"]>[number];
type Win = NonNullable<Rec["window"]>[number];

function num(kind: string, v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "?";
  return kind === "pressure" ? fmtExp(v) : fmt3(v);
}

const order = (k: string) => {
  const i = KIND_ORDER.indexOf(k);
  return i < 0 ? 99 : i;
};

function ValueRow({ v }: { v: Val }) {
  const src = String(v.source || "");
  const approx = src === "history" || src === "inferred" ? "≈" : "";
  const bucket = v.bucket as { n?: number; min?: number; max?: number } | null | undefined;
  return (
    <tr className={clsx(!v.fresh && "text-[#8a939c]")}>
      <td className="pr-2 align-top text-[#8a939c]">{KIND_LABEL[v.kind] ?? v.kind}</td>
      <td className="pr-2 align-top font-mono text-[#eee]">
        {approx}
        {num(v.kind, v.value)} {v.unit}
      </td>
      <td className="align-top">
        <div className="break-all">{v.sensor}</div>
        <div className="text-[11px] text-[#8a939c]">
          {src === "history" || src === "inferred"
            ? tr(
                `${fmtTs(v.t)} 起 ${Math.round(v.span_s ?? 0)} s 的桶均值` + (bucket?.n ? `（${bucket.n} 条）` : ""),
                `mean of the ${Math.round(v.span_s ?? 0)} s bucket from ${fmtTs(v.t)}` + (bucket?.n ? ` (${bucket.n} readings)` : ""),
              )
            : tr(`读于 ${fmtTs(v.t)}`, `read at ${fmtTs(v.t)}`)}
          {" · "}
          {offsetText(v.dt_s, v.fresh, src === "history" || src === "inferred")}
          {" · "}
          {SOURCE_LABEL[src] ?? src}
          {!v.fresh && <span className="text-[#fbbf24]">{tr(" · 过期", " · stale")}</span>}
          {v.status && v.status !== "ok" && <span className="text-[#fbbf24]"> · {v.status}</span>}
        </div>
      </td>
    </tr>
  );
}

function WindowRow({ w }: { w: Win }) {
  // 侧栏如实列出最小→最大（不用卡片的阈值）；警示色只给逐条读数算的窗口 —— 桶比采集期间宽，
  // 起伏可能发生在采集前后。
  const differs = num(w.kind, w.min) !== num(w.kind, w.max);
  const changed = differs && w.method === "readings";
  const method: Record<string, string> = {
    readings: tr("逐条读数", "individual readings"),
    buckets: tr("1 分钟桶（覆盖比窗口宽）", "1-minute buckets (cover more than the window)"),
    "buckets+readings": tr("桶 + 最近的逐条读数", "buckets + recent readings"),
  };
  return (
    <div className="mt-0.5">
      <span className="text-[#8a939c]">{KIND_LABEL[w.kind] ?? w.kind}</span>{" "}
      <span className={clsx("font-mono", changed ? "text-[#fbbf24]" : "text-[#eee]")}>
        {differs ? `${num(w.kind, w.min)} → ${num(w.kind, w.max)}` : num(w.kind, w.min)} {w.unit}
      </span>
      <span className="text-[11px] text-[#8a939c]">
        {" · "}
        {w.sensor} · {method[w.method] ?? w.method} · n {w.n}
        {!w.complete &&
          tr(
            ` · 只覆盖 ${fmtTs(w.from)}–${fmtTs(w.to)}`,
            ` · covers only ${fmtTs(w.from)}–${fmtTs(w.to)}`,
          )}
      </span>
    </div>
  );
}

export function EnvPanel({ it }: { it: GalleryItem }) {
  const q = useGalleryEnv(it.id);
  const d = q.data;
  const rec = d?.record ?? null;
  const values = [...(rec?.values ?? [])].sort(
    (a, b) => order(a.kind) - order(b.kind) || Number(b.primary) - Number(a.primary) || a.sensor.localeCompare(b.sensor),
  );
  const acq = rec?.acquisition ?? null;
  const tagged = (rec?.tagged ?? {}) as { at?: number; origin?: string; max_age_s?: number; clock_skew_s?: number | null };

  return (
    <section data-gallery-env className="mt-2.5 rounded-[3px] border border-[#333] bg-[#16191d] px-2.5 py-2 text-xs leading-normal text-[#b9c0c8]">
      <div className="mb-1 flex items-center justify-between gap-2">
        <span className="font-semibold text-[#eee]">{tr("环境", "Environment")}</span>
        {d && (
          <span className={clsx("text-[11px]", d.how === "inferred" ? "italic text-[#c4b5fd]" : d.how === "none" ? "text-[#6d7681]" : "text-[#5fc39d]")}>
            {HOW_LABEL[d.how] ?? d.how}
          </span>
        )}
      </div>
      {q.isPending && <div className="text-[#8a939c]">{tr("读取中…", "Loading…")}</div>}
      {q.isError && <div className="text-[#e58585]">{tr("读不到环境记录", "Could not load the environment record")}</div>}
      {d?.degraded && <div className="text-[#fbbf24]">{d.detail || tr("后端不可用", "Backend unavailable")}</div>}
      {d && !rec && !d.degraded && (
        <div className="text-[#8a939c]">
          {tr(
            "这个文件没有环境记录：保存时没有读数，环境历史里那一刻也没有。",
            "No environment record for this file: nothing was being read when it was saved, and the environment history has nothing for that moment.",
          )}
        </div>
      )}
      {values.length > 0 && (
        <table className="w-full border-collapse">
          <tbody>
            {values.map((v, i) => (
              <ValueRow key={`${v.sensor}-${i}`} v={v} />
            ))}
          </tbody>
        </table>
      )}
      {acq && (
        <div className="mt-1.5 border-t border-[#2a2e33] pt-1">
          <div>
            {tr("采集 ", "Acquisition ")}
            <span className="font-mono text-[#eee]">
              {fmtTs(acq.start)} → {fmtTs(acq.end)}
            </span>
            {acq.duration_s != null && ` (${dur(acq.duration_s)})`}
          </div>
          {(rec?.window ?? []).length === 0 && (
            <div className="text-[#8a939c]">{tr("窗口内没有读数", "No readings inside the window")}</div>
          )}
          {(rec?.window ?? []).map((w, i) => (
            <WindowRow key={`${w.sensor}-${i}`} w={w} />
          ))}
        </div>
      )}
      {(rec?.missing ?? []).length > 0 && (
        <div className="mt-1.5 text-[#8a939c]">
          {tr("没有可用读数：", "No usable reading: ")}
          {(rec?.missing ?? []).map((m) => `${m.sensor}${m.status ? `（${m.status}）` : ""}`).join(tr("、", ", "))}
        </div>
      )}
      {rec && d && d.how !== "inferred" && (
        <div className="mt-1.5 border-t border-[#2a2e33] pt-1 text-[11px] text-[#8a939c]">
          {tr(`记录于 ${fmtTs(tagged.at)}`, `Recorded at ${fmtTs(tagged.at)}`)}
          {tagged.origin && ` · ${ORIGIN_LABEL[tagged.origin] ?? tagged.origin}`}
          {tagged.max_age_s != null && tr(` · 新鲜度阈值 ${tagged.max_age_s} s`, ` · freshness ${tagged.max_age_s} s`)}
          {tagged.clock_skew_s != null && (
            <span className="text-[#fbbf24]">
              {tr(` · 文件时刻比 MAST 看到它晚 ${tagged.clock_skew_s} s（时钟？）`, ` · file time is ${tagged.clock_skew_s} s after MAST saw it (clock?)`)}
            </span>
          )}
          {d.sidecar_path && (
            <div className="mt-0.5 break-all">
              {d.sidecar_path}{" "}
              <CopyButton text={d.sidecar_path} label={tr("复制", "Copy")} className="!border-[#444] !bg-[#2a2e33] !text-[#eee]" />
            </div>
          )}
        </div>
      )}
    </section>
  );
}
