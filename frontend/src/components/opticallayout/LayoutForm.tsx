import { useMemo, useState } from "react";
import clsx from "clsx";
import { tr } from "@/lib/i18n";
import type { OpticalLayout } from "@/lib/opticalLayout/types";
import { isSourceType } from "@/lib/opticalLayout/types";
import type { AlignLevel } from "@/lib/opticalLayout/indicators";
import { typeLabel } from "@/lib/opticalLayout/catalog";
import { GRID_KINDS, LIMITS } from "@/lib/opticalLayout/types";
import { removeText, setNotes, setTable, setTitle, updateText } from "@/lib/opticalLayout/edit";
import { holeCounts } from "@/lib/opticalLayout/geometry";
import { shortcutHelp } from "@/lib/opticalLayout/keys";
import { CommitNumber, CommitText, CommitTextarea, Group, NumberRow, Row } from "./fields";

// What the inspector shows when nothing is selected: the layout itself (title, platform,
// holes, free notes, text labels), a list of its components (a way to find and select one
// without the mouse, and in a crowded drawing) and the keyboard help.

const DOT: Record<AlignLevel, string> = { ok: "bg-mast-auto", warn: "bg-mast-warn", bad: "bg-mast-danger" };

function ComponentList({ layout, levels, traceCut, onSelect }: { layout: OpticalLayout; levels: ReadonlyMap<string, AlignLevel>; traceCut: boolean; onSelect: (id: string) => void }) {
  const [q, setQ] = useState("");
  const needle = q.trim().toLowerCase();
  const rows = useMemo(
    () =>
      layout.components.filter((c) => needle === "" || c.id.toLowerCase().includes(needle) || c.label.toLowerCase().includes(needle) || typeLabel(c.type).toLowerCase().includes(needle) || c.annotations.model.toLowerCase().includes(needle)),
    [layout.components, needle],
  );
  return (
    <div className="space-y-1.5">
      {layout.components.length > 8 && (
        <input
          type="search"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder={tr("按名称、类型、型号筛选…", "Filter by name, type, model…")}
          aria-label={tr("筛选器件", "Filter components")}
          className="w-full rounded-mast-ctl border border-mast-border-strong bg-mast-panel px-2 py-1 text-sm text-mast-text outline-none focus:border-mast-accent"
        />
      )}
      <ul className="max-h-64 space-y-0.5 overflow-auto" data-component-list="">
        {rows.map((c) => (
          <li key={c.id}>
            <button type="button" onClick={() => onSelect(c.id)} className="flex w-full items-center gap-2 rounded px-1.5 py-0.5 text-left text-sm hover:bg-mast-panel-2">
              <span className={clsx("h-2 w-2 shrink-0 rounded-full", levels.has(c.id) ? DOT[levels.get(c.id) ?? "ok"] : "bg-mast-border-strong")} aria-hidden="true" title={levels.has(c.id) || isSourceType(c.type) ? undefined : traceCut ? tr("追迹被截断，不知道有没有光打到它", "The trace is cut: not known whether a beam reaches it") : tr("没有光打到它", "No beam reaches it")} />
              <span className="truncate text-mast-text">{c.label || c.id}</span>
              <span className="ml-auto shrink-0 text-xs text-mast-faint">
                {typeLabel(c.type)}
                {c.locked ? " 🔒" : ""}
              </span>
            </button>
          </li>
        ))}
        {rows.length === 0 && <li className="px-1.5 text-xs text-mast-faint">{layout.components.length === 0 ? tr("还没有器件：从左边的面板添加。", "No components yet: add one from the palette.") : tr("没有匹配的器件。", "No component matches.")}</li>}
      </ul>
    </div>
  );
}

export function LayoutForm({
  layout,
  readOnly,
  edit,
  onAddText,
  levels,
  traceCut,
  onSelect,
}: {
  layout: OpticalLayout;
  readOnly: boolean;
  edit: (fn: (l: OpticalLayout) => OpticalLayout) => void;
  onAddText: () => void;
  /** how each component's beams are aligned, for the dot in the list */
  levels: ReadonlyMap<string, AlignLevel>;
  /** the trace was cut by its budget: a component without a dot is not known to be dark */
  traceCut: boolean;
  onSelect: (id: string) => void;
}) {
  const t = layout.table;
  const counts = holeCounts(t);
  return (
    <div data-inspector="layout">
      <Group title={tr("这张光路图", "This layout")}>
        <Row label={tr("标题", "Title")}>
          <CommitText value={layout.title} disabled={readOnly} maxLength={LIMITS.titleMax} aria-label={tr("标题", "Title")} onCommit={(v) => edit((l) => setTitle(l, v))} />
        </Row>
        <Row label={tr("备注", "Notes")} hint={`${layout.notes.length} / ${LIMITS.notesMax}`}>
          <CommitTextarea value={layout.notes} disabled={readOnly} rows={4} maxLength={LIMITS.notesMax} placeholder={tr("关于整套光路的说明，给人和 AI 看。", "About the whole setup, for people and the AI.")} onCommit={(v) => edit((l) => setNotes(l, v))} />
        </Row>
      </Group>

      <Group title={tr(`器件（${layout.components.length}）`, `Components (${layout.components.length})`)}>
        <ComponentList layout={layout} levels={levels} traceCut={traceCut} onSelect={onSelect} />
      </Group>

      <Group title={tr("平台与孔位", "Platform and holes")} defaultOpen={false}>
        <NumberRow label={tr("宽", "Width")} unit="mm" value={t.width_mm} disabled={readOnly} rules={{ min: LIMITS.tableSizeMin, max: LIMITS.tableSizeMax, step: 10 }} onCommit={(v) => v !== null && edit((l) => setTable(l, { width_mm: v }))} />
        <NumberRow label={tr("高", "Height")} unit="mm" value={t.height_mm} disabled={readOnly} rules={{ min: LIMITS.tableSizeMin, max: LIMITS.tableSizeMax, step: 10 }} onCommit={(v) => v !== null && edit((l) => setTable(l, { height_mm: v }))} />
        <NumberRow label={tr("孔距", "Hole pitch")} unit="mm" value={t.hole_pitch_mm} disabled={readOnly} rules={{ min: LIMITS.pitchMin, max: LIMITS.pitchMax, step: 1 }} hint={tr("公制面包板 25 mm，英制 25.4 mm", "25 mm for a metric breadboard, 25.4 mm for an imperial one")} onCommit={(v) => v !== null && edit((l) => setTable(l, { hole_pitch_mm: v }))} />
        <NumberRow
          label={tr("首孔偏移", "First hole at")}
          unit="mm"
          value={t.hole_offset_mm}
          disabled={readOnly}
          rules={{ min: 0, max: LIMITS.holeOffsetMax, step: 1 }}
          hint={tr(`第一个孔离台面左下角的距离。共 ${counts.cols} 列 × ${counts.rows} 行`, `Distance of the first hole from the lower-left corner. ${counts.cols} columns × ${counts.rows} rows`)}
          onCommit={(v) => v !== null && edit((l) => setTable(l, { hole_offset_mm: v }))}
        />
        <Row label={tr("孔位样式", "Hole style")} help={tr("metric 和 imperial 只是标注；none 表示台面上不画孔和网格。", "metric and imperial are labels only; none draws no holes and no grid on the table.")}>
          <select
            className="w-full min-w-0 rounded-mast-ctl border border-mast-border-strong bg-mast-panel px-2 py-1 text-sm text-mast-text outline-none focus:border-mast-accent disabled:opacity-60"
            disabled={readOnly}
            value={t.grid}
            aria-label={tr("孔位样式", "Hole style")}
            onChange={(e) => edit((l) => setTable(l, { grid: e.target.value }))}
          >
            {GRID_KINDS.map((g) => (
              <option key={g} value={g}>
                {g === "none" ? tr("none（不画孔和网格）", "none (no holes, no grid)") : g}
              </option>
            ))}
          </select>
        </Row>
      </Group>

      <Group title={tr("文字标注", "Text labels")} defaultOpen={layout.texts.length > 0}>
        {layout.texts.length === 0 && <p className="text-xs text-mast-faint">{tr("画布上的自由文字，比如“机箱门”。", "Free text on the canvas, such as “enclosure door”.")}</p>}
        {layout.texts.map((x) => (
          <div key={x.id} className="space-y-1 rounded-mast-ctl border border-mast-border p-2">
            <CommitText value={x.text} disabled={readOnly} maxLength={LIMITS.stringMax} aria-label={tr("标注文字", "Label text")} onCommit={(v) => edit((l) => updateText(l, x.id, { text: v }))} />
            <div className="flex flex-wrap items-center gap-2 text-xs text-mast-muted">
              <span>x</span>
              <div className="w-20">
                <CommitNumber value={x.x_mm} disabled={readOnly} rules={{ step: 1 }} onCommit={(v) => v !== null && edit((l) => updateText(l, x.id, { x_mm: v }))} aria-label="x" />
              </div>
              <span>y</span>
              <div className="w-20">
                <CommitNumber value={x.y_mm} disabled={readOnly} rules={{ step: 1 }} onCommit={(v) => v !== null && edit((l) => updateText(l, x.id, { y_mm: v }))} aria-label="y" />
              </div>
              <span>{tr("大小", "size")}</span>
              <div className="w-16">
                <CommitNumber value={x.size} disabled={readOnly} rules={{ min: LIMITS.textSizeMin, max: LIMITS.textSizeMax, step: 0.1 }} onCommit={(v) => v !== null && edit((l) => updateText(l, x.id, { size: v }))} aria-label={tr("大小", "size")} />
              </div>
              <button type="button" disabled={readOnly} className="ml-auto text-mast-muted hover:text-mast-danger disabled:opacity-50" aria-label={tr("删除这条标注", "Delete this label")} onClick={() => edit((l) => removeText(l, x.id))}>
                ✕
              </button>
            </div>
          </div>
        ))}
        <button type="button" disabled={readOnly || layout.texts.length >= LIMITS.texts} className="rounded-mast-ctl border border-dashed border-mast-border px-2 py-0.5 text-xs text-mast-muted hover:border-mast-border-strong hover:text-mast-text disabled:opacity-50" onClick={onAddText}>
          + {tr("加一条文字", "Add a text label")}
        </button>
      </Group>

      <Group title={tr("快捷键", "Keyboard")} defaultOpen={false}>
        <dl className="space-y-1 text-xs">
          {shortcutHelp().map((h) => (
            <div key={h.keys} className="grid grid-cols-[7.5rem_minmax(0,1fr)] gap-2">
              <dt>
                <kbd className="rounded border border-mast-border bg-mast-code-bg px-1 py-0.5 font-mono text-[11px] text-mast-text">{h.keys}</kbd>
              </dt>
              <dd className="text-mast-muted">{tr(h.zh, h.en)}</dd>
            </div>
          ))}
          <div className="grid grid-cols-[7.5rem_minmax(0,1fr)] gap-2">
            <dt>
              <kbd className="rounded border border-mast-border bg-mast-code-bg px-1 py-0.5 font-mono text-[11px] text-mast-text">Ctrl</kbd>
            </dt>
            <dd className="text-mast-muted">{tr("拖动时按住：暂时不吸附", "Hold while dragging: no snapping")}</dd>
          </div>
          <div className="grid grid-cols-[7.5rem_minmax(0,1fr)] gap-2">
            <dt>
              <kbd className="rounded border border-mast-border bg-mast-code-bg px-1 py-0.5 font-mono text-[11px] text-mast-text">Shift</kbd>
            </dt>
            <dd className="text-mast-muted">{tr("点击多选；在空白处拖出方框框选", "Click to add to the selection; drag a box on empty canvas to select several")}</dd>
          </div>
        </dl>
      </Group>
    </div>
  );
}
