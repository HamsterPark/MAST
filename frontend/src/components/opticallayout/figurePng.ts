// The figure PNG with its size on paper (docs/v2/design/optical_layout_exports.md): the figure SVG drawn by the
// browser at the planned scale (rasterize.ts), then a pHYs chunk that says how many pixels go to the metre, so Word
// and LaTeX place it at the size it was planned for. The numbers are lib/opticalLayout/exportPng.ts's; this file is
// the part that needs a browser.

import { planPng, pngSize, ppmFor, ppmToDpi, withPhys, type PngPlan, type PngResolution } from "@/lib/opticalLayout/exportPng";
import { rasterizeSvg } from "./rasterize";

export interface FigurePng {
  blob: Blob;
  plan: PngPlan;
  /** the resolution the file says it has: from the pixels that came out and the paper width */
  dpi: number;
  width: number;
  height: number;
}

/**
 * The PNG of a standalone figure SVG `figure.width` × `figure.height` CSS pixels, at resolution `res` on paper
 * `paperWidthMm` wide (default: the figure's own size, 96 pixels to the inch), with a pHYs chunk.
 */
export async function figurePng(svgText: string, figure: { width: number; height: number }, res: PngResolution, paperWidthMm?: number): Promise<FigurePng> {
  const plan = planPng(figure, res, paperWidthMm);
  const drawn = new Uint8Array(await (await rasterizeSvg(svgText, figure.width, figure.height, plan.scale)).arrayBuffer());
  const { width, height } = pngSize(drawn);
  const ppm = ppmFor(width, plan.paperWidthMm);
  return { blob: new Blob([withPhys(drawn, ppm)], { type: "image/png" }), plan, dpi: ppmToDpi(ppm), width, height };
}
