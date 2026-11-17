import { useMemo, useRef, useState } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import type { components } from "@/api/schema";
import { tr } from "@/lib/i18n";
import { filterMarkerRows, isStaleEpoch, markerKindOptions } from "@/lib/scanMapView";
import { fmtDateTime, parseStamp } from "@/lib/scanMapTime";
import { scanMapKindColor, scanMapKindLabel } from "@/components/vision/ScanMapCanvas";

type Marker = components["schemas"]["MapMarkerView"];

export interface MarkerRow {
  /** markerKeys() key — stable across polls */
  key: string;
  /** index in map.markers (record order) */
  index: number;
  m: Marker;
}

const ROW_H = 30;

/**
 * Every operation the map is drawing, as a list: kind, label, time, outcome.
 *
 * The map answers "where"; this answers "which one" — on a crowded surface a
 * dot under three others cannot be hovered, but its row can. Hovering a row
 * rings the marker on the map; clicking one brings it into view and opens its
 * details. The rows are exactly the markers the map draws (same layers, same
 * time window), so the list never names something that is not on the map.
 *
 * Virtualised (@tanstack/react-virtual): a long session records thousands of
 * operations, and the list must not cost the map its frame rate.
 */
export function ScanMapMarkerList({
  rows,
  currentEpoch,
  latestIndex,
  highlightKey,
  onHover,
  onPick,
  defaultOpen = true,
}: {
  rows: MarkerRow[];
  currentEpoch?: number | null;
  latestIndex: number;
  highlightKey: string | null;
  onHover: (key: string | null) => void;
  onPick: (key: string) => void;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const [kind, setKind] = useState("");
  const [query, setQuery] = useState("");
  // The selected kind stays an option even when no row has it any more, or
  // the select would show "All kinds" over a list it still narrows.
  const kinds = useMemo(() => markerKindOptions(rows, kind), [rows, kind]);
  const shown = useMemo(
    () => filterMarkerRows(rows, { kind, query }, scanMapKindLabel),
    [rows, kind, query],
  );
  const scrollRef = useRef<HTMLDivElement>(null);
  const virt = useVirtualizer({
    count: open ? shown.length : 0,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => ROW_H,
    overscan: 10,
  });
  const today = new Date();

  return (
    <div className="rounded-lg border border-mast-border bg-mast-panel">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 px-3 py-2 text-xs">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className="font-medium text-mast-text hover:text-mast-accent"
        >
          {open ? "▾" : "▸"} {tr("标记列表", "Marker list")}
          <span className="ml-1.5 tabular-nums text-mast-muted">
            {shown.length === rows.length ? rows.length : `${shown.length}/${rows.length}`}
          </span>
        </button>
        {open && (
          <>
            <select
              value={kind}
              onChange={(e) => setKind(e.target.value)}
              aria-label={tr("按类别筛选", "Filter by kind")}
              className="rounded border border-mast-border bg-mast-bg px-1.5 py-0.5 text-xs text-mast-text"
            >
              <option value="">{tr("全部类别", "All kinds")}</option>
              {kinds.map((k) => (
                <option key={k} value={k}>
                  {scanMapKindLabel(k) || k}
                </option>
              ))}
            </select>
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={tr("搜索标签 / 技能", "Search label / skill")}
              aria-label={tr("搜索标记", "Search markers")}
              className="w-44 rounded border border-mast-border bg-mast-bg px-1.5 py-0.5 text-xs text-mast-text placeholder:text-mast-faint"
            />
            <span className="text-mast-muted">
              {tr("悬停一行在地图上圈出；点一行放大到它并打开详情", "Hover a row to ring it on the map; click to zoom to it and open its details")}
            </span>
          </>
        )}
      </div>
      {open && (
        <div
          ref={scrollRef}
          className="max-h-64 overflow-auto border-t border-mast-border"
          onMouseLeave={() => onHover(null)}
        >
          {shown.length === 0 ? (
            <div className="px-3 py-3 text-xs text-mast-muted">
              {rows.length === 0
                ? tr("地图上现在没有可列的标记（看看时间窗和图层开关）。", "No markers on the map right now (check the time window and the layer switches).")
                : tr("没有符合筛选条件的标记。", "No marker matches the filter.")}
            </div>
          ) : (
            <div style={{ height: virt.getTotalSize(), position: "relative" }}>
              {virt.getVirtualItems().map((vi) => {
                const r = shown[vi.index]!;
                const t = parseStamp(r.m.timestamp);
                const d = t == null ? null : new Date(t);
                const sameDay =
                  d != null && d.getFullYear() === today.getFullYear() && d.getMonth() === today.getMonth() &&
                  d.getDate() === today.getDate();
                const when = t == null ? tr("无时间", "no time") : sameDay ? fmtDateTime(t).slice(11) : fmtDateTime(t).slice(5, 16);
                const failed = (r.m.status ?? "") === "failed";
                const stale = isStaleEpoch(r.m, currentEpoch);
                const active = r.key === highlightKey;
                return (
                  <button
                    key={r.key}
                    type="button"
                    onMouseEnter={() => onHover(r.key)}
                    onFocus={() => onHover(r.key)}
                    onBlur={() => onHover(null)}
                    onClick={() => onPick(r.key)}
                    className={
                      "absolute left-0 flex w-full items-center gap-2.5 px-3 text-left text-xs transition-colors " +
                      (active ? "bg-mast-accent-soft" : "hover:bg-mast-panel-2")
                    }
                    style={{ top: vi.start, height: ROW_H }}
                  >
                    <span
                      className="inline-block h-2.5 w-2.5 shrink-0 rounded-sm"
                      style={{ backgroundColor: scanMapKindColor(r.m.kind) }}
                    />
                    <span className="w-20 shrink-0 truncate text-mast-muted">{scanMapKindLabel(r.m.kind) || r.m.kind}</span>
                    <span className="min-w-0 flex-1 truncate text-mast-text">
                      {r.m.label || r.m.skill_name || "—"}
                      {r.index === latestIndex && (
                        <span className="ml-1.5 rounded border border-mast-border px-1 text-[10px] text-mast-text">
                          {tr("最新", "latest")}
                        </span>
                      )}
                      {stale && (
                        <span className="ml-1.5 text-[10px] text-mast-muted">{tr("旧坐标", "old coords")}</span>
                      )}
                    </span>
                    <span className="w-24 shrink-0 text-right font-mono tabular-nums text-mast-muted">{when}</span>
                    <span className={"w-12 shrink-0 text-right " + (failed ? "text-mast-warn" : "text-mast-muted")}>
                      {failed ? tr("失败", "failed") : tr("完成", "done")}
                    </span>
                  </button>
                );
              })}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
