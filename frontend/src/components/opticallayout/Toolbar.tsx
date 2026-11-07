import { useEffect, useRef, useState, type ReactNode } from "react";
import clsx from "clsx";
import { tr } from "@/lib/i18n";
import type { SnapMode } from "@/lib/opticalLayout/geometry";
import type { ComponentType } from "@/lib/opticalLayout/types";
import { CATEGORY_ORDER, categoryLabel, typeLabel, typesIn } from "@/lib/opticalLayout/catalog";
import type { CanvasTool } from "./LayoutCanvas";
import type { DisplayOptions } from "./LayoutSvg";

// The strip above the canvas: tools, snapping, what is drawn, undo / redo, save, export.

const BTN = "inline-flex items-center gap-1 rounded-mast-ctl border px-2.5 py-1 text-sm transition-colors disabled:cursor-not-allowed disabled:opacity-50";
const BTN_OFF = "border-mast-border-strong bg-mast-panel text-mast-text hover:bg-mast-panel-2";
const BTN_ON = "border-mast-accent bg-mast-accent-soft font-semibold text-mast-accent";

export function ToolButton({ on, onClick, disabled, title, children }: { on?: boolean; onClick: () => void; disabled?: boolean; title?: string; children: ReactNode }) {
  return (
    <button type="button" aria-pressed={on} title={title} disabled={disabled} className={clsx(BTN, on ? BTN_ON : BTN_OFF)} onClick={onClick}>
      {children}
    </button>
  );
}

const SNAP_LABEL: Record<SnapMode, () => string> = {
  off: () => tr("不吸附", "Off"),
  hole: () => tr("吸到孔", "Holes"),
  half: () => tr("吸到孔和半孔", "Holes and halves"),
  mm: () => tr("吸到整毫米", "Whole mm"),
};

const DISPLAY_ITEMS: Array<{ key: keyof DisplayOptions; label: () => string }> = [
  { key: "holes", label: () => tr("孔位", "Holes") },
  { key: "grid", label: () => tr("网格", "Grid") },
  { key: "rulers", label: () => tr("标尺", "Rulers") },
  { key: "labels", label: () => tr("名称", "Labels") },
  { key: "beams", label: () => tr("光束", "Beams") },
  { key: "alignment", label: () => tr("对准读数", "Alignment") },
  { key: "comments", label: () => tr("评论标记", "Comment pins") },
];

function Menu({ label, children, align = "left" }: { label: ReactNode; children: ReactNode; align?: "left" | "right" }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  return (
    <div className="relative" ref={ref}>
      <button type="button" aria-haspopup="menu" aria-expanded={open} className={clsx(BTN, BTN_OFF)} onClick={() => setOpen((o) => !o)}>
        {label} <span aria-hidden="true" className="text-xs">▾</span>
      </button>
      {open && (
        <div role="menu" className={clsx("absolute top-full z-30 mt-1 min-w-[11rem] rounded-mast-ctl border border-mast-border bg-mast-panel p-1.5 shadow-mast", align === "right" ? "right-0" : "left-0")} onClick={() => setOpen(false)}>
          {children}
        </div>
      )}
    </div>
  );
}

export function MenuItem({ onClick, disabled, children, title }: { onClick: () => void; disabled?: boolean; children: ReactNode; title?: string }) {
  return (
    <button type="button" role="menuitem" disabled={disabled} title={title} className="block w-full rounded px-2 py-1.5 text-left text-sm text-mast-text hover:bg-mast-panel-2 disabled:opacity-50" onClick={onClick}>
      {children}
    </button>
  );
}

export interface ToolbarProps {
  /** adds a component of a type at the centre of the view (the palette does the same, where it fits) */
  onAdd: (type: ComponentType) => void;
  tool: CanvasTool;
  onTool: (t: CanvasTool) => void;
  snap: SnapMode;
  onSnap: (s: SnapMode) => void;
  display: DisplayOptions;
  onDisplay: (d: DisplayOptions) => void;
  canUndo: boolean;
  canRedo: boolean;
  undoSteps: number;
  onUndo: () => void;
  onRedo: () => void;
  readOnly: boolean;
  dirty: boolean;
  saving: boolean;
  canSave: boolean;
  saveTitle?: string;
  onSave: () => void;
  onSaveAs: () => void;
  onExportSvg: () => void;
  onExportPng: () => void;
}

export function Toolbar(p: ToolbarProps) {
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-2" role="toolbar" aria-label={tr("光路图工具", "Layout tools")}>
      {/* the palette sits beside the canvas on wide windows; on narrow ones this menu replaces it */}
      <div className="xl:hidden">
        <Menu label={tr("添加器件", "Add")}>
          <div className="max-h-[26rem] overflow-auto">
            {CATEGORY_ORDER.map((cat) => (
              <div key={cat}>
                <div className="px-2 pt-1.5 text-[11px] font-semibold uppercase tracking-wide text-mast-faint">{categoryLabel(cat)}</div>
                {typesIn(cat).map((t) => (
                  <MenuItem key={t} disabled={p.readOnly} onClick={() => p.onAdd(t)}>
                    {typeLabel(t)}
                  </MenuItem>
                ))}
              </div>
            ))}
          </div>
        </Menu>
      </div>
      <div className="flex items-center gap-1" role="group" aria-label={tr("工具", "Tools")}>
        <ToolButton on={p.tool === "select"} onClick={() => p.onTool("select")} title={tr("选择、拖动", "Select and drag")}>
          {tr("选择", "Select")}
        </ToolButton>
        <ToolButton on={p.tool === "aim"} disabled={p.readOnly} onClick={() => p.onTool(p.tool === "aim" ? "select" : "aim")} title={tr("瞄准：先点镜子，再点目标", "Aim: click a mirror, then the target")}>
          {tr("瞄准", "Aim")}
        </ToolButton>
        <ToolButton on={p.tool === "measure"} onClick={() => p.onTool(p.tool === "measure" ? "select" : "measure")} title={tr("测量：点两个点", "Measure: click two points")}>
          {tr("测量", "Measure")}
        </ToolButton>
      </div>

      <label className="flex items-center gap-1.5 text-sm text-mast-muted">
        {tr("吸附", "Snap")}
        <select value={p.snap} onChange={(e) => p.onSnap(e.target.value as SnapMode)} className="rounded-mast-ctl border border-mast-border-strong bg-mast-panel px-1.5 py-1 text-sm text-mast-text" aria-label={tr("吸附方式", "Snap mode")}>
          {(Object.keys(SNAP_LABEL) as SnapMode[]).map((m) => (
            <option key={m} value={m}>
              {SNAP_LABEL[m]()}
            </option>
          ))}
        </select>
      </label>

      <Menu label={tr("显示", "View")}>
        {DISPLAY_ITEMS.map((it) => (
          <label key={it.key} className="flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-sm text-mast-text hover:bg-mast-panel-2" onClick={(e) => e.stopPropagation()}>
            <input type="checkbox" checked={p.display[it.key]} onChange={(e) => p.onDisplay({ ...p.display, [it.key]: e.target.checked })} />
            {it.label()}
          </label>
        ))}
      </Menu>

      <div className="flex items-center gap-1" role="group" aria-label={tr("撤销与重做", "Undo and redo")}>
        <button type="button" className={clsx(BTN, BTN_OFF)} disabled={!p.canUndo || p.readOnly} onClick={p.onUndo} title={tr(`撤销 (Ctrl+Z)，还能撤销 ${p.undoSteps} 步`, `Undo (Ctrl+Z), ${p.undoSteps} steps available`)}>
          ↶ {tr("撤销", "Undo")}
        </button>
        <button type="button" className={clsx(BTN, BTN_OFF)} disabled={!p.canRedo || p.readOnly} onClick={p.onRedo} title={tr("重做 (Ctrl+Y)", "Redo (Ctrl+Y)")}>
          ↷ {tr("重做", "Redo")}
        </button>
      </div>

      <div className="ml-auto flex items-center gap-2">
        <Menu label={tr("导出", "Export")} align="right">
          <MenuItem onClick={p.onExportSvg}>{tr("SVG（画布原样，整张台面）", "SVG (the canvas as drawn, whole table)")}</MenuItem>
          <MenuItem onClick={p.onExportPng} title={tr("由服务器渲染已保存的那一版", "Rendered by the server from the saved revision")}>
            {tr("PNG（服务器渲染）", "PNG (rendered by the server)")}
          </MenuItem>
        </Menu>
        <button type="button" disabled={p.readOnly} className={clsx(BTN, BTN_OFF)} onClick={p.onSaveAs} title={tr("用当前内容存成一张新的光路图", "Save the current content as a new layout")}>
          {tr("另存为…", "Save as…")}
        </button>
        <button
          type="button"
          disabled={!p.canSave || p.saving}
          title={p.saveTitle ?? tr("保存 (Ctrl+S)", "Save (Ctrl+S)")}
          className={clsx(BTN, "border-transparent bg-mast-accent font-semibold text-mast-accent-ink hover:opacity-90")}
          onClick={p.onSave}
        >
          {p.saving ? tr("保存中…", "Saving…") : p.dirty ? tr("保存 ●", "Save ●") : tr("已保存", "Saved")}
        </button>
      </div>
    </div>
  );
}
