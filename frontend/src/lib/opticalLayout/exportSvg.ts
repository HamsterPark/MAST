// Helpers for exporting the canvas as an SVG file (section 8 of
// docs/v2/design/optical_layout.md: "SVG: the canvas as it is").
//
// The canvas is painted with CSS variables of the design system (`var(--mast-text)`),
// which mean nothing once the file leaves the page. The export renders the same React
// component to a string and these helpers make it standalone: variables replaced by the
// values the page has right now, an XML header, and a file name that says which revision
// it is. Pure, so the string handling is tested without a browser
// (test/opticalLayoutFrame.test.ts).

import type { OpticalLayout } from "./types.ts";

const VAR_RE = /var\(\s*(--[A-Za-z0-9_-]+)\s*(?:,\s*([^()]*?))?\s*\)/g;

/**
 * Replaces CSS variable references (a custom property name in a var() call, with or
 * without a fallback) by what `resolve` says. A variable that
 * cannot be resolved takes its fallback, else a neutral grey, so the file never keeps a
 * reference it cannot satisfy.
 */
export function resolveCssVars(svg: string, resolve: (name: string) => string | null | undefined): string {
  return svg.replace(VAR_RE, (_all, name: string, fallback: string | undefined) => {
    const v = resolve(name)?.trim();
    if (v) return v;
    return fallback?.trim() || "#888888";
  });
}

export function xmlEscape(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

/** Wraps the markup of a rendered `<svg>` into a file: XML declaration and the SVG namespace. */
export function standaloneSvg(svgMarkup: string): string {
  const withNs = /<svg[^>]*\sxmlns=/.test(svgMarkup) ? svgMarkup : svgMarkup.replace(/^<svg/, '<svg xmlns="http://www.w3.org/2000/svg"');
  return `<?xml version="1.0" encoding="UTF-8"?>\n${withNs}\n`;
}

/** `<id>-r<revision>.<ext>`, with nothing in it a file system dislikes. */
export function exportFileName(layout: Pick<OpticalLayout, "id" | "revision">, ext: "svg" | "png", unsaved = false): string {
  const id = layout.id.replace(/[^A-Za-z0-9_-]/g, "_") || "layout";
  return `${id}-r${Math.max(0, Math.floor(layout.revision))}${unsaved ? "-unsaved" : ""}.${ext}`;
}

/** The pixel size of the exported picture: `mmPerPixel` millimetres per pixel over the given extent. */
export function exportSize(widthMm: number, heightMm: number, pxPerMm: number): { width: number; height: number } {
  return { width: Math.max(1, Math.ceil(widthMm * pxPerMm)), height: Math.max(1, Math.ceil(heightMm * pxPerMm)) };
}
