// The figure as a PDF (docs/v2/design/optical_layout_exports.md): the editor sends the figure's SVG (the same file
// the "Figure SVG" export saves) to POST /api/optical-layouts/figure.pdf, and the server turns it into a vector PDF
// with PyMuPDF (MASTv2/mast/optical_layout/figure_pdf.py): Chinese labels in an embedded font subset, the text still
// text, the page the size the SVG has on paper (96 CSS pixels to the inch).
//
// Why on the server: MuPDF draws the figure SVG the way it was built to be drawn (dash.ts, the two-layer halo of
// LayoutSvg), it carries a CJK font, and the result is a plain download. The browser's print dialog is not a
// download and depends on the fonts of the machine; a PDF library in the page would need a CJK font of several MB.
//
// This module only talks to that route: the request, and what each refusal means to the operator. It takes the fetch
// function as an argument and imports neither React nor the `@/` alias, so the tests drive it with a fake server
// (test/opticalLayoutExportPdf.test.ts).

import { tr } from "../i18n.ts";
import type { OpticalLayout } from "./types.ts";

export const FIGURE_PDF_PATH = "/api/optical-layouts/figure.pdf";

/** The server refuses a request body above this many bytes (MAX_FIGURE_REQUEST_BYTES of the route); checked here first. */
export const FIGURE_PDF_MAX_BYTES = 16_000_000;

export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

/** Why there is no PDF. `kind` decides what the editor says; `message` is that, in the language in use. */
export class FigurePdfError extends Error {
  kind: "unavailable" | "too_large" | "refused" | "failed" | "network";
  status: number;
  constructor(kind: FigurePdfError["kind"], status: number, message: string) {
    super(message);
    this.name = "FigurePdfError";
    this.kind = kind;
    this.status = status;
  }
}

/** The body of the request: the figure (a standalone SVG file) and the title the PDF carries in its properties. */
export interface FigurePdfRequest {
  svg: string;
  title: string;
}

/** The `detail` of an error reply, as text (FastAPI's 422 has a list). */
function detailOf(data: unknown): string {
  const d = (data as { detail?: unknown } | null)?.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) return d.map((x) => (typeof x === "object" && x !== null && "msg" in x ? String((x as { msg: unknown }).msg) : String(x))).join("; ");
  return "";
}

/**
 * The PDF of a figure, as a Blob. Refuses before sending a body the server would refuse for its size. Throws
 * FigurePdfError:
 *   * "unavailable": a MAST without the route (404 / 405, or the page's own HTML instead of a PDF) or without PyMuPDF
 *     (501): the editor suggests the SVG;
 *   * "too_large": 413, or too large to send;
 *   * "refused": another 4xx (a figure the server does not take as an SVG, a cross-site refusal), with its detail;
 *   * "failed": 5xx; "network": nothing answered.
 */
export async function requestFigurePdf(body: FigurePdfRequest, fetchImpl?: FetchLike, signal?: AbortSignal): Promise<Blob> {
  const doFetch: FetchLike = fetchImpl ?? ((input, init) => fetch(input, init));
  const text = JSON.stringify(body);
  // the UTF-8 length of the body, as the server counts it
  const bytes = new TextEncoder().encode(text).length;
  if (bytes > FIGURE_PDF_MAX_BYTES) {
    throw new FigurePdfError(
      "too_large",
      0,
      tr(
        `这张图的 SVG 有 ${(bytes / 1e6).toFixed(1)} MB，超过了 PDF 转换的上限 ${FIGURE_PDF_MAX_BYTES / 1e6} MB。请用论文风格 SVG。`,
        `The figure's SVG is ${(bytes / 1e6).toFixed(1)} MB, over the ${FIGURE_PDF_MAX_BYTES / 1e6} MB the PDF conversion takes. Use the figure SVG.`,
      ),
    );
  }
  let res: Response;
  try {
    res = await doFetch(FIGURE_PDF_PATH, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/pdf" },
      body: text,
      credentials: "same-origin",
      signal,
    });
  } catch (e) {
    if ((e as { name?: string } | null)?.name === "AbortError") throw e;
    throw new FigurePdfError("network", 0, tr(`没有连上 MAST 服务：${String((e as Error)?.message ?? e)}`, `Could not reach the MAST service: ${String((e as Error)?.message ?? e)}`));
  }
  const ctype = (res.headers.get("content-type") ?? "").toLowerCase();
  if (res.ok && ctype.includes("application/pdf")) return res.blob();
  let detail = "";
  try {
    detail = detailOf(JSON.parse(await res.text()));
  } catch {
    /* not JSON: the status says enough */
  }
  if (res.ok || res.status === 404 || res.status === 405 || res.status === 501) {
    // a build without the route answers 404 / 405, or 200 with the page's own HTML; one without PyMuPDF 501
    throw new FigurePdfError(
      "unavailable",
      res.status,
      res.status === 501 && detail
        ? tr(`导不了 PDF：${detail}。请先用论文风格 SVG。`, `No PDF: ${detail}. Use the figure SVG for now.`)
        : tr("这个 MAST 服务还不能导出 PDF（版本较旧）。请先用论文风格 SVG。", "This MAST service cannot make a PDF yet (an older version). Use the figure SVG for now."),
    );
  }
  if (res.status === 413) {
    throw new FigurePdfError("too_large", 413, tr("这张图太大，服务器不收（超过 PDF 转换的上限）。请用论文风格 SVG。", "The figure is too large for the server's PDF conversion. Use the figure SVG."));
  }
  if (res.status >= 400 && res.status < 500) {
    throw new FigurePdfError("refused", res.status, tr(`服务器拒绝了这次 PDF 导出（HTTP ${res.status}）：${detail || res.statusText}`, `The server refused the PDF export (HTTP ${res.status}): ${detail || res.statusText}`));
  }
  throw new FigurePdfError("failed", res.status, tr(`PDF 转换失败（HTTP ${res.status}）：${detail || res.statusText}`, `The PDF conversion failed (HTTP ${res.status}): ${detail || res.statusText}`));
}

/** `<id>-r<revision>[-unsaved]-figure.pdf`, named like the other figure exports (exportStyle.ts paperFileName). */
export function figurePdfFileName(layout: Pick<OpticalLayout, "id" | "revision">, unsaved = false): string {
  const id = layout.id.replace(/[^A-Za-z0-9_-]/g, "_") || "layout";
  return `${id}-r${Math.max(0, Math.floor(layout.revision))}${unsaved ? "-unsaved" : ""}-figure.pdf`;
}
