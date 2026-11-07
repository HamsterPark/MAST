import { memo, useId, useMemo, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import { tr } from "@/lib/i18n";
import type { LayoutComponent, LayoutText, OpticalLayout, TableSpec } from "@/lib/opticalLayout/types";
import type { DetailedBranch, DetailedTrace } from "@/lib/opticalLayout/trace";
import { branchPoints } from "@/lib/opticalLayout/trace";
import type { AlignLevel, HitIndicator, NearMiss } from "@/lib/opticalLayout/indicators";
import { componentLevel } from "@/lib/opticalLayout/indicators";
import { glyphRadius, glyphShapes } from "@/lib/opticalLayout/glyphs";
import { beamStyle, type ThemeName } from "@/lib/opticalLayout/colors";
import { holesVisible, niceStep, tickValues, toScreen, visibleBounds, type View } from "@/lib/opticalLayout/view";
import { hitMarks, planTexts } from "@/lib/opticalLayout/textPlan";
import { GlyphShapes } from "./GlyphShapes";

// The picture of a layout: table, holes, rulers, components, beams, alignment marks, names.
//
// It is a pure function of its props. The interactive canvas wraps it and adds the
// pointer handling; the SVG export renders the very same component to a string with a
// view that fits the whole table, so "the canvas as it is" holds by construction.
//
// Everything is placed in screen pixels (the y flip happens in `toScreen`), so strokes are
// as thick as a pixel and text is as large as a font size, at any zoom. Only the glyphs of
// the components live in a scaled group (millimetres), with non-scaling strokes.

export interface DisplayOptions {
  holes: boolean;
  grid: boolean;
  rulers: boolean;
  labels: boolean;
  beams: boolean;
  alignment: boolean;
  comments: boolean;
}

export const DEFAULT_DISPLAY: DisplayOptions = { holes: true, grid: true, rulers: true, labels: true, beams: true, alignment: true, comments: true };

export const RULER_LEFT = 32;
export const RULER_BOTTOM = 16;

export interface ComponentHandlers {
  onDown: (id: string, e: ReactPointerEvent<SVGGElement>) => void;
  onEnter: (id: string) => void;
  onLeave: (id: string) => void;
}

export interface LayoutSvgProps {
  layout: OpticalLayout;
  trace: DetailedTrace;
  indicators: ReadonlyMap<string, HitIndicator[]>;
  nearMisses?: readonly NearMiss[];
  view: View;
  width: number;
  height: number;
  theme: ThemeName;
  display: DisplayOptions;
  selected: ReadonlySet<string>;
  hover?: string | null;
  /** unresolved comments per component */
  commentCounts?: ReadonlyMap<string, number>;
  /** present on the live canvas, absent in the export */
  handlers?: ComponentHandlers;
}

const LEVEL_INK: Record<AlignLevel, string | undefined> = {
  ok: undefined,
  warn: "var(--mast-warn)",
  bad: "var(--mast-danger)",
};

const LEVEL_TEXT: Record<AlignLevel, string> = {
  ok: "var(--mast-auto)",
  warn: "var(--mast-warn)",
  bad: "var(--mast-danger)",
};

const FONT = "ui-monospace, 'Cascadia Code', Consolas, monospace";

/** Text that stays readable over beams and holes: a halo in the panel colour. */
const HALO = { paintOrder: "stroke" as const, stroke: "var(--mast-panel)", strokeWidth: 3, strokeLinejoin: "round" as const };

// ── table, grid, holes ────────────────────────────────────────────────────────

const Table = memo(function Table({ table, view, display }: { table: TableSpec; view: View; display: DisplayOptions }) {
  const pid = useId().replace(/:/g, "");
  const tl = toScreen(view, { x: 0, y: table.height_mm });
  const w = table.width_mm * view.k;
  const h = table.height_mm * view.k;

  const gridPath = useMemo(() => {
    if (!display.grid || table.grid === "none") return "";
    const step = Math.max(niceStep(100 / view.k, 1), 1e-6);
    const parts: string[] = [];
    const nx = Math.min(400, Math.floor(table.width_mm / step));
    for (let i = 1; i <= nx; i++) {
      const x = tl.x + i * step * view.k;
      parts.push(`M${x.toFixed(1)} ${tl.y.toFixed(1)}v${h.toFixed(1)}`);
    }
    const ny = Math.min(400, Math.floor(table.height_mm / step));
    for (let j = 1; j <= ny; j++) {
      const y = tl.y + h - j * step * view.k;
      parts.push(`M${tl.x.toFixed(1)} ${y.toFixed(1)}h${w.toFixed(1)}`);
    }
    return parts.join("");
  }, [display.grid, table.grid, view.k, table.width_mm, table.height_mm, tl.x, tl.y, w, h]);

  const showHoles = display.holes && table.grid !== "none" && table.hole_pitch_mm > 0 && holesVisible(table.hole_pitch_mm, view.k);
  const tile = table.hole_pitch_mm * view.k;
  const c0 = toScreen(view, { x: table.hole_offset_mm, y: table.hole_offset_mm });
  const holeR = Math.min(4, Math.max(0.8, 3 * view.k));

  return (
    <g>
      <rect x={tl.x} y={tl.y} width={w} height={h} fill="var(--mast-panel)" />
      {gridPath && <path d={gridPath} stroke="var(--mast-border)" strokeWidth={1} strokeOpacity={0.55} fill="none" />}
      {showHoles && (
        <>
          <defs>
            <pattern id={pid} patternUnits="userSpaceOnUse" x={c0.x - tile / 2} y={c0.y - tile / 2} width={tile} height={tile}>
              <circle cx={tile / 2} cy={tile / 2} r={holeR} fill="var(--mast-border-strong)" fillOpacity={0.55} />
            </pattern>
          </defs>
          <rect x={tl.x} y={tl.y} width={w} height={h} fill={`url(#${pid})`} />
        </>
      )}
      <rect x={tl.x} y={tl.y} width={w} height={h} fill="none" stroke="var(--mast-border-strong)" strokeWidth={1.5} />
    </g>
  );
});

// ── rulers ────────────────────────────────────────────────────────────────────

const Rulers = memo(function Rulers({ view, width, height }: { view: View; width: number; height: number }) {
  const vb = visibleBounds(view, width, height);
  const step = niceStep(90 / view.k, 1);
  const xs = tickValues(vb.minX, vb.maxX, step);
  const ys = tickValues(vb.minY, vb.maxY, step);
  const label = (v: number) => String(Math.round(v * 1000) / 1000);
  return (
    <g fontFamily={FONT} fontSize={9} fill="var(--mast-muted)" pointerEvents="none">
      <rect x={0} y={height - RULER_BOTTOM} width={width} height={RULER_BOTTOM} fill="var(--mast-panel)" fillOpacity={0.88} />
      <rect x={0} y={0} width={RULER_LEFT} height={height - RULER_BOTTOM} fill="var(--mast-panel)" fillOpacity={0.88} />
      {xs.map((v) => {
        const x = view.ox + v * view.k;
        if (x < RULER_LEFT) return null;
        return (
          <g key={`x${v}`}>
            <line x1={x} x2={x} y1={height - RULER_BOTTOM} y2={height - RULER_BOTTOM + 4} stroke="var(--mast-muted)" strokeWidth={1} />
            <text x={x + 2} y={height - 3}>{label(v)}</text>
          </g>
        );
      })}
      {ys.map((v) => {
        const y = view.oy - v * view.k;
        if (y > height - RULER_BOTTOM) return null;
        return (
          <g key={`y${v}`}>
            <line x1={RULER_LEFT - 4} x2={RULER_LEFT} y1={y} y2={y} stroke="var(--mast-muted)" strokeWidth={1} />
            <text x={2} y={y - 2}>{label(v)}</text>
          </g>
        );
      })}
      <text x={RULER_LEFT + 4} y={height - RULER_BOTTOM - 4} fontSize={9} fill="var(--mast-faint)">mm</text>
    </g>
  );
});

// ── texts of the layout itself ────────────────────────────────────────────────

const Texts = memo(function Texts({ texts, view }: { texts: readonly LayoutText[]; view: View }) {
  return (
    <g fontFamily="inherit" fill="var(--mast-muted)" pointerEvents="none">
      {texts.map((t) => {
        const p = toScreen(view, { x: t.x_mm, y: t.y_mm });
        const size = Math.max(9, 8 * Math.max(0.2, t.size) * view.k);
        return (
          <text key={t.id} x={p.x} y={p.y} fontSize={size} {...HALO}>
            {t.text}
          </text>
        );
      })}
    </g>
  );
});

// ── components ────────────────────────────────────────────────────────────────

interface GlyphProps {
  c: LayoutComponent;
  sx: number;
  sy: number;
  k: number;
  selected: boolean;
  hover: boolean;
  level: AlignLevel;
  interactive: boolean;
  onDown?: ComponentHandlers["onDown"];
  onEnter?: ComponentHandlers["onEnter"];
  onLeave?: ComponentHandlers["onLeave"];
}

const ComponentGlyph = memo(function ComponentGlyph({ c, sx, sy, k, selected, hover, level, interactive, onDown, onEnter, onLeave }: GlyphProps) {
  const shapes = useMemo(() => glyphShapes(c), [c]);
  const r = useMemo(() => glyphRadius(shapes), [shapes]);
  const ink = LEVEL_INK[level] ?? (selected || hover ? "var(--mast-accent)" : undefined);
  const grab = Math.max(r, 9 / k);
  return (
    <g
      data-component={c.id}
      transform={`translate(${sx} ${sy}) rotate(${-c.angle_deg}) scale(${k} ${-k})`}
      style={interactive ? { cursor: c.locked ? "not-allowed" : "move" } : undefined}
      onPointerDown={interactive && onDown ? (e) => onDown(c.id, e) : undefined}
      onPointerEnter={interactive && onEnter ? () => onEnter(c.id) : undefined}
      onPointerLeave={interactive && onLeave ? () => onLeave(c.id) : undefined}
    >
      {interactive && <circle r={grab} fill="transparent" />}
      <GlyphShapes shapes={shapes} ink={ink} />
    </g>
  );
});

function Lock({ x, y }: { x: number; y: number }) {
  return (
    <g transform={`translate(${x} ${y})`} stroke="var(--mast-muted)" strokeWidth={1.2} fill="none" pointerEvents="none">
      <rect x={-3} y={0} width={6} height={5} fill="var(--mast-muted)" stroke="none" />
      <path d="M-2 0v-2a2 2 0 0 1 4 0v2" />
    </g>
  );
}

// ── beams ─────────────────────────────────────────────────────────────────────

const END_MARK = 4;

interface BeamDraw {
  key: string;
  d: string;
  style: ReturnType<typeof beamStyle>;
  markers: ReactNode[];
}

function beamDraw(source: string, wavelength: number | null, branch: DetailedBranch, view: View, theme: ThemeName): BeamDraw | null {
  const pts = branchPoints(branch).map((p) => toScreen(view, p));
  if (pts.length < 2) return null;
  const style = beamStyle(wavelength, branch.fraction, theme);
  const d = pts.map((p, i) => `${i === 0 ? "M" : "L"}${p.x.toFixed(2)} ${p.y.toFixed(2)}`).join("");
  const last = pts[pts.length - 1]!;
  const markers: ReactNode[] = [];
  // a direction arrow in the middle of every long enough leg
  for (let i = 1; i < pts.length; i++) {
    const a = pts[i - 1]!;
    const b = pts[i]!;
    const len = Math.hypot(b.x - a.x, b.y - a.y);
    if (len < 70) continue;
    const ang = (Math.atan2(b.y - a.y, b.x - a.x) * 180) / Math.PI;
    markers.push(<path key={`a${i}`} d="M-4 -3.2L3 0L-4 3.2Z" transform={`translate(${(a.x + b.x) / 2} ${(a.y + b.y) / 2}) rotate(${ang})`} fill={style.color} fillOpacity={style.opacity} />);
  }
  if (branch.end === "blocked" || branch.end === "back_hit") {
    const colour = branch.end === "back_hit" ? "var(--mast-danger)" : "var(--mast-warn)";
    markers.push(<path key="end" d={`M${last.x - END_MARK} ${last.y - END_MARK}l${2 * END_MARK} ${2 * END_MARK}m0 ${-2 * END_MARK}l${-2 * END_MARK} ${2 * END_MARK}`} stroke={colour} strokeWidth={2} fill="none" />);
  } else if (branch.end === "max_events") {
    markers.push(<circle key="end" cx={last.x} cy={last.y} r={END_MARK} stroke="var(--mast-warn)" strokeWidth={1.6} fill="none" />);
  } else if (branch.end === "budget") {
    // where the trace stopped: the light beyond this point is not traced (the backend's picture has the same red square)
    markers.push(<rect key="end" x={last.x - 8} y={last.y - 8} width={16} height={16} fill="var(--mast-danger)" stroke="var(--mast-panel)" strokeWidth={2.5} />);
  } else if (branch.end === "stop") {
    markers.push(<circle key="end" cx={last.x} cy={last.y} r={2.4} fill={style.color} fillOpacity={style.opacity} />);
  }
  return { key: `${source}|${wavelength}|${branch.branch}`, d, style, markers };
}

// Every halo is drawn first and every coloured line after, so beams that share a path show
// through each other (the dashes of an infrared beam over a green one) instead of the later
// beam's halo dimming the earlier one.
const Beams = memo(function Beams({ trace, view, theme }: { trace: DetailedTrace; view: View; theme: ThemeName }) {
  const draws = useMemo(() => {
    const out: BeamDraw[] = [];
    for (const s of trace) {
      for (const b of s.branches) {
        const d = beamDraw(s.source, s.wavelength_nm, b, view, theme);
        if (d) out.push(d);
      }
    }
    return out;
  }, [trace, view, theme]);
  return (
    <g pointerEvents="none">
      <g>
        {draws.map((b) => (
          <path key={`h${b.key}`} d={b.d} fill="none" stroke="var(--mast-panel)" strokeOpacity={0.6} strokeWidth={b.style.width + 3} strokeLinejoin="round" strokeLinecap="round" />
        ))}
      </g>
      <g>
        {draws.map((b) => (
          <g key={`c${b.key}`} data-beam={b.key}>
            <path d={b.d} fill="none" stroke={b.style.color} strokeOpacity={b.style.opacity} strokeWidth={b.style.width} strokeDasharray={b.style.dash ?? undefined} strokeLinejoin="round" strokeLinecap="round" />
            {b.markers}
          </g>
        ))}
      </g>
    </g>
  );
});

// ── the whole picture ─────────────────────────────────────────────────────────

export const LayoutSvg = memo(function LayoutSvg(props: LayoutSvgProps) {
  const { layout, trace, indicators, nearMisses, view, width, height, theme, display, selected, hover, commentCounts, handlers } = props;
  const interactive = handlers !== undefined;

  const levels = useMemo(() => {
    const m = new Map<string, AlignLevel>();
    for (const [id, hits] of indicators) m.set(id, componentLevel(hits));
    return m;
  }, [indicators]);

  const selectedList = layout.components.filter((c) => selected.has(c.id));
  const readouts = display.beams && display.alignment;

  // Names, read-outs and near-miss notes compete for the same pixels: one plan decides.
  const texts = useMemo(
    () =>
      planTexts({
        components: layout.components,
        indicators,
        misses: nearMisses ?? [],
        view,
        selected,
        hover,
        showLabels: display.labels,
        showReadouts: readouts,
      }),
    [layout.components, indicators, nearMisses, view, selected, hover, display.labels, readouts],
  );
  const marks = useMemo(() => (readouts ? hitMarks(layout.components, indicators, view) : []), [readouts, layout.components, indicators, view]);

  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label={tr("光路图画布", "Optical layout canvas")} style={{ display: "block" }}>
      <rect x={0} y={0} width={width} height={height} fill="var(--mast-bg)" data-canvas-background="" />
      <Table table={layout.table} view={view} display={display} />
      {layout.texts.length > 0 && <Texts texts={layout.texts} view={view} />}
      <g>
        {layout.components.map((c) => {
          const p = toScreen(view, { x: c.x_mm, y: c.y_mm });
          return (
            <ComponentGlyph
              key={c.id}
              c={c}
              sx={p.x}
              sy={p.y}
              k={view.k}
              selected={selected.has(c.id)}
              hover={hover === c.id}
              level={levels.get(c.id) ?? "ok"}
              interactive={interactive}
              onDown={handlers?.onDown}
              onEnter={handlers?.onEnter}
              onLeave={handlers?.onLeave}
            />
          );
        })}
      </g>
      {display.beams && <Beams trace={trace} view={view} theme={theme} />}
      {readouts && (
        <g pointerEvents="none">
          {marks.map((m) => (
            <circle key={m.key} cx={m.at.x} cy={m.at.y} r={3.4} fill="none" stroke={LEVEL_TEXT[m.level]} strokeWidth={1.6} />
          ))}
          {(nearMisses ?? []).map((m) => {
            const p = toScreen(view, { x: m.x, y: m.y });
            return <path key={`${m.componentId}|${m.source}|${m.wavelength_nm}|${m.branch}`} d={`M${p.x - 4} ${p.y - 4}l8 8m0 -8l-8 8`} stroke="var(--mast-danger)" strokeWidth={1.8} fill="none" />;
          })}
        </g>
      )}
      <g pointerEvents="none" fontSize={11}>
        {texts.map((t) =>
          t.kind === "label" ? (
            <g key={t.key}>
              <text x={t.x} y={t.y} fill="var(--mast-text)" fontWeight={t.emphasis ? 700 : 400} {...HALO}>
                {t.text}
              </text>
              {t.locked && <Lock x={t.x + 3 + t.text.length * 6.4} y={t.y - 8} />}
            </g>
          ) : (
            <text key={t.key} x={t.x} y={t.y} fontFamily={FONT} fontSize={10} fill={t.kind === "miss" ? "var(--mast-danger)" : LEVEL_TEXT[t.level]} {...HALO}>
              {t.text}
            </text>
          ),
        )}
      </g>
      {display.comments && commentCounts && commentCounts.size > 0 && (
        <g pointerEvents="none">
          {layout.components.map((c) => {
            const n = commentCounts.get(c.id);
            if (!n) return null;
            const p = toScreen(view, { x: c.x_mm, y: c.y_mm });
            return (
              <g key={c.id} transform={`translate(${p.x - 11} ${p.y - 12})`}>
                <circle r={7} fill="var(--mast-info)" />
                <text y={3.5} textAnchor="middle" fontSize={9.5} fontWeight={700} fill="var(--mast-panel)">
                  {n > 9 ? "9+" : n}
                </text>
              </g>
            );
          })}
        </g>
      )}
      {selectedList.length > 0 && (
        <g pointerEvents="none">
          {selectedList.map((c) => {
            const p = toScreen(view, { x: c.x_mm, y: c.y_mm });
            const r = Math.max(10, glyphRadius(glyphShapes(c)) * view.k + 5);
            return <circle key={c.id} cx={p.x} cy={p.y} r={r} fill="none" stroke="var(--mast-accent)" strokeWidth={1.4} strokeDasharray="4 3" />;
          })}
        </g>
      )}
      {display.rulers && <Rulers view={view} width={width} height={height} />}
    </svg>
  );
});
