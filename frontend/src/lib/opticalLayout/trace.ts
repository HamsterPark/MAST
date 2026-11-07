// 2-D ray trace of an optical layout, section 4 of docs/v2/design/optical_layout.md and the
// supplement in its section 11.
//
// This is a line-by-line port of MASTv2/mast/optical_layout/trace.py. The two are checked
// against each other on the shared fixtures in tests/fixtures/optical_layout/
// (test/opticalLayoutFixtures.test.ts reads every file there and compares at 1e-6). A change
// here that makes a fixture differ is a change of the contract, not of this file; keep the
// structure of this function the same as the Python one so a rule that changes there is
// found here by reading the two side by side.
//
// Two flavours share one core:
//   * traceLayoutDetailed(): unrounded geometry plus the incoming direction of every
//     event. The editor draws from it and the alignment tools (aim, place on beam)
//     compute from it, so a result is exact and not off by the 0.001 mm of the output.
//   * toContractTrace(): the same trace rounded the way the contract says (0.001 mm,
//     0.001 degree, fractions at 1e-6, no -0) and stripped to the keys of the contract,
//     which is what the fixtures contain.
//
// The rules, short (the Python docstring has them in full):
//   * a ray is P + t d; it meets the working surface of a NON-source component at
//     t = ((C - P).n) / (d.n); no meeting when |d.n| < 1e-9; a hit needs t > 1e-6, a
//     component that has just acted is ignored below t = 1e-3, and |offset| <= aperture/2 +
//     1e-9; the nearest hit wins; hits whose t differ by no more than 1e-9 mm are a tie and the
//     earlier component wins (a later one replaces the best only when t < best - 1e-9: two
//     components with collinear, overlapping surfaces are hit at one point and their t differ in
//     the last bit of a double, which Python and JavaScript do not even compute alike);
//   * front is d.n < 0; mirrors reflect on the front and end on the back; terminals stop on
//     the front and end on the back; splitters and dichroics act on both faces;
//   * a splitter ends its branch ("split") and starts ".r" and ".t" children whose
//     intensities are R and 1 - R (below 1e-3: not created);
//   * 64 events along a lineage, 1 m of margin round the table, 10 m per segment;
//   * 5000 events in the whole trace (all trees): the hit that would be number 5001 is not
//     recorded, its branch gets the segment to that point and ends with "budget", and the
//     trace stops there (no later branch, tree or fibre continuation). Section 11.2 point 13;
//   * a fibre input that continues at a fibre output lights it up with the same wavelength.

import type { BranchEnd, LayoutComponent, OpticalLayout, Segment, SourceTrace, Trace, TraceBranch, TraceEvent, Truncation, Vec } from "./types.ts";
import { frameOf, pyRound, reflect, round3, type Frame } from "./geometry.ts";

export const TRACE_LIMITS = {
  /** smallest ray parameter that counts as a hit (mm) */
  minT: 1e-6,
  /** a component that just acted is ignored below this distance (mm) */
  selfT: 1e-3,
  /** tolerance on the aperture test (mm) */
  apertureEps: 1e-9,
  /** |d.n| below this means the ray runs parallel to the working surface: no hit */
  parallelEps: 1e-9,
  /** two hits whose t differ by no more than this (mm) are a tie: the component earlier in the list wins */
  tieEps: 1e-9,
  maxEvents: 64,
  /** events one whole trace (all trees, all branches) records; the hit after the last one ends the trace with "budget" */
  maxTraceEvents: 5000,
  minFraction: 1e-3,
  tableMarginMm: 1000,
  maxSegmentMm: 10_000,
} as const;

/** An event with the extra data the editor needs. */
export interface DetailedEvent extends TraceEvent {
  /** unit direction of the ray when it arrived */
  dir: Vec;
  /** exact hit point */
  pos: Vec;
}

export interface DetailedBranch {
  branch: string;
  /** relative intensity at the start of the branch, unrounded */
  fraction: number;
  /** id of the branch this one split off from, null for the root */
  parent: string | null;
  /** cumulative path at the start of the branch */
  startPath: number;
  /** unrounded */
  segments: Segment[];
  events: DetailedEvent[];
  end: BranchEnd;
}

export interface DetailedSourceTrace {
  source: string;
  wavelength_nm: number | null;
  branches: DetailedBranch[];
}

export type DetailedTrace = DetailedSourceTrace[];

// ── what a component is to the tracer ─────────────────────────────────────────

type Kind = "source" | "reflect" | "split" | "dichroic" | "transmit" | "filter" | "iris" | "shutter" | "stop";

function kindOf(c: LayoutComponent): Kind {
  switch (c.type) {
    case "laser":
    case "fiber_out":
    case "lamp":
    case "led":
      return "source";
    case "mirror":
    case "grating":
      return "reflect";
    case "beam_splitter":
      return "split";
    case "dichroic":
      return "dichroic";
    case "filter":
      return "filter";
    case "iris":
      return "iris";
    case "shutter":
      return "shutter";
    case "detector":
    case "camera":
    case "spectrometer":
    case "power_meter":
    case "beam_dump":
    case "blocker":
    case "fiber_in":
      return "stop";
    case "sample":
      return c.props["reflects"] ? "reflect" : "stop";
    case "custom": {
      const b = c.props["behavior"];
      return b === "reflect" ? "reflect" : b === "stop" ? "stop" : "transmit";
    }
    default:
      // lens, objective, waveplate, polarizer, window, nd, aotf, prism, periscope
      return "transmit";
  }
}

function numProp(c: LayoutComponent, key: string): number | null {
  const v = c.props[key];
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

/**
 * The wavelengths a source emits: the list `wavelengths_nm` when it has entries, else the
 * single `wavelength_nm`, else one unlabelled ray (null).
 */
export function ownWavelengths(c: LayoutComponent): (number | null)[] {
  const list = c.props["wavelengths_nm"];
  if (Array.isArray(list) && list.length > 0) return list.filter((w): w is number => typeof w === "number" && Number.isFinite(w));
  const single = numProp(c, "wavelength_nm");
  return single !== null ? [single] : [null];
}

interface Prepared {
  c: LayoutComponent;
  f: Frame;
  kind: Kind;
}

/** What a hit does: the action recorded and how the branch ends (null = it goes on; a split is handled by the caller). */
function interact(p: Prepared, dn: number, offset: number, wl: number | null): { action: string; end: BranchEnd | null } {
  const { c, kind } = p;
  const front = dn < 0;
  switch (kind) {
    case "reflect":
      return front ? { action: "reflect", end: null } : { action: "back_hit", end: "back_hit" };
    case "stop":
      return front ? { action: "stop", end: "stop" } : { action: "back_hit", end: "back_hit" };
    case "split":
      return { action: "split", end: "split" };
    case "dichroic": {
      const edge = numProp(c, "edge_nm");
      if (wl === null || edge === null) return { action: "transmit", end: null };
      const reflected = c.props["reflects"] === "long" ? wl > edge : wl < edge;
      return { action: reflected ? "reflect" : "transmit", end: null };
    }
    case "filter": {
      const lo = numProp(c, "pass_min_nm");
      const hi = numProp(c, "pass_max_nm");
      if (wl !== null && ((lo !== null && wl < lo) || (hi !== null && wl > hi))) return { action: "blocked", end: "blocked" };
      return { action: "transmit", end: null };
    }
    case "iris": {
      const open = numProp(c, "open_mm");
      if (open !== null && Math.abs(offset) > open / 2 + TRACE_LIMITS.apertureEps) return { action: "blocked", end: "blocked" };
      return { action: "transmit", end: null };
    }
    case "shutter":
      return c.props["closed"] ? { action: "blocked", end: "blocked" } : { action: "transmit", end: null };
    default:
      return { action: "transmit", end: null };
  }
}

// ── the walk ──────────────────────────────────────────────────────────────────

interface Hit {
  target: Prepared;
  t: number;
  x: number;
  y: number;
  offset: number;
  /** d.n */
  dn: number;
}

/** What the whole trace may still record (the module header has the rule). Shared by every walk. */
interface Budget {
  /** events the trace may record in all */
  limit: number;
  /** events recorded so far, in every tree */
  events: number;
  /** a hit found no room: the trace stops */
  cut: boolean;
}

interface World {
  targets: readonly Prepared[];
  xmin: number;
  xmax: number;
  ymin: number;
  ymax: number;
}

/** How far a ray can run before it leaves the rectangle (0 when it starts outside). */
function distanceToEdge(w: World, px: number, py: number, dx: number, dy: number): number {
  if (!(w.xmin <= px && px <= w.xmax && w.ymin <= py && py <= w.ymax)) return 0;
  let tx = Infinity;
  if (dx > 0) tx = (w.xmax - px) / dx;
  else if (dx < 0) tx = (w.xmin - px) / dx;
  let ty = Infinity;
  if (dy > 0) ty = (w.ymax - py) / dy;
  else if (dy < 0) ty = (w.ymin - py) / dy;
  return Math.min(tx, ty);
}

function nearestHit(w: World, px: number, py: number, dx: number, dy: number, last: Prepared | null): Hit | null {
  let best: Hit | null = null;
  let bestT = Infinity;
  for (const tg of w.targets) {
    const f = tg.f;
    const dn = dx * f.n.x + dy * f.n.y;
    if (Math.abs(dn) < TRACE_LIMITS.parallelEps) continue;
    const t = ((f.c.x - px) * f.n.x + (f.c.y - py) * f.n.y) / dn;
    if (t <= TRACE_LIMITS.minT) continue;
    if (tg === last && t < TRACE_LIMITS.selfT) continue;
    const hx = px + t * dx;
    const hy = py + t * dy;
    // + 0: a hit on the centre computes to -0, which nobody wants to read
    const offset = (hx - f.c.x) * -f.n.y + (hy - f.c.y) * f.n.x + 0;
    if (Math.abs(offset) > f.half + TRACE_LIMITS.apertureEps) continue;
    if (t < bestT - TRACE_LIMITS.tieEps) {
      bestT = t;
      best = { target: tg, t, x: hx, y: hy, offset, dn };
    }
  }
  return best;
}

function walk(
  w: World,
  budget: Budget,
  out: DetailedBranch[],
  id: string,
  parent: string | null,
  px0: number,
  py0: number,
  dx0: number,
  dy0: number,
  fraction: number,
  path0: number,
  events0: number,
  last0: Prepared | null,
  wl: number | null,
): void {
  const branch: DetailedBranch = { branch: id, fraction, parent, startPath: path0, segments: [], events: [], end: "stop" };
  out.push(branch);
  let px = px0;
  let py = py0;
  let dx = dx0;
  let dy = dy0;
  let path = path0;
  let nEvents = events0;
  let last = last0;

  for (;;) {
    const limit = Math.min(distanceToEdge(w, px, py, dx, dy), TRACE_LIMITS.maxSegmentMm);
    const hit = nearestHit(w, px, py, dx, dy, last);
    if (hit === null || hit.t > limit) {
      branch.segments.push({ x0: px, y0: py, x1: px + limit * dx, y1: py + limit * dy });
      branch.end = "left_table";
      return;
    }
    const { target, t, x: hx, y: hy, offset, dn } = hit;
    const f = target.f;
    path += t;
    branch.segments.push({ x0: px, y0: py, x1: hx, y1: hy });
    if (budget.events >= budget.limit) {
      // no room for this hit: it is not recorded, the branch ends here and the trace stops
      branch.end = "budget";
      budget.cut = true;
      return;
    }
    budget.events += 1;
    const { action, end } = interact(target, dn, offset, wl);
    branch.events.push({
      component: target.c.id,
      action,
      x: hx,
      y: hy,
      offset_mm: offset,
      incidence_deg: Math.acos(Math.min(1, Math.abs(dn))) * (180 / Math.PI),
      path_mm: path,
      dir: { x: dx, y: dy },
      pos: { x: hx, y: hy },
    });
    nEvents += 1;
    last = target;
    px = hx;
    py = hy;
    const reflectedDir = reflect({ x: dx, y: dy }, f.n);

    if (action === "split") {
      if (nEvents >= TRACE_LIMITS.maxEvents) {
        branch.end = "max_events";
        return;
      }
      branch.end = "split";
      const r = numProp(target.c, "reflect_fraction") ?? 0.5;
      const kids: Array<{ suffix: string; f: number; dir: Vec }> = [
        { suffix: ".r", f: fraction * r, dir: reflectedDir },
        { suffix: ".t", f: fraction * (1 - r), dir: { x: dx, y: dy } },
      ];
      for (const k of kids) {
        if (k.f < TRACE_LIMITS.minFraction) continue;
        walk(w, budget, out, id + k.suffix, id, px, py, k.dir.x, k.dir.y, k.f, path, nEvents, last, wl);
        if (budget.cut) return;
      }
      return;
    }
    if (end !== null) {
      branch.end = end;
      return;
    }
    if (action === "reflect") {
      dx = reflectedDir.x;
      dy = reflectedDir.y;
    }
    if (nEvents >= TRACE_LIMITS.maxEvents) {
      branch.end = "max_events";
      return;
    }
  }
}

export interface TraceOptions {
  /**
   * The budget of the whole trace (default TRACE_LIMITS.maxTraceEvents). A smaller one gives a
   * cut trace of a small layout, as the shared trace_budget_cut fixture does. A fraction is cut
   * off, a negative number counts as 0, and anything that is not a finite number is the default.
   */
  maxTraceEvents?: number;
  /** Components to leave out of the trace altogether (used to ask "what if this were not here"). */
  skip?: ReadonlySet<string>;
}

/** Traces every source of a layout. See the header for the rules. */
export function traceLayoutDetailed(layout: OpticalLayout, opts: TraceOptions = {}): DetailedTrace {
  const usable = layout.components.filter(
    (c) =>
      !opts.skip?.has(c.id) &&
      Number.isFinite(c.x_mm) &&
      Number.isFinite(c.y_mm) &&
      Number.isFinite(c.angle_deg) &&
      Number.isFinite(c.aperture_mm),
  );
  const prepared: Prepared[] = usable.map((c) => ({ c, f: frameOf(c), kind: kindOf(c) }));
  const byId = new Map(prepared.map((p) => [p.c.id, p]));
  const m = TRACE_LIMITS.tableMarginMm;
  const world: World = {
    targets: prepared.filter((p) => p.kind !== "source"),
    xmin: -m,
    xmax: layout.table.width_mm + m,
    ymin: -m,
    ymax: layout.table.height_mm + m,
  };

  // A fibre output named by some `continues_at` emits only what reaches the fibre input.
  const continued = new Set<string>();
  for (const p of prepared) {
    const to = p.c.type === "fiber_in" ? p.c.props["continues_at"] : undefined;
    if (typeof to === "string" && to !== "") continued.add(to);
  }
  const key = (id: string, wl: number | null) => `${id}\u0000${wl === null ? "null" : wl}`;
  const queue: Array<{ src: Prepared; wl: number | null }> = [];
  const seen = new Set<string>();
  for (const p of prepared) {
    if (p.kind !== "source" || (p.c.type === "fiber_out" && continued.has(p.c.id))) continue;
    for (const wl of ownWavelengths(p.c)) {
      queue.push({ src: p, wl });
      seen.add(key(p.c.id, wl));
    }
  }

  const asked = opts.maxTraceEvents;
  const budget: Budget = {
    limit: asked === undefined || !Number.isFinite(asked) ? TRACE_LIMITS.maxTraceEvents : Math.max(0, Math.trunc(asked)),
    events: 0,
    cut: false,
  };
  const trees: DetailedTrace = [];
  for (let head = 0; head < queue.length; head++) {
    const { src, wl } = queue[head]!;
    const branches: DetailedBranch[] = [];
    walk(world, budget, branches, "0", null, src.c.x_mm, src.c.y_mm, src.f.n.x, src.f.n.y, 1, 0, 0, null, wl);
    trees.push({ source: src.c.id, wavelength_nm: wl, branches });
    if (budget.cut) break; // no later source, and no fibre continuation is looked for
    for (const br of branches) {
      if (br.end !== "stop" || br.events.length === 0) continue;
      const lastEvent = br.events[br.events.length - 1]!;
      const fin = byId.get(lastEvent.component);
      if (!fin || fin.c.type !== "fiber_in") continue;
      const to = fin.c.props["continues_at"];
      const next = typeof to === "string" ? byId.get(to) : undefined;
      if (!next || next.c.type !== "fiber_out" || seen.has(key(next.c.id, wl))) continue;
      seen.add(key(next.c.id, wl));
      queue.push({ src: next, wl });
    }
  }
  return trees;
}

// ── the contract form ─────────────────────────────────────────────────────────

/** round3 without the sign of zero: -0 is not a value anybody wants to read in a report or a JSON file. */
const r3 = (x: number): number => round3(x) + 0;
const r6 = (x: number): number => pyRound(x, 6) + 0;

/** The trace as the contract (and the shared fixtures) have it: rounded, only the specified keys. */
export function toContractTrace(detailed: DetailedTrace): Trace {
  return detailed.map(
    (s): SourceTrace => ({
      source: s.source,
      wavelength_nm: s.wavelength_nm,
      branches: s.branches.map(
        (b): TraceBranch => ({
          branch: b.branch,
          fraction: r6(b.fraction),
          segments: b.segments.map((g) => ({
            x0: r3(g.x0),
            y0: r3(g.y0),
            x1: r3(g.x1),
            y1: r3(g.y1),
          })),
          events: b.events.map(
            (e): TraceEvent => ({
              component: e.component,
              action: e.action,
              x: r3(e.x),
              y: r3(e.y),
              offset_mm: r3(e.offset_mm),
              incidence_deg: r3(e.incidence_deg),
              path_mm: r3(e.path_mm),
            }),
          ),
          end: b.end,
        }),
      ),
    }),
  );
}

/** Traces a layout and returns the contract form. */
export function traceLayout(layout: OpticalLayout, opts?: TraceOptions): Trace {
  return toContractTrace(traceLayoutDetailed(layout, opts));
}

// ── lookups over a trace ──────────────────────────────────────────────────────

/**
 * Where a cut trace stopped: the end of the last segment of the branch that ends with "budget", the point of
 * the hit that was not recorded. null for a complete trace.
 */
export function cutPoint(trace: ReadonlyArray<{ branches: ReadonlyArray<{ end: string; segments: readonly Segment[] }> }>): Vec | null {
  for (const tree of trace) {
    for (const br of tree.branches) {
      if (br.end !== "budget") continue;
      const last = br.segments[br.segments.length - 1];
      return last ? { x: last.x1, y: last.y1 } : null;
    }
  }
  return null;
}

/** The part of a trace that truncation() reads: the detailed trace and the contract trace both have it. */
interface Cuttable {
  source: string;
  wavelength_nm: number | null;
  branches: ReadonlyArray<{ branch: string; end: string; events: readonly unknown[] }>;
}

/**
 * null for a complete trace. For one the budget cut: what was traced and where it stopped
 * (trace.py truncation()): `events` events in `branches` branches of `trees` trees, the last of
 * them the tree of `source` at `wavelength_nm`, cut in `branch`. Worked out from the trace alone
 * (a branch that ends with "budget"), so it holds for a trace that came through JSON as well.
 */
export function truncation(trace: readonly Cuttable[]): Truncation | null {
  for (const tree of trace) {
    for (const br of tree.branches) {
      if (br.end !== "budget") continue;
      let events = 0;
      let branches = 0;
      for (const t of trace) {
        branches += t.branches.length;
        for (const b of t.branches) events += b.events.length;
      }
      return { reason: "events", events, branches, trees: trace.length, source: tree.source, wavelength_nm: tree.wavelength_nm, branch: br.branch };
    }
  }
  return null;
}

export interface EventRef {
  source: string;
  wavelength_nm: number | null;
  branch: DetailedBranch;
  /** index of the event in `branch.events` */
  index: number;
  event: DetailedEvent;
}

/** Every event of a trace, grouped by the component it happened on. */
export function eventsByComponent(trace: DetailedTrace): Map<string, EventRef[]> {
  const map = new Map<string, EventRef[]>();
  for (const s of trace) {
    for (const branch of s.branches) {
      branch.events.forEach((event, index) => {
        const list = map.get(event.component);
        const hit: EventRef = { source: s.source, wavelength_nm: s.wavelength_nm, branch, index, event };
        if (list) list.push(hit);
        else map.set(event.component, [hit]);
      });
    }
  }
  return map;
}

/**
 * The wavelengths of the light that ends on a component's front face (a branch that stops
 * there), each once, in the order the trees are listed. Light of an unknown wavelength is
 * not a number and is left out. The tracer continues from a fibre input with exactly this:
 * what arrives there is what the far end emits.
 */
export function arrivingWavelengths(trace: DetailedTrace, componentId: string): number[] {
  const out: number[] = [];
  for (const tree of trace) {
    const wl = tree.wavelength_nm;
    if (wl === null || out.includes(wl)) continue;
    const arrives = tree.branches.some((b) => b.end === "stop" && b.events.length > 0 && b.events[b.events.length - 1]!.component === componentId);
    if (arrives) out.push(wl);
  }
  return out;
}

/** The polyline of a branch: the start of its first segment, then the end of every segment. */
export function branchPoints(b: { segments: readonly Segment[] }): Vec[] {
  const first = b.segments[0];
  if (!first) return [];
  const pts: Vec[] = [{ x: first.x0, y: first.y0 }];
  for (const s of b.segments) pts.push({ x: s.x1, y: s.y1 });
  return pts;
}
