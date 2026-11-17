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
//
// The beam (docs/v2/design/optical_layout_v2.md section 4; the formulas are in beam.ts): every ray
// carries a Stokes vector S (S0 = power relative to the source of the tree) and a Gaussian beam
// parameter q (or none). A segment records the state at its start, an event the state arriving at the
// component; then the component acts (section 4.6): mirrors R_m and their throughput, waveplates and
// polarisers their Mueller matrix at the axis angle the ray sees, ND 10^-od, lenses 1/q' = 1/q - 1/f,
// beam splitters R and 1 - R, a PBS P(0) to the transmitted and R_m P(90°) to the reflected port (the
// only change of geometry: its children's fractions are their shares of the arriving power). Light of
// no power is traced on like any other. A fibre continuation starts a tree whose power is what arrived
// at the fibre input times its coupling, with the polarisation and beam size of the fibre output.
//
// Heights (docs/v2/design/optical_layout_3d.md section 3; heights.ts reads them with their defaults): every ray
// stays level at a height z, the height of its source (a fibre continuation: of its fibre output). A meeting in
// the plane is a hit only when |z - z_port| <= aperture/2 + 1e-9, z_port the component's axis (a periscope: its
// entrance for this direction, z_mm along its +x and z_out_mm the other way); otherwise the ray passes over or
// under it and goes on, and the segment lists it in `passes` (those before the hit, t < t_hit - 1e-9; on a
// segment that leaves the table, those up to its end; in the order of t, those within 1e-9 by id; at most 5000 in the
// whole trace). An iris with an opening blocks in height as it does in the plane. After a periscope the ray is
// at z + (z_exit - z_entry), and the vertical run |z_exit - z_entry| is added to the path and to q. Heights are
// only added, subtracted and compared - no trigonometry - so this side and the server get the same bits.

import type { BranchEnd, EventBeam, LayoutComponent, OpticalLayout, QParam, Segment, SegmentBeam, SourceTrace, StokesNorm, Trace, TraceBranch, TraceEvent, TracePass, TreeBeam, Truncation, Vec } from "./types.ts";
import { frameOf, pyRound, reflect, round3, type Frame } from "./geometry.ts";
import { componentZ, periscopeHeights } from "./heights.ts";
import {
  UNPOLARIZED,
  diameterOf,
  linearStokes,
  mirrorStokes,
  normStokes,
  pbsReflected,
  pbsTransmitted,
  polarizerStokes,
  projectedAxisRad,
  propagateQ,
  qThroughLens,
  retardanceOf,
  retarderStokes,
  scaleStokes,
  waistQ,
  type Stokes,
} from "./beam.ts";

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
  /** passes (over or under a component) one whole trace records (3D contract section 3.5) */
  maxTracePasses: 5000,
} as const;

/**
 * An event with the extra data the editor needs. Its `beam` (v2 section 4.7, unrounded) is always there
 * in a trace made here; the type keeps it optional because the contract trace of an older server has none.
 */
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
  /** v2 section 4.7: power and beam quality of the tree (unrounded) */
  beam?: TreeBeam;
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
    case "pbs":
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
  /** heights (3D section 2): the axis; a periscope's entrance along +x (zIn) and its exit (zOut) */
  zIn: number;
  zOut: number;
}

/** What a hit does: the action recorded and how the branch ends (null = it goes on; a split is handled by the caller). `dz`: the ray's height minus the axis' (only an iris looks at it). */
function interact(p: Prepared, dn: number, offset: number, dz: number, wl: number | null): { action: string; end: BranchEnd | null } {
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
      // in the plane, and in height (3D section 3.3)
      if (open !== null && (Math.abs(offset) > open / 2 + TRACE_LIMITS.apertureEps || Math.abs(dz) > open / 2 + TRACE_LIMITS.apertureEps)) return { action: "blocked", end: "blocked" };
      return { action: "transmit", end: null };
    }
    case "shutter":
      return c.props["closed"] ? { action: "blocked", end: "blocked" } : { action: "transmit", end: null };
    default:
      return { action: "transmit", end: null };
  }
}

// ── the beam (v2 section 4) ───────────────────────────────────────────────────

/** What the walk of one tree knows about its light (section 4.2). */
interface TreeLight {
  wl: number | null;
  /** M² of the tree, null when it has no Gaussian beam */
  m2: number | null;
  /** power of the source in mW, null when not known */
  sourceMw: number | null;
}

/**
 * The light a source sends into a tree (section 4.2): its polarisation and Gaussian beam come from its own
 * props (a fibre output fed by a fibre input too), its power is `sourceMw`. Lamps and LEDs are always
 * unpolarised and without a Gaussian beam.
 */
function sourceLight(c: LayoutComponent, wl: number | null, sourceMw: number | null): { light: TreeLight; S: Stokes; q: QParam | null } {
  const lampLike = c.type === "lamp" || c.type === "led";
  const S: Stokes = !lampLike && c.props["polarization"] === "linear" ? linearStokes(numProp(c, "pol_angle_deg") ?? 0) : [...UNPOLARIZED];
  const diameter = lampLike ? null : numProp(c, "beam_diameter_mm");
  const m2 = numProp(c, "m2") ?? 1;
  const q = diameter === null ? null : waistQ(diameter, wl, m2);
  return { light: { wl, m2: q === null ? null : m2, sourceMw }, S, q };
}

/** `throughput` of a component, 1 when it has none (section 3.2). */
const throughputOf = (c: LayoutComponent): number => numProp(c, "throughput") ?? 1;

/**
 * The Stokes vector after a component that reflects or transmits (section 4.6; splits are made by
 * splitChildren, the ray ends at the rest). `dn` = d · n of the hit, for the axis angle the ray sees.
 */
function afterAction(c: LayoutComponent, action: string, S: Stokes, dn: number): Stokes {
  if (action === "reflect") return c.type === "dichroic" ? mirrorStokes(S) : scaleStokes(mirrorStokes(S), throughputOf(c));
  switch (c.type) {
    case "dichroic":
    case "iris":
    case "shutter":
      return S;
    case "nd": {
      const od = numProp(c, "od");
      return od === null ? S : scaleStokes(S, 10 ** -od);
    }
    case "waveplate": {
      const r = retardanceOf(c.props["retardance"]);
      const out = r === null ? S : retarderStokes(S, projectedAxisRad(numProp(c, "fast_axis_deg") ?? 0, dn), r);
      return scaleStokes(out, throughputOf(c));
    }
    case "polarizer":
      return scaleStokes(polarizerStokes(S, projectedAxisRad(numProp(c, "axis_deg") ?? 0, dn)), throughputOf(c));
    default:
      // lens, objective, filter that passes, window, aotf, prism, periscope, custom that transmits
      return scaleStokes(S, throughputOf(c));
  }
}

/** q after a component (section 4.6): a lens or objective with a focal length that is not 0 focuses, the rest leave it. */
function afterActionQ(c: LayoutComponent, q: QParam | null): QParam | null {
  if (q === null || (c.type !== "lens" && c.type !== "objective")) return q;
  const f = numProp(c, "focal_mm");
  return f === null || f === 0 ? q : qThroughLens(q, f);
}

interface Child {
  suffix: ".r" | ".t";
  /** the geometric fraction of the child (v1: the product of the split ratios; a PBS: the share of the arriving power) */
  f: number;
  dir: Vec;
  S: Stokes;
}

/**
 * The two children of a split (section 4.6), reflected first. A beam splitter: R R_m S and (1 - R) S, fractions
 * R and 1 - R as in v1. A PBS: R_m P(90°) S and P(0) S, fractions the shares of the arriving power, 50 / 50
 * when no light arrives (only the geometry needs them then).
 */
function splitChildren(c: LayoutComponent, S: Stokes, fraction: number, reflected: Vec, straight: Vec): [Child, Child] {
  if (c.type === "pbs") {
    const r = pbsReflected(S);
    const t = pbsTransmitted(S);
    const s0 = S[0];
    // fraction x k / S0, multiplied first as the server does (section 12); no light (S0 = 0 exactly, beam.ts
    // EPS_DARK): 50 / 50, only so that the geometry goes on
    return [
      { suffix: ".r", f: s0 > 0 ? (fraction * r[0]) / s0 : fraction * 0.5, dir: reflected, S: r },
      { suffix: ".t", f: s0 > 0 ? (fraction * t[0]) / s0 : fraction * 0.5, dir: straight, S: t },
    ];
  }
  const rf = numProp(c, "reflect_fraction") ?? 0.5;
  return [
    { suffix: ".r", f: fraction * rf, dir: reflected, S: scaleStokes(mirrorStokes(S), rf) },
    { suffix: ".t", f: fraction * (1 - rf), dir: straight, S: scaleStokes(S, 1 - rf) },
  ];
}

/** The 1/e² diameter where a beam of parameter q is, null without a Gaussian beam. */
function diameterHere(light: TreeLight, q: QParam | null): number | null {
  return q === null || light.m2 === null || light.wl === null ? null : diameterOf(q, light.wl, light.m2);
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
  /** the ray's height minus that of the component's axis (a periscope: its entrance) */
  dz: number;
}

/** A meeting in the plane that the height rules out (3D section 3.5): the ray passes over or under the component. */
interface Passed {
  target: Prepared;
  t: number;
  x: number;
  y: number;
  dz: number;
}

/** What the whole trace may still record (the module header has the rule). Shared by every walk. */
interface Budget {
  /** events the trace may record in all */
  limit: number;
  /** events recorded so far, in every tree */
  events: number;
  /** a hit found no room: the trace stops */
  cut: boolean;
  /** passes recorded so far (at most TRACE_LIMITS.maxTracePasses) */
  passes: number;
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

/**
 * The nearest hit of a ray at height z, and every meeting in the plane its height rules out (the ray passes over
 * or under those; in the order of the layout, whatever their t).
 */
function nearestHit(w: World, px: number, py: number, dx: number, dy: number, last: Prepared | null, z: number): { hit: Hit | null; passed: Passed[] } {
  let best: Hit | null = null;
  let bestT = Infinity;
  const passed: Passed[] = [];
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
    // the height (3D section 3.2): the entrance of a periscope depends on the direction
    const dz = z - (dn > 0 ? tg.zIn : tg.zOut);
    if (Math.abs(dz) > f.half + TRACE_LIMITS.apertureEps) {
      passed.push({ target: tg, t, x: hx, y: hy, dz });
      continue;
    }
    if (t < bestT - TRACE_LIMITS.tieEps) {
      bestT = t;
      best = { target: tg, t, x: hx, y: hy, offset, dn, dz };
    }
  }
  return { hit: best, passed };
}

/** Ids by their code points (the server's string order; never localeCompare). */
const byId = (a: Passed, b: Passed): number => (a.target.c.id < b.target.c.id ? -1 : a.target.c.id > b.target.c.id ? 1 : 0);

/**
 * Meetings in the order along the ray (3D section 3.5): by t; those within tieEps (1e-9) of the first of a run are at
 * one point - their t differ in the last bit, which this side and the server do not compute alike - and go by id.
 */
function inOrder(passed: readonly Passed[]): Passed[] {
  const byT = [...passed].sort((a, b) => a.t - b.t);
  const out: Passed[] = [];
  for (let i = 0; i < byT.length; ) {
    let j = i + 1;
    while (j < byT.length && byT[j]!.t - byT[i]!.t <= TRACE_LIMITS.tieEps) j++;
    out.push(...byT.slice(i, j).sort(byId));
    i = j;
  }
  return out;
}

/**
 * The passes of a segment that ends at `endT` (3D section 3.5): the meetings its height ruled out with t below endT
 * (up to it when `inclusive`), in the order along the ray (inOrder), as many as the trace may still record.
 */
function passesOf(passed: readonly Passed[], endT: number, inclusive: boolean, budget: Budget): TracePass[] {
  const kept = inOrder(passed.filter((p) => (inclusive ? p.t <= endT : p.t < endT)));
  const room = Math.max(0, TRACE_LIMITS.maxTracePasses - budget.passes);
  const out = kept.slice(0, room);
  budget.passes += out.length;
  return out.map((p) => ({ component: p.target.c.id, x: p.x, y: p.y, dz_mm: p.dz + 0 }));
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
  light: TreeLight,
  S0: Stokes,
  q0: QParam | null,
  z0: number,
): void {
  const wl = light.wl;
  const branch: DetailedBranch = { branch: id, fraction, parent, startPath: path0, segments: [], events: [], end: "stop" };
  out.push(branch);
  let px = px0;
  let py = py0;
  let dx = dx0;
  let dy = dy0;
  let path = path0;
  let nEvents = events0;
  let last = last0;
  let S = S0;
  let q = q0;
  // the height of the ray (3D section 3.1): only a periscope changes it
  let z = z0;

  for (;;) {
    const limit = Math.min(distanceToEdge(w, px, py, dx, dy), TRACE_LIMITS.maxSegmentMm);
    const { hit, passed } = nearestHit(w, px, py, dx, dy, last, z);
    // the state at the start of this segment: after the previous action (section 4.7)
    const segBeam: SegmentBeam = { p: S[0], s: normStokes(S), q };
    if (hit === null || hit.t > limit) {
      branch.segments.push({ x0: px, y0: py, x1: px + limit * dx, y1: py + limit * dy, z_mm: z, beam: segBeam, passes: passesOf(passed, limit, true, budget) });
      branch.end = "left_table";
      return;
    }
    const { target, t, x: hx, y: hy, offset, dn, dz } = hit;
    const f = target.f;
    path += t;
    branch.segments.push({ x0: px, y0: py, x1: hx, y1: hy, z_mm: z, beam: segBeam, passes: passesOf(passed, t - TRACE_LIMITS.tieEps, false, budget) });
    if (budget.events >= budget.limit) {
      // no room for this hit: it is not recorded, the branch ends here and the trace stops
      branch.end = "budget";
      budget.cut = true;
      return;
    }
    budget.events += 1;
    const { action, end } = interact(target, dn, offset, dz, wl);
    // the state arriving here, before the component acts (section 4.7)
    const qHere = q === null ? null : propagateQ(q, t);
    const evBeam: EventBeam = { p: S[0], mw: light.sourceMw === null ? null : light.sourceMw * S[0], d_mm: diameterHere(light, qHere), s: normStokes(S) };
    branch.events.push({
      component: target.c.id,
      action,
      x: hx,
      y: hy,
      z_mm: z,
      offset_mm: offset,
      dz_mm: dz + 0,
      incidence_deg: Math.acos(Math.min(1, Math.abs(dn))) * (180 / Math.PI),
      path_mm: path,
      beam: evBeam,
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
      for (const k of splitChildren(target.c, S, fraction, reflectedDir, { x: dx, y: dy })) {
        if (k.f < TRACE_LIMITS.minFraction) continue;
        walk(w, budget, out, id + k.suffix, id, px, py, k.dir.x, k.dir.y, k.f, path, nEvents, last, light, k.S, qHere, z);
        if (budget.cut) return;
      }
      return;
    }
    if (end !== null) {
      branch.end = end;
      return;
    }
    S = afterAction(target.c, action, S, dn);
    q = afterActionQ(target.c, qHere);
    if (action === "reflect") {
      dx = reflectedDir.x;
      dy = reflectedDir.y;
    }
    // a periscope that goes up or down (3D section 3.4): the same offset from the exit as from the entrance, and the
    // vertical run is path (and propagation of q)
    const entry = dn > 0 ? target.zIn : target.zOut;
    const exit = dn > 0 ? target.zOut : target.zIn;
    if (exit !== entry) {
      z = z + (exit - entry);
      const rise = Math.abs(exit - entry);
      path += rise;
      if (q !== null) q = propagateQ(q, rise);
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
  // heights (3D section 2): the axis of every component, a periscope's entrance along +x and its exit
  const prepared: Prepared[] = usable.map((c) => {
    const { zIn, zOut } = c.type === "periscope" ? periscopeHeights(c, layout.table) : { zIn: componentZ(c, layout.table), zOut: componentZ(c, layout.table) };
    return { c, f: frameOf(c), kind: kindOf(c), zIn, zOut };
  });
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
  // `mw`: the power the tree's source emits (section 4.2): its own power_mw for a direct source, what
  // arrived at the fibre input times the coupling for a fibre continuation (the first arrival only)
  const queue: Array<{ src: Prepared; wl: number | null; mw: number | null }> = [];
  const seen = new Set<string>();
  for (const p of prepared) {
    if (p.kind !== "source" || (p.c.type === "fiber_out" && continued.has(p.c.id))) continue;
    const ownMw = p.c.type === "lamp" || p.c.type === "led" ? null : numProp(p.c, "power_mw");
    for (const wl of ownWavelengths(p.c)) {
      queue.push({ src: p, wl, mw: ownMw });
      seen.add(key(p.c.id, wl));
    }
  }

  const asked = opts.maxTraceEvents;
  const budget: Budget = {
    limit: asked === undefined || !Number.isFinite(asked) ? TRACE_LIMITS.maxTraceEvents : Math.max(0, Math.trunc(asked)),
    events: 0,
    cut: false,
    passes: 0,
  };
  const trees: DetailedTrace = [];
  for (let head = 0; head < queue.length; head++) {
    const { src, wl, mw } = queue[head]!;
    const branches: DetailedBranch[] = [];
    const { light, S, q } = sourceLight(src.c, wl, mw);
    // a source emits at its own height, a fibre continuation at that of its fibre output (3D section 3.1)
    walk(world, budget, branches, "0", null, src.c.x_mm, src.c.y_mm, src.f.n.x, src.f.n.y, 1, 0, 0, null, light, S, q, src.zIn);
    trees.push({ source: src.c.id, wavelength_nm: wl, beam: { source_mw: light.sourceMw, m2: light.m2 }, branches });
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
      const arrived = lastEvent.beam?.mw ?? null;
      queue.push({ src: next, wl, mw: arrived === null ? null : arrived * (numProp(fin.c, "coupling") ?? 1) });
    }
  }
  return trees;
}

// ── the contract form ─────────────────────────────────────────────────────────

/** round3 without the sign of zero: -0 is not a value anybody wants to read in a report or a JSON file. */
const r3 = (x: number): number => round3(x) + 0;
const r6 = (x: number): number => pyRound(x, 6) + 0;
/** The beam numbers (v2 section 4.8, mW too since section 12): 1e-9, finer than the 1e-6 they are compared at; q see rQ. */
const r9 = (x: number): number => pyRound(x, 9) + 0;
const r9n = (x: number | null): number | null => (x === null ? null : r9(x));
const rStokes = (s: StokesNorm | null): StokesNorm | null => (s === null ? null : [r9(s[0]), r9(s[1]), r9(s[2])]);
/** q at 9 significant digits (zR runs from µm to km, a fixed number of decimals fits neither end). */
const sig9 = (x: number): number => Number(x.toPrecision(9)) + 0;
const rQ = (q: QParam | null): QParam | null => (q === null ? null : [sig9(q[0]), sig9(q[1])]);

/** The contract form of a segment: its end points, its height, its beam and its passes (3D section 4) when it has them. */
function contractSegment(g: Segment): Segment {
  const out: Segment = { x0: r3(g.x0), y0: r3(g.y0), x1: r3(g.x1), y1: r3(g.y1) };
  if (g.z_mm !== undefined) out.z_mm = r3(g.z_mm);
  if (g.beam) out.beam = { p: r9(g.beam.p), s: rStokes(g.beam.s), q: rQ(g.beam.q) };
  if (g.passes) out.passes = g.passes.map((p) => ({ component: p.component, x: r3(p.x), y: r3(p.y), dz_mm: r3(p.dz_mm) }));
  return out;
}

function contractEvent(e: TraceEvent): TraceEvent {
  // the keys in the order of the contract (3D section 4), the heights only when the event has them
  const out: TraceEvent = {
    component: e.component,
    action: e.action,
    x: r3(e.x),
    y: r3(e.y),
    ...(e.z_mm !== undefined ? { z_mm: r3(e.z_mm) } : {}),
    offset_mm: r3(e.offset_mm),
    ...(e.dz_mm !== undefined ? { dz_mm: r3(e.dz_mm) } : {}),
    incidence_deg: r3(e.incidence_deg),
    path_mm: r3(e.path_mm),
  };
  if (e.beam) out.beam = { p: r9(e.beam.p), mw: r9n(e.beam.mw), d_mm: r9n(e.beam.d_mm), s: rStokes(e.beam.s) };
  return out;
}

/** The trace as the contract (and the shared fixtures) have it: rounded, only the specified keys. */
export function toContractTrace(detailed: DetailedTrace): Trace {
  return detailed.map((s): SourceTrace => {
    const tree: SourceTrace = { source: s.source, wavelength_nm: s.wavelength_nm, branches: [] };
    if (s.beam) tree.beam = { source_mw: r9n(s.beam.source_mw), m2: r9n(s.beam.m2) };
    tree.branches = s.branches.map(
      (b): TraceBranch => ({
        branch: b.branch,
        fraction: r6(b.fraction),
        segments: b.segments.map(contractSegment),
        events: b.events.map(contractEvent),
        end: b.end,
      }),
    );
    return tree;
  });
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
