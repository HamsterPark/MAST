// Builders for the optical-layout tests: short ways to say "a laser at (50, 62.5) facing
// +x, a fold mirror at (112.5, 62.5), a detector above it".

import { emptyLayout, newComponent, type NewComponentOptions } from "../../src/lib/opticalLayout/edit.ts";
import type { ComponentType, LayoutComponent, OpticalLayout, Props, TableSpec } from "../../src/lib/opticalLayout/types.ts";

/** A component; `props` replaces the catalogue defaults only when given. */
export function comp(
  type: ComponentType,
  id: string,
  x: number,
  y: number,
  angle: number,
  more: { aperture?: number; props?: Props; label?: string } & Partial<Pick<LayoutComponent, "locked">> = {},
): LayoutComponent {
  const opts: NewComponentOptions = { angle_deg: angle };
  if (more.aperture !== undefined) opts.aperture_mm = more.aperture;
  if (more.props !== undefined) opts.props = more.props;
  if (more.label !== undefined) opts.label = more.label;
  const c = newComponent(type, id, { x, y }, opts);
  return more.locked ? { ...c, locked: true } : c;
}

export function layoutOf(components: LayoutComponent[], table?: Partial<TableSpec>): OpticalLayout {
  const l = emptyLayout("test", "Test layout");
  return { ...l, table: { ...l.table, ...table }, components };
}

/** laser (50, 62.5) -> fold mirror m1 (112.5, 62.5) at 135 degrees -> detector det1 above it, facing the beam. */
export function foldLayout(over: { mirrorAngle?: number; laserY?: number } = {}): OpticalLayout {
  return layoutOf([
    comp("laser", "las1", 50, over.laserY ?? 62.5, 0, { props: { wavelength_nm: 532 } }),
    comp("mirror", "m1", 112.5, 62.5, over.mirrorAngle ?? 135),
    comp("detector", "det1", 112.5, 200, 270),
  ]);
}

const near = (a: number, b: number, tol: number) => Math.abs(a - b) <= tol;

/**
 * Compares two JSON-like values: numbers within `tol`, everything else exactly. Returns
 * the path of the first difference, or null when they agree.
 */
export function firstDifference(actual: unknown, expected: unknown, tol = 1e-6, path = "$"): string | null {
  if (typeof expected === "number") {
    if (typeof actual !== "number") return `${path}: expected number ${expected}, got ${JSON.stringify(actual)}`;
    return near(actual, expected, tol) ? null : `${path}: ${actual} != ${expected}`;
  }
  if (Array.isArray(expected)) {
    if (!Array.isArray(actual)) return `${path}: expected an array, got ${JSON.stringify(actual)}`;
    if (actual.length !== expected.length) return `${path}: length ${actual.length} != ${expected.length}`;
    for (let i = 0; i < expected.length; i++) {
      const d = firstDifference(actual[i], expected[i], tol, `${path}[${i}]`);
      if (d) return d;
    }
    return null;
  }
  if (expected !== null && typeof expected === "object") {
    if (actual === null || typeof actual !== "object" || Array.isArray(actual)) return `${path}: expected an object, got ${JSON.stringify(actual)}`;
    const a = actual as Record<string, unknown>;
    const e = expected as Record<string, unknown>;
    const keys = new Set([...Object.keys(a), ...Object.keys(e)]);
    for (const k of [...keys].sort()) {
      if (!(k in e)) return `${path}.${k}: unexpected key (value ${JSON.stringify(a[k])})`;
      if (!(k in a)) return `${path}.${k}: missing`;
      const d = firstDifference(a[k], e[k], tol, `${path}.${k}`);
      if (d) return d;
    }
    return null;
  }
  return Object.is(actual, expected) || actual === expected ? null : `${path}: ${JSON.stringify(actual)} != ${JSON.stringify(expected)}`;
}

/**
 * A worst case that the model still allows: 361 beam splitters at 45
 * degrees, 3 mm apart, and 32 lasers of 16 wavelengths each firing into them, so that every
 * lineage splits down to the intensity floor. 393 components. Without a budget its trace is
 * 233 760 branches and 217 328 events.
 */
export function denseSplitterLayout(): OpticalLayout {
  const parts: LayoutComponent[] = [];
  let k = 0;
  for (let i = 0; i < 19; i++) {
    for (let j = 0; j < 19; j++) {
      parts.push(comp("beam_splitter", `bs${k}`, 50 + i * 3, 50 + j * 3, 45, { aperture: 25, props: { reflect_fraction: 0.5 } }));
      k++;
    }
  }
  const wavelengths = Array.from({ length: 16 }, (_, n) => 400 + 10 * n);
  for (let s = 0; s < 32; s++) parts.push(comp("laser", `L${s}`, 1, 50 + s * 0.5, 0, { aperture: 5, props: { wavelengths_nm: wavelengths } }));
  return layoutOf(parts, { width_mm: 300, height_mm: 300 });
}

/**
 * Two lasers and a beam splitter: las1 splits at bs1 (150, 62.5); the reflected beam goes up to
 * the mirror m1 and on to det_r, the transmitted one to det_t; las2 goes straight to det_2.
 * Five events in all (the same layout as the shared trace_budget_* fixtures).
 */
export function budgetLayout(): OpticalLayout {
  return layoutOf([
    comp("laser", "las1", 50, 62.5, 0, { props: { wavelength_nm: 532 } }),
    comp("laser", "las2", 50, 400, 0, { props: { wavelength_nm: 633 } }),
    comp("beam_splitter", "bs1", 150, 62.5, 135),
    comp("mirror", "m1", 150, 150, 315),
    comp("detector", "det_r", 300, 150, 180),
    comp("detector", "det_t", 300, 62.5, 180),
    comp("detector", "det_2", 300, 400, 180),
  ]);
}
