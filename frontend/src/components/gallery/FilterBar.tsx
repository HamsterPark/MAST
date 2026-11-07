// Filter bar, including a filename-prefix filter.
//
// The displayed values are the caller's `effective` filters (values absent from the current
// list already dropped); changes are written back to filtersStore (persisted). The bar sticks
// below the gallery header; the header height changes as it wraps with window width, so `top`
// uses the --gallery-head CSS variable that GalleryApp measures and writes.

import type { ReactNode } from "react";
import type { GalleryItem } from "@/lib/gallery/types";
import type { ViewName } from "@/lib/gallery/route";
import { CARD_WIDTH_MAX, CARD_WIDTH_MIN, type Facets, type Filters } from "@/lib/gallery/filters";
import { fmtB, fullPath } from "@/lib/gallery/format";
import { useFiltersStore } from "./filtersStore";
import { CopyButton } from "./clipboard";
import { SMALL_SELECT } from "./bits";
import { tr } from "@/lib/i18n";

function Sel<T extends string>({
  label,
  value,
  onChange,
  options,
  title,
}: {
  label: string;
  value: T;
  onChange: (v: T) => void;
  options: [T, string][];
  title?: string;
}) {
  return (
    <label className="flex items-center gap-1 whitespace-nowrap" title={title}>
      {label}
      <select className={SMALL_SELECT} value={value} onChange={(e) => onChange(e.target.value as T)}>
        {options.map(([v, lab]) => (
          <option key={v} value={v}>
            {lab}
          </option>
        ))}
      </select>
    </label>
  );
}

export function FilterBar({
  view,
  effective,
  fac,
  tags,
  list,
  total,
  extra,
  showHint = true,
}: {
  view: ViewName;
  effective: Filters;
  fac: Facets;
  tags: readonly string[];
  /** Current filter result (for the count and for copying paths). */
  list: readonly GalleryItem[];
  total: number;
  extra?: ReactNode;
  showHint?: boolean;
}) {
  const setF = useFiltersStore((s) => s.setF);
  const F = effective;

  return (
    <div
      className="sticky z-10 flex flex-wrap items-center gap-x-3.5 gap-y-1.5 border-b border-mast-border bg-mast-bg py-[7px] text-[13px] text-mast-text"
      style={{ top: "var(--gallery-head, 0px)" }}
    >
      {view !== "all" && (
        <Sel
          label={tr("类型", "Type")}
          value={F.k}
          onChange={(k) => setF({ k })}
          options={[["all", tr("全部", "All")], ["f", tr("帧", "Frames")], ["s", tr("谱", "Spectra")], ["g", tr("网格", "Grids")]]}
        />
      )}
      <Sel
        label={tr("偏压", "Bias")}
        value={F.b}
        onChange={(b) => setF({ b })}
        options={[["", tr("全部", "All")], ...fac.biases.map(([k, b]): [string, string] => [k, fmtB(b)])]}
      />
      <Sel
        label={tr("帧宽", "Frame width")}
        value={F.w}
        onChange={(w) => setF({ w })}
        options={[["", tr("全部", "All")], ...fac.widths.map(([k, w]): [string, string] => [k, `${+w.toFixed(1)} nm`])]}
      />
      {fac.prefixes.length > 1 && (
        <Sel
          label={tr("前缀", "Prefix")}
          title={tr("文件名去掉末尾编号之后的部分——通常就是 Nanonis 里起的扫描系列名", "The filename without its trailing number, usually the scan series name set in Nanonis")}
          value={F.pf}
          onChange={(pf) => setF({ pf })}
          options={[["", tr("全部", "All")], ...fac.prefixes.map(([p, n]): [string, string] => [p, `${p} (${n})`])]}
        />
      )}
      <Sel
        label={tr("衬度", "Contrast")}
        value={F.c}
        onChange={(c) => setF({ c })}
        options={[["", tr("全部", "All")], ["atom", tr("有原子分辨", "Atomic resolution")], ["half", tr("有超结构", "Superstructure")], ["li", tr("有 lock-in dI/dV", "Has lock-in dI/dV")]]}
      />
      <label className="flex items-center gap-1 whitespace-nowrap">
        <input type="checkbox" checked={F.full} onChange={(e) => setF({ full: e.target.checked })} />
        {tr("只看扫完", "Completed scans only")}
      </label>
      {view !== "marked" && view !== "series" && (
        <label
          className="flex items-center gap-1 whitespace-nowrap"
          title={tr("同一次扫描存了两份的文件（数据块逐字节相同），只留先存的那份；自己带标记或在系列里的照样显示", "Files saved twice from the same scan (byte-identical data blocks): only the first save is kept. Items that carry a mark or belong to a series are still shown.")}
        >
          <input type="checkbox" checked={F.nodup} onChange={(e) => setF({ nodup: e.target.checked })} />
          {tr("隐藏重复保存", "Hide duplicate saves")}
        </label>
      )}
      <Sel
        label={tr("标记", "Mark")}
        value={F.m}
        onChange={(m) => setF({ m })}
        options={[
          ["", tr("全部", "All")], ["none", tr("未标记", "Unmarked")], ["any", tr("已标记", "Marked")],
          ["2", tr("★ 重点", "★ Key")], ["1", tr("✓ 可用", "✓ Usable")], ["-1", tr("✗ 排除", "✗ Excluded")],
          ["hidex", tr("隐藏 ✗", "Hide ✗")], ["ser", tr("属于系列", "In a series")], ["anc", tr("位置已系定", "Position anchored")],
        ]}
      />
      <Sel
        label={tr("标签", "Tag")}
        value={F.tag}
        onChange={(tag) => setF({ tag })}
        options={[["", tr("全部", "All")], ...tags.map((t): [string, string] => [t, t])]}
      />
      <label className="flex items-center gap-1 whitespace-nowrap">
        {tr("搜索", "Search")}
        <input
          className={`${SMALL_SELECT} w-24`}
          placeholder={tr("编号 / 备注", "Number / note")}
          value={F.q}
          onChange={(e) => setF({ q: e.target.value })}
        />
      </label>
      <Sel
        label={tr("排序", "Sort")}
        value={F.sort}
        onChange={(sort) => setF({ sort })}
        options={[["asc", tr("时间 ↑", "Time ↑")], ["desc", tr("时间 ↓", "Time ↓")]]}
      />
      <label className="flex items-center gap-1 whitespace-nowrap">
        {tr("卡片", "Cards")}
        <input
          type="range"
          min={CARD_WIDTH_MIN}
          max={CARD_WIDTH_MAX}
          step={10}
          value={F.cw}
          onChange={(e) => setF({ cw: Number(e.target.value) })}
        />
      </label>
      <span className="text-xs text-mast-muted">
        {tr("显示", "Showing")} <b className="text-mast-text">{list.length}</b> / {total}
      </span>
      <CopyButton
        text={() => list.map(fullPath).join("\n")}
        label={tr("复制这些文件的路径", "Copy these file paths")}
        className="!text-[13px]"
        title={tr("每行一个完整路径", "One full path per line")}
      />
      {extra}
      {showHint && (
        <span className="text-xs text-mast-muted">
          {tr("勾选框选中，Shift 连选一段 → 底部可一起打分或存为系列", "Tick the checkboxes (Shift for a range) → rate them together or save them as a series from the bar at the bottom")}
        </span>
      )}
    </div>
  );
}
