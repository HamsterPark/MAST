// The resolution of the figure PNG (docs/v2/design/optical_layout_exports.md): how many pixels for a figure of a
// given size on paper, and the pHYs chunk that tells Word and LaTeX that size.
//
// The figure (exportStyle.ts paperFrame) is laid out in CSS pixels, 96 to the inch: that is the size it has on paper
// as an SVG in Word or Inkscape and as the PDF, and the PNG says the same unless the operator asks for another width.
// The PNG is then made `scale` times larger than the figure (rasterize.ts), either to reach a resolution on that paper
// (150, 300, 600 dpi) or by a plain factor (1× to 4×), and the pHYs chunk records pixels per metre so that a program
// placing the picture knows how large it is. Word reads pHYs, and so does pdfLaTeX's \includegraphics (without it a
// PNG is taken at 72 dpi, the wrong size).
//
// The pixels per metre are worked out from the width of the PNG that came out (its IHDR), not from what was asked:
// the browser caps a canvas at MAX_SIDE_PX a side, and a picture that was made smaller must say it has fewer dots per
// inch, not claim a smaller paper size. Pure, so the tests run without a browser (test/opticalLayoutExportPng.test.ts).

/** CSS pixels per inch: the size of a pixel of the figure on paper. */
export const CSS_PX_PER_INCH = 96;
export const MM_PER_INCH = 25.4;

/** The resolutions offered for a printed figure. */
export const DPI_CHOICES: readonly number[] = [150, 300, 600];
/** The factors offered instead (times the figure's own size in pixels). */
export const SCALE_CHOICES: readonly number[] = [1, 2, 3, 4];

/**
 * Largest side of the PNG. rasterize.ts caps the canvas at the same number (browsers refuse much larger ones); the
 * plan keeps under it so that what is asked for is what comes out, and says so when it cannot.
 */
export const MAX_SIDE_PX = 8000;

/** The paper widths the dialog offers besides the figure's own (journal columns, mm). */
export const PAPER_WIDTH_PRESETS: ReadonlyArray<{ id: string; mm: number }> = [
  { id: "single", mm: 85 },
  { id: "double", mm: 178 },
];
/** Bounds of a paper width the operator types (mm). */
export const PAPER_WIDTH_MIN_MM = 10;
export const PAPER_WIDTH_MAX_MM = 2000;

/** Font size (CSS px) of the component names in the figure: LayoutSvg's PAPER_LABEL_PX (test/opticalLayoutExportPng pins it). */
export const FIGURE_LABEL_PX = 14;
/** Below this many points the PNG dialog warns that the names will be hard to read in print. */
export const SMALL_TEXT_PT = 5;

/** The size on paper (mm) of `px` CSS pixels. */
export function pxToMm(px: number): number {
  return (px * MM_PER_INCH) / CSS_PX_PER_INCH;
}

/** Dots per inch as pixels per metre, the unit of pHYs (rounded to a whole number, as the chunk stores it). */
export function dpiToPpm(dpi: number): number {
  return Math.round(dpi / (MM_PER_INCH / 1000));
}

/** Pixels per metre as dots per inch. */
export function ppmToDpi(ppm: number): number {
  return (ppm * MM_PER_INCH) / 1000;
}

export type PngResolution = { kind: "dpi"; dpi: number } | { kind: "scale"; scale: number };

export interface PngPlan {
  /** output pixels per pixel of the figure */
  scale: number;
  /** size of the PNG, pixels */
  width: number;
  height: number;
  /** size on paper the PNG says it has, mm */
  paperWidthMm: number;
  paperHeightMm: number;
  /** pixels per inch on that paper */
  dpi: number;
  /** the asked resolution would have made a PNG wider or taller than MAX_SIDE_PX: it was lowered */
  limited: boolean;
}

/**
 * The PNG of a figure `figure.width` × `figure.height` CSS pixels, at a resolution: `dpi` dots per inch on paper
 * `paperWidthMm` wide (default: the figure's own size, 96 pixels to the inch), or `scale` times the figure's pixels
 * (the paper size then stays). The scale is lowered to keep both sides within `maxSide`; `dpi` is then what the
 * picture really has.
 */
export function planPng(figure: { width: number; height: number }, res: PngResolution, paperWidthMm?: number, maxSide = MAX_SIDE_PX): PngPlan {
  const w = Math.max(1, figure.width);
  const h = Math.max(1, figure.height);
  const paperW = paperWidthMm !== undefined && Number.isFinite(paperWidthMm) && paperWidthMm > 0 ? paperWidthMm : pxToMm(w);
  const paperH = (paperW * h) / w;
  const wanted = res.kind === "dpi" ? ((paperW / MM_PER_INCH) * res.dpi) / w : res.scale;
  const cap = maxSide / Math.max(w, h);
  const limited = wanted > cap;
  const scale = Math.max(1e-3, limited ? cap : wanted);
  const width = Math.max(1, Math.round(w * scale));
  const height = Math.max(1, Math.round(h * scale));
  return { scale, width, height, paperWidthMm: paperW, paperHeightMm: paperH, dpi: width / (paperW / MM_PER_INCH), limited };
}

/**
 * The size in points a text of `px` figure pixels comes out at when the figure is printed `paperWidthMm` wide (the
 * figure being `figureWidthPx` wide): the dialog says it, since a figure squeezed into one column has small letters.
 */
export function textPt(px: number, figureWidthPx: number, paperWidthMm: number): number {
  return ((px * paperWidthMm) / pxToMm(Math.max(1, figureWidthPx))) * (72 / CSS_PX_PER_INCH);
}

// ── PNG chunks ────────────────────────────────────────────────────────────────

/** The eight bytes every PNG starts with. */
export const PNG_SIGNATURE: readonly number[] = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];

let crcTable: Uint32Array | null = null;

/** CRC-32 (ISO 3309, the one PNG uses) of `bytes[start, end)`. */
export function crc32(bytes: Uint8Array, start = 0, end = bytes.length): number {
  if (!crcTable) {
    crcTable = new Uint32Array(256);
    for (let n = 0; n < 256; n++) {
      let c = n;
      for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
      crcTable[n] = c >>> 0;
    }
  }
  let c = 0xffffffff;
  for (let i = start; i < end; i++) c = crcTable[(c ^ bytes[i]!) & 0xff]! ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

export interface PngChunk {
  type: string;
  /** offset of the chunk (its length field) in the file */
  at: number;
  /** length of its data */
  length: number;
}

const u32 = (b: Uint8Array, at: number): number => ((b[at]! << 24) | (b[at + 1]! << 16) | (b[at + 2]! << 8) | b[at + 3]!) >>> 0;

/** The chunks of a PNG, in order; throws on what is not a whole PNG (bad signature, a chunk past the end, a bad CRC, no IHDR first, no IEND last). */
export function pngChunks(png: Uint8Array): PngChunk[] {
  if (png.length < 8 || PNG_SIGNATURE.some((v, i) => png[i] !== v)) throw new Error("not a PNG file");
  const out: PngChunk[] = [];
  let at = 8;
  while (at < png.length) {
    if (at + 12 > png.length) throw new Error("the PNG ends inside a chunk");
    const length = u32(png, at);
    const end = at + 12 + length;
    if (end > png.length) throw new Error("the PNG ends inside a chunk");
    const type = String.fromCharCode(png[at + 4]!, png[at + 5]!, png[at + 6]!, png[at + 7]!);
    if (crc32(png, at + 4, at + 8 + length) !== u32(png, at + 8 + length)) throw new Error(`bad CRC in the ${type} chunk`);
    out.push({ type, at, length });
    at = end;
    if (type === "IEND") break;
  }
  if (out[0]?.type !== "IHDR" || out[out.length - 1]?.type !== "IEND") throw new Error("not a whole PNG (IHDR first, IEND last)");
  return out;
}

/** Width and height of a PNG, from its IHDR. */
export function pngSize(png: Uint8Array): { width: number; height: number } {
  const ihdr = pngChunks(png)[0]!;
  return { width: u32(png, ihdr.at + 8), height: u32(png, ihdr.at + 12) };
}

/** A chunk: length, type, data, CRC of type and data. */
function chunk(type: string, data: Uint8Array): Uint8Array<ArrayBuffer> {
  const out = new Uint8Array(12 + data.length);
  const view = new DataView(out.buffer);
  view.setUint32(0, data.length);
  for (let i = 0; i < 4; i++) out[4 + i] = type.charCodeAt(i);
  out.set(data, 8);
  view.setUint32(8 + data.length, crc32(out, 4, 8 + data.length));
  return out;
}

/** The pHYs chunk: `ppm` pixels per metre both ways (square pixels), unit 1 = the metre. */
export function physChunk(ppm: number): Uint8Array<ArrayBuffer> {
  const v = Math.round(ppm);
  if (!(v >= 1 && v <= 0xffffffff)) throw new Error(`pixels per metre out of range: ${ppm}`);
  const data = new Uint8Array(9);
  const view = new DataView(data.buffer);
  view.setUint32(0, v);
  view.setUint32(4, v);
  data[8] = 1;
  return chunk("pHYs", data);
}

/** What the pHYs chunk of a PNG says: pixels per metre (x, y) and the unit (1 = metre, 0 = only the aspect); null without one. */
export function readPhys(png: Uint8Array): { x: number; y: number; unit: number } | null {
  const c = pngChunks(png).find((k) => k.type === "pHYs");
  if (!c || c.length !== 9) return null;
  return { x: u32(png, c.at + 8), y: u32(png, c.at + 12), unit: png[c.at + 16]! };
}

/**
 * The PNG with one pHYs chunk saying `ppm` pixels per metre, right after the IHDR (the PNG standard wants it before
 * the first IDAT); a pHYs the encoder wrote is dropped. Everything else is copied byte for byte.
 */
export function withPhys(png: Uint8Array, ppm: number): Uint8Array<ArrayBuffer> {
  const chunks = pngChunks(png);
  const phys = physChunk(ppm);
  const kept = chunks.filter((c) => c.type !== "pHYs");
  const size = 8 + kept.reduce((n, c) => n + 12 + c.length, 0) + phys.length;
  const out = new Uint8Array(size);
  out.set(png.subarray(0, 8), 0);
  let at = 8;
  for (const c of kept) {
    out.set(png.subarray(c.at, c.at + 12 + c.length), at);
    at += 12 + c.length;
    if (c.type === "IHDR") {
      out.set(phys, at);
      at += phys.length;
    }
  }
  return out;
}

/**
 * The pixels per metre of a PNG `pixelWidth` wide meant to be `paperWidthMm` wide on paper: what pHYs should say.
 * From the pixels that are there, so a PNG the browser made smaller than asked says it has fewer dots per inch.
 */
export function ppmFor(pixelWidth: number, paperWidthMm: number): number {
  return Math.round(pixelWidth / (paperWidthMm / 1000));
}
