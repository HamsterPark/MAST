// Figures page (docs/v2/design/data_gallery.md section 10).
//
// Figures are listed by category (marked frames / grid spectra / line-scan spectra / single
// spectrum stitching / rotation series). Besides browsing, the page can start a job, show its
// progress, and re-run a figure with its original parameters. Each figure's options and summary
// come from the figure.json the server writes next to the image; it is the basis for re-running.
//
// When the backend is not connected (degraded) the five categories are still shown, each
// describing how to produce it, instead of a blank page.

import { useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { ErrorNote, Spinner } from "@/components/ui";
import {
  CATEGORY_HINT,
  KIND_LABEL,
  fileLinks,
  optionsText,
  parsePositiveInt,
  previewUrl,
  summaryChips,
  withAllCategories,
  type FigureEntry,
} from "@/lib/gallery/figures";
import { H1, Sub } from "../ListHeads";
import { SMALL_BTN, SMALL_SELECT, chipClass } from "../bits";
import { toast } from "../toast";
import { useGalleryFigures } from "./useFigures";
import { FigureJobBar, startFigureJob } from "./figureJob";
import { FigureViewer } from "./FigureViewer";
import { tr } from "@/lib/i18n";

const ACC = "!border-mast-accent !bg-mast-accent !text-mast-accent-ink";

export function FiguresView() {
  const qc = useQueryClient();
  const list = useGalleryFigures();
  const [maxSeries, setMaxSeries] = useState("100");
  const [includeGrids, setIncludeGrids] = useState(true);
  const [viewer, setViewer] = useState<{ key: string; j: number } | null>(null);

  const cats = useMemo(() => withAllCategories(list.data?.categories ?? []), [list.data]);
  const nAll = cats.reduce((a, c) => a + (c.figures?.length ?? 0), 0);
  const viewerCat = viewer ? cats.find((c) => c.key === viewer.key) : undefined;

  const markedFrames = () => {
    const n = parsePositiveInt(maxSeries);
    if (n == null) {
      toast(tr("系列帧数上限要是正整数", "The series frame limit must be a positive integer"), "err");
      return;
    }
    void startFigureJob(qc, { kind: "marked_frames", options: { max_series_frames: n, include_grids: includeGrids } });
  };

  return (
    <div>
      <H1>{tr("出图", "Figures")} · {nAll}</H1>
      <Sub>
        {tr(
          "从标记与系列生成正式图：标记帧对比、网格谱逐层、拉线谱热图与瀑布、单根谱拼接、旋转系列拼图与叠加。产物在图库状态目录的 figures/ 下；每张图旁边的 figure.json 记着输入与参数，「重出」就是按它再跑一遍。点预览看全尺寸（←/→ 翻页，Esc 关）。",
          "Generate publication figures from marks and series: marked-frame comparisons, per-layer grid spectra, line-scan heatmaps and waterfalls, single-spectrum stitching, rotation-series mosaics and stacks. Output lives under figures/ in the gallery state directory; the figure.json next to each image records its inputs and parameters, and \"Re-run\" runs it again from those. Click a preview for full size (←/→ to page, Esc to close).",
        )}
      </Sub>
      {list.isPending && <Spinner label={tr("读取出图列表…", "Reading figure list…")} />}
      {list.isError && <ErrorNote error={list.error} />}
      {list.data?.degraded && (
        <div className="mb-2 rounded-mast-ctl border border-mast-warn-border bg-mast-warn-bg px-3 py-2 text-sm text-mast-warn">
          {tr("出图后端不可用：", "Figure backend unavailable: ")}
          {list.data.detail || tr("未知原因", "unknown reason")}
          {tr("。已有的图列不出来，发起的出图会提示「没有开始」。", ". Existing figures cannot be listed, and starting a job will report that it did not start.")}
        </div>
      )}

      <div className="my-2 flex flex-wrap items-center gap-2 text-[13px]">
        <label
          className="flex items-center gap-1"
          title={tr(
            "帧数超过它的系列不逐张出对比图（默认 100；一百多帧的旋转系列用系列页的「16:9 拼图」）",
            "Series with more frames than this do not get per-frame comparison figures (default 100; for rotation series of over a hundred frames use \"16:9 sheet\" on the series page)",
          )}
        >
          {tr("系列帧数上限", "Series frame limit")}
          <input className={`${SMALL_SELECT} w-16`} value={maxSeries} onChange={(e) => setMaxSeries(e.target.value)} />
        </label>
        <label className="flex items-center gap-1">
          <input type="checkbox" checked={includeGrids} onChange={(e) => setIncludeGrids(e.target.checked)} />
          {tr("同时出全部网格谱", "Also export all grid spectra")}
        </label>
        <button
          type="button"
          className={clsx(SMALL_BTN, ACC)}
          onClick={markedFrames}
          title={tr(
            "单条标记的帧 + 帧数不超过上限的系列成员，逐张出「原版 | 逐行调平」对比图",
            "For individually marked frames plus members of series within the frame limit, export one \"original | line-flattened\" comparison figure each",
          )}
        >
          {tr("为已标记出图", "Make figures for marked")}
        </button>
        <button type="button" className={SMALL_BTN} onClick={() => void startFigureJob(qc, { kind: "grid_sheets" })}>
          {tr("全部网格谱出图", "Make figures for all grids")}
        </button>
        <button type="button" className={SMALL_BTN} disabled={list.isFetching} onClick={() => void list.refetch()}>
          {list.isFetching ? tr("刷新中…", "Refreshing…") : tr("刷新列表", "Refresh list")}
        </button>
      </div>

      <FigureJobBar />

      {cats.map((cat) => {
        const figs = cat.figures ?? [];
        return (
          <section key={cat.key} className="mt-4">
            <h2 className="mb-1.5 text-[1.05rem] font-semibold">
              {cat.title} · {figs.length}
            </h2>
            {figs.length === 0 ? (
              <Sub>
                {tr("还没有。", "None yet. ")}
                {CATEGORY_HINT[cat.key] ?? ""}
              </Sub>
            ) : (
              <div className="grid grid-cols-[repeat(auto-fill,minmax(300px,1fr))] gap-2.5">
                {figs.map((entry, j) => (
                  <FigureCard
                    key={entry.key}
                    entry={entry}
                    onOpen={() => setViewer({ key: cat.key, j })}
                    onRerun={() =>
                      void startFigureJob(qc, {
                        kind: entry.kind,
                        ids: entry.ids ?? [],
                        series: entry.series ?? [],
                        options: entry.options ?? {},
                      })
                    }
                  />
                ))}
              </div>
            )}
          </section>
        );
      })}

      {viewer && viewerCat && (
        <FigureViewer
          entries={viewerCat.figures ?? []}
          index={viewer.j}
          onIndex={(j) => setViewer({ key: viewer.key, j })}
          onClose={() => setViewer(null)}
        />
      )}
    </div>
  );
}

function FigureCard({ entry, onOpen, onRerun }: { entry: FigureEntry; onOpen: () => void; onRerun: () => void }) {
  const pv = previewUrl(entry);
  const chips = summaryChips(entry.summary);
  const opts = optionsText(entry.options);
  const nIds = entry.ids?.length ?? 0;
  const nSeries = entry.series?.length ?? 0;
  return (
    <div className="flex flex-col overflow-hidden rounded-[3px] border border-mast-border bg-mast-panel">
      <button type="button" onClick={onOpen} className="block cursor-zoom-in bg-[#181614]" title={tr("看全尺寸（←/→ 翻页，Esc 关）", "View full size (←/→ to page, Esc to close)")}>
        {pv ? (
          <img loading="lazy" src={pv} alt={entry.base} className="block h-auto w-full" />
        ) : (
          <div className="flex h-32 items-center justify-center text-xs text-[#9aa4af]">{tr("没有预览图", "No preview")}</div>
        )}
      </button>
      <div className="px-2 py-1.5 text-[12px]">
        <div className="break-all font-medium text-mast-text">{entry.title || entry.base}</div>
        <div className="font-mono text-[11.5px] text-mast-muted">
          {KIND_LABEL[entry.kind]} · {entry.created || "—"}
          {nIds ? tr(` · ${nIds} 个条目`, ` · ${nIds} items`) : ""}
          {nSeries ? tr(` · ${nSeries} 个系列`, ` · ${nSeries} series`) : ""}
        </div>
        {chips.length > 0 && (
          <div className="mt-0.5">
            {chips.map((c) => (
              <span key={c.key} className={chipClass("user")}>
                {c.text}
              </span>
            ))}
          </div>
        )}
        {opts && (
          <div className="mt-0.5 break-all font-mono text-[11px] text-mast-faint" title={opts}>
            {opts}
          </div>
        )}
        <div className="mt-1 flex flex-wrap items-center gap-1">
          {fileLinks(entry.files).map((f) => (
            <a key={f.name} href={f.url} download={f.name} className={SMALL_BTN} title={f.name}>
              {f.label}
            </a>
          ))}
          <button type="button" className={SMALL_BTN} onClick={onRerun} title={tr("按原来的输入与参数再出一次", "Run again with the original inputs and parameters")}>
            {tr("重出", "Re-run")}
          </button>
        </div>
      </div>
    </div>
  );
}
