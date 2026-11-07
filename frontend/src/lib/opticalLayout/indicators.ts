// Alignment indicators (section 5 of docs/v2/design/optical_layout.md): for every
// component a beam hits, how far off its centre and at what angle; yellow beyond 20 % of
// the aperture, red for the back of a mirror or a beam that passes just beside the
// aperture. The same list feeds the labels on the canvas, the inspector and the
// "beam check" panel.
//
// A trace the budget cut (trace.ts truncation()) cannot say what no beam does: the light that
// would reach a component, or pass beside it, may be exactly the light that was not traced. So
// for a cut trace there are no near misses and no "no beam reaches it" (the backend's narrative
// and picture leave them out in the same way), and the one problem that is listed is the cut.
//
// What the trace does not say it cannot know: a beam that misses a component leaves no
// event. A "near miss" is therefore worked out from the segments, with the definition the
// backend uses (trace.py near_misses, section 11.2 point 12): a segment crosses the line of
// a component's active surface, at least 1e-3 mm from its start and 1e-6 mm from its end,
// outside the aperture (|offset| > aperture/2) but within one aperture of the centre
// (|offset| <= aperture).

import { tr } from "../i18n.ts";
import type { LayoutComponent, OpticalLayout, Truncation } from "./types.ts";
import { isSourceType } from "./types.ts";
import type { DetailedBranch, DetailedTrace } from "./trace.ts";
import { TRACE_LIMITS, eventsByComponent, truncation } from "./trace.ts";
import { frameOf } from "./geometry.ts";
import { wavelengthLabel } from "./colors.ts";

/** |offset| beyond this share of the aperture turns the indicator yellow. */
export const OFFSET_WARN_FRACTION = 0.2;
/** A near miss is a crossing no further than this many apertures from the centre. */
export const NEAR_MISS_APERTURES = 1;

export type AlignLevel = "ok" | "warn" | "bad";
export type AlignReason = "ok" | "off_center" | "back_hit" | "blocked";

const RANK: Record<AlignLevel, number> = { ok: 0, warn: 1, bad: 2 };

export function worseOf(a: AlignLevel, b: AlignLevel): AlignLevel {
  return RANK[a] >= RANK[b] ? a : b;
}

export interface HitIndicator {
  componentId: string;
  source: string;
  wavelength_nm: number | null;
  branch: string;
  fraction: number;
  action: string;
  offset_mm: number;
  incidence_deg: number;
  /** cumulative path from the source */
  path_mm: number;
  /** path since the previous event of the branch, or since the branch began */
  leg_mm: number;
  x: number;
  y: number;
  level: AlignLevel;
  reason: AlignReason;
}

/** The aperture the offset is judged against: an iris is judged by its opening. */
export function effectiveAperture(c: LayoutComponent): number {
  if (c.type === "iris") {
    const open = c.props["open_mm"];
    if (typeof open === "number" && Number.isFinite(open) && open > 0) return open;
  }
  return c.aperture_mm;
}

export function judgeHit(c: LayoutComponent, action: string, offset: number): { level: AlignLevel; reason: AlignReason } {
  if (action === "back_hit") return { level: "bad", reason: "back_hit" };
  if (action === "blocked") return { level: "warn", reason: "blocked" };
  if (Math.abs(offset) > OFFSET_WARN_FRACTION * effectiveAperture(c)) return { level: "warn", reason: "off_center" };
  return { level: "ok", reason: "ok" };
}

/** Every hit of every beam, by component id. */
export function hitIndicators(layout: OpticalLayout, trace: DetailedTrace): Map<string, HitIndicator[]> {
  const byId = new Map(layout.components.map((c) => [c.id, c]));
  const out = new Map<string, HitIndicator[]>();
  for (const [id, hits] of eventsByComponent(trace)) {
    const c = byId.get(id);
    if (!c) continue;
    const list: HitIndicator[] = hits.map(({ source, wavelength_nm, branch, index, event }) => {
      const before = index > 0 ? (branch.events[index - 1]?.path_mm ?? branch.startPath) : branch.startPath;
      const j = judgeHit(c, event.action, event.offset_mm);
      return {
        componentId: id,
        source,
        wavelength_nm,
        branch: branch.branch,
        fraction: branch.fraction,
        action: event.action,
        offset_mm: event.offset_mm,
        incidence_deg: event.incidence_deg,
        path_mm: event.path_mm,
        leg_mm: event.path_mm - before,
        x: event.x,
        y: event.y,
        level: j.level,
        reason: j.reason,
      };
    });
    out.set(id, list);
  }
  return out;
}

export function componentLevel(list: readonly HitIndicator[] | undefined): AlignLevel {
  let level: AlignLevel = "ok";
  for (const h of list ?? []) level = worseOf(level, h.level);
  return level;
}

export interface NearMiss {
  componentId: string;
  source: string;
  wavelength_nm: number | null;
  branch: string;
  x: number;
  y: number;
  /** where along the surface the beam crosses, from the centre */
  offset_mm: number;
  /** how far past the edge of the aperture */
  beyond_mm: number;
}

/** Beams that pass just outside the aperture of a component they cross. None for a cut trace (see the header). */
export function nearMisses(layout: OpticalLayout, trace: DetailedTrace): NearMiss[] {
  if (truncation(trace)) return [];
  const targets = layout.components.filter((c) => !isSourceType(c.type)).map((c) => ({ c, f: frameOf(c) }));
  const out: NearMiss[] = [];
  const seen = new Set<string>();
  for (const s of trace) {
    for (const br of s.branches) {
      for (const g of br.segments) {
        const len = Math.hypot(g.x1 - g.x0, g.y1 - g.y0);
        if (len <= TRACE_LIMITS.selfT) continue;
        const dx = (g.x1 - g.x0) / len;
        const dy = (g.y1 - g.y0) / len;
        for (const { c, f } of targets) {
          const dn = dx * f.n.x + dy * f.n.y;
          if (Math.abs(dn) < TRACE_LIMITS.parallelEps) continue;
          const t = ((f.c.x - g.x0) * f.n.x + (f.c.y - g.y0) * f.n.y) / dn;
          if (!(t > TRACE_LIMITS.selfT && t < len - TRACE_LIMITS.minT)) continue;
          const px = g.x0 + dx * t;
          const py = g.y0 + dy * t;
          const off = (px - f.c.x) * f.u.x + (py - f.c.y) * f.u.y;
          const beyond = Math.abs(off) - f.half;
          if (beyond <= TRACE_LIMITS.apertureEps) continue;
          if (Math.abs(off) > f.half * 2 * NEAR_MISS_APERTURES + TRACE_LIMITS.apertureEps) continue;
          const key = `${c.id}|${s.source}|${s.wavelength_nm}|${br.branch}`;
          if (seen.has(key)) continue;
          seen.add(key);
          out.push({ componentId: c.id, source: s.source, wavelength_nm: s.wavelength_nm, branch: br.branch, x: px, y: py, offset_mm: off, beyond_mm: beyond });
        }
      }
    }
  }
  return out;
}

/** Non-source components no beam reaches. None for a cut trace (see the header). */
export function unlitComponents(layout: OpticalLayout, trace: DetailedTrace): string[] {
  if (truncation(trace)) return [];
  const lit = eventsByComponent(trace);
  return layout.components.filter((c) => !isSourceType(c.type) && !lit.has(c.id)).map((c) => c.id);
}

const NO_MISSES: readonly NearMiss[] = Object.freeze([]);

/**
 * The near misses to draw and list. None while a drag is being previewed: every frame of the drag would pay for
 * them (about 20 to 40 ms on a heavy layout, and thousands of marks to draw), the same as the beam check, and the
 * end of the drag draws them again. While previewing it is always the same empty list.
 */
export function shownNearMisses(layout: OpticalLayout, trace: DetailedTrace, previewing: boolean): readonly NearMiss[] {
  return previewing ? NO_MISSES : nearMisses(layout, trace);
}

export type DiagnosticKind = "back_hit" | "off_center" | "blocked" | "miss" | "left_table" | "max_events" | "unlit" | "trace_cut";

/**
 * The sentence for a trace the budget cut, in both languages (apart from tr() so that the English one can be
 * tested: tr() answers in Chinese under node). It counts in the singular where it is one ("1 branch of 1 beam").
 */
export function traceCutTexts(cut: Truncation): { zh: string; en: string } {
  const beam = `${cut.source} ${wavelengthLabel(cut.wavelength_nm)}`;
  const count = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;
  return {
    zh: `追迹在 ${cut.events} 个事件处截断（整张图的上限；已追迹 ${cut.branches} 个分支、${cut.trees} 条光路），结果不完整：停在 ${beam} 的分支 ${cut.branch}，后面的光没有追迹，所以“没有光打到某个器件”这类结论这里不给。多半是分束器、光源或波长太多。`,
    en: `The trace is cut after ${count(cut.events, "event", "events")} (the limit for the whole layout; ${count(cut.branches, "branch", "branches")} of ${count(cut.trees, "beam", "beams")} traced), so the result is incomplete: it stops in branch ${cut.branch} of ${beam} and nothing after that is traced, so conclusions such as "no beam reaches this component" are left out. There are usually too many splitters, sources or wavelengths.`,
  };
}

/** The banner above the canvas and the first problem of the check panel. */
export function traceCutMessage(cut: Truncation): string {
  const t = traceCutTexts(cut);
  return tr(t.zh, t.en);
}

export interface Diagnostic {
  kind: DiagnosticKind;
  level: AlignLevel;
  componentId: string | null;
  message: string;
}

const nameOf = (layout: OpticalLayout, id: string): string => {
  const c = layout.components.find((x) => x.id === id);
  return c ? (c.label && c.label !== id ? `${c.label} (${id})` : id) : id;
};

const percent = (offset: number, aperture: number) => Math.round((Math.abs(offset) / aperture) * 100);

/**
 * The problems worth a line in the beam-check panel, worst first. The hit indicators and
 * near misses are worked out again unless the caller already has them.
 */
export function diagnostics(
  layout: OpticalLayout,
  trace: DetailedTrace,
  pre: { indicators?: ReadonlyMap<string, HitIndicator[]>; misses?: readonly NearMiss[] } = {},
): Diagnostic[] {
  const out: Diagnostic[] = [];
  const cut = truncation(trace);
  const byId = new Map(layout.components.map((c) => [c.id, c]));
  const seen = new Set<string>();
  const once = (key: string): boolean => {
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  };

  for (const [id, list] of pre.indicators ?? hitIndicators(layout, trace)) {
    const c = byId.get(id);
    if (!c) continue;
    const name = nameOf(layout, id);
    for (const h of list) {
      const beam = `${h.source} ${wavelengthLabel(h.wavelength_nm)}`;
      if (h.reason === "back_hit" && once(`back|${id}`)) {
        out.push({ kind: "back_hit", level: "bad", componentId: id, message: tr(`${name}：光从背面打来（${beam}）。`, `${name}: the beam arrives at the back (${beam}).`) });
      } else if (h.reason === "off_center" && once(`off|${id}`)) {
        const ap = effectiveAperture(c);
        out.push({
          kind: "off_center",
          level: "warn",
          componentId: id,
          message: tr(
            `${name}：命中点偏离中心 ${Math.abs(h.offset_mm).toFixed(2)} mm（口径的 ${percent(h.offset_mm, ap)}%，${beam}）。`,
            `${name}: hit ${Math.abs(h.offset_mm).toFixed(2)} mm off centre (${percent(h.offset_mm, ap)}% of the aperture, ${beam}).`,
          ),
        });
      } else if (h.reason === "blocked" && once(`blk|${id}`)) {
        out.push({ kind: "blocked", level: "warn", componentId: id, message: tr(`${name}：光在此被挡住（${beam}）。`, `${name}: the beam is blocked here (${beam}).`) });
      }
    }
  }
  for (const m of cut ? [] : (pre.misses ?? nearMisses(layout, trace))) {
    if (!once(`miss|${m.componentId}`)) continue;
    out.push({
      kind: "miss",
      level: "bad",
      componentId: m.componentId,
      message: tr(
        `${nameOf(layout, m.componentId)}：光从口径边缘外 ${m.beyond_mm.toFixed(1)} mm 处擦过（${m.source} ${wavelengthLabel(m.wavelength_nm)}）。`,
        `${nameOf(layout, m.componentId)}: the beam passes ${m.beyond_mm.toFixed(1)} mm outside the aperture (${m.source} ${wavelengthLabel(m.wavelength_nm)}).`,
      ),
    });
  }
  const ends = (br: DetailedBranch, s: string, wl: number | null) => {
    const beam = `${s} ${wavelengthLabel(wl)}`;
    if (br.end === "left_table" && once(`left|${s}|${wl}|${br.branch}`)) {
      out.push({ kind: "left_table", level: "warn", componentId: null, message: tr(`${beam}：光线离开了平台（分支 ${br.branch}）。`, `${beam}: the beam leaves the table (branch ${br.branch}).`) });
    } else if (br.end === "max_events" && once(`max|${s}|${wl}|${br.branch}`)) {
      out.push({ kind: "max_events", level: "warn", componentId: null, message: tr(`${beam}：作用次数达到上限 64，追迹停止（分支 ${br.branch}）。`, `${beam}: stopped after 64 interactions (branch ${br.branch}).`) });
    }
  };
  for (const s of trace) for (const br of s.branches) ends(br, s.source, s.wavelength_nm);

  // Without a source nothing can be lit, and a list of "unlit" for every component helps nobody.
  if (trace.length > 0 && !cut) {
    for (const id of unlitComponents(layout, trace)) {
      out.push({ kind: "unlit", level: "warn", componentId: id, message: tr(`${nameOf(layout, id)}：没有任何光照到。`, `${nameOf(layout, id)}: no beam reaches it.`) });
    }
  }
  const rank = { bad: 0, warn: 1, ok: 2 } as const;
  out.sort((a, b) => rank[a.level] - rank[b.level]);
  // the cut comes first, ahead of the red problems: nothing below it is the whole story
  if (cut) out.unshift({ kind: "trace_cut", level: "warn", componentId: null, message: traceCutMessage(cut) });
  return out;
}
