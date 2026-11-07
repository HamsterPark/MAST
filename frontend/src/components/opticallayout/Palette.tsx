import { memo, useMemo, useState } from "react";
import clsx from "clsx";
import { tr } from "@/lib/i18n";
import type { ComponentType } from "@/lib/opticalLayout/types";
import { CATEGORY_ORDER, categoryLabel, typeLabel, typesIn } from "@/lib/opticalLayout/catalog";
import { glyphShapes, shapeBounds } from "@/lib/opticalLayout/glyphs";
import { newComponent } from "@/lib/opticalLayout/edit";
import { GlyphShapes } from "./GlyphShapes";
import { PALETTE_MIME } from "./LayoutCanvas";

// The component palette, grouped by category (section 8 of
// docs/v2/design/optical_layout.md). A click adds the component at the centre of the view;
// dragging one onto the canvas drops it where it lands. The icons are the same glyphs the
// canvas draws, scaled to fit.

const ICON_W = 44;
const ICON_H = 28;

const TypeIcon = memo(function TypeIcon({ type }: { type: ComponentType }) {
  const { shapes, vb } = useMemo(() => {
    const shapes = glyphShapes(newComponent(type, "p", { x: 0, y: 0 }));
    const b = shapeBounds(shapes);
    const pad = Math.max(2, Math.max(b.maxX - b.minX, b.maxY - b.minY) * 0.08);
    // the group below flips y, so the viewBox is taken in flipped coordinates
    const w = b.maxX - b.minX + 2 * pad;
    const h = b.maxY - b.minY + 2 * pad;
    return { shapes, vb: `${b.minX - pad} ${-b.maxY - pad} ${w} ${h}` };
  }, [type]);
  return (
    <svg width={ICON_W} height={ICON_H} viewBox={vb} preserveAspectRatio="xMidYMid meet" aria-hidden="true" className="shrink-0">
      <g transform="scale(1 -1)">
        <GlyphShapes shapes={shapes} />
      </g>
    </svg>
  );
});

export function Palette({
  onAdd,
  disabled,
  refusal,
}: {
  onAdd: (type: ComponentType) => void;
  disabled: boolean;
  /** why nothing can be added (read-only revision, limits) */
  refusal: string | null;
}) {
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
                  <TypeIcon type={t} />
                  <span className="max-w-full truncate">{typeLabel(t)}</span>
                </button>
              ))}
            </div>
          </section>
        ))}
        {groups.length === 0 && <p className="text-xs text-mast-muted">{tr("没有匹配的器件。", "No component matches.")}</p>}
      </div>
    </div>
  );
}
