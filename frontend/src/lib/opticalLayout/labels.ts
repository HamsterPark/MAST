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
}

const overlaps = (a: LabelBox, b: LabelBox, gap: number): boolean =>
  a.x < b.x + b.w + gap && b.x < a.x + a.w + gap && a.y < b.y + b.h + gap && b.y < a.y + a.h + gap;

/** The ids of the labels to draw, in the order given for the unforced ones. */
export function placeLabels(boxes: readonly LabelBox[], gap = 2): Set<string> {
  const placed: LabelBox[] = [];
  const shown = new Set<string>();
  // forced boxes first, so nothing placed earlier can push them out
  for (const b of boxes) {
    if (!b.force) continue;
    placed.push(b);
    shown.add(b.id);
  }
  for (const b of boxes) {
    if (b.force) continue;
    if (placed.some((p) => overlaps(b, p, gap))) continue;
    placed.push(b);
    shown.add(b.id);
  }
  return shown;
}

/** The size of a text label at `fontPx`, estimated without measuring the DOM. */
export function labelSize(text: string, fontPx = 11): { w: number; h: number } {
  // CJK characters are about twice as wide as Latin ones
  let units = 0;
  for (const ch of text) units += ch.charCodeAt(0) > 0x2e80 ? 1.8 : 1;
  return { w: Math.ceil(units * fontPx * 0.58) + 4, h: Math.ceil(fontPx * 1.3) };
}
