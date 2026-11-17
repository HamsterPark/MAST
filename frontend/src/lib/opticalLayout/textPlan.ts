// Which texts the canvas draws, and where: component names, the alignment read-outs
// (offset and incidence of a hit) and the "misses by" notes of near misses.
//
// All of them compete for the same pixels, so they are planned together: the selected and
// hovered component's texts always show, then problems before the plain ones (a red
// read-out wins over a name wins over a green read-out), each only if it does not overlap
// something already placed. Zoomed in, every text has room and every text appears.
// Pure, so the rule is tested (test/opticalLayoutLabels.test.ts).

import { tr } from "../i18n.ts";
import type { LayoutComponent, Vec } from "./types.ts";
import type { AlignLevel, HitIndicator, NearMiss } from "./indicators.ts";
import { componentLevel } from "./indicators.ts";
import { labelSize, placeLabelsAt, type LabelBox, type SoftObstacle } from "./labels.ts";
import { toScreen, type View } from "./view.ts";
import type { ScreenBox } from "./glyphs.ts";

/** Zoomed in at least this far (px per mm), every hit carries its numbers; further out only problems and the selected component do. */
export const ALL_READOUTS_MIN_K = 0.9;
/** Below this the whole table is a thumbnail: not even the problems get a text (their marks and the Check tab remain). */
export const PROBLEM_TEXT_MIN_K = 0.3;

export type TextKind = "label" | "readout" | "miss" | "power";

export interface PlannedText {
  key: string;
  kind: TextKind;
  /** where the text starts (its baseline) on the screen */
  x: number;
  y: number;
  text: string;
  level: AlignLevel;
  locked: boolean;
  emphasis: boolean;
  /** a power tag: one coloured dot and number per beam that ends on the component */
  parts?: ReadonlyArray<{ text: string; color: string }>;
  /** a name's second line: the component's height mark (docs/v2/design/optical_layout_3d.md section 7) */
  sub?: HeightSub;
}

/**
 * The height mark under a name ("z 120"; a periscope "z 75→250" with a ▲ or ▼ before it, `rise` +1 / -1): where its
 * baseline starts relative to the name's (dx, dy) and its font size. It moves with the name.
 */
export interface HeightSub {
  text: string;
  rise: -1 | 0 | 1;
  dx: number;
  dy: number;
  px: number;
}

/** The font size of a height mark against its name's, and the room (in its font sizes) a ▲ / ▼ takes before it. */
export const HEIGHT_MARK_SCALE = 0.85;
export const HEIGHT_TRIANGLE_EM = 0.9;

/** "-4.00": the sign only where there is one (never "-0.00"). */
export function signedText(v: number, digits = 2): string {
  const s = v.toFixed(digits);
  return Number(s) === 0 ? (0).toFixed(digits) : s;
}

/** The read-out of a component's worst hit: `Δ-4.00 mm · 0.0° ×2`. */
export function readoutText(hit: HitIndicator, hits: number): string {
  return `Δ${signedText(hit.offset_mm)} mm · ${signedText(hit.incidence_deg, 1)}°${hits > 1 ? ` ×${hits}` : ""}`;
}

export function missText(m: NearMiss): string {
  return tr(`错过口径 ${m.beyond_mm.toFixed(1)} mm`, `misses by ${m.beyond_mm.toFixed(1)} mm`);
}

const RANK: Record<AlignLevel, number> = { ok: 0, warn: 1, bad: 2 };

/** The hit that speaks for a component: the worst, the strongest beam first among equals. */
export function worstHit(hits: readonly HitIndicator[]): HitIndicator | undefined {
  return [...hits].sort((a, b) => RANK[b.level] - RANK[a.level] || b.fraction - a.fraction)[0];
}

export interface PlanInput {
  components: readonly LayoutComponent[];
  indicators: ReadonlyMap<string, HitIndicator[]>;
  misses: readonly NearMiss[];
  view: View;
  selected: ReadonlySet<string>;
  hover: string | null | undefined;
  showLabels: boolean;
  showReadouts: boolean;
  /**
   * Half extents on the screen (px) of a component's drawing. Given, a name is centred just below the drawing
   * (the way a schematic is lettered) instead of beside its centre, where a large glyph would sit under it.
   */
  extent?: (c: LayoutComponent) => ScreenBox;
  /** font size of the names (px), 11 unless given */
  labelPx?: number;
  /**
   * Whether a crowded name or power tag may move (above, beside) before it is dropped. On for the figure, where
   * every name matters, and on the canvas for layouts small enough to read them all; off for a crowded canvas,
   * where zooming in is the answer and fewer names read better.
   */
  movable?: boolean;
  /** with `movable`: rectangles (the beams) a name or tag keeps clear of when one of its places allows */
  avoid?: readonly SoftObstacle[];
  /**
   * The power arriving where beams end, one entry per component (lib/opticalLayout/beamDraw.ts powerTags, grouped),
   * the numbers already in the language in use.
   */
  powers?: ReadonlyArray<{ key: string; componentId: string; at: Vec; parts: ReadonlyArray<{ text: string; color: string }> }>;
  /**
   * The height mark of a component (heights.ts heightMark), drawn as a second line under its name and placed with it;
   * null for a component at the table's beam height. Absent: no marks (a layout without heights has none anyway).
   */
  heightMark?: (c: LayoutComponent) => { text: string; rise: -1 | 0 | 1 } | null;
}

/** Font size of a power tag (px), the room a colour dot takes before its number, and the gap between two numbers. */
export const POWER_PX = 10.5;
export const POWER_DOT_PX = 9;
export const POWER_GAP_PX = 7;

/** The width of a power tag of these parts (px), as the canvas lays it out. */
export function powerTagWidth(parts: ReadonlyArray<{ text: string }>): number {
  return parts.reduce((w, p, i) => w + (i > 0 ? POWER_GAP_PX : 0) + POWER_DOT_PX + labelSize(p.text, POWER_PX).w - 4, 0) + 4;
}
/** Power tags rank above names (the number at the end of the path is what the person came for), below problems. */
const POWER_PRIORITY = 2.5;

/** Pixels between the bottom of a drawing and the top of its name. */
export const LABEL_GAP_PX = 3;

interface Candidate {
  item: PlannedText;
  box: LabelBox;
  priority: number;
}

export function planTexts(input: PlanInput): PlannedText[] {
  const { components, indicators, misses, view, selected, hover, showLabels, showReadouts } = input;
  const cands: Candidate[] = [];
  const mine = (id: string) => selected.has(id) || hover === id;

  if (showLabels) {
    for (const c of components) {
      const p = toScreen(view, { x: c.x_mm, y: c.y_mm });
      const text = c.label || c.id;
      const px = input.labelPx ?? 11;
      const { w, h } = labelSize(text, px);
      // the height mark is a second line of the name, centred under it; the box holds both (without a mark it is the
      // name's box, as it always was)
      const mark = input.heightMark?.(c) ?? null;
      const subPx = Math.round(px * HEIGHT_MARK_SCALE * 10) / 10;
      const subSize = mark ? labelSize(mark.text, subPx) : null;
      const subW = mark && subSize ? subSize.w + (mark.rise !== 0 ? Math.ceil(subPx * HEIGHT_TRIANGLE_EM) : 0) : 0;
      const W = Math.max(w, subW);
      const H = h + (subSize ? subSize.h : 0);
      const emphasis = mine(c.id);
      let left = p.x + 9;
      let top = p.y + 5;
      let alts: Array<{ x: number; y: number }> | undefined;
      if (input.extent) {
        const b = input.extent(c);
        const cx = p.x + b.dx;
        const cy = p.y + b.dy;
        const ey = Math.max(b.ey, 4);
        const ex = Math.max(b.ex, 4);
        left = cx - W / 2;
        top = cy + ey + LABEL_GAP_PX;
        // crowded below: above the drawing, then right of it, then left of it
        if (input.movable) alts = [
          { x: left, y: cy - ey - LABEL_GAP_PX - H },
          { x: cx + ex + LABEL_GAP_PX + 2, y: cy - H / 2 },
          { x: cx - ex - LABEL_GAP_PX - 2 - W, y: cy - H / 2 },
        ];
      }
      // both lines centred in the box under a drawing (the name exactly where it was without a mark), at its left else
      const x = input.extent ? left + (W - w) / 2 : left;
      const sub: HeightSub | undefined =
        mark && subSize ? { text: mark.text, rise: mark.rise, dx: (input.extent ? left + (W - subW) / 2 : left) - x, dy: h + subPx - px, px: subPx } : undefined;
      cands.push({
        item: { key: `l:${c.id}`, kind: "label", x, y: top + px, text, level: "ok", locked: c.locked, emphasis, ...(sub ? { sub } : {}) },
        box: { id: `l:${c.id}`, x: left, y: top, w: W, h: H, force: emphasis, ...(alts ? { alts } : {}) },
        priority: 2,
      });
    }
  }

  if (showReadouts) {
    for (const c of components) {
      const hits = indicators.get(c.id);
      if (!hits || hits.length === 0) continue;
      const level = componentLevel(hits);
      const worst = worstHit(hits);
      if (!worst) continue;
      const emphasis = mine(c.id);
      if (!(emphasis || view.k >= (level !== "ok" ? PROBLEM_TEXT_MIN_K : ALL_READOUTS_MIN_K))) continue;
      const p = toScreen(view, { x: worst.x, y: worst.y });
      const text = readoutText(worst, hits.length);
      const { w, h } = labelSize(text, 10.4);
      cands.push({
        item: { key: `r:${c.id}`, kind: "readout", x: p.x + 7, y: p.y - 6, text, level, locked: false, emphasis },
        box: { id: `r:${c.id}`, x: p.x + 7, y: p.y - 17, w, h, force: emphasis },
        priority: level === "bad" ? 4 : level === "warn" ? 3 : 1,
      });
    }
    for (const m of misses) {
      if (!(mine(m.componentId) || view.k >= PROBLEM_TEXT_MIN_K)) continue;
      const p = toScreen(view, { x: m.x, y: m.y });
      const text = missText(m);
      const { w, h } = labelSize(text, 10.4);
      const key = `m:${m.componentId}|${m.source}|${m.wavelength_nm}|${m.branch}`;
      cands.push({
        item: { key, kind: "miss", x: p.x + 7, y: p.y - 6, text, level: "bad", locked: false, emphasis: mine(m.componentId) },
        box: { id: key, x: p.x + 7, y: p.y - 17, w, h, force: mine(m.componentId) },
        priority: 4,
      });
    }
  }

  const byId = input.powers && input.powers.length > 0 ? new Map(components.map((c) => [c.id, c])) : null;
  for (const t of input.powers ?? []) {
    if (!(mine(t.componentId) || view.k >= PROBLEM_TEXT_MIN_K) || t.parts.length === 0) continue;
    const w = powerTagWidth(t.parts);
    const h = labelSize("0", POWER_PX).h;
    // beside the drawing of the component the light ends on (right of its box, from its top down), so the number
    // never sits on the component; without a drawing, just above and right of where the light arrives
    const c = byId?.get(t.componentId);
    let x: number;
    let top: number;
    let alts: Array<{ x: number; y: number }> | undefined;
    if (c && input.extent) {
      const p = toScreen(view, { x: c.x_mm, y: c.y_mm });
      const b = input.extent(c);
      const ex = Math.max(b.ex, 3);
      const ey = Math.max(b.ey, 6);
      x = p.x + b.dx + ex + 5;
      top = p.y + b.dy - ey;
      // taken: left of the drawing, then above it, then below its name
      if (input.movable) alts = [
        { x: p.x + b.dx - ex - 5 - w, y: top },
        { x: p.x + b.dx - w / 2, y: p.y + b.dy - ey - 4 - h },
        { x: p.x + b.dx - w / 2, y: p.y + b.dy + ey + 4 + h + 2 },
      ];
    } else {
      const p = toScreen(view, t.at);
      x = p.x + 8;
      top = p.y - 20;
    }
    const key = `p:${t.key}`;
    cands.push({
      item: { key, kind: "power", x, y: top + 11, text: t.parts.map((q) => q.text).join(" "), level: "ok", locked: false, emphasis: mine(t.componentId), parts: t.parts },
      box: { id: key, x, y: top, w, h, force: mine(t.componentId), ...(alts ? { alts } : {}) },
      priority: POWER_PRIORITY,
    });
  }

  // highest priority first; the sort is stable, so equals keep the layout order
  const ordered = [...cands].sort((a, b) => b.priority - a.priority);
  const at = placeLabelsAt(ordered.map((c) => c.box), 2, input.movable ? (input.avoid ?? []) : []);
  const out: PlannedText[] = [];
  for (const c of cands) {
    const pos = at.get(c.box.id);
    if (!pos) continue;
    // the text keeps its place in its box wherever the box went
    out.push(pos.x === c.box.x && pos.y === c.box.y ? c.item : { ...c.item, x: c.item.x + pos.x - c.box.x, y: c.item.y + pos.y - c.box.y });
  }
  return out;
}

/** Positions of the rings that mark every hit, and the crosses of near misses, for the canvas. */
export interface HitMark {
  key: string;
  at: Vec;
  level: AlignLevel;
}

export function hitMarks(components: readonly LayoutComponent[], indicators: ReadonlyMap<string, HitIndicator[]>, view: View): HitMark[] {
  const out: HitMark[] = [];
  for (const c of components) {
    const hits = indicators.get(c.id);
    if (!hits || hits.length === 0) continue;
    const worst = worstHit(hits);
    if (!worst) continue;
    out.push({ key: c.id, at: toScreen(view, { x: worst.x, y: worst.y }), level: componentLevel(hits) });
  }
  return out;
}
