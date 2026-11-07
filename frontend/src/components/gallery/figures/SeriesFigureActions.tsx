// Figure entry points on the series page.
//
// Frame series -> "16:9 sheet" (one column per scan angle, one row per round) and "Stack…"
// (anchor + field of view); spectrum series -> "STS line figure…" (dialog, see StsLinesDialog).
// A mixed series gets both sets of buttons; the server only takes members of the matching kind.

import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import type { GalleryItem } from "@/lib/gallery/types";
import type { GalleryModel } from "../useGalleryData";
import { SMALL_BTN, SMALL_SELECT } from "../bits";
import { toast } from "../toast";
import { startFigureJob } from "./figureJob";
import { StsLinesDialog } from "./StsLinesDialog";
import { tr } from "@/lib/i18n";

const ANCHORS: readonly (readonly [string, string])[] = [
  ["darkest", tr("中心附近最暗处（缺陷，默认做法）", "Darkest point near the center (a defect; default)")],
  ["brightest", tr("中心附近最亮处", "Brightest point near the center")],
  ["center", tr("帧中心（不找锚点）", "Frame center (no anchor search)")],
];

const ACC = "!border-mast-accent !bg-mast-accent !text-mast-accent-ink";

export function SeriesFigureActions({ sid, mem, model }: { sid: string; mem: GalleryItem[]; model: GalleryModel }) {
  const qc = useQueryClient();
  const [stackOpen, setStackOpen] = useState(false);
  const [anchor, setAnchor] = useState("darkest");
  const [fov, setFov] = useState(() => String(mem.find((it) => it.k === "f")?.w || ""));
  const [linesOpen, setLinesOpen] = useState(false);

  const nF = mem.filter((it) => it.k === "f").length;
  const nS = mem.filter((it) => it.k === "s").length;
  if (!nF && !nS) return null;

  const runStack = () => {
    const t = fov.trim();
    const f = /^\d*\.?\d+$/.test(t) ? Number(t) : NaN;
    if (!(f > 0)) {
      toast(tr("视野要是正数（nm）", "The field of view must be a positive number (nm)"), "err");
      return;
    }
    void startFigureJob(qc, { kind: "series_stack", series: [sid], options: { anchor, fov_nm: f } });
    setStackOpen(false);
  };

  return (
    <div className="my-1.5 flex flex-wrap items-center gap-x-2 gap-y-1.5 text-[13px]">
      <span className="text-mast-muted">{tr("出图：", "Figures:")}</span>
      {nF > 0 && (
        <>
          <button
            type="button"
            className={SMALL_BTN}
            title={tr(
              "16:9 两页：一列一个扫描转角（从第 1 帧起按采集顺序），一行一轮；每帧转到第 1 帧朝向、以锚点为中心",
              "16:9, two pages: one column per scan angle (in acquisition order from frame 1), one row per round; each frame is rotated to frame 1's orientation and centered on the anchor",
            )}
            onClick={() => void startFigureJob(qc, { kind: "series_slides", series: [sid] })}
          >
            {tr("16:9 拼图", "16:9 sheet")}
          </button>
          <button
            type="button"
            className={clsx(SMALL_BTN, stackOpen && "!border-mast-accent")}
            title={tr(
              "刚性叠加 + 晶格仿射校正叠加，另出配准表（漂移轨迹）与 npy 数组",
              "Rigid stack + lattice-affine-corrected stack, plus a registration table (drift trajectory) and npy arrays",
            )}
            onClick={() => setStackOpen((o) => !o)}
          >
            {tr("叠加…", "Overlay…")}
          </button>
        </>
      )}
      {nS > 0 && (
        <button
          type="button"
          className={SMALL_BTN}
          title={tr(
            "勾上同一条线的各个区组系列，出热图与瀑布（分区组比较 / 站位均值）",
            "Tick the block series of the same line to export heatmaps and waterfalls (per-block comparison / per-site average)",
          )}
          onClick={() => setLinesOpen(true)}
        >
          {tr("拉线谱出图…", "STS line figure…")}
        </button>
      )}
      {stackOpen && (
        <span className="inline-flex flex-wrap items-center gap-1.5 rounded-[3px] border border-mast-border bg-mast-panel px-2 py-1">
          {tr("锚点", "Anchor")}
          <select className={SMALL_SELECT} value={anchor} onChange={(e) => setAnchor(e.target.value)}>
            {ANCHORS.map(([v, label]) => (
              <option key={v} value={v}>
                {label}
              </option>
            ))}
          </select>
          {tr("视野", "Field of view")}
          <input className={`${SMALL_SELECT} w-14`} value={fov} onChange={(e) => setFov(e.target.value)} />
          nm
          <button type="button" className={clsx(SMALL_BTN, ACC)} onClick={runStack}>
            {tr("开始叠加", "Start stacking")}
          </button>
          <button type="button" className={SMALL_BTN} onClick={() => setStackOpen(false)}>
            {tr("收起", "Collapse")}
          </button>
        </span>
      )}
      {linesOpen && <StsLinesDialog sid={sid} model={model} onClose={() => setLinesOpen(false)} />}
    </div>
  );
}
