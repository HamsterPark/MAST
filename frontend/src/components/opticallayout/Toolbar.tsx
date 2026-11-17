import { useEffect, useRef, useState, type ReactNode } from "react";
import clsx from "clsx";
import { tr } from "@/lib/i18n";
import type { SnapMode } from "@/lib/opticalLayout/geometry";
import type { ComponentType } from "@/lib/opticalLayout/types";
import { CATEGORY_ORDER, categoryLabel, typeLabel, typesIn } from "@/lib/opticalLayout/catalog";
import { BEAM_LAYERS, LAYER_GROUPS, layerLabel } from "@/lib/opticalLayout/layers";
import { TEMPLATES } from "@/lib/opticalLayout/templates";
import { drawnIn3d } from "@/lib/opticalLayout/scene3d/layers3d";
import type { CanvasTool } from "./LayoutCanvas";
import type { DisplayOptions } from "./LayoutSvg";

// The strip above the canvas: tools, snapping, the layers, focus mode, undo / redo, save, export.

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


function Menu({ label, children, align = "left" }: { label: ReactNode; children: ReactNode; align?: "left" | "right" }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    // Escape closes the menu and is spent: the editor (which listens on window, after document) leaves it alone,
    // so it does not also leave focus mode or clear the selection
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.preventDefault();
      setOpen(false);
    };
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
  /** adds an assembly at the centre of the view */
  onAddTemplate: (id: string) => void;
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
  /** the figure export (white, black line art, coloured beams, scale bar) */
  onExportPaper: (kind: "svg" | "png") => void;
  /** whether the figure shows the table's holes */
  paperHoles: boolean;
  onPaperHoles: (on: boolean) => void;
  /** the figure as a vector PDF (the server converts the figure SVG; docs/v2/design/optical_layout_exports.md) */
  onExportPaperPdf: () => void;
  /** the figure PNG at a chosen resolution and size on paper (a dialog) */
  onExportPaperPngAt: () => void;
  /** the bill of materials, or the table of the beams, as CSV */
  onExportTable: (kind: "bom" | "beams") => void;
  /** focus mode: the palette and the inspector are folded away and the canvas takes the page */
  focus: boolean;
  onFocus: (on: boolean) => void;
  /** the 3D view instead of the 2D canvas (V); the buttons are there when the editor offers it */
  view3d?: boolean;
  onView3d?: (on: boolean) => void;
}

export function Toolbar(p: ToolbarProps) {
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-2" role="toolbar" aria-label={tr("光路图工具", "Layout tools")}>
      {/* the palette sits beside the canvas on wide windows; on narrow ones (and in focus mode) this menu replaces it */}
      <div className={p.focus ? undefined : "xl:hidden"}>
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
            <div>
              <div className="px-2 pt-1.5 text-[11px] font-semibold uppercase tracking-wide text-mast-faint">{tr("组件组", "Assemblies")}</div>
              {TEMPLATES.map((t) => (
                <MenuItem key={t.id} disabled={p.readOnly} onClick={() => p.onAddTemplate(t.id)} title={t.hint()}>
                  {t.name()}
                </MenuItem>
              ))}
            </div>
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

      <Menu label={tr("图层", "Layers")}>
        <div data-layers-menu="" className="min-w-[15rem]">
          {LAYER_GROUPS.map((g) => (
            <div key={g.key} role="group" aria-label={g.label()} className="pb-1">
              <div className="px-2 pt-1.5 text-[11px] font-semibold text-mast-faint">{g.label()}</div>
              {g.layers.map((id) => {
                // a layer about the beams has nothing to draw while the beams are off; the 3D view does not draw two of them
                const needsBeams = BEAM_LAYERS.includes(id) && !p.display.beams;
                const only2d = p.view3d === true && !drawnIn3d(id);
                return (
                  <label
                    key={id}
                    className={clsx("flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-sm hover:bg-mast-panel-2", needsBeams || only2d ? "text-mast-faint" : "text-mast-text", BEAM_LAYERS.includes(id) && "pl-6")}
                    title={only2d ? tr("仅 2D：3D 视图不画这一层", "2D only: the 3D view does not draw this layer") : needsBeams ? tr("先打开“光束”", "Turn the beams on first") : undefined}
                    data-only-2d={only2d ? "" : undefined}
                    onClick={(e) => e.stopPropagation()}
                  >
                    <input type="checkbox" checked={p.display[id]} disabled={only2d} onChange={(e) => p.onDisplay({ ...p.display, [id]: e.target.checked })} />
                    {layerLabel(id)}
                  </label>
                );
              })}
            </div>
          ))}
        </div>
      </Menu>

      <ToolButton on={p.focus} onClick={() => p.onFocus(!p.focus)} title={tr("专注模式 (F)：收起两侧面板，画布占满；Esc 退出", "Focus mode (F): fold the side panels away and let the canvas take the page; Esc leaves")}>
        <span aria-hidden="true">⤢</span> {tr("专注", "Focus")}
      </ToolButton>

      {p.onView3d && (
        <div className="flex items-center gap-1" role="group" aria-label={tr("2D / 3D 视图 (V)", "2D / 3D view (V)")} data-view-switch="">
          <ToolButton on={!p.view3d} onClick={() => p.onView3d?.(false)} title={tr("2D 俯视图 (V 切换)", "2D plan (V switches)")}>
            2D
          </ToolButton>
          <ToolButton on={p.view3d === true} onClick={() => p.onView3d?.(true)} title={tr("3D 视图 (V 切换)：同一张图、同一份追迹，可旋转、拖动器件、导出 PNG / GLB", "3D view (V switches): the same layout and trace; orbit, drag components, export PNG / GLB")}>
            3D
          </ToolButton>
        </div>
      )}

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
          <div className="px-2 pt-1 text-[11px] font-semibold text-mast-faint">{tr("论文风格：白底、黑色线稿、彩色光束、比例尺", "Figure: white, black line art, coloured beams, scale bar")}</div>
          <MenuItem onClick={() => p.onExportPaper("svg")} title={tr("矢量图，可直接插入论文或幻灯片，再缩放也清晰", "Vector, goes straight into a paper or a slide and stays sharp at any size")}>
            {tr("论文风格 SVG", "Figure SVG")}
          </MenuItem>
          <MenuItem onClick={() => p.onExportPaper("png")} title={tr("三倍分辨率的位图", "Bitmap at three times the resolution")}>
            {tr("论文风格 PNG", "Figure PNG")}
          </MenuItem>
          <MenuItem onClick={p.onExportPaperPngAt} title={tr("按纸面宽度选 150 / 300 / 600 dpi 或倍率；文件里写上纸面尺寸，Word 和 LaTeX 按它放", "150 / 300 / 600 dpi on a paper width, or a factor; the file records its size on paper, which Word and LaTeX use")}>
            {tr("论文风格 PNG，选分辨率…", "Figure PNG at a resolution…")}
          </MenuItem>
          <MenuItem onClick={p.onExportPaperPdf} title={tr("矢量 PDF，由服务器从论文风格 SVG 转成；中文字体嵌在里面，文字可选中", "Vector PDF, made by the server from the figure SVG; the Chinese font is embedded and the text can be selected")}>
            {tr("论文风格 PDF", "Figure PDF")}
          </MenuItem>
          <label className="flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-sm text-mast-text hover:bg-mast-panel-2" onClick={(e) => e.stopPropagation()}>
            <input type="checkbox" checked={p.paperHoles} onChange={(e) => p.onPaperHoles(e.target.checked)} />
            {tr("图里画出孔位", "Show the table's holes")}
          </label>
          <div className="my-1 border-t border-mast-border" />
          <MenuItem onClick={p.onExportSvg}>{tr("SVG（画布原样，整张台面）", "SVG (the canvas as drawn, whole table)")}</MenuItem>
          <MenuItem onClick={p.onExportPng} title={tr("由服务器渲染已保存的那一版", "Rendered by the server from the saved revision")}>
            {tr("PNG（服务器渲染）", "PNG (rendered by the server)")}
          </MenuItem>
          <div className="my-1 border-t border-mast-border" />
          <div className="px-2 pt-1 text-[11px] font-semibold text-mast-faint">{tr("表格：CSV，Excel 可直接打开", "Tables: CSV, opens in Excel")}</div>
          <MenuItem onClick={() => p.onExportTable("bom")} title={tr("每个器件一行（型号、厂家、用途、连接、位置……），后面按型号与厂家汇总数量", "One row per component (model, maker, function, wiring, position, ...), then the counts by model and manufacturer")}>
            {tr("物料清单 CSV", "Bill of materials (CSV)")}
          </MenuItem>
          <MenuItem onClick={() => p.onExportTable("beams")} title={tr("每段光一行：起止器件、长度、功率、偏振、光斑、束腰，与光束检查器一致", "One row per beam segment: from, to, length, power, polarisation, spot, waist, as the beam inspector shows them")}>
            {tr("光束表 CSV", "Beam table (CSV)")}
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
