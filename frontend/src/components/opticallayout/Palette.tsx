import { memo, useMemo, useState } from "react";
import clsx from "clsx";
import { tr } from "@/lib/i18n";
import type { ComponentType } from "@/lib/opticalLayout/types";
import { CATEGORY_ORDER, categoryLabel, typeLabel, typesIn } from "@/lib/opticalLayout/catalog";
import { glyphShapes, shapeBounds } from "@/lib/opticalLayout/glyphs";
import { newComponent } from "@/lib/opticalLayout/edit";
import type { ThemeName } from "@/lib/opticalLayout/colors";
import { useUiStore } from "@/store";
import { TEMPLATES, type Template } from "@/lib/opticalLayout/templates";
import { rotatedBox } from "@/lib/opticalLayout/exportStyle";
import { GlyphShapes } from "./GlyphShapes";
import { PALETTE_MIME, TEMPLATE_MIME } from "./LayoutCanvas";

// The component palette, grouped by category (section 8 of
// docs/v2/design/optical_layout.md). A click adds the component at the centre of the view;
// dragging one onto the canvas drops it where it lands. The icons are the same glyphs the
// canvas draws, scaled to fit. Below the components, the assemblies (v2 section 8.6): groups that
// are set up together, added in one go and turned as a group afterwards.

const ICON_W = 44;
const ICON_H = 28;

const TypeIcon = memo(function TypeIcon({ type, theme }: { type: ComponentType; theme: ThemeName }) {
  const { shapes, vb, k } = useMemo(() => {
    const shapes = glyphShapes(newComponent(type, "p", { x: 0, y: 0 }));
    const b = shapeBounds(shapes);
    const pad = Math.max(2, Math.max(b.maxX - b.minX, b.maxY - b.minY) * 0.08);
    // the group below flips y, so the viewBox is taken in flipped coordinates
    const w = b.maxX - b.minX + 2 * pad;
    const h = b.maxY - b.minY + 2 * pad;
    // the scale the icon ends up at, so the glyph leaves out the details too small to see
    return { shapes, vb: `${b.minX - pad} ${-b.maxY - pad} ${w} ${h}`, k: Math.min(ICON_W / w, ICON_H / h) };
  }, [type]);
  return (
    <svg width={ICON_W} height={ICON_H} viewBox={vb} preserveAspectRatio="xMidYMid meet" aria-hidden="true" className="shrink-0">
      <g transform="scale(1 -1)">
        <GlyphShapes shapes={shapes} theme={theme} k={k} angle={0} />
      </g>
    </svg>
  );
});

/** An assembly drawn as its parts, laid out as they will be on the table (a beam along +x). */
const TemplateIcon = memo(function TemplateIcon({ template, theme }: { template: Template; theme: ThemeName }) {
  const { parts, vb, k } = useMemo(() => {
    const parts = template.parts.map((p, i) => {
      const c = newComponent(p.type, `p${i}`, { x: p.dx, y: p.dy }, { angle_deg: p.angle, ...(p.props ? { props: p.props } : {}), ...(p.aperture !== undefined ? { aperture_mm: p.aperture } : {}) });
      return { c, shapes: glyphShapes(c) };
    });
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    for (const { c, shapes } of parts) {
      const b = rotatedBox(c, shapeBounds(shapes));
      minX = Math.min(minX, b.minX);
      minY = Math.min(minY, b.minY);
      maxX = Math.max(maxX, b.maxX);
      maxY = Math.max(maxY, b.maxY);
    }
    const pad = Math.max(2, Math.max(maxX - minX, maxY - minY) * 0.06);
    const w = maxX - minX + 2 * pad;
    const h = maxY - minY + 2 * pad;
    return { parts, vb: `${minX - pad} ${-maxY - pad} ${w} ${h}`, k: Math.min(TEMPLATE_ICON_W / w, ICON_H / h) };
  }, [template]);
  return (
    <svg width={TEMPLATE_ICON_W} height={ICON_H} viewBox={vb} preserveAspectRatio="xMidYMid meet" aria-hidden="true" className="shrink-0">
      {parts.map(({ c, shapes }) => (
        <g key={c.id} transform={`translate(${c.x_mm} ${-c.y_mm}) rotate(${-c.angle_deg}) scale(1 -1)`}>
          <GlyphShapes shapes={shapes} theme={theme} k={k} angle={c.angle_deg} />
        </g>
      ))}
    </svg>
  );
});

const TEMPLATE_ICON_W = 64;

// memo: the editor re-renders on every frame of a drag; the palette has nothing to redraw then
export const Palette = memo(function Palette({
  onAdd,
  onAddTemplate,
  disabled,
  refusal,
}: {
  onAdd: (type: ComponentType) => void;
  /** adds an assembly (lib/opticalLayout/templates.ts) at the centre of the view */
  onAddTemplate: (id: string) => void;
  disabled: boolean;
  /** why nothing can be added (read-only revision, limits) */
  refusal: string | null;
}) {
  const theme = useUiStore((st) => st.theme);
  const [query, setQuery] = useState("");
  const q = query.trim().toLowerCase();
  const groups = useMemo(
    () =>
      CATEGORY_ORDER.map((cat) => ({
        cat,
        types: typesIn(cat).filter((t) => q === "" || t.includes(q) || typeLabel(t).toLowerCase().includes(q)),
      })).filter((g) => g.types.length > 0),
    [q],
  );
  const templates = useMemo(() => TEMPLATES.filter((t) => q === "" || t.id.includes(q) || t.name().toLowerCase().includes(q)), [q]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <input
        type="search"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder={tr("找器件…", "Find a component…")}
        aria-label={tr("找器件", "Find a component")}
        className="mb-2 w-full rounded-mast-ctl border border-mast-border-strong bg-mast-panel px-2 py-1 text-sm text-mast-text outline-none focus:border-mast-accent"
      />
      {refusal && <p className="mb-2 rounded-mast-ctl border border-mast-warn-border bg-mast-warn-bg px-2 py-1 text-xs text-mast-warn">{refusal}</p>}
      <div className="min-h-0 flex-1 space-y-3 overflow-auto pr-1">
        {groups.map(({ cat, types }) => (
          <section key={cat} aria-label={categoryLabel(cat)}>
            <h3 className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-mast-faint">{categoryLabel(cat)}</h3>
            <div className="grid grid-cols-2 gap-1.5">
              {types.map((t) => (
                <button
                  key={t}
                  type="button"
                  disabled={disabled}
                  draggable={!disabled}
                  title={`${typeLabel(t)}${tr("（点击添加，或拖到画布上）", " (click to add, or drag onto the canvas)")}`}
                  onClick={() => onAdd(t)}
                  onDragStart={(e) => {
                    e.dataTransfer.setData(PALETTE_MIME, t);
                    e.dataTransfer.setData("text/plain", t);
                    e.dataTransfer.effectAllowed = "copy";
                  }}
                  data-palette-type={t}
                  className={clsx(
                    "flex flex-col items-center gap-0.5 rounded-mast-ctl border border-mast-border bg-mast-panel px-1 py-1.5 text-center text-[11px] leading-tight text-mast-text",
                    disabled ? "cursor-not-allowed opacity-50" : "cursor-grab hover:border-mast-accent hover:bg-mast-panel-2",
                  )}
                >
                  <TypeIcon type={t} theme={theme} />
                  <span className="max-w-full truncate">{typeLabel(t)}</span>
                </button>
              ))}
            </div>
          </section>
        ))}
        {templates.length > 0 && (
          <section aria-label={tr("组件组", "Assemblies")} data-palette-assemblies="">
            <h3 className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-mast-faint">{tr("组件组", "Assemblies")}</h3>
            <div className="grid grid-cols-1 gap-1.5">
              {templates.map((t) => (
                <button
                  key={t.id}
                  type="button"
                  disabled={disabled}
                  draggable={!disabled}
                  title={`${t.name()}：${t.hint()}${tr("（点击添加，或拖到画布上；之后用 [ ] 整组旋转）", " (click to add, or drag onto the canvas; turn the group with [ and ] afterwards)")}`}
                  onClick={() => onAddTemplate(t.id)}
                  onDragStart={(e) => {
                    e.dataTransfer.setData(TEMPLATE_MIME, t.id);
                    e.dataTransfer.setData("text/plain", t.id);
                    e.dataTransfer.effectAllowed = "copy";
                  }}
                  data-palette-template={t.id}
                  className={clsx(
                    "flex items-center gap-1.5 rounded-mast-ctl border border-mast-border bg-mast-panel px-1.5 py-1 text-left text-[11px] leading-tight text-mast-text",
                    disabled ? "cursor-not-allowed opacity-50" : "cursor-grab hover:border-mast-accent hover:bg-mast-panel-2",
                  )}
                >
                  <TemplateIcon template={t} theme={theme} />
                  <span className="min-w-0 flex-1">{t.name()}</span>
                </button>
              ))}
            </div>
          </section>
        )}
        {groups.length === 0 && templates.length === 0 && <p className="text-xs text-mast-muted">{tr("没有匹配的器件。", "No component matches.")}</p>}
      </div>
    </div>
  );
});
