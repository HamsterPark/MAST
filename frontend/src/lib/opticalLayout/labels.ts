// Label placement: which component names get drawn when the view is crowded.
//
// Names are drawn next to their component. In a dense layout, or zoomed out, they pile on
// top of each other and none can be read. A greedy pass decides which ones are drawn: the
// selected and hovered components first (they always show), then the rest in layout order,
// each one only if its box does not overlap a name that was already placed. Zooming in
// gives every name room and they all appear. Pure, so the rule is tested
// (test/opticalLayoutLabels.test.ts).

export interface LabelBox {
  id: string;
  /** top-left of the text box on the screen */
  x: number;
  y: number;
  w: number;
  h: number;
  /** drawn whatever overlaps (selected, hovered) */
  force?: boolean;
  /** other top-left corners to try, in order, before the label is dropped (a name above its component, ...) */
  alts?: ReadonlyArray<{ x: number; y: number }>;
}

const overlaps = (a: LabelBox, b: LabelBox, gap: number): boolean =>
  a.x < b.x + b.w + gap && b.x < a.x + a.w + gap && a.y < b.y + b.h + gap && b.y < a.y + a.h + gap;

/** The ids of the labels to draw, in the order given for the unforced ones. */
export function placeLabels(boxes: readonly LabelBox[], gap = 2): Set<string> {
  return new Set(placeLabelsAt(boxes, gap).keys());
}

/** Side (px) of the cells the placed boxes are filed under, so a test only meets its neighbours. */
const CELL_PX = 64;

/** A rectangle a label would rather not cover (a beam), screen px. */
export interface SoftObstacle {
  x: number;
  y: number;
  w: number;
  h: number;
}

/**
 * Where each label that is drawn goes (top-left corner), by id. Forced boxes are placed first, where they are;
 * then the others in the order given, each at the first of its own position and its `alts` that overlaps
 * nothing placed so far, or not at all. With `soft` obstacles (the beams), a place that also keeps clear of them
 * is preferred; only when none does is the first place clear of the other labels taken. The placed boxes are
 * filed in a grid of CELL_PX cells, so a layout of hundreds of names costs about as much per name as one of ten
 * (the plan runs on every frame of a drag).
 */
export function placeLabelsAt(boxes: readonly LabelBox[], gap = 2, soft: readonly SoftObstacle[] = []): Map<string, { x: number; y: number }> {
  const grid = new Map<number, LabelBox[]>();
  const softGrid = new Map<number, SoftObstacle[]>();
  // a cell's key as one number (no string per test): rows of 2^20 cells, far more than a screen has
  const cells = (bx: LabelBox, f: (key: number) => boolean | void): boolean => {
    const x0 = Math.floor((bx.x - gap) / CELL_PX);
    const x1 = Math.floor((bx.x + bx.w + gap) / CELL_PX);
    const y0 = Math.floor((bx.y - gap) / CELL_PX);
    const y1 = Math.floor((bx.y + bx.h + gap) / CELL_PX);
    for (let i = x0; i <= x1; i++) for (let j = y0; j <= y1; j++) if (f(i * 1048576 + j) === true) return true;
    return false;
  };
  const place = (bx: LabelBox) =>
    cells(bx, (key) => {
      const list = grid.get(key);
      if (list) list.push(bx);
      else grid.set(key, [bx]);
    });
  const blocked = (bx: LabelBox) => cells(bx, (key) => grid.get(key)?.some((p) => overlaps(bx, p, gap)) === true);
  for (const o of soft) {
    const ob = { id: "", ...o };
    cells(ob, (key) => {
      const list = softGrid.get(key);
      if (list) list.push(o);
      else softGrid.set(key, [o]);
    });
  }
  const onBeam = (bx: LabelBox) => soft.length > 0 && cells(bx, (key) => softGrid.get(key)?.some((p) => overlaps(bx, { id: "", ...p }, 0)) === true);
  const at = new Map<string, { x: number; y: number }>();
  // forced boxes first, so nothing placed earlier can push them out
  for (const b of boxes) {
    if (!b.force) continue;
    place(b);
    at.set(b.id, { x: b.x, y: b.y });
  }
  for (const b of boxes) {
    if (b.force) continue;
    const places = [{ x: b.x, y: b.y }, ...(b.alts ?? [])];
    let chosen: { x: number; y: number } | null = null;
    let fallback: { x: number; y: number } | null = null;
    for (const pos of places) {
      const tryBox = { ...b, x: pos.x, y: pos.y };
      if (blocked(tryBox)) continue;
      if (!onBeam(tryBox)) {
        chosen = pos;
        break;
      }
      fallback ??= pos;
    }
    const pos = chosen ?? fallback;
    if (!pos) continue;
    place({ ...b, x: pos.x, y: pos.y });
    at.set(b.id, pos);
  }
  return at;
}

/** Widths in character units of texts seen lately (the plan asks for the same names on every frame of a drag). */
const UNITS = new Map<string, number>();
const UNITS_MAX = 4000;

/** The size of a text label at `fontPx`, estimated without measuring the DOM. */
export function labelSize(text: string, fontPx = 11): { w: number; h: number } {
  let units = UNITS.get(text);
  if (units === undefined) {
    // CJK characters are about twice as wide as Latin ones
    units = 0;
    for (const ch of text) units += ch.charCodeAt(0) > 0x2e80 ? 1.8 : 1;
    if (UNITS.size >= UNITS_MAX) UNITS.clear();
    UNITS.set(text, units);
  }
  return { w: Math.ceil(units * fontPx * 0.58) + 4, h: Math.ceil(fontPx * 1.3) };
}
