import { memo, useCallback, useId, useMemo, type PointerEvent as ReactPointerEvent, type SVGProps } from "react";
import { tr } from "@/lib/i18n";
import type { LayoutComponent, LayoutText, OpticalLayout, TableSpec, Vec } from "@/lib/opticalLayout/types";
import type { DetailedTrace } from "@/lib/opticalLayout/trace";
import { branchPoints } from "@/lib/opticalLayout/trace";
import { branchPath } from "@/lib/opticalLayout/beam";
import { beamObstacles, beamScene, powerTags, type BeamScene, type BranchEndMark, type PolMark, type SegRef } from "@/lib/opticalLayout/beamDraw";
import type { AlignLevel, HitIndicator, NearMiss } from "@/lib/opticalLayout/indicators";
import { componentLevel } from "@/lib/opticalLayout/indicators";
import { glyphShapes, paperShapes, screenBox, shapeBounds, type ScreenBox, type ShapeBounds } from "@/lib/opticalLayout/glyphs";
import { DARK_P, beamLook, wavelengthLabel, type BeamLook, type ThemeName } from "@/lib/opticalLayout/colors";
import { holesVisible, niceStep, tickValues, toScreen, visibleBounds, type View } from "@/lib/opticalLayout/view";
import { POWER_DOT_PX, POWER_GAP_PX, POWER_PX, hitMarks, planTexts, powerTagWidth, type PlannedText } from "@/lib/opticalLayout/textPlan";
import { DEFAULT_LAYERS, layerShown, type Layers } from "@/lib/opticalLayout/layers";
import { PAPER_FONT, legendOf, scaleBar } from "@/lib/opticalLayout/exportStyle";
import { labelSize } from "@/lib/opticalLayout/labels";
import { dashedPath, parseDash, polylineOf } from "@/lib/opticalLayout/dash";
import { paperHolesPath } from "@/lib/opticalLayout/paperHoles";
import { GlyphShapes } from "./GlyphShapes";
import { heightMark } from "@/lib/opticalLayout/heights";
import { HEIGHT_TRIANGLE_EM } from "@/lib/opticalLayout/textPlan";
import { HeightMarkText } from "./HeightMark";

// The picture of a layout: table, holes, rulers, components, beams, alignment marks, names.
//
// It is a pure function of its props. The interactive canvas wraps it and adds the
// pointer handling; the SVG export renders the very same component to a string with a
// view that fits the whole table, so "the canvas as it is" holds by construction.
//
// Everything is placed in screen pixels (the y flip happens in `toScreen`), so strokes are
// as thick as a pixel and text is as large as a font size, at any zoom. Only the glyphs of
// the components live in a scaled group (millimetres), with non-scaling strokes.
//
// `variant="paper"` is the figure export (v2 section 8.5): the same drawing without anything that
// belongs to editing (grid, rulers, alignment read-outs, comment pins, locks, selection), glass
// outlined in ink, names in a font every viewer has, and a scale bar. Its colours are the design
// tokens like the canvas's; the export swaps them for print colours (lib/opticalLayout/exportStyle.ts).

/** What is drawn: the layers of the Layers menu (lib/opticalLayout/layers.ts). */
export type DisplayOptions = Layers;

export const DEFAULT_DISPLAY: Readonly<DisplayOptions> = DEFAULT_LAYERS;

export type SvgVariant = "screen" | "paper";

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
  /** "paper": the figure export (see above) */
  variant?: SvgVariant;
  /** the beam segment the beam inspector shows */
  beamPick?: SegRef | null;
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

/**
 * A text with a halo. On the screen one element with paint-order; in the figure (paper) two: the halo drawn as a
 * stroked copy first, the text over it, because slide programs, PDF converters and Illustrator ignore paint-order
 * and would paint the white stroke over the letters. The figure's text also names its font itself: MuPDF does not
 * take it from the group (it would fall back on a serif). The halo is a stroke and nothing else (fill none): the text
 * on top covers the inside of the letters anyway, and a PDF made from the figure then holds the halo once, not as a
 * filled and a stroked copy that a reader would select and find as two more texts.
 */
function HaloText({ paper, children, ...p }: SVGProps<SVGTextElement> & { paper: boolean }) {
  if (!paper) {
    return (
      <text {...p} {...HALO}>
        {children}
      </text>
    );
  }
  const q = { fontFamily: PAPER_FONT, ...p };
  return (
    <g>
      <text {...q} fill="none" stroke="var(--mast-panel)" strokeWidth={3} strokeLinejoin="round" aria-hidden="true">
        {children}
      </text>
      <text {...q}>{children}</text>
    </g>
  );
}

/** A beam line of the figure with its dashes drawn as segments (see lib/opticalLayout/dash.ts); null: keep the dasharray. */
function paperBeamDashes(d: string, dash: string | null): string | null {
  const pattern = parseDash(dash);
  const pts = pattern ? polylineOf(d) : null;
  return pattern && pts ? dashedPath(pts, pattern) : null;
}

// ── table, grid, holes ────────────────────────────────────────────────────────

const Table = memo(function Table({ table, view, display, paper, width, height }: { table: TableSpec; view: View; display: DisplayOptions; paper: boolean; width: number; height: number }) {
  const pid = useId().replace(/:/g, "");
  const tl = toScreen(view, { x: 0, y: table.height_mm });
  const w = table.width_mm * view.k;
  const h = table.height_mm * view.k;

  const gridPath = useMemo(() => {
    if (!display.grid || paper || table.grid === "none") return "";
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
  }, [display.grid, paper, table.grid, view.k, table.width_mm, table.height_mm, tl.x, tl.y, w, h]);

  const showHoles = display.holes && table.grid !== "none" && table.hole_pitch_mm > 0 && holesVisible(table.hole_pitch_mm, view.k);
  const tile = table.hole_pitch_mm * view.k;
  const c0 = toScreen(view, { x: table.hole_offset_mm, y: table.hole_offset_mm });
  const holeR = Math.min(4, Math.max(0.8, 3 * view.k));
  // the figure draws every hole itself (lib/opticalLayout/paperHoles.ts): MuPDF, and with it the PDF export, fills a
  // <pattern> solid black. The screen keeps the pattern.
  const paperHoles = useMemo(
    () => (paper && showHoles ? paperHolesPath(table, view, width, height, holeR) : null),
    [paper, showHoles, table, view.k, view.ox, view.oy, width, height, holeR],
  );

  return (
    <g>
      <rect x={tl.x} y={tl.y} width={w} height={h} fill="var(--mast-panel)" />
      {gridPath && <path d={gridPath} stroke="var(--mast-border)" strokeWidth={1} strokeOpacity={0.55} fill="none" />}
      {showHoles && !paper && (
        <>
          <defs>
            <pattern id={pid} patternUnits="userSpaceOnUse" x={c0.x - tile / 2} y={c0.y - tile / 2} width={tile} height={tile}>
              <circle cx={tile / 2} cy={tile / 2} r={holeR} fill="var(--mast-border-strong)" fillOpacity={0.55} />
            </pattern>
          </defs>
          <rect x={tl.x} y={tl.y} width={w} height={h} fill={`url(#${pid})`} />
        </>
      )}
      {paperHoles && paperHoles.d && <path d={paperHoles.d} fill="var(--mast-border-strong)" fillOpacity={0.55} />}
      <rect x={tl.x} y={tl.y} width={w} height={h} fill="none" stroke="var(--mast-border-strong)" strokeWidth={paper ? 1 : 1.5} />
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

/** The font size (px) of a text note at zoom k: it grows with the zoom, never below 9 px. */
export function layoutTextPx(t: Pick<LayoutText, "size">, k: number): number {
  return Math.max(9, 8 * Math.max(0.2, t.size) * k);
}

const Texts = memo(function Texts({ texts, view, paper }: { texts: readonly LayoutText[]; view: View; paper: boolean }) {
  return (
    <g fontFamily={paper ? PAPER_FONT : "inherit"} fill="var(--mast-muted)" pointerEvents="none">
      {texts.map((t) => {
        const p = toScreen(view, { x: t.x_mm, y: t.y_mm });
        return (
          <HaloText key={t.id} paper={paper} x={p.x} y={p.y} fontSize={layoutTextPx(t, view.k)}>
            {t.text}
          </HaloText>
        );
      })}
    </g>
  );
});

// ── components ────────────────────────────────────────────────────────────────

/** The glyph box of a component, worked out once per component object (an edit makes a new object). */
const BOUNDS = new WeakMap<LayoutComponent, ShapeBounds>();
function boundsOf(c: LayoutComponent): ShapeBounds {
  let b = BOUNDS.get(c);
  if (!b) {
    b = shapeBounds(glyphShapes(c));
    BOUNDS.set(c, b);
  }
  return b;
}

/** The screen box of a component's glyph at zoom k, kept per component object for the last zoom asked. */
const SCREEN_BOXES = new WeakMap<LayoutComponent, { k: number; box: ScreenBox }>();
function screenBoxOf(c: LayoutComponent, k: number): ScreenBox {
  const hit = SCREEN_BOXES.get(c);
  if (hit && hit.k === k) return hit.box;
  const box = screenBox(boundsOf(c), c.angle_deg, k);
  SCREEN_BOXES.set(c, { k, box });
  return box;
}

/** The same for the figure, whose texts and dials may be drawn larger (glyphs.ts paperShapes); worked out per export. */
function paperBoxOf(c: LayoutComponent, k: number): ScreenBox {
  return screenBox(shapeBounds(paperShapes(glyphShapes(c), k)), c.angle_deg, k);
}

interface GlyphProps {
  c: LayoutComponent;
  sx: number;
  sy: number;
  k: number;
  theme: ThemeName;
  paper: boolean;
  selected: boolean;
  hover: boolean;
  level: AlignLevel;
  interactive: boolean;
  onDown?: ComponentHandlers["onDown"];
  onEnter?: ComponentHandlers["onEnter"];
  onLeave?: ComponentHandlers["onLeave"];
}

/** A selected glyph smaller than this (half extent, px) also gets a ring: its glow alone would be hard to see. */
const SMALL_GLYPH_PX = 12;

/** Pixels the grab area reaches beyond a glyph, and the smallest grab area (so a speck can still be picked up). */
const GRAB_PAD_PX = 4;
const GRAB_MIN_PX = 18;

const ComponentGlyph = memo(function ComponentGlyph({ c, sx, sy, k, theme, paper, selected, hover, level, interactive, onDown, onEnter, onLeave }: GlyphProps) {
  const shapes = useMemo(() => glyphShapes(c), [c]);
  const b = useMemo(() => shapeBounds(shapes), [shapes]);
  const ink = LEVEL_INK[level] ?? (selected ? "var(--mast-accent)" : undefined);
  // the grab area is the glyph's own box (not a circle round it, which would take the clicks meant for a beam
  // that leaves a long laser), padded a little and never smaller than a fingertip
  const pad = GRAB_PAD_PX / k;
  const half = GRAB_MIN_PX / 2 / k;
  const gx0 = Math.min(b.minX - pad, (b.minX + b.maxX) / 2 - half);
  const gx1 = Math.max(b.maxX + pad, (b.minX + b.maxX) / 2 + half);
  const gy0 = Math.min(b.minY - pad, (b.minY + b.maxY) / 2 - half);
  const gy1 = Math.max(b.maxY + pad, (b.minY + b.maxY) / 2 + half);
  return (
    <g
      data-component={c.id}
      transform={`translate(${sx} ${sy}) rotate(${-c.angle_deg}) scale(${k} ${-k})`}
      style={interactive ? { cursor: c.locked ? "not-allowed" : "move" } : undefined}
      onPointerDown={interactive && onDown ? (e) => onDown(c.id, e) : undefined}
      onPointerEnter={interactive && onEnter ? () => onEnter(c.id) : undefined}
      onPointerLeave={interactive && onLeave ? () => onLeave(c.id) : undefined}
    >
      {interactive && <rect x={gx0} y={gy0} width={gx1 - gx0} height={gy1 - gy0} fill="transparent" />}
      {(selected || hover) && <GlyphShapes shapes={shapes} theme={theme} k={k} angle={c.angle_deg} halo={{ color: "var(--mast-accent)", opacity: selected ? 0.34 : 0.2 }} />}
      <GlyphShapes shapes={shapes} theme={theme} k={k} angle={c.angle_deg} ink={ink} paper={paper} />
    </g>
  );
});

function Lock({ x, y }: { x: number; y: number }) {
  return (
    <g transform={`translate(${x} ${y})`} pointerEvents="none" data-lock="">
      <circle cx={0} cy={1} r={6.5} fill="var(--mast-panel)" stroke="var(--mast-border-strong)" strokeWidth={1} />
      <path d="M-2 0v-1.6a2 2 0 0 1 4 0v1.6" fill="none" stroke="var(--mast-muted)" strokeWidth={1.2} />
      <rect x={-3} y={0} width={6} height={4.4} rx={0.8} fill="var(--mast-muted)" />
    </g>
  );
}

// ── beams ─────────────────────────────────────────────────────────────────────
//
// What is drawn comes from lib/opticalLayout/beamDraw.ts (beamScene), in this order: the halos that keep
// crossing beams apart, the 1/e² envelope bands (their real size, translucent), the picked beam's highlight,
// the beam lines (width and opacity by power, "no light" dotted grey), the waist ticks, the direction
// arrows, the ends, the polarisation marks.

const END_MARK = 4;

type EndMarkerProps = { branch: { end: BranchEndMark["end"] }; last: Vec; look: BeamLook };
const sameEnd = (a: EndMarkerProps, b: EndMarkerProps) => a.branch.end === b.branch.end && a.last.x === b.last.x && a.last.y === b.last.y && sameLookValue(a.look, b.look);

const EndMarker = memo(function EndMarker({ branch, last, look }: EndMarkerProps) {
  if (branch.end === "blocked" || branch.end === "back_hit") {
    const colour = branch.end === "back_hit" ? "var(--mast-danger)" : "var(--mast-warn)";
    return <path d={`M${last.x - END_MARK} ${last.y - END_MARK}l${2 * END_MARK} ${2 * END_MARK}m0 ${-2 * END_MARK}l${-2 * END_MARK} ${2 * END_MARK}`} stroke={colour} strokeWidth={2} fill="none" />;
  }
  if (branch.end === "max_events") return <circle cx={last.x} cy={last.y} r={END_MARK} stroke="var(--mast-warn)" strokeWidth={1.6} fill="none" />;
  if (branch.end === "budget") {
    // where the trace stopped: the light beyond this point is not traced (the backend's picture has the same red square)
    return <rect key="end" x={last.x - 8} y={last.y - 8} width={16} height={16} fill="var(--mast-danger)" stroke="var(--mast-panel)" strokeWidth={2.5} />;
  }
  if (branch.end === "stop" && !look.dark) return <circle cx={last.x} cy={last.y} r={2.4} fill={look.color} fillOpacity={look.opacity} />;
  return null;
}, sameEnd);

/** A double arrow across the beam: the field in the table plane (p). Drawn in a frame turned to the beam. */
function PArrow({ half = 6.5 }: { half?: number }) {
  return (
    <>
      <path d={`M0 ${-half}V${half}`} stroke="var(--mast-panel)" strokeWidth={3.6} strokeLinecap="round" />
      <path d={`M0 ${-half}V${half}M-2.4 ${-half + 3}L0 ${-half}L2.4 ${-half + 3}M-2.4 ${half - 3}L0 ${half}L2.4 ${half - 3}`} stroke="var(--mast-text)" strokeWidth={1.3} fill="none" strokeLinecap="round" strokeLinejoin="round" />
    </>
  );
}

/** Dots on the beam: the field vertical to the table (s). */
function SDots({ at = [-4.5, 4.5] }: { at?: number[] }) {
  return (
    <>
      {at.map((x) => (
        <circle key={x} cx={x} cy={0} r={2.1} fill="var(--mast-text)" stroke="var(--mast-panel)" strokeWidth={1.2} />
      ))}
    </>
  );
}

/** The polarisation ellipse seen against the light: right = e_h (the left of the direction of travel), up = up. */
export function PolBadgeShape({ reading, r = 8, paper = false }: { reading: PolMark["reading"]; r?: number; paper?: boolean }) {
  const R = r * 0.68;
  const chi = Math.abs(reading.chi_deg) * (Math.PI / 180);
  const a = R * Math.cos(chi);
  const b = R * Math.sin(chi);
  const fade = 0.35 + 0.65 * Math.min(1, Math.max(0, reading.dop));
  return (
    <>
      <circle r={r} fill="var(--mast-panel)" stroke="var(--mast-border-strong)" strokeWidth={paper ? 0.8 : 1} />
      {paper ? (
        <path
          d={`${dashedPath([{ x: -r + 1.5, y: 0 }, { x: r - 1.5, y: 0 }], [1.2, 1.4])}${dashedPath([{ x: 0, y: -r + 1.5 }, { x: 0, y: r - 1.5 }], [1.2, 1.4])}`}
          stroke="var(--mast-faint)"
          strokeWidth={0.6}
        />
      ) : (
        <path d={`M${-r + 1.5} 0H${r - 1.5}M0 ${-r + 1.5}V${r - 1.5}`} stroke="var(--mast-faint)" strokeWidth={0.6} strokeDasharray="1.2 1.4" />
      )}
      {reading.kind === "unpolarized" ? (
        // every direction at once: both axes, as the mark on the beam has both
        <path
          d={`M${-R} 0H${R}M0 ${-R}V${R}M${-R + 2} -1.8L${-R} 0L${-R + 2} 1.8M${R - 2} -1.8L${R} 0L${R - 2} 1.8M-1.8 ${-R + 2}L0 ${-R}L1.8 ${-R + 2}M-1.8 ${R - 2}L0 ${R}L1.8 ${R - 2}`}
          stroke="var(--mast-text)"
          strokeWidth={1.1}
          fill="none"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      ) : (
        <g transform={`rotate(${-reading.psi_deg})`} opacity={fade}>
          {b < 0.7 ? (
            <path d={`M${-a} 0H${a}M${-a + 2} -1.8L${-a} 0L${-a + 2} 1.8M${a - 2} -1.8L${a} 0L${a - 2} 1.8`} stroke="var(--mast-text)" strokeWidth={1.3} fill="none" strokeLinecap="round" strokeLinejoin="round" />
          ) : (
            <ellipse rx={a} ry={b} fill="none" stroke="var(--mast-text)" strokeWidth={1.3} />
          )}
        </g>
      )}
    </>
  );
}

/** Two looks of a beam the same (the scene makes new objects every frame: compared by value). */
const sameLookValue = (a: BeamLook, b: BeamLook) => a.color === b.color && a.width === b.width && a.opacity === b.opacity && a.dash === b.dash && a.dark === b.dark;

/** Two polarisation marks the same, by value. */
function samePolMark(a: { m: PolMark; paper: boolean }, b: { m: PolMark; paper: boolean }): boolean {
  const p = a.m;
  const q = b.m;
  const r = p.reading;
  const s = q.reading;
  return a.paper === b.paper && p.kind === q.kind && p.x === q.x && p.y === q.y && p.deg === q.deg && p.bx === q.bx && p.by === q.by && r.kind === s.kind && r.dop === s.dop && r.psi_deg === s.psi_deg && r.chi_deg === s.chi_deg && r.ps === s.ps;
}

const PolMarkGlyph = memo(function PolMarkGlyph({ m, paper }: { m: PolMark; paper: boolean }) {
  const fade = 0.4 + 0.6 * Math.min(1, Math.max(0, m.reading.dop));
  if (m.kind === "badge") {
    return (
      <g data-pol="badge">
        <line x1={m.x} y1={m.y} x2={m.bx} y2={m.by} stroke="var(--mast-faint)" strokeWidth={0.8} />
        <g transform={`translate(${m.bx} ${m.by})`}>
          <PolBadgeShape reading={m.reading} paper={paper} />
        </g>
      </g>
    );
  }
  return (
    <g data-pol={m.kind} transform={`translate(${m.x} ${m.y}) rotate(${m.deg})`} opacity={m.kind === "unpolarized" ? 1 : fade}>
      {m.kind !== "s" && <PArrow />}
      {m.kind === "s" && <SDots />}
      {m.kind === "unpolarized" && <SDots at={[0]} />}
    </g>
  );
}, samePolMark);

interface BeamsProps {
  trace: DetailedTrace;
  view: View;
  theme: ThemeName;
  envelope: boolean;
  polarization: boolean;
  width: number;
  height: number;
  paper: boolean;
  /** the beam the inspector shows: its whole path is underlaid, the picked segment more strongly */
  picked?: SegRef | null;
}

type BeamLineProps = { c: BeamScene["cores"][number]; paper: boolean };
const sameLine = (a: BeamLineProps, b: BeamLineProps) => a.paper === b.paper && a.c.key === b.c.key && a.c.d === b.c.d && sameLookValue(a.c.look, b.c.look);

/** One beam line: dashed by stroke-dasharray on the screen, by separate dashes in the figure. */
const BeamLine = memo(function BeamLine({ c, paper }: BeamLineProps) {
  const dashes = paper && c.look.dash ? paperBeamDashes(c.d, c.look.dash) : null;
  return (
    <path
      data-beam={c.key}
      d={dashes ?? c.d}
      fill="none"
      stroke={c.look.color}
      strokeOpacity={c.look.opacity}
      strokeWidth={c.look.width}
      strokeDasharray={dashes === null ? (c.look.dash ?? undefined) : undefined}
      strokeLinejoin="round"
      strokeLinecap={c.look.dark ? "butt" : "round"}
    />
  );
}, sameLine);

const Beams = memo(function Beams({ trace, view, theme, envelope, polarization, width, height, paper, picked }: BeamsProps) {
  const scene = useMemo(() => beamScene(trace, view, theme, { envelope, polarization, clip: { x0: 0, y0: 0, x1: width, y1: height } }), [trace, view, theme, envelope, polarization, width, height]);
  const pickedPath = useMemo(() => {
    if (!picked) return null;
    const tree = trace[picked.tree];
    if (!tree) return null;
    const path = branchPath(tree, picked.branch);
    if (path.length === 0) return null;
    const line = (pts: Vec[]) => pts.map((p, i) => `${i === 0 ? "M" : "L"}${p.x.toFixed(2)} ${p.y.toFixed(2)}`).join("");
    const all = path.map((b) => line(branchPoints(b).map((p) => toScreen(view, p)))).join("");
    const seg = path[path.length - 1]!.segments[picked.segment];
    const one = seg ? line([toScreen(view, { x: seg.x0, y: seg.y0 }), toScreen(view, { x: seg.x1, y: seg.y1 })]) : "";
    return { all, one };
  }, [trace, picked, view]);
  return (
    <g pointerEvents="none" data-beams="">
      <g>
        {scene.cores.map((c) => (
          <path key={`h${c.key}`} d={c.d} fill="none" stroke="var(--mast-panel)" strokeOpacity={0.6} strokeWidth={c.look.width + 3} strokeLinejoin="round" strokeLinecap="round" />
        ))}
      </g>
      {/* the bands are in table millimetres: the group carries the view (a pan or zoom rebuilds nothing); their edge is
          0.7 px on the screen, written as 0.7 / k in the figure, where vector-effect is not understood */}
      <g data-envelope="" transform={`translate(${view.ox} ${view.oy}) scale(${view.k} ${-view.k})`}>
        {scene.bands.map((b) => (
          <path
            key={b.key}
            d={b.d}
            fill={b.color}
            fillOpacity={b.opacity}
            stroke={b.color}
            strokeOpacity={Math.min(1, b.opacity * 2.2)}
            strokeWidth={paper ? 0.7 / view.k : 0.7}
            vectorEffect={paper ? undefined : "non-scaling-stroke"}
            strokeLinejoin="round"
          />
        ))}
      </g>
      {pickedPath && (
        <g data-picked-beam="">
          <path d={pickedPath.all} fill="none" stroke="var(--mast-accent)" strokeOpacity={0.28} strokeWidth={9} strokeLinejoin="round" strokeLinecap="round" />
          {pickedPath.one && <path d={pickedPath.one} fill="none" stroke="var(--mast-accent)" strokeOpacity={0.5} strokeWidth={9} strokeLinecap="round" />}
        </g>
      )}
      <g>
        {scene.cores.map((c) => (
          <BeamLine key={c.key} c={c} paper={paper} />
        ))}
      </g>
      <g data-waists="">
        {scene.waists.map((w) => (
          <line key={w.key} x1={w.x1} y1={w.y1} x2={w.x2} y2={w.y2} stroke={w.color} strokeWidth={1.4} strokeLinecap="round" />
        ))}
      </g>
      <g>
        {scene.arrows.map((a) => (
          <path key={a.key} d="M-4 -3.2L3 0L-4 3.2Z" transform={`translate(${a.x.toFixed(2)} ${a.y.toFixed(2)}) rotate(${a.deg.toFixed(1)})`} fill={a.color} fillOpacity={a.opacity} />
        ))}
      </g>
      <g>
        {scene.ends.map((e) => (
          <EndMarker key={e.key} branch={e} last={e.at} look={e.look} />
        ))}
      </g>
      {scene.pols.length > 0 && (
        <g data-polarization="">
          {scene.pols.map((m) => (
            <PolMarkGlyph key={m.key} m={m} paper={paper} />
          ))}
        </g>
      )}
    </g>
  );
});

// ── scale bar (paper) ─────────────────────────────────────────────────────────

/** Where the paper picture's scale bar starts (px from the left) and its baseline (px from the bottom). */
export const PAPER_BAR_X = 28;
export const PAPER_BAR_BOTTOM = 14;

function ScaleBarMark({ k, x, y }: { k: number; x: number; y: number }) {
  const bar = scaleBar(k, 160);
  return (
    <g data-scale-bar="" pointerEvents="none" fontFamily={PAPER_FONT} fontSize={12} fill="var(--mast-text)">
      <path d={`M${x} ${y - 9}v6h${bar.px.toFixed(2)}v-6`} fill="none" stroke="var(--mast-text)" strokeWidth={1.4} />
      <text x={x + bar.px + 6} y={y - 2} fontFamily={PAPER_FONT} fontSize={12}>
        {bar.label}
      </text>
    </g>
  );
}

/** The figure's legend, right-aligned on the scale bar's line: a short stretch of every beam colour and its wavelength. */
function PaperLegend({ trace, theme, right, y }: { trace: DetailedTrace; theme: ThemeName; right: number; y: number }) {
  const { entries, dark } = legendOf(trace, DARK_P);
  const items = [
    ...entries.map((e) => ({ key: String(e.nm), look: beamLook(e.nm, 1, theme), text: e.nm === null ? tr("未知波长", "unknown wavelength") : wavelengthLabel(e.nm) })),
    ...(dark ? [{ key: "dark", look: beamLook(null, 0, theme), text: tr("无光", "no light") }] : []),
  ];
  if (items.length === 0) return null;
  const SAMPLE = 22;
  const widths = items.map((it) => SAMPLE + 5 + labelSize(it.text, 12).w + 10);
  let x = right - widths.reduce((a, b) => a + b, 0);
  return (
    <g data-legend="" pointerEvents="none" fontFamily={PAPER_FONT} fontSize={12} fill="var(--mast-text)">
      {items.map((it, i) => {
        const x0 = x;
        x += widths[i]!;
        return (
          <g key={it.key}>
            <path
              d={paperBeamDashes(`M${x0} ${y - 6}L${x0 + SAMPLE} ${y - 6}`, it.look.dash) ?? `M${x0} ${y - 6}L${x0 + SAMPLE} ${y - 6}`}
              fill="none"
              stroke={it.look.color}
              strokeWidth={Math.min(it.look.width, 2.6)}
              strokeLinecap={it.look.dark ? "butt" : "round"}
            />
            <text x={x0 + SAMPLE + 5} y={y - 2} fontFamily={PAPER_FONT} fontSize={12}>
              {it.text}
            </text>
          </g>
        );
      })}
    </g>
  );
}

const NO_IDS: ReadonlySet<string> = new Set();
/** Up to this many components the canvas lets a crowded name move (above, beside) rather than drop it. */
const MOVABLE_LABELS_MAX = 120;

/** A power tag: for every beam ending on the component, a dot in its colour and the power it brings. */
function PowerTagText({ x, y, parts, paper }: { x: number; y: number; parts: ReadonlyArray<{ text: string; color: string }>; paper: boolean }) {
  let at = x;
  return (
    <g data-power-tag="">
      {parts.map((p, i) => {
        const x0 = at + (i > 0 ? POWER_GAP_PX : 0);
        // where this part ends: its width as the planner counts it (the dot included), less the box's padding
        at = x0 + powerTagWidth([p]) - 4;
        return (
          <g key={i}>
            <circle cx={x0 + 3.5} cy={y - 3.6} r={3.2} fill={p.color} stroke="var(--mast-panel)" strokeWidth={1} />
            <HaloText paper={paper} x={x0 + POWER_DOT_PX} y={y} fontFamily={paper ? PAPER_FONT : "inherit"} fontSize={POWER_PX} fontWeight={600} fill="var(--mast-text)">
              {p.text}
            </HaloText>
          </g>
        );
      })}
    </g>
  );
}

/** One tag per component: a coloured dot and a number for every beam that ends on it, in the language in use. */
function powerGroupsOf(trace: DetailedTrace, theme: ThemeName): Array<{ key: string; componentId: string; at: Vec; parts: Array<{ text: string; color: string }> }> {
  const byComponent = new Map<string, { key: string; componentId: string; at: Vec; parts: Array<{ text: string; color: string }> }>();
  for (const t of powerTags(trace, theme)) {
    const g = byComponent.get(t.componentId);
    const part = { text: tr(t.zh, t.en), color: t.color };
    if (g) g.parts.push(part);
    else byComponent.set(t.componentId, { key: t.componentId, componentId: t.componentId, at: t.at, parts: [part] });
  }
  return [...byComponent.values()];
}

/**
 * The boxes (screen px) of every text the figure draws at `view`: the names and power tags exactly as the paper
 * variant plans them, and the text notes. The export frames the picture on them too (a name or a power at the edge
 * of the drawing would otherwise be cut off).
 */
export function paperTextBoxes(layout: OpticalLayout, trace: DetailedTrace, view: View, display: DisplayOptions, theme: ThemeName): Array<{ x: number; y: number; w: number; h: number }> {
  const powers = layerShown(display, "power") ? powerGroupsOf(trace, theme) : [];
  const avoid = display.beams ? beamObstacles(trace, view) : [];
  const plan = planTexts({
    components: layout.components,
    indicators: NO_HITS,
    misses: [],
    view,
    selected: NO_IDS,
    hover: null,
    showLabels: display.labels,
    showReadouts: false,
    extent: (c) => paperBoxOf(c, view.k),
    labelPx: PAPER_LABEL_PX,
    powers,
    movable: true,
    avoid,
    heightMark: (c) => heightMark(c, layout.table),
  });
  const out = plan.map((t: PlannedText) => {
    if (t.kind === "power") {
      const h = labelSize("0", POWER_PX).h;
      return { x: t.x, y: t.y - 11, w: powerTagWidth(t.parts ?? []), h };
    }
    const s = labelSize(t.text, PAPER_LABEL_PX);
    return { x: t.x, y: t.y - PAPER_LABEL_PX, w: s.w, h: s.h };
  });
  // the height marks under the names (3D section 7), so that one at the edge is not cut off either
  for (const t of plan) {
    if (!t.sub) continue;
    const s = labelSize(t.sub.text, t.sub.px);
    const tri = t.sub.rise !== 0 ? Math.ceil(t.sub.px * HEIGHT_TRIANGLE_EM) : 0;
    out.push({ x: t.x + t.sub.dx, y: t.y + t.sub.dy - t.sub.px, w: s.w + tri, h: s.h });
  }
  if (display.texts) {
    for (const t of layout.texts) {
      const p = toScreen(view, { x: t.x_mm, y: t.y_mm });
      const px = layoutTextPx(t, view.k);
      const s = labelSize(t.text, px);
      out.push({ x: p.x, y: p.y - px, w: s.w, h: s.h });
    }
  }
  return out;
}

const NO_HITS: ReadonlyMap<string, HitIndicator[]> = new Map();
/** Names on the figure: larger than on the screen, the figure is usually shrunk to a column. */
const PAPER_LABEL_PX = 14;

// ── the whole picture ─────────────────────────────────────────────────────────

export const LayoutSvg = memo(function LayoutSvg(props: LayoutSvgProps) {
  const { layout, trace, indicators, nearMisses, view, width, height, theme, display, selected, hover, commentCounts, handlers } = props;
  const interactive = handlers !== undefined;
  const paper = props.variant === "paper";

  const levels = useMemo(() => {
    const m = new Map<string, AlignLevel>();
    for (const [id, hits] of indicators) m.set(id, componentLevel(hits));
    return m;
  }, [indicators]);

  const selectedList = paper ? [] : layout.components.filter((c) => selected.has(c.id));
  const readouts = !paper && layerShown(display, "alignment");

  // the box of every glyph on the screen, for the labels to keep clear of and the badges to sit on; kept per
  // component object and zoom, so a drag works out again only the box of what moved
  const extentOf = useCallback((c: LayoutComponent): ScreenBox => (paper ? paperBoxOf(c, view.k) : screenBoxOf(c, view.k)), [paper, view.k]);

  // the power where each beam ends, in the language in use
  const showPower = layerShown(display, "power");
  const powers = useMemo(() => {
    if (!showPower) return [];
    return powerGroupsOf(trace, theme);
  }, [showPower, trace, theme]);

  // where names may move to, they keep off the beams when they can
  const movable = paper || layout.components.length <= MOVABLE_LABELS_MAX;
  const avoid = useMemo(() => (movable && display.beams ? beamObstacles(trace, view) : []), [movable, display.beams, trace, view]);

  // Names, read-outs, power tags and near-miss notes compete for the same pixels: one plan decides. A name carries the
  // component's height mark as its second line (3D section 7).
  const table = layout.table;
  const markOf = useCallback((c: LayoutComponent) => heightMark(c, table), [table]);
  const texts = useMemo(
    () =>
      planTexts({
        components: layout.components,
        indicators,
        misses: nearMisses ?? [],
        view,
        selected: paper ? NO_IDS : selected,
        hover: paper ? null : hover,
        showLabels: display.labels,
        showReadouts: readouts,
        extent: extentOf,
        labelPx: paper ? PAPER_LABEL_PX : undefined,
        powers,
        movable,
        avoid,
        heightMark: markOf,
      }),
    [layout.components, indicators, nearMisses, view, selected, hover, display.labels, readouts, extentOf, paper, powers, movable, avoid, markOf],
  );
  const marks = useMemo(() => (readouts ? hitMarks(layout.components, indicators, view) : []), [readouts, layout.components, indicators, view]);

  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label={tr("光路图画布", "Optical layout canvas")} style={{ display: "block" }}>
      <rect x={0} y={0} width={width} height={height} fill="var(--mast-bg)" data-canvas-background="" />
      {(!paper || display.holes) && <Table table={layout.table} view={view} display={display} paper={paper} width={width} height={height} />}
      {display.texts && layout.texts.length > 0 && <Texts texts={layout.texts} view={view} paper={paper} />}
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
              theme={theme}
              paper={paper}
              selected={!paper && selected.has(c.id)}
              hover={!paper && hover === c.id}
              level={paper ? "ok" : (levels.get(c.id) ?? "ok")}
              interactive={interactive}
              onDown={handlers?.onDown}
              onEnter={handlers?.onEnter}
              onLeave={handlers?.onLeave}
            />
          );
        })}
      </g>
      {display.beams && (
        <Beams
          trace={trace}
          view={view}
          theme={theme}
          envelope={layerShown(display, "envelope")}
          polarization={layerShown(display, "polarization")}
          width={width}
          height={height}
          paper={paper}
          picked={paper ? null : props.beamPick}
        />
      )}
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
      <g pointerEvents="none" fontSize={paper ? PAPER_LABEL_PX : 11} fontFamily={paper ? PAPER_FONT : undefined}>
        {texts.map((t) =>
          t.kind === "label" ? (
            <HaloText
              key={t.key}
              paper={paper}
              x={t.x}
              y={t.y}
              fill={t.emphasis || paper ? "var(--mast-text)" : "var(--mast-muted)"}
              fontWeight={t.emphasis ? 700 : paper ? 400 : 500}
              fontSize={paper ? PAPER_LABEL_PX : undefined}
            >
              {t.text}
            </HaloText>
          ) : t.kind === "power" ? (
            <PowerTagText key={t.key} x={t.x} y={t.y} parts={t.parts ?? []} paper={paper} />
          ) : (
            <HaloText key={t.key} paper={paper} x={t.x} y={t.y} fontFamily={FONT} fontSize={10} fill={t.kind === "miss" ? "var(--mast-danger)" : LEVEL_TEXT[t.level]}>
              {t.text}
            </HaloText>
          ),
        )}
      </g>
      {texts.some((t) => t.sub) && (
        <g data-height-marks="">
          {texts.map((t) => (t.sub ? <HeightMarkText key={`h:${t.key}`} x={t.x + t.sub.dx} y={t.y + t.sub.dy} sub={t.sub} paper={paper} /> : null))}
        </g>
      )}
      {!paper && display.comments && commentCounts && commentCounts.size > 0 && (
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
        <g pointerEvents="none" data-selection="">
          {selectedList.map((c) => {
            // the glow of the glyph says it; a glyph too small to glow visibly gets a ring as well
            const { dx, dy, ex, ey } = extentOf(c);
            if (Math.max(ex, ey) >= SMALL_GLYPH_PX) return null;
            const p = toScreen(view, { x: c.x_mm, y: c.y_mm });
            return <circle key={c.id} cx={p.x + dx} cy={p.y + dy} r={Math.max(ex, ey) + 7} fill="none" stroke="var(--mast-accent)" strokeWidth={1.4} strokeDasharray="4 3" />;
          })}
        </g>
      )}
      {interactive && (
        <g pointerEvents="none">
          {layout.components.map((c) => {
            if (!c.locked) return null;
            const p = toScreen(view, { x: c.x_mm, y: c.y_mm });
            const { dx, dy, ex, ey } = extentOf(c);
            return <Lock key={c.id} x={p.x + dx + Math.max(ex, 6) + 3} y={p.y + dy - Math.max(ey, 6) - 3} />;
          })}
        </g>
      )}
      {!paper && display.rulers && <Rulers view={view} width={width} height={height} />}
      {paper && <ScaleBarMark k={view.k} x={PAPER_BAR_X} y={height - PAPER_BAR_BOTTOM} />}
      {paper && display.beams && <PaperLegend trace={trace} theme={theme} right={width - PAPER_BAR_X} y={height - PAPER_BAR_BOTTOM} />}
    </svg>
  );
});
