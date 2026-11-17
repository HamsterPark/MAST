// Which labels the 3D view shows: the names of 400 components on top of each other help nobody and cost the
// browser a style change per label per frame. Each frame the view projects every label and keeps those that do
// not overlap a label it already kept, the most important first (the selected and hovered components, then the
// nearer ones), up to a number. Pure (test/opticalLayout3dPick.test.ts).

export interface LabelBox {
  id: string;
  /** centre on the screen, px */
  x: number;
  y: number;
  /** size, px */
  w: number;
  h: number;
  /** higher first (selected 2, hovered 1, others 0) */
  priority: number;
  /** distance from the camera: nearer first among equals */
  depth: number;
}

/** At most this many labels at once. */
export const MAX_LABELS = 160;
const CELL = 48;

/** The ids of the labels to show on a view `width` × `height` px. */
export function declutter(boxes: readonly LabelBox[], width: number, height: number, max = MAX_LABELS): Set<string> {
  const order = [...boxes].sort((a, b) => b.priority - a.priority || a.depth - b.depth || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  const kept = new Set<string>();
  const grid = new Map<number, LabelBox[]>();
  const cellsOf = (b: LabelBox): number[] => {
    const out: number[] = [];
    const c0 = Math.floor((b.x - b.w / 2) / CELL);
    const c1 = Math.floor((b.x + b.w / 2) / CELL);
    const r0 = Math.floor((b.y - b.h / 2) / CELL);
    const r1 = Math.floor((b.y + b.h / 2) / CELL);
    for (let c = c0; c <= c1; c++) for (let r = r0; r <= r1; r++) out.push(c * 100003 + r);
    return out;
  };
  const overlaps = (a: LabelBox, b: LabelBox) => Math.abs(a.x - b.x) * 2 < a.w + b.w && Math.abs(a.y - b.y) * 2 < a.h + b.h;
  for (const b of order) {
    if (kept.size >= max) break;
    // off the view (a little margin): not shown, and it takes no room
    if (b.x + b.w / 2 < -8 || b.x - b.w / 2 > width + 8 || b.y + b.h / 2 < -8 || b.y - b.h / 2 > height + 8) continue;
    const cells = cellsOf(b);
    let free = true;
    for (const k of cells) {
      for (const o of grid.get(k) ?? []) {
        if (overlaps(b, o)) {
          free = false;
          break;
        }
      }
      if (!free) break;
    }
    // the selected (and hovered) component's label is always shown
    if (!free && b.priority < 1) continue;
    kept.add(b.id);
    for (const k of cells) {
      const list = grid.get(k);
      if (list) list.push(b);
      else grid.set(k, [b]);
    }
  }
  return kept;
}
