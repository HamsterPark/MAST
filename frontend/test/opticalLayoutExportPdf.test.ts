// ════════════════════════════════════════════════════════════════════════════
// Tests for src/lib/opticalLayout/exportPdf.ts (the figure as a PDF, made by the server) and for the export
// menu's wiring (docs/v2/design/optical_layout_exports.md).
//
//     cd frontend && npm run test:unit
//
// What is pinned:
//   · the request: POST /api/optical-layouts/figure.pdf, same origin, a JSON body {svg, title} with its
//     Content-Type (the write guard wants both), and a PDF back;
//   · what each answer means to the operator: an older MAST or one without PyMuPDF (the SVG is offered), too
//     large (checked before sending, at the server's own limit), refused with the server's reason, failed,
//     no answer; an abort stays an abort;
//   · the menu: the four new items call the editor's handlers, the editor builds them from the figure, the
//     CSVs and the PDF are named like the other exports.
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it } from "node:test";

import { FIGURE_PDF_MAX_BYTES, FIGURE_PDF_PATH, FigurePdfError, figurePdfFileName, requestFigurePdf, type FetchLike } from "../src/lib/opticalLayout/exportPdf.ts";

const HERE = fileURLToPath(new URL(".", import.meta.url));
const ROOT = join(HERE, "..", "..");
const COMPONENTS = join(HERE, "..", "src", "components", "opticallayout");
const TOOLBAR = readFileSync(join(COMPONENTS, "Toolbar.tsx"), "utf8");
const EDITOR = readFileSync(join(COMPONENTS, "LayoutEditor.tsx"), "utf8");

const PDF_BYTES = new TextEncoder().encode("%PDF-1.7\n%%EOF\n");
const SVG = '<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><text>反射镜</text></svg>\n';

interface Seen {
  url: string;
  init: RequestInit | undefined;
}

function server(answer: () => Response | Promise<Response>): { fetch: FetchLike; seen: Seen[] } {
  const seen: Seen[] = [];
  return {
    seen,
    fetch: async (url, init) => {
      seen.push({ url, init });
      return answer();
    },
  };
}

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

async function refusal(p: Promise<unknown>): Promise<FigurePdfError> {
  try {
    await p;
  } catch (e) {
    assert.ok(e instanceof FigurePdfError, String(e));
    return e;
  }
  assert.fail("no refusal");
}

describe("asking the server for the PDF", () => {
  it("POSTs the figure as JSON to /api/optical-layouts/figure.pdf, same origin, and returns the PDF", async () => {
    const s = server(() => new Response(PDF_BYTES, { status: 200, headers: { "Content-Type": "application/pdf" } }));
    const blob = await requestFigurePdf({ svg: SVG, title: "SHG 光路" }, s.fetch);
    assert.deepEqual(new Uint8Array(await blob.arrayBuffer()), PDF_BYTES);
    assert.equal(s.seen.length, 1);
    const { url, init } = s.seen[0]!;
    assert.equal(url, FIGURE_PDF_PATH);
    assert.equal(FIGURE_PDF_PATH, "/api/optical-layouts/figure.pdf");
    assert.equal(init?.method, "POST");
    assert.equal(init?.credentials, "same-origin");
    assert.equal((init?.headers as Record<string, string>)["Content-Type"], "application/json");
    assert.deepEqual(JSON.parse(String(init?.body)), { svg: SVG, title: "SHG 光路" });
  });

  it("an older MAST (404, 405, or its page's HTML) or one without PyMuPDF (501): unavailable, and the SVG is offered", async () => {
    for (const r of [() => new Response("nope", { status: 404 }), () => new Response("", { status: 405 }), () => new Response("<!doctype html><html></html>", { status: 200, headers: { "Content-Type": "text/html" } })]) {
      const e = await refusal(requestFigurePdf({ svg: SVG, title: "" }, server(r).fetch));
      assert.equal(e.kind, "unavailable");
      assert.match(e.message, /SVG/);
    }
    const e = await refusal(requestFigurePdf({ svg: SVG, title: "" }, server(() => json(501, { detail: "这个 MAST 没有装 PyMuPDF" })).fetch));
    assert.equal(e.kind, "unavailable");
    assert.equal(e.status, 501);
    assert.match(e.message, /PyMuPDF/);
  });

  it("too large: refused before sending at the server's limit, and a 413 says the same", async () => {
    const s = server(() => new Response(PDF_BYTES, { status: 200, headers: { "Content-Type": "application/pdf" } }));
    // one byte past the limit, the JSON envelope ({"svg":"","title":""}) counted
    const envelope = JSON.stringify({ svg: "", title: "" }).length;
    const big = "x".repeat(FIGURE_PDF_MAX_BYTES - envelope + 1);
    const e = await refusal(requestFigurePdf({ svg: big, title: "" }, s.fetch));
    assert.equal(e.kind, "too_large");
    assert.equal(s.seen.length, 0, "nothing was sent");
    await requestFigurePdf({ svg: big.slice(1), title: "" }, s.fetch);
    assert.equal(s.seen.length, 1, "at the limit it is sent");
    // counted in UTF-8, as the server counts: a Chinese character is three bytes
    const cjk = "光".repeat(Math.floor((FIGURE_PDF_MAX_BYTES - envelope) / 3) + 1);
    assert.equal((await refusal(requestFigurePdf({ svg: cjk, title: "" }, s.fetch))).kind, "too_large");
    const e413 = await refusal(requestFigurePdf({ svg: SVG, title: "" }, server(() => json(413, { detail: "too large" })).fetch));
    assert.equal(e413.kind, "too_large");
  });

  it("a refusal carries the server's reason; a 5xx is a failure; no answer is a network error; an abort stays an abort", async () => {
    const e422 = await refusal(requestFigurePdf({ svg: SVG, title: "" }, server(() => json(422, { detail: "根元素不是 SVG 命名空间里的 svg" })).fetch));
    assert.equal(e422.kind, "refused");
    assert.match(e422.message, /根元素不是 SVG/);
    const eList = await refusal(requestFigurePdf({ svg: SVG, title: "" }, server(() => json(422, { detail: [{ msg: "Field required" }, { msg: "Extra inputs" }] })).fetch));
    assert.match(eList.message, /Field required; Extra inputs/);
    const e403 = await refusal(requestFigurePdf({ svg: SVG, title: "" }, server(() => json(403, { detail: "跨站写请求被拒" })).fetch));
    assert.equal(e403.kind, "refused");
    const e500 = await refusal(requestFigurePdf({ svg: SVG, title: "" }, server(() => json(500, { detail: "MuPDF said no" })).fetch));
    assert.equal(e500.kind, "failed");
    assert.match(e500.message, /MuPDF said no/);
    const down: FetchLike = async () => {
      throw new TypeError("Failed to fetch");
    };
    assert.equal((await refusal(requestFigurePdf({ svg: SVG, title: "" }, down))).kind, "network");
    const aborted: FetchLike = async () => {
      throw Object.assign(new Error("aborted"), { name: "AbortError" });
    };
    await assert.rejects(requestFigurePdf({ svg: SVG, title: "" }, aborted), (e: Error) => e.name === "AbortError");
  });

  it("the limit is the server's (MAX_FIGURE_REQUEST_BYTES of the route)", () => {
    const route = readFileSync(join(ROOT, "MASTv2", "mast", "api", "routes", "optical_layout_figure.py"), "utf8");
    const m = route.match(/^MAX_FIGURE_REQUEST_BYTES = ([\d_]+)$/m);
    assert.ok(m, "the route names its limit");
    assert.equal(Number(m[1]!.replace(/_/g, "")), FIGURE_PDF_MAX_BYTES);
    assert.match(route, /@router\.post\("\/optical-layouts\/figure\.pdf"/);
  });

  it("names the file like the other figure exports", () => {
    assert.equal(figurePdfFileName({ id: "shg-main", revision: 4 }), "shg-main-r4-figure.pdf");
    assert.equal(figurePdfFileName({ id: "shg-main", revision: 4 }, true), "shg-main-r4-unsaved-figure.pdf");
    assert.equal(figurePdfFileName({ id: "../x", revision: 1.7 }), "___x-r1-figure.pdf");
  });
});

describe("the export menu", () => {
  it("has the four new items, each calling its own handler", () => {
    const menu = TOOLBAR.slice(TOOLBAR.indexOf('<Menu label={tr("导出", "Export")}'), TOOLBAR.indexOf("</Menu>", TOOLBAR.indexOf('<Menu label={tr("导出", "Export")}')));
    assert.match(menu, /<MenuItem onClick=\{p\.onExportPaperPdf\}[^>]*>\s*\{tr\("论文风格 PDF", "Figure PDF"\)\}/);
    assert.match(menu, /<MenuItem onClick=\{p\.onExportPaperPngAt\}[^>]*>\s*\{tr\("论文风格 PNG，选分辨率…", "Figure PNG at a resolution…"\)\}/);
    assert.match(menu, /<MenuItem onClick=\{\(\) => p\.onExportTable\("bom"\)\}[^>]*>\s*\{tr\("物料清单 CSV", "Bill of materials \(CSV\)"\)\}/);
    assert.match(menu, /<MenuItem onClick=\{\(\) => p\.onExportTable\("beams"\)\}[^>]*>\s*\{tr\("光束表 CSV", "Beam table \(CSV\)"\)\}/);
  });

  it("the editor wires them: the PDF and the PNG from the figure the SVG export makes, the tables from what is shown", () => {
    assert.match(EDITOR, /onExportPaperPdf=\{\(\) => void exportPaperPdf\(\)\}/);
    assert.match(EDITOR, /onExportPaperPngAt=\{\(\) => setPngFigure\(paperFigure\(\)\)\}/);
    assert.match(EDITOR, /onExportTable=\{exportTable\}/);
    const pdf = EDITOR.slice(EDITOR.indexOf("const exportPaperPdf = async"), EDITOR.indexOf("const exportPngAt = async"));
    assert.match(pdf, /const fig = paperFigure\(\);/);
    assert.match(pdf, /requestFigurePdf\(\{ svg: fig\.text, title: shown\.title \|\| shown\.id \}\)/);
    assert.match(pdf, /e instanceof FigurePdfError/);
    const table = EDITOR.slice(EDITOR.indexOf("const exportTable = "), EDITOR.indexOf("const exportPng = async"));
    assert.match(table, /bomCsv\(shown\)/);
    assert.match(table, /beamTableCsv\(shown, trace\)/);
    assert.match(table, /type: "text\/csv;charset=utf-8"/);
    const png = EDITOR.slice(EDITOR.indexOf("const exportPngAt = async"), EDITOR.indexOf("const exportTable = "));
    assert.match(png, /figurePng\(fig\.text, fig, c\.res, c\.paperWidthMm\)/);
    assert.match(EDITOR, /<PngExportDialog key=\{pngFigure \? "png-open" : "png-closed"\} open=\{pngFigure !== null\} figure=\{pngFigure\}/);
    // its own keys: the save-as dialog beside it is keyed "open" / "closed", and two siblings with one key confuse React
    const keys = [...EDITOR.matchAll(/<(\w+Dialog)\s+key=\{[^}]*\? "([^"]+)" : "([^"]+)"\}/g)].flatMap((m) => [m[2]!, m[3]!]);
    assert.ok(keys.length >= 4, keys.join(", "));
    assert.equal(new Set(keys).size, keys.length, keys.join(", "));
  });

  it("an export of an old revision is named after that revision, not after the edits", () => {
    assert.match(EDITOR, /const exportName = viewingRev !== null \? \{ id: shown\.id, revision: viewingRev \} : present;/);
    assert.match(EDITOR, /const exportUnsaved = viewingRev === null && dirty;/);
  });
});
