// The alignment tools of section 5 of docs/v2/design/optical_layout.md, as pure functions
// on a layout and its trace:
//
//   * aim:  turn a mirror (beam splitter, grating, ...) so the beam that hits it is
//     reflected exactly at a target. The normal is the normalised sum of the reversed
//     incoming direction and the unit vector towards the target. A 45 degree fold that
//     turns +x into +y comes out at 135 degrees.
//   * put on the beam: move a component to the nearest point of the beams, or to a given
//     distance along the beam behind another component.
//   * measure: the distance between two points, the path length along the beam.
//
// They read the unrounded (detailed) trace, so a result is exact to the last digit and
// not to the 0.001 mm of the contract output (test/opticalLayoutAlign.test.ts).

import { tr } from "../i18n.ts";
import type { LayoutComponent, OpticalLayout, Vec } from "./types.ts";
import { isSourceType } from "./types.ts";
import type { DetailedBranch, DetailedEvent, DetailedTrace, EventRef } from "./trace.ts";
import { eventsByComponent, traceLayoutDetailed } from "./trace.ts";
import { angleOf, canonicalAngle, closestOnSegment, dot, normalize, sub } from "./geometry.ts";
import { CATALOG } from "./catalog.ts";
import { setPose, setPosition } from "./edit.ts";

// ── aim ───────────────────────────────────────────────────────────────────────

export type AimError =
  | "unknown-component"
  | "not-reflective"
  | "no-beam"
  | "no-target"
  | "same-component"
  | "straight-ahead"
  | "target-at-hit";

export interface AimSolution {
  /** the angle to give the mirror, in [0, 360), unrounded */
  angle_deg: number;
  /** where the beam will hit the mirror after the turn */
  hit: Vec;
  /** that hit point's signed distance from the mirror centre along its surface */
  offset_mm: number;
}

/**
 * The mirror angle that sends a ray arriving with unit direction `dir` along the line
 * through `onBeam` to `target`, for a mirror centred at `center`.
 *
 * Turning the mirror moves the point where the beam meets it, and the reflection has to
 * start from that point, so this iterates: normal from (target - hit point), new hit
 * point from the new plane, until it settles (a few rounds unless the beam is far off
 * the centre). For a beam through the centre the first round is already exact.
 */
export function solveAim(args: { dir: Vec; center: Vec; onBeam: Vec; target: Vec }): AimSolution | AimError {
  const { dir, center, onBeam, target } = args;
  let hit = onBeam;
  let normal: Vec | null = null;
  for (let i = 0; i < 60; i++) {
    const w = normalize(sub(target, hit));
    if (!w) return "target-at-hit";
    const n = normalize({ x: w.x - dir.x, y: w.y - dir.y });
    if (!n) return "straight-ahead";
    normal = n;
    const denom = dot(dir, n);
    if (Math.abs(denom) < 1e-12) return "straight-ahead";
    const t = dot(sub(center, onBeam), n) / denom;
    const next = { x: onBeam.x + dir.x * t, y: onBeam.y + dir.y * t };
    const moved = Math.hypot(next.x - hit.x, next.y - hit.y);
    hit = next;
    if (moved < 1e-12) break;
  }
  if (!normal) return "straight-ahead";
  const angle = angleOf(normal);
  const u = { x: -normal.y, y: normal.x };
  return { angle_deg: angle, hit, offset_mm: (hit.x - center.x) * u.x + (hit.y - center.y) * u.y };
}

/** Whether a component reflects, so that aiming it makes sense. */
export function isAimable(c: LayoutComponent): boolean {
  switch (CATALOG[c.type].role) {
    case "mirror":
    case "splitter":
    case "dichroic":
      return true;
    case "terminate":
      return c.type === "sample" && c.props["reflects"] === true;
    case "custom":
      return c.props["behavior"] === "reflect";
    default:
      return false;
  }
}

/** The hit to aim with: the strongest branch that reaches the component, the earliest on a tie. */
export function strongestHit(refs: readonly EventRef[]): EventRef | null {
  let best: EventRef | null = null;
  for (const r of refs) {
    if (r.event.action !== "reflect" && r.event.action !== "split" && r.event.action !== "back_hit") continue;
    if (best === null || r.branch.fraction > best.branch.fraction + 1e-12) best = r;
  }
  return best;
}

export type AimOutcome =
  | { ok: true; layout: OpticalLayout; angle_deg: number; offset_mm: number; withinAperture: boolean }
  | { ok: false; error: AimError };

/** A target is a point on the platform or another component (its centre). */
export type AimTarget = Vec | { componentId: string };

function targetPoint(layout: OpticalLayout, target: AimTarget): Vec | null {
  if ("componentId" in target) {
    const c = layout.components.find((x) => x.id === target.componentId);
    return c ? { x: c.x_mm, y: c.y_mm } : null;
  }
  return target;
}

/**
 * Turns a mirror so that the beam that hits it goes to the target. Only the angle
 * changes (to 0.01 degree, like every saved angle); the mirror stays where it is.
 * `trace` is the trace of `layout` as it is now.
 */
export function aimMirror(layout: OpticalLayout, trace: DetailedTrace, mirrorId: string, target: AimTarget): AimOutcome {
  const mirror = layout.components.find((c) => c.id === mirrorId);
  if (!mirror) return { ok: false, error: "unknown-component" };
  if (!isAimable(mirror)) return { ok: false, error: "not-reflective" };
  if ("componentId" in target && target.componentId === mirrorId) return { ok: false, error: "same-component" };
  const to = targetPoint(layout, target);
  if (!to) return { ok: false, error: "no-target" };
  const hit = strongestHit(eventsByComponent(trace).get(mirrorId) ?? []);
  if (!hit) return { ok: false, error: "no-beam" };

  const sol = solveAim({
    dir: hit.event.dir,
    center: { x: mirror.x_mm, y: mirror.y_mm },
    onBeam: hit.event.pos,
    target: to,
  });
  if (typeof sol === "string") return { ok: false, error: sol };
  const next = setPose(layout, mirrorId, { x: mirror.x_mm, y: mirror.y_mm, angle: canonicalAngle(sol.angle_deg) });
  return {
    ok: true,
    layout: next,
    angle_deg: canonicalAngle(sol.angle_deg),
    offset_mm: sol.offset_mm,
    withinAperture: Math.abs(sol.offset_mm) <= mirror.aperture_mm / 2,
  };
}

/** `traceCut`: the trace was cut by its budget, so "no beam" is not known to be true. */
export function aimErrorText(e: AimError, traceCut = false): string {
  switch (e) {
    case "unknown-component":
      return tr("找不到这个器件。", "That component does not exist.");
    case "not-reflective":
      return tr("这个器件不反射光，没法瞄准。请选反射镜、分束器、二向色镜或光栅。", "This component does not reflect light, so it cannot be aimed. Pick a mirror, beam splitter, dichroic mirror or grating.");
    case "no-beam":
      if (traceCut) return tr("追迹被截断，不知道有没有光打到这个器件上，所以现在没法瞄准它。", "The trace is cut, so it is not known whether a beam reaches this component, and it cannot be aimed now.");
      return tr("没有光打到这个器件上。先用“放到光束上”把它放进光路。", "No beam reaches this component. Use “Put on the beam” to place it in the light path first.");
    case "no-target":
      return tr("找不到目标。", "The target was not found.");
    case "same-component":
      return tr("目标不能是它自己。", "The target cannot be the component itself.");
    case "straight-ahead":
      return tr("目标正好在光继续前进的方向上，镜子转不出这个反射。", "The target lies straight ahead of the incoming beam; no mirror angle reflects it there.");
    default:
      return tr("目标就在光打到镜子的那一点上。", "The target sits right at the point where the beam meets the mirror.");
  }
}

// ── put on the beam ───────────────────────────────────────────────────────────

export interface BeamPoint {
  point: Vec;
  /** unit direction of the beam there */
  dir: Vec;
  /** distance from the component's current centre to the point */
  distance: number;
  source: string;
  wavelength_nm: number | null;
  branch: string;
}

/**
 * The point on any beam nearest to a component's centre, with the component itself left
 * out of the trace (a mirror on the beam would otherwise only find its own reflection).
 * Null for a source, or when there is no beam at all.
 */
export function nearestBeamPoint(layout: OpticalLayout, componentId: string): BeamPoint | null {
  const c = layout.components.find((x) => x.id === componentId);
  if (!c || isSourceType(c.type)) return null;
  const trace = traceLayoutDetailed(layout, { skip: new Set([componentId]) });
  const centre = { x: c.x_mm, y: c.y_mm };
  let best: BeamPoint | null = null;
  for (const s of trace) {
    for (const br of s.branches) {
      for (const g of br.segments) {
        const a = { x: g.x0, y: g.y0 };
        const b = { x: g.x1, y: g.y1 };
        const len = Math.hypot(b.x - a.x, b.y - a.y);
        if (len < 1e-9) continue;
        const cl = closestOnSegment(centre, a, b);
        if (best === null || cl.dist < best.distance) {
          best = {
            point: cl.point,
            dir: { x: (b.x - a.x) / len, y: (b.y - a.y) / len },
            distance: cl.dist,
            source: s.source,
            wavelength_nm: s.wavelength_nm,
            branch: br.branch,
          };
        }
      }
    }
  }
  return best;
}

/** Moves a component onto the nearest beam. Null when there is no beam to move onto. */
export function putOnBeam(layout: OpticalLayout, componentId: string): { layout: OpticalLayout; beam: BeamPoint } | null {
  const beam = nearestBeamPoint(layout, componentId);
  if (!beam) return null;
  return { layout: setPosition(layout, componentId, beam.point.x, beam.point.y), beam };
}

/** The angle that makes a component's axis follow a beam of direction `dir`; null for types where it is not defined. */
export function axisAngleFor(c: LayoutComponent, dir: Vec): number | null {
  switch (CATALOG[c.type].role) {
    case "transmit":
    case "gate":
    case "custom":
    case "source":
      return canonicalAngle(angleOf(dir));
    case "terminate":
      return canonicalAngle(angleOf(dir) + 180);
    default:
      return null;
  }
}

export interface Leg {
  start: Vec;
  dir: Vec;
  /** length of the leg up to the next event or the end of the branch */
  length: number;
  source: string;
  wavelength_nm: number | null;
  branch: string;
}

function legOf(g: { x0: number; y0: number; x1: number; y1: number }, s: string, wl: number | null, branch: string): Leg | null {
  const length = Math.hypot(g.x1 - g.x0, g.y1 - g.y0);
  if (length < 1e-9) return null;
  return { start: { x: g.x0, y: g.y0 }, dir: { x: (g.x1 - g.x0) / length, y: (g.y1 - g.y0) / length }, length, source: s, wavelength_nm: wl, branch };
}

/** Beam legs that leave a component (a source: its first leg). Strongest branch first. */
export function legsAfter(layout: OpticalLayout, trace: DetailedTrace, componentId: string): Leg[] {
  const comp = layout.components.find((c) => c.id === componentId);
  if (!comp) return [];
  const legs: Array<{ leg: Leg; fraction: number }> = [];
  if (isSourceType(comp.type)) {
    for (const s of trace) {
      if (s.source !== componentId) continue;
      const root = s.branches[0];
      const g = root?.segments[0];
      const leg = g ? legOf(g, s.source, s.wavelength_nm, root!.branch) : null;
      if (leg && root) legs.push({ leg, fraction: root.fraction });
    }
  } else {
    for (const s of trace) {
      for (const br of s.branches) {
        br.events.forEach((ev: DetailedEvent, i) => {
          if (ev.component !== componentId) return;
          if (ev.action === "reflect" || ev.action === "transmit") {
            const g = br.segments[i + 1];
            const leg = g ? legOf(g, s.source, s.wavelength_nm, br.branch) : null;
            if (leg) legs.push({ leg, fraction: br.fraction });
          } else if (ev.action === "split") {
            for (const kid of s.branches) {
              if (kid.parent !== br.branch || Math.abs(kid.startPath - ev.path_mm) > 1e-9) continue;
              const g = kid.segments[0];
              const leg = g ? legOf(g, s.source, s.wavelength_nm, kid.branch) : null;
              if (leg) legs.push({ leg, fraction: kid.fraction });
            }
          }
        });
      }
    }
  }
  return legs.sort((a, b) => b.fraction - a.fraction).map((x) => x.leg);
}

export interface AlongBeam {
  point: Vec;
  dir: Vec;
  /** the distance asked for is longer than the leg: the point lies past the next component */
  beyondLeg: boolean;
  leg: Leg;
}

/** The point `distance` mm along the beam behind a component (or source), on its strongest outgoing leg. */
export function pointAlongBeam(layout: OpticalLayout, trace: DetailedTrace, afterId: string, distance: number, legIndex = 0): AlongBeam | null {
  if (!Number.isFinite(distance) || distance < 0) return null;
  const leg = legsAfter(layout, trace, afterId)[legIndex];
  if (!leg) return null;
  return {
    point: { x: leg.start.x + leg.dir.x * distance, y: leg.start.y + leg.dir.y * distance },
    dir: leg.dir,
    beyondLeg: distance > leg.length + 1e-9,
    leg,
  };
}

/** Puts a component `distance` mm along the beam behind another one; optionally turns its axis along the beam. */
export function placeAlongBeam(
  layout: OpticalLayout,
  trace: DetailedTrace,
  componentId: string,
  afterId: string,
  distance: number,
  opts: { alignAxis?: boolean; legIndex?: number } = {},
): { layout: OpticalLayout; along: AlongBeam } | null {
  const c = layout.components.find((x) => x.id === componentId);
  if (!c || componentId === afterId) return null;
  const along = pointAlongBeam(layout, trace, afterId, distance, opts.legIndex ?? 0);
  if (!along) return null;
  const axis = opts.alignAxis ? axisAngleFor(c, along.dir) : null;
  const next =
    axis === null
      ? setPosition(layout, componentId, along.point.x, along.point.y)
      : setPose(layout, componentId, { x: along.point.x, y: along.point.y, angle: axis });
  return { layout: next, along };
}

// ── measuring ─────────────────────────────────────────────────────────────────

export interface Measurement {
  dx: number;
  dy: number;
  distance: number;
  /** direction from a to b in [0, 360) */
  angle_deg: number;
}

export function measure(a: Vec, b: Vec): Measurement {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  return { dx, dy, distance: Math.hypot(dx, dy), angle_deg: dx === 0 && dy === 0 ? 0 : angleOf({ x: dx, y: dy }) };
}

export interface BeamPath {
  mm: number;
  source: string;
  wavelength_nm: number | null;
  branch: string;
}

const isUnder = (branch: string, ancestor: string) => branch === ancestor || branch.startsWith(`${ancestor}.`);

/**
 * The path length along the beam from one component to another (the delay between two
 * mirrors of a pump-probe line). The shortest path where a beam leads from the first to
 * the second; the first may be a source. Null if no beam does.
 */
export function beamPathBetween(layout: OpticalLayout, trace: DetailedTrace, fromId: string, toId: string): BeamPath | null {
  if (fromId === toId) return null;
  const from = layout.components.find((c) => c.id === fromId);
  if (!from) return null;
  let best: BeamPath | null = null;
  const consider = (mm: number, s: string, wl: number | null, branch: string) => {
    if (mm < 0) return;
    if (best === null || mm < best.mm) best = { mm, source: s, wavelength_nm: wl, branch };
  };
  for (const s of trace) {
    const starts: Array<{ branch: DetailedBranch; index: number; path: number }> = [];
    if (isSourceType(from.type)) {
      if (s.source !== fromId) continue;
      starts.push({ branch: s.branches[0]!, index: -1, path: 0 });
    } else {
      for (const br of s.branches) {
        br.events.forEach((ev, i) => {
          if (ev.component === fromId) starts.push({ branch: br, index: i, path: ev.path_mm });
        });
      }
    }
    for (const st of starts) {
      for (const br of s.branches) {
        if (!isUnder(br.branch, st.branch.branch)) continue;
        br.events.forEach((ev, i) => {
          if (ev.component !== toId) return;
          if (br === st.branch && i <= st.index) return;
          consider(ev.path_mm - st.path, s.source, s.wavelength_nm, br.branch);
        });
      }
    }
  }
  return best;
}
