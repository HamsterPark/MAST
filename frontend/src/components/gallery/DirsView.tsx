// Directory overview: one card per directory plus a "latest batch" card.

import { useMemo } from "react";
import { Link } from "react-router-dom";
import clsx from "clsx";
import { dirLabel, dirnameOf, fmtBShort, fmtT } from "@/lib/gallery/format";
import { newBatch, shortList, summariseDirs } from "@/lib/gallery/dirs";
import { seriesCountByDir } from "@/lib/gallery/series";
import { viewSearch } from "@/lib/gallery/route";
import type { GalleryModel } from "./useGalleryData";
import { useMarksStore } from "./marksStore";
import { Chip } from "./bits";
import { H1, Sub } from "./ListHeads";
import { tr } from "@/lib/i18n";

const N = "font-mono text-xs leading-[1.45] text-mast-muted";

export function DirsView({ model }: { model: GalleryModel }) {
  const items = useMarksStore((s) => s.doc.items);
  const days = useMarksStore((s) => s.doc.days);
  const series = useMarksStore((s) => s.doc.series);

  const dirs = useMemo(
    () => summariseDirs(model.items, model.lastBatch, (id) => items[id]),
    [model, items],
  );
  const nser = useMemo(() => seriesCountByDir(series, model.byId), [series, model]);
  const nNew = useMemo(() => newBatch(model.items, model.lastBatch).length, [model]);
  const rootText = model.roots.map((r) => `${r.name} (${r.path})`).join(tr("、", ", ")) || "—";

  return (
    <div>
      <H1>{tr("数据图库", "Data gallery")}</H1>
      <Sub>
        {rootText}{tr("：", ": ")}
        {tr(
          `${model.counts.f} 帧（其中 ${model.counts.dup} 张是同一次扫描的重复保存，默认隐藏）· ${model.counts.s} 条谱 · ${model.counts.g} 个网格谱（缩略图更新于 ${model.generated || "—"}）。进某个目录逐张看、打标记、圈系列；标记写进 marks.json / marks.md / marks.csv。`,
          `${model.counts.f} frames (${model.counts.dup} of them are duplicate saves of the same scan, hidden by default) · ${model.counts.s} spectra · ${model.counts.g} grid spectra (thumbnails updated ${model.generated || "—"}). Open a directory to browse frame by frame, mark items and group series; marks are written to marks.json / marks.md / marks.csv.`,
        )}
      </Sub>
      <div className="grid gap-2.5" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(250px, 1fr))" }}>
        {nNew > 0 && (
          <Link
            to={{ search: `?${viewSearch({ v: "new" })}` }}
            className="block rounded-[3px] border border-mast-border bg-mast-panel px-3 py-[9px] text-mast-text hover:border-mast-accent hover:no-underline"
          >
            <b className="text-[1.02rem]">{tr(`最新一批 · ${nNew} 个文件`, `Latest batch · ${nNew} files`)}</b>
            <div className={N}>{tr("加入于", "Added")} {model.lastBatch}</div>
            <div className={N}>{tr("跨目录集中看这次新到的数据", "Review the newly arrived data across directories")}</div>
          </Link>
        )}
        {dirs.map((s) => {
          const dv = days[s.d];
          return (
            <Link
              key={s.d}
              to={{ search: `?${viewSearch({ v: "dir", d: s.d })}` }}
              title={dirnameOf(s.sample.p) || s.d}
              className={clsx(
                "block rounded-[3px] border border-mast-border bg-mast-panel px-3 py-[9px] text-mast-text hover:border-mast-accent hover:no-underline",
                dv?.done && "border-l-4 border-l-mast-auto",
              )}
            >
              <b className="text-[1.02rem]">{dirLabel(s.d)}</b>{" "}
              {s.nw > 0 && <Chip kind="new">{tr(`+${s.nw} 新`, `+${s.nw} new`)}</Chip>}
              {s.prefixes.slice(0, 3).map((p) => (
                <Chip key={p} kind="copies">
                  {p}
                </Chip>
              ))}
              <div className={N}>
                {tr(`${s.f} 帧`, `${s.f} frames`)}
                {s.dup ? tr(`（重复 ${s.dup}）`, ` (${s.dup} duplicates)`) : ""} · {tr("原子分辨", "atomic res.")} {s.atom} ·{" "}
                {tr("超结构", "superstructure")} {s.half}
                {s.s ? tr(` · 谱 ${s.s}`, ` · spectra ${s.s}`) : ""}
                {s.g ? tr(` · 网格 ${s.g}`, ` · grids ${s.g}`) : ""}
              </div>
              <div className={N}>
                {s.t1 ? `${fmtT(s.t0)} → ${fmtT(s.t1)}` : tr("没有时刻", "No timestamps")}
              </div>
              {s.biases.length > 0 && <div className={N}>{tr("偏压", "Bias")} {shortList(s.biases, fmtBShort)} V</div>}
              {s.widths.length > 0 && <div className={N}>{tr("帧宽", "Frame width")} {shortList(s.widths, (w) => String(+w.toFixed(1)))} nm</div>}
              <div className={N}>
                {s.mk
                  ? tr(`已标记 ${s.mk}${s.star ? ` · ★ ${s.star}` : ""}`, `Marked ${s.mk}${s.star ? ` · ★ ${s.star}` : ""}`)
                  : tr("未标记", "Unmarked")}
                {nser[s.d] ? tr(` · 系列 ${nser[s.d]}`, ` · series ${nser[s.d]}`) : ""}
                {dv?.done && <b className="text-mast-auto"> · {tr("已过完", "Reviewed")}</b>}
              </div>
            </Link>
          );
        })}
      </div>
    </div>
  );
}
