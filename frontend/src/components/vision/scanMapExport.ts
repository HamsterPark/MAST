// Export the scan map as a PNG figure: the canvas as drawn (scale bar, axes,
// ruler included) plus a caption and a legend band, which live in HTML on the
// page and would otherwise be missing from the picture.
//
// DOM canvas code, so not unit-tested; the file name is (exportFileName in
// lib/scanMapView.ts) and the composition was checked by exporting from the
// headless browser and opening the file.

export interface LegendEntry {
  label: string;
  color: string;
  /** "14", "5/14" … */
  count?: string;
  /** draw an outlined ring instead of a filled square (the latest-action ring) */
  ring?: boolean;
}

/** The map canvas, with a caption line and a wrapped legend band underneath. */
export function composeMapPng(
  map: HTMLCanvasElement,
  opts: { ratio: number; bg: string; ink: string; muted: string; entries: LegendEntry[]; caption: string; font: string },
): HTMLCanvasElement {
  const r = opts.ratio;
  const W = map.width;
  const pad = 12 * r;
  const sw = 10 * r;
  const gap = 6 * r;
  const itemGap = 16 * r;
  const lineH = 20 * r;
  const fs = 12 * r;
  const font = `${fs}px ${opts.font}`;

  const probe = document.createElement("canvas").getContext("2d");
  const measure = (s: string) => {
    if (!probe) return s.length * fs * 0.6;
    probe.font = font;
    return probe.measureText(s).width;
  };
  const textOf = (e: LegendEntry) => (e.count ? `${e.label} ${e.count}` : e.label);

  const lines: { e: LegendEntry; x: number }[][] = [[]];
  let x = pad;
  for (const e of opts.entries) {
    const w = sw + gap + measure(textOf(e));
    const cur = lines[lines.length - 1]!;
    if (x + w > W - pad && cur.length > 0) {
      lines.push([]);
      x = pad;
    }
    lines[lines.length - 1]!.push({ e, x });
    x += w + itemGap;
  }
  const bandH = pad + lineH * (1 + lines.length) + pad;

  const out = document.createElement("canvas");
  out.width = W;
  out.height = map.height + bandH;
  const ctx = out.getContext("2d");
  if (!ctx) return map;
  // The stage is transparent; the page colour behind it is the map background.
  ctx.fillStyle = opts.bg;
  ctx.fillRect(0, 0, out.width, out.height);
  ctx.drawImage(map, 0, 0);
  ctx.fillStyle = opts.muted;
  ctx.globalAlpha = 0.5;
  ctx.fillRect(0, map.height, W, Math.max(1, Math.round(r)));
  ctx.globalAlpha = 1;

  ctx.font = font;
  ctx.textBaseline = "middle";
  ctx.fillStyle = opts.ink;
  ctx.fillText(opts.caption, pad, map.height + pad + lineH / 2);

  lines.forEach((line, li) => {
    const y = map.height + pad + lineH * (li + 1) + lineH / 2;
    for (const { e, x: lx } of line) {
      if (e.ring) {
        ctx.strokeStyle = e.color;
        ctx.lineWidth = 2 * r;
        ctx.beginPath();
        ctx.arc(lx + sw / 2, y, sw / 2 - r, 0, 2 * Math.PI);
        ctx.stroke();
      } else {
        ctx.fillStyle = e.color;
        ctx.fillRect(lx, y - sw / 2, sw, sw);
      }
      ctx.fillStyle = opts.ink;
      ctx.fillText(textOf(e), lx + sw + gap, y);
    }
  });
  return out;
}

/** Save a canvas as a PNG download. */
export function downloadCanvas(c: HTMLCanvasElement, name: string): void {
  c.toBlob((blob) => {
    if (!blob) return;
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  }, "image/png");
}
