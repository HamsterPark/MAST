// Heights for the 3D view: where above the table top a beam runs and a component sits.
//
// The rules are the height contract's (docs/v2/design/optical_layout_3d.md sections 2 and 4) and are written once,
// in lib/opticalLayout/heights.ts. In short, all in mm above the table top:
//
//   table.beam_height_mm   the table's beam height H                    (absent: 75 mm)
//   props.z_mm             a component's optical axis, a source's exit  (absent: H)
//   props.z_out_mm         a periscope's exit                           (absent: its entrance, no change)
//   segment.z_mm           the height of a traced segment, the same all along it   (absent: H)
//   event.z_mm             the height of the beam arriving at a component
//
// The functions here only adapt the editor's types, which do not name these keys (a
// trace of an older server, or a layout that never gave a height, has none of them: then everything is at H and
// the 3D view works all the same). The 2D data model is the only source; nothing here writes a height.

import type { LayoutComponent, Segment, TableSpec } from "../types.ts";
import { DEFAULT_BEAM_HEIGHT_MM, beamHeightOf, componentZ, periscopeHeights, segmentZ, type HeightTable } from "../heights.ts";

export { DEFAULT_BEAM_HEIGHT_MM };

const heightTable = (t: TableSpec): HeightTable => t as unknown as HeightTable;

/** H: `table.beam_height_mm`, or 75. */
export function tableBeamHeight(table: TableSpec): number {
  return beamHeightOf(heightTable(table));
}

/** The height of a component's optical axis: `props.z_mm`, or H. */
export function componentHeight(c: Pick<LayoutComponent, "props">, table: TableSpec): number {
  return componentZ(c, heightTable(table));
}

/** A periscope's exit height: `props.z_out_mm`, or its entrance (no change of height). */
export function periscopeOutHeight(c: Pick<LayoutComponent, "props">, table: TableSpec): number {
  return periscopeHeights(c, heightTable(table)).zOut;
}

/** The height of a traced segment: `segment.z_mm`, or H. */
export function segmentHeight(seg: Segment, table: TableSpec): number {
  return segmentZ(seg as { z_mm?: number | null }, heightTable(table));
}

/** The height of a traced event (where a ray met a component), when the trace says; null otherwise. */
export function eventHeight(ev: object): number | null {
  const z = (ev as { z_mm?: unknown }).z_mm;
  return typeof z === "number" && Number.isFinite(z) ? z : null;
}
