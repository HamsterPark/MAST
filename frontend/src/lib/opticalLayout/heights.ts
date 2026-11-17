// Heights: the third dimension of the optical layout, docs/v2/design/optical_layout_3d.md.
//
// The layout stays a top view; what is added is the height of things above the table top (mm):
//   * `table.beam_height_mm`, H: the height of every component and source that does not give its own
//     (75 mm when the table does not give one);
//   * `props.z_mm` of any component: the height of its optical axis (a source: of its exit);
//   * `props.z_out_mm` of a periscope: the height of its exit. Light that travels along the periscope's +x
//     (its `angle_deg`, d · n > 0) enters at z_mm and leaves at z_out_mm; light the other way round enters at
//     z_out_mm and leaves at z_mm (section 3.4).
// A trace (section 4) gives every segment its height `z_mm`, every event the height of the arriving beam `z_mm`
// and `dz_mm`, that height minus the component's axis; after a periscope the next segment is at another height,
// the vertical run between the two being at the periscope's event.
//
// Everything that needs a height reads it through these functions (the tracer, the checks, the form, the 2D
// marks, the 3D view), so the defaults are written once. Pure, and on purpose without imports: the types below
// are the few fields read, so the file means the same next to any version of types.ts.

/** The part of a table a height is read from. */
export interface HeightTable {
  /** H, mm above the table top; absent (or not a finite number) = DEFAULT_BEAM_HEIGHT_MM */
  beam_height_mm?: number | null;
}

/** The part of a component a height is read from. */
export interface HeightComponent {
  type: string;
  props: Readonly<Record<string, unknown>>;
}

/** H when the table gives none (section 2). */
export const DEFAULT_BEAM_HEIGHT_MM = 75;

const finite = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

/** H, the table's beam height (mm): `table.beam_height_mm`, else 75. */
export function beamHeightOf(table: HeightTable): number {
  const h = table.beam_height_mm;
  return finite(h) ? h : DEFAULT_BEAM_HEIGHT_MM;
}

/** The height (mm) of a component's optical axis, a source's exit: `props.z_mm`, else H. A periscope: its entrance along +x. */
export function componentZ(c: Pick<HeightComponent, "props">, table: HeightTable): number {
  const z = c.props["z_mm"];
  return finite(z) ? z : beamHeightOf(table);
}

/** A periscope's two heights (mm): `zIn` = componentZ, `zOut` = `props.z_out_mm`, else zIn. */
export function periscopeHeights(c: Pick<HeightComponent, "props">, table: HeightTable): { zIn: number; zOut: number } {
  const zIn = componentZ(c, table);
  const out = c.props["z_out_mm"];
  return { zIn, zOut: finite(out) ? out : zIn };
}

/**
 * Where light that travels with d · n = `dn` enters a component and where it leaves it (mm): one height for every
 * type but a periscope, entered at z_mm going along its +x (dn > 0) and at z_out_mm the other way (section 3.4).
 */
export function portsOf(c: HeightComponent, table: HeightTable, dn: number): { entry: number; exit: number } {
  if (c.type !== "periscope") {
    const z = componentZ(c, table);
    return { entry: z, exit: z };
  }
  const { zIn, zOut } = periscopeHeights(c, table);
  return dn > 0 ? { entry: zIn, exit: zOut } : { entry: zOut, exit: zIn };
}

/** The height (mm) of a segment of a trace: its `z_mm`, else (the trace of a server without heights) H. */
export function segmentZ(seg: { z_mm?: number | null }, table: HeightTable): number {
  return finite(seg.z_mm) ? seg.z_mm : beamHeightOf(table);
}

/** A vertical run of a beam inside a periscope: at (x, y), from the height z0 to z1 (mm), at the event on `component`. */
export interface VerticalRun {
  component: string;
  x: number;
  y: number;
  z0: number;
  z1: number;
}

/**
 * The vertical runs of a branch (section 3.4): wherever the segment after an event is at another height than the
 * beam that arrived there - only after a periscope that goes up or down. A trace without heights has none.
 */
export function verticalRuns(
  branch: {
    segments: ReadonlyArray<{ z_mm?: number | null }>;
    events: ReadonlyArray<{ component: string; x: number; y: number; z_mm?: number | null }>;
  },
  table: HeightTable,
): VerticalRun[] {
  const out: VerticalRun[] = [];
  branch.events.forEach((ev, i) => {
    const here = branch.segments[i];
    const next = branch.segments[i + 1];
    if (!here || !next) return;
    const z0 = finite(ev.z_mm) ? ev.z_mm : segmentZ(here, table);
    const z1 = segmentZ(next, table);
    if (z1 !== z0) out.push({ component: ev.component, x: ev.x, y: ev.y, z0, z1 });
  });
  return out;
}

/**
 * Whether a layout says anything about heights: a table beam height, or a z_mm / z_out_mm on some component. A
 * layout that does not is drawn and described exactly as before (section 6.1).
 */
export function usesHeights(layout: { table: HeightTable; components: ReadonlyArray<Pick<HeightComponent, "props">> }): boolean {
  if (finite(layout.table.beam_height_mm)) return true;
  return layout.components.some((c) => finite(c.props["z_mm"]) || finite(c.props["z_out_mm"]));
}

/** A height as a person writes it: at most two decimals, no trailing zeros, no "-0" (112.05, 75, 87.7). */
export function heightText(mm: number): string {
  const t = String(Number(mm.toFixed(2)));
  return t === "-0" ? "0" : t;
}

/**
 * The height mark the 2D canvas puts under a component's name (section 7), null when there is nothing to say:
 *   * `z 120` for a component whose axis is not at H;
 *   * a periscope whose exit is not at its entrance: `z 75→250`, with `rise` +1 (up) or -1 (down) for the ▲ / ▼
 *     drawn next to it; a periscope that goes neither way is like any other component.
 */
export function heightMark(c: HeightComponent, table: HeightTable): { text: string; rise: -1 | 0 | 1 } | null {
  if (c.type === "periscope") {
    const { zIn, zOut } = periscopeHeights(c, table);
    if (zOut !== zIn) return { text: `z ${heightText(zIn)}→${heightText(zOut)}`, rise: zOut > zIn ? 1 : -1 };
  }
  const z = componentZ(c, table);
  return z === beamHeightOf(table) ? null : { text: `z ${heightText(z)}`, rise: 0 };
}
