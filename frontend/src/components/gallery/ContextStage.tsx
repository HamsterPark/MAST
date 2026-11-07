// Spectrum <-> neighbouring-frame comparison.
//
// When a spectrum (or grid spectrum) is opened, the lightbox shows: spectrum | previous frame |
// next frame. Previous = the last frame saved before the spectrum started; next = the first
// frame whose scan began after the spectrum ended (frames spanning the spectrum and duplicate
// saves are skipped). The spectrum position is drawn on the frame (a grid draws its extent);
// positions outside the field of view are drawn as yellow dashes. "Earlier / Later" switch
// frames; "Anchor to this frame" records the position against that frame (the A / D keys in
// the lightbox do the same).
//
// Which frame each side currently shows is owned by Lightbox (the keyboard needs it); this
// file only renders.

import { Link } from "react-router-dom";
import clsx from "clsx";
import type { GalleryItem } from "@/lib/gallery/types";
import { fmtB, fmtTs, nm, num } from "@/lib/gallery/format";
import { fStart, inside, itemGeometry, relation, tBeg, tEnd, type UV } from "@/lib/gallery/context";
import { viewSearch } from "@/lib/gallery/route";
import { SpectrumChart } from "@/components/records/SpectrumChart";
import { FrameWithMarks } from "./FrameWithMarks";
import { DARK_BTN, Kbd } from "./bits";
import { tr } from "@/lib/i18n";

export type Side = "prev" | "next";

/** One side's frame panel. Shared by the series page (light background) and the lightbox (dark). */
export function FramePanel({
  side,
  frame,
  t0,
  t1,
  noun,
  pts,
  poly,
  anchoredId,
  posText,
  onStep,
  onAnchor,
  dark,
}: {
  side: Side;
  frame: GalleryItem | undefined;
  t0: number;
  t1: number;
  noun: string;
  pts: UV[];
  poly: UV[] | null;
  anchoredId: string | null | undefined;
  /** The "position in frame" part of the footnote (a single spectrum gives coordinates, a series gives "n/N in view"). */
  posText: (frame: GalleryItem) => React.ReactNode;
  onStep: (side: Side, step: number) => void;
  onAnchor: ((side: Side) => void) | null;
  dark?: boolean;
}) {
  const lab = side === "prev" ? tr("前一张帧", "Previous frame") : tr("后一张帧", "Next frame");
  const btn = dark ? DARK_BTN : "rounded-[3px] border border-mast-border bg-mast-panel px-[7px] py-px text-[13px] hover:border-mast-accent";
  const stepButtons = (anchor: React.ReactNode) => (
    <div className="flex flex-wrap gap-1">
      <button type="button" className={btn} onClick={() => onStep(side, -1)}>
        {tr("◀ 更早", "◀ Earlier")}
      </button>
      {anchor}
      <button type="button" className={btn} onClick={() => onStep(side, 1)}>
        {tr("更晚 ▶", "Later ▶")}
      </button>
    </div>
  );

  if (!frame) {
    return (
      <div className="flex min-w-0 flex-col gap-[5px] text-[12.5px]">
        <div>
          <b className={dark ? "text-white" : "text-mast-text"}>{lab}</b>
        </div>
        <div className={clsx("py-10 text-center", dark ? "text-[#9aa4af]" : "text-mast-muted")}>
          {side === "prev" ? tr("之前没有帧", "No earlier frame") : tr("之后没有帧", "No later frame")}
        </div>
        {stepButtons(null)}
      </div>
    );
  }

  const r = relation(frame, t0, t1, noun);
  const anchored = anchoredId === frame.id;
  return (
    <div className={clsx("flex min-w-0 flex-col gap-[5px] text-[12.5px]", dark && "text-[#dde3ea]")}>
      <div>
        <b className={dark ? "text-white" : "text-mast-text"}>{lab}</b>{" "}
        <Link
          to={{ search: `?${viewSearch({ v: "dir", d: frame.d })}` }}
          title={frame.fn}
          className={dark ? "text-[#c4b5fd]" : "text-mast-accent"}
        >
          {num(frame)}
        </Link>{" "}
        · {r.desc}
      </div>
      <FrameWithMarks key={frame.id} frame={frame} pts={pts} poly={poly} anchored={anchored} />
      <div className={clsx("font-mono text-[11.5px]", dark ? "text-[#9aa4af]" : "text-mast-muted")}>
        {tr("扫描", "Scan")} {fmtTs(fStart(frame))}–{fmtTs(frame.mt)} · {nm(frame.w)} nm · {fmtB(frame.b)} ·{" "}
        {Math.round(frame.sp || 0)} pA{frame.ang ? tr(` · 转角 ${frame.ang}°`, ` · angle ${frame.ang}°`) : ""}
        {posText(frame)}
      </div>
      {stepButtons(
        onAnchor ? (
          <button
            type="button"
            className={clsx(btn, anchored && "!border-[#22d3ee] !bg-[#0e7490] !text-white")}
            onClick={() => onAnchor(side)}
          >
            {anchored ? (
              tr("✓ 已系于此帧（再点取消）", "✓ Anchored to this frame (click to undo)")
            ) : (
              <>
                {tr("⌖ 系于此帧", "⌖ Anchor to this frame")} <Kbd dark={dark}>{side === "prev" ? "A" : "D"}</Kbd>
              </>
            )}
          </button>
        ) : null,
      )}
    </div>
  );
}

/** The comparison area in the lightbox: spectrum | previous | next. */
export function ContextStage({
  it,
  frames,
  sides,
  anchoredId,
  interactive,
  onStep,
  onAnchor,
}: {
  it: GalleryItem;
  frames: readonly GalleryItem[];
  sides: { prev: number; next: number };
  anchoredId: string | null | undefined;
  /** Use the interactive curve (MAST's existing SpectrumChart) in the spectrum column. */
  interactive: boolean;
  onStep: (side: Side, step: number) => void;
  onAnchor: (side: Side) => void;
}) {
  const t0 = tBeg(it);
  const t1 = tEnd(it);
  const panel = (side: Side) => {
    const f = frames[sides[side]];
    const geom = f ? itemGeometry(it, f) : { pts: [], poly: null };
    return (
      <FramePanel
        side={side}
        frame={f}
        t0={t0}
        t1={t1}
        noun={tr("谱", "spectrum")}
        pts={geom.pts}
        poly={geom.poly}
        anchoredId={anchoredId}
        onStep={onStep}
        onAnchor={onAnchor}
        dark
        posText={() => {
          const p = geom.pts[0];
          if (!p) return null;
          return (
            <>
              {tr(` · 帧内 (${p.u.toFixed(2)}, ${p.v.toFixed(2)})`, ` · in frame (${p.u.toFixed(2)}, ${p.v.toFixed(2)})`)}
              {!inside(p) && <span className="font-semibold text-[#fbbf24]"> {it.k === "g" ? tr("网格中心在视野外", "grid center outside field of view") : tr("谱位置在视野外", "spectrum position outside field of view")}</span>}
            </>
          );
        }}
      />
    );
  };

  return (
    <div className="grid w-full grid-cols-3 items-start gap-3 self-start overflow-auto max-[760px]:grid-cols-1">
      <div className="flex min-w-0 flex-col gap-[5px] text-[12.5px] text-[#dde3ea]">
        <div>
          <b className="text-white">{it.k === "g" ? tr("网格谱", "Grid spectrum") : tr("谱", "Spectrum")}</b> {num(it)} · {fmtTs(t0)}–{fmtTs(t1)}
        </div>
        {interactive && it.k === "s" ? (
          <div className="rounded bg-mast-panel p-2 text-mast-text">
            <SpectrumChart path={it.p} />
          </div>
        ) : (
          <img src={it.th} alt="" className="w-full bg-white object-contain" />
        )}
      </div>
      {panel("prev")}
      {panel("next")}
    </div>
  );
}
