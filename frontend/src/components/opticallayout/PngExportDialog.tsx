import { useState } from "react";
import clsx from "clsx";
import { tr } from "@/lib/i18n";
import { Modal } from "@/components/controls";
import { parseNumberInput } from "@/lib/operatorPrompts";
import {
  DPI_CHOICES,
  FIGURE_LABEL_PX,
  MAX_SIDE_PX,
  PAPER_WIDTH_MAX_MM,
  PAPER_WIDTH_MIN_MM,
  PAPER_WIDTH_PRESETS,
  SCALE_CHOICES,
  SMALL_TEXT_PT,
  planPng,
  pxToMm,
  textPt,
  type PngResolution,
} from "@/lib/opticalLayout/exportPng";
import { INPUT_CLS, Row } from "./fields";

// The figure PNG at a chosen resolution (docs/v2/design/optical_layout_exports.md): the width the figure will have on
// paper (its own size, 96 CSS pixels to the inch, or a journal column) and either a resolution on that paper (150, 300,
// 600 dpi) or a factor of the figure's pixels. The dialog shows what comes out: pixels, millimetres, dots per inch, and
// how large the names will be printed, since a figure squeezed into one column has small letters. The file carries a
// pHYs chunk with that size (figurePng.ts), so Word and LaTeX place it at that width.


export interface PngExportChoice {
  res: PngResolution;
  paperWidthMm: number;
}

const PRIMARY = "rounded-mast-ctl bg-mast-accent px-3.5 py-1.5 text-sm font-semibold text-mast-accent-ink hover:opacity-90 disabled:opacity-50";
const SECONDARY = "rounded-mast-ctl border border-mast-border-strong px-3.5 py-1.5 text-sm text-mast-text hover:bg-mast-panel-2";
const CHIP = "rounded-mast-ctl border px-2 py-0.5 text-xs";

const fmt1 = (x: number): string => String(Number(x.toFixed(1)));

/** The radio value of a resolution: "dpi:300", "scale:2". */
const keyOf = (r: PngResolution): string => (r.kind === "dpi" ? `dpi:${r.dpi}` : `scale:${r.scale}`);

export function PngExportDialog({ open, figure, busy, onClose, onExport }: { open: boolean; figure: { width: number; height: number } | null; busy: boolean; onClose: () => void; onExport: (c: PngExportChoice) => void }) {
  const natural = figure ? pxToMm(figure.width) : 0;
  const [res, setRes] = useState<PngResolution>({ kind: "dpi", dpi: 300 });
  const [widthText, setWidthText] = useState(() => fmt1(natural));
  const parsed = parseNumberInput(widthText);
  const widthError = !parsed.ok
    ? parsed.error
    : parsed.value < PAPER_WIDTH_MIN_MM || parsed.value > PAPER_WIDTH_MAX_MM
      ? tr(`纸面宽度要在 ${PAPER_WIDTH_MIN_MM} 到 ${PAPER_WIDTH_MAX_MM} mm 之间`, `The paper width must be ${PAPER_WIDTH_MIN_MM} to ${PAPER_WIDTH_MAX_MM} mm`)
      : null;
  const paperWidthMm = parsed.ok && widthError === null ? parsed.value : natural;
  const plan = figure ? planPng(figure, res, paperWidthMm) : null;
  const pt = figure ? textPt(FIGURE_LABEL_PX, figure.width, paperWidthMm) : 0;
  const presets = [{ id: "natural", mm: natural, label: tr(`原始 ${fmt1(natural)} mm`, `Own size ${fmt1(natural)} mm`) }, ...PAPER_WIDTH_PRESETS.map((p) => ({ ...p, label: p.id === "single" ? tr(`单栏 ${p.mm} mm`, `One column ${p.mm} mm`) : tr(`双栏 ${p.mm} mm`, `Two columns ${p.mm} mm`) }))];

  return (
    <Modal open={open} onClose={onClose} title={tr("论文风格 PNG：选分辨率", "Figure PNG at a resolution")}>
      <form
        className="space-y-3"
        data-png-dialog=""
        onSubmit={(e) => {
          e.preventDefault();
          if (figure && widthError === null && !busy) onExport({ res, paperWidthMm });
        }}
      >
        <p className="text-sm text-mast-muted">
          {tr(
            "PNG 里会写上纸面尺寸（pHYs），插进 Word 或用 LaTeX 的 \\includegraphics 时按这个宽度放。原始尺寸与论文风格 SVG、PDF 一样（每英寸 96 像素）。",
            "The PNG records its size on paper (pHYs): Word and LaTeX's \\includegraphics place it at that width. Its own size is that of the figure SVG and PDF (96 pixels to the inch).",
          )}
        </p>
        <Row label={tr("纸面宽度 (mm)", "Paper width (mm)")} error={widthError}>
          <div className="space-y-1.5">
            <input className={clsx(INPUT_CLS, "font-mono")} inputMode="decimal" value={widthText} onChange={(e) => setWidthText(e.target.value)} aria-label={tr("纸面宽度 (mm)", "Paper width (mm)")} data-paper-width="" />
            <div className="flex flex-wrap gap-1.5">
              {presets.map((p) => (
                <button key={p.id} type="button" className={clsx(CHIP, Math.abs(paperWidthMm - p.mm) < 0.05 ? "border-mast-accent bg-mast-accent-soft text-mast-accent" : "border-mast-border-strong text-mast-text hover:bg-mast-panel-2")} onClick={() => setWidthText(fmt1(p.mm))}>
                  {p.label}
                </button>
              ))}
            </div>
          </div>
        </Row>
        <Row label={tr("分辨率", "Resolution")}>
          <div className="space-y-1" role="radiogroup" aria-label={tr("分辨率", "Resolution")}>
            <div className="flex flex-wrap gap-x-3 gap-y-1">
              {DPI_CHOICES.map((dpi) => (
                <label key={dpi} className="flex cursor-pointer items-center gap-1.5 text-sm text-mast-text">
                  <input type="radio" name="png-res" checked={keyOf(res) === `dpi:${dpi}`} onChange={() => setRes({ kind: "dpi", dpi })} />
                  {dpi} dpi
                </label>
              ))}
            </div>
            <div className="flex flex-wrap gap-x-3 gap-y-1">
              {SCALE_CHOICES.map((scale) => (
                <label key={scale} className="flex cursor-pointer items-center gap-1.5 text-sm text-mast-text" title={tr("论文图像素的倍数，纸面尺寸不变", "Times the figure's pixels; the paper size stays")}>
                  <input type="radio" name="png-res" checked={keyOf(res) === `scale:${scale}`} onChange={() => setRes({ kind: "scale", scale })} />
                  {scale}×
                </label>
              ))}
            </div>
          </div>
        </Row>
        {plan && (
          <div className="rounded-mast-ctl border border-mast-border bg-mast-panel-2 px-3 py-2 text-sm text-mast-text" data-png-plan="">
            <p className="font-mono">
              {tr(`${plan.width} × ${plan.height} 像素 · ${Math.round(plan.dpi)} dpi`, `${plan.width} × ${plan.height} px · ${Math.round(plan.dpi)} dpi`)}
            </p>
            <p className="text-xs text-mast-muted">
              {tr(`纸面 ${fmt1(plan.paperWidthMm)} × ${fmt1(plan.paperHeightMm)} mm；器件名称印出来约 ${fmt1(pt)} pt`, `On paper ${fmt1(plan.paperWidthMm)} × ${fmt1(plan.paperHeightMm)} mm; the component names print at about ${fmt1(pt)} pt`)}
            </p>
            {plan.limited && (
              <p className="mt-1 text-xs text-mast-warn" data-png-limited="">
                {tr(`浏览器最多画 ${MAX_SIDE_PX} 像素一边，这张图只能做到 ${Math.round(plan.dpi)} dpi。`, `The browser draws at most ${MAX_SIDE_PX} pixels a side: this figure gets ${Math.round(plan.dpi)} dpi.`)}
              </p>
            )}
            {pt < SMALL_TEXT_PT && (
              <p className="mt-1 text-xs text-mast-warn">
                {tr(`文字会小于 ${SMALL_TEXT_PT} pt，印出来很难读；可以用更宽的纸面。`, `The text will be under ${SMALL_TEXT_PT} pt and hard to read in print; a wider paper width helps.`)}
              </p>
            )}
          </div>
        )}
        <div className="flex justify-end gap-2 pt-1">
          <button type="button" className={SECONDARY} onClick={onClose}>
            {tr("取消", "Cancel")}
          </button>
          <button type="submit" className={PRIMARY} disabled={!figure || widthError !== null || busy}>
            {busy ? tr("导出中…", "Exporting…") : tr("导出 PNG", "Export PNG")}
          </button>
        </div>
      </form>
    </Modal>
  );
}
