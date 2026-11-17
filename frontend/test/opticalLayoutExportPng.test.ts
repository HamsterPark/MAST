// ════════════════════════════════════════════════════════════════════════════
// Tests for src/lib/opticalLayout/exportPng.ts: the resolution of the figure PNG and its pHYs chunk
// (docs/v2/design/optical_layout_exports.md).
//
//     cd frontend && npm run test:unit
//
// What is pinned:
//   · CRC-32 against the standard check values and against node's own zlib.crc32 (an independent oracle);
//   · pHYs: one chunk, right after IHDR (before any IDAT), pixels per metre both ways, unit metre, a valid
//     CRC; a pHYs the encoder wrote is replaced, every other chunk copied byte for byte; what is not a whole
//     PNG is refused;
//   · the plan: 96 CSS pixels to the inch is the figure's own size on paper; 150 / 300 / 600 dpi on that or
//     another width; a factor keeps the paper size; the browser's cap lowers the scale and the dpi says so;
//   · the pixels per metre come from the PNG that came out, not from what was asked;
//   · the constants shared with other files (the browser's cap in rasterize.ts, the names' size in LayoutSvg).
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it } from "node:test";
import zlib from "node:zlib";

import {
  CSS_PX_PER_INCH,
  DPI_CHOICES,
  FIGURE_LABEL_PX,
  MAX_SIDE_PX,
  PNG_SIGNATURE,
  SCALE_CHOICES,
  crc32,
  dpiToPpm,
  physChunk,
  planPng,
  pngChunks,
  pngSize,
  ppmFor,
  ppmToDpi,
  pxToMm,
  readPhys,
  textPt,
  withPhys,
} from "../src/lib/opticalLayout/exportPng.ts";

const HERE = fileURLToPath(new URL(".", import.meta.url));
const COMPONENTS = join(HERE, "..", "src", "components", "opticallayout");

/** A chunk built with node's zlib.crc32, not with the code under test. */
function rawChunk(type: string, data: Uint8Array): Buffer {
  const head = Buffer.alloc(8);
  head.writeUInt32BE(data.length, 0);
  head.write(type, 4, "latin1");
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(zlib.crc32(Buffer.concat([head.subarray(4), Buffer.from(data)])) >>> 0, 0);
  return Buffer.concat([head, Buffer.from(data), crc]);
}

/** A real PNG of `w` × `h` grey pixels with whatever chunks `extra` adds after IHDR (zlib from node). */
function makePng(w: number, h: number, extra: Buffer[] = []): Uint8Array {
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(w, 0);
  ihdr.writeUInt32BE(h, 4);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 2; // RGB
  const raw = Buffer.alloc(h * (1 + 3 * w), 0x80);
  for (let y = 0; y < h; y++) raw[y * (1 + 3 * w)] = 0; // filter: none
  return new Uint8Array(Buffer.concat([Buffer.from(PNG_SIGNATURE), rawChunk("IHDR", ihdr), ...extra, rawChunk("IDAT", zlib.deflateSync(raw)), rawChunk("IEND", new Uint8Array(0))]));
}

const types = (png: Uint8Array) => pngChunks(png).map((c) => c.type);

describe("CRC-32", () => {
  it("gives the standard check values", () => {
    assert.equal(crc32(new TextEncoder().encode("123456789")), 0xcbf43926);
    assert.equal(crc32(new TextEncoder().encode("IEND")), 0xae426082, "the CRC every PNG ends with");
    assert.equal(crc32(new Uint8Array(0)), 0);
  });

  it("agrees with node's zlib.crc32 on random data, and over a part of a buffer", () => {
    let seed = 7;
    const bytes = new Uint8Array(4096).map(() => (seed = (seed * 1103515245 + 12345) & 0x7fffffff) & 0xff);
    assert.equal(crc32(bytes), zlib.crc32(bytes) >>> 0);
    assert.equal(crc32(bytes, 100, 900), zlib.crc32(bytes.subarray(100, 900)) >>> 0);
  });
});

describe("the pHYs chunk", () => {
  it("is 9 bytes: pixels per metre x, y, unit 1 (the metre), with a CRC zlib agrees with", () => {
    const c = physChunk(11811);
    assert.equal(c.length, 21);
    const b = Buffer.from(c);
    assert.equal(b.readUInt32BE(0), 9);
    assert.equal(b.toString("latin1", 4, 8), "pHYs");
    assert.equal(b.readUInt32BE(8), 11811);
    assert.equal(b.readUInt32BE(12), 11811);
    assert.equal(b[16], 1);
    assert.equal(b.readUInt32BE(17), zlib.crc32(b.subarray(4, 17)) >>> 0);
    assert.throws(() => physChunk(0));
    assert.throws(() => physChunk(Number.NaN));
  });

  it("goes right after IHDR, before the first IDAT; the rest is copied byte for byte", () => {
    const text = rawChunk("tEXt", new TextEncoder().encode("Software\u0000MAST"));
    const png = makePng(3, 2, [text]);
    const out = withPhys(png, 11811);
    assert.deepEqual(types(out), ["IHDR", "pHYs", "tEXt", "IDAT", "IEND"]);
    assert.deepEqual(readPhys(out), { x: 11811, y: 11811, unit: 1 });
    assert.equal(out.length, png.length + 21);
    // everything but the new chunk is the old file
    const ihdrEnd = 8 + 25;
    assert.deepEqual(Buffer.from(out.subarray(0, ihdrEnd)), Buffer.from(png.subarray(0, ihdrEnd)));
    assert.deepEqual(Buffer.from(out.subarray(ihdrEnd + 21)), Buffer.from(png.subarray(ihdrEnd)));
    assert.deepEqual(pngSize(out), { width: 3, height: 2 });
  });

  it("replaces a pHYs the encoder wrote (72 dpi), wherever it was: one is left, the new one", () => {
    const old = rawChunk("pHYs", Buffer.from([0, 0, 0x0b, 0x13, 0, 0, 0x0b, 0x13, 1]));
    const png = makePng(2, 2, [rawChunk("sRGB", Buffer.from([0])), old]);
    assert.deepEqual(readPhys(png), { x: 2835, y: 2835, unit: 1 });
    const out = withPhys(png, dpiToPpm(300));
    assert.deepEqual(types(out), ["IHDR", "pHYs", "sRGB", "IDAT", "IEND"]);
    assert.deepEqual(readPhys(out), { x: 11811, y: 11811, unit: 1 });
    assert.deepEqual(readPhys(withPhys(out, 5906)), { x: 5906, y: 5906, unit: 1 }, "and again");
  });

  it("the PNG it makes is still read by a strict reader (every CRC checked) and inflates to the same pixels", () => {
    const png = makePng(4, 3);
    const out = withPhys(png, dpiToPpm(600));
    const chunks = pngChunks(out);
    const idat = chunks.find((c) => c.type === "IDAT")!;
    const data = Buffer.from(out.subarray(idat.at + 8, idat.at + 8 + idat.length));
    assert.equal(zlib.inflateSync(data).length, 3 * (1 + 3 * 4));
  });

  it("refuses what is not a whole PNG: another file, a cut one, a bad CRC, no IEND", () => {
    const png = makePng(2, 2);
    assert.throws(() => withPhys(new TextEncoder().encode("%PDF-1.7"), 100), /not a PNG/);
    assert.throws(() => withPhys(png.subarray(0, png.length - 5), 100), /ends inside a chunk/);
    const bad = new Uint8Array(png);
    bad[8 + 8 + 2] ^= 0xff; // a byte of the IHDR data
    assert.throws(() => withPhys(bad, 100), /bad CRC in the IHDR chunk/);
    const noEnd = png.subarray(0, png.length - 12);
    assert.throws(() => withPhys(noEnd, 100), /IEND last/);
  });
});

describe("the plan of the PNG", () => {
  const fig = { width: 1100, height: 760 };

  it("the figure's own size on paper is 96 CSS pixels to the inch: 1100 px are 291.04 mm", () => {
    assert.equal(CSS_PX_PER_INCH, 96);
    assert.ok(Math.abs(pxToMm(1100) - 291.0417) < 1e-3);
    assert.ok(Math.abs(pxToMm(96) - 25.4) < 1e-12);
    assert.deepEqual(DPI_CHOICES, [150, 300, 600]);
    assert.deepEqual(SCALE_CHOICES, [1, 2, 3, 4]);
  });

  it("300 dpi at its own size: 1100 × 300 / 96 pixels wide, the same paper size, 300 dpi", () => {
    const p = planPng(fig, { kind: "dpi", dpi: 300 });
    assert.equal(p.width, 3438);
    assert.equal(p.height, 2375);
    assert.ok(Math.abs(p.dpi - 300) < 0.05, String(p.dpi));
    assert.ok(Math.abs(p.paperWidthMm - pxToMm(1100)) < 1e-9);
    assert.ok(Math.abs(p.paperHeightMm - pxToMm(760)) < 1e-9);
    assert.equal(p.limited, false);
    assert.equal(planPng(fig, { kind: "dpi", dpi: 150 }).width, 1719);
    assert.equal(planPng(fig, { kind: "dpi", dpi: 600 }).width, 6875);
  });

  it("on a journal column of 85 mm at 300 dpi: 1004 pixels wide, the height in proportion", () => {
    const p = planPng(fig, { kind: "dpi", dpi: 300 }, 85);
    assert.equal(p.width, 1004);
    assert.equal(p.height, Math.round(760 * (1004 / 1100)));
    assert.ok(Math.abs(p.paperHeightMm - (85 * 760) / 1100) < 1e-9);
    assert.ok(Math.abs(p.dpi - 300) < 0.2);
  });

  it("a factor multiplies the pixels and keeps the paper: 3× is 288 dpi at the figure's own size", () => {
    const p = planPng(fig, { kind: "scale", scale: 3 });
    assert.deepEqual([p.width, p.height, p.scale], [3300, 2280, 3]);
    assert.ok(Math.abs(p.dpi - 288) < 1e-9);
    const q = planPng(fig, { kind: "scale", scale: 2 }, 178);
    assert.equal(q.width, 2200);
    assert.ok(Math.abs(q.paperWidthMm - 178) < 1e-12);
    assert.ok(Math.abs(q.dpi - 2200 / (178 / 25.4)) < 1e-9);
  });

  it("past the browser's cap the scale is lowered: the longer side is MAX_SIDE_PX and the dpi is what is left", () => {
    const tall = { width: 1100, height: 1500 };
    const p = planPng(tall, { kind: "dpi", dpi: 600 });
    assert.equal(p.limited, true);
    assert.equal(p.height, MAX_SIDE_PX);
    assert.equal(p.width, Math.round(1100 * (MAX_SIDE_PX / 1500)));
    assert.ok(Math.abs(p.dpi - p.width / (pxToMm(1100) / 25.4)) < 1e-9);
    assert.ok(p.dpi < 600 && p.dpi > 500, String(p.dpi));
    assert.ok(Math.abs(p.paperWidthMm - pxToMm(1100)) < 1e-9, "the paper size stays; the resolution gives");
    assert.equal(planPng(tall, { kind: "dpi", dpi: 300 }).limited, false);
    assert.equal(planPng(tall, { kind: "scale", scale: 4 }).limited, false, "6000 px is under the cap");
  });

  it("a paper width that is not a positive number is the figure's own", () => {
    for (const bad of [Number.NaN, 0, -5, Number.POSITIVE_INFINITY]) assert.ok(Math.abs(planPng(fig, { kind: "dpi", dpi: 300 }, bad).paperWidthMm - pxToMm(1100)) < 1e-9, String(bad));
  });

  it("pixels per metre come from the PNG that came out: fewer pixels than asked say fewer dpi, not a smaller paper", () => {
    const paper = pxToMm(1100);
    assert.equal(ppmFor(3438, paper), Math.round(3438 / (paper / 1000)));
    // 3438 px (3437.5 rounded) over 11.458 in, the metre rounded to whole pixels: 300.05 dpi
    assert.ok(Math.abs(ppmToDpi(ppmFor(3438, paper)) - 300) < 0.1);
    assert.ok(Math.abs(ppmToDpi(ppmFor(1719, paper)) - 150) < 0.1);
    assert.equal(dpiToPpm(300), 11811);
    assert.equal(dpiToPpm(96), 3780);
    const png = withPhys(makePng(1004, 2), ppmFor(1004, 85));
    const phys = readPhys(png)!;
    assert.ok(Math.abs((pngSize(png).width / phys.x) * 1000 - 85) < 0.01, "Word puts it 85 mm wide");
  });

  it("the names' size in print: 14 px are 10.5 pt at the figure's own size, half that at half the width", () => {
    assert.ok(Math.abs(textPt(FIGURE_LABEL_PX, 1100, pxToMm(1100)) - 10.5) < 1e-9);
    assert.ok(Math.abs(textPt(FIGURE_LABEL_PX, 1100, pxToMm(1100) / 2) - 5.25) < 1e-9);
  });
});

describe("constants shared with other files", () => {
  it("the cap is rasterize.ts's, and the names' size is LayoutSvg's", () => {
    assert.match(readFileSync(join(COMPONENTS, "rasterize.ts"), "utf8"), new RegExp(`const MAX_SIDE_PX = ${MAX_SIDE_PX};`));
    assert.match(readFileSync(join(COMPONENTS, "LayoutSvg.tsx"), "utf8"), new RegExp(`const PAPER_LABEL_PX = ${FIGURE_LABEL_PX};`));
  });

  it("the dialog plans with the lib and the export writes the pHYs from the PNG that came out", () => {
    const dialog = readFileSync(join(COMPONENTS, "PngExportDialog.tsx"), "utf8");
    assert.match(dialog, /planPng\(figure, res, paperWidthMm\)/);
    const helper = readFileSync(join(COMPONENTS, "figurePng.ts"), "utf8");
    assert.match(helper, /const \{ width, height \} = pngSize\(drawn\);\s*const ppm = ppmFor\(width, plan\.paperWidthMm\);/);
    assert.match(helper, /withPhys\(drawn, ppm\)/);
  });
});
