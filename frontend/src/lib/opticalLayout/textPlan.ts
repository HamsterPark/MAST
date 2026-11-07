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
import { labelSize, placeLabels, type LabelBox } from "./labels.ts";
import { toScreen, type View } from "./view.ts";

/** Zoomed in at least this far (px per mm), every hit carries its numbers; further out only problems and the selected component do. */
export const ALL_READOUTS_MIN_K = 0.9;
/** Below this the whole table is a thumbnail: not even the problems get a text (their marks and the Check tab remain). */
export const PROBLEM_TEXT_MIN_K = 0.3;

export type TextKind = "label" | "readout" | "miss";

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
}

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
}

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
      const { w, h } = labelSize(text, 11);
      const emphasis = mine(c.id);
      cands.push({
        item: { key: `l:${c.id}`, kind: "label", x: p.x + 9, y: p.y + 16, text, level: "ok", locked: c.locked, emphasis },
        box: { id: `l:${c.id}`, x: p.x + 9, y: p.y + 5, w: w + (c.locked ? 12 : 0), h, force: emphasis },
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

  // highest priority first; the sort is stable, so equals keep the layout order
  const ordered = [...cands].sort((a, b) => b.priority - a.priority);
  const shown = placeLabels(ordered.map((c) => c.box));
  return cands.filter((c) => shown.has(c.box.id)).map((c) => c.item);
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
