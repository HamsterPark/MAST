// File names of the 3D view's exports, and whether the browser can draw it at all. Pure.

import type { OpticalLayout } from "../types.ts";

/**
 * `<id>-r<revision>[-unsaved]-3d[<suffix>][@2x].<ext>`, with nothing in it a file system dislikes (exportSvg.ts
 * exportFileName's rule); the suffix names a variant ("-instanced").
 */
export function export3dFileName(layout: Pick<OpticalLayout, "id" | "revision">, ext: "png" | "glb", unsaved = false, scale = 1, suffix = ""): string {
  const id = layout.id.replace(/[^A-Za-z0-9_-]/g, "_") || "layout";
  const rev = Math.max(0, Math.floor(layout.revision));
  const tail = suffix.replace(/[^A-Za-z0-9_-]/g, "");
  return `${id}-r${rev}${unsaved ? "-unsaved" : ""}-3d${tail}${scale > 1 ? `@${scale}x` : ""}.${ext}`;
}

/** What a canvas offers, for the check (a real canvas in the browser, a stand-in in the tests). */
export interface ContextSource {
  getContext(kind: "webgl2"): unknown;
}

interface ProbeGl {
  getExtension?: (n: string) => { loseContext?: () => void; UNMASKED_RENDERER_WEBGL?: number } | null;
  getParameter?: (p: number) => unknown;
  RENDERER?: number;
}

/**
 * Whether the browser can draw the 3D view, and with what. The view needs WebGL 2 (three.js has drawn with WebGL 2
 * only since r163); a browser without it (an old one, a remote desktop without a graphics driver, WebGL switched
 * off) gets a message and keeps the 2D view. `renderer` names what draws ("" when the browser does not say).
 */
export function probeWebgl(make: () => ContextSource | null): { ok: boolean; renderer: string } {
  try {
    const c = make();
    if (!c) return { ok: false, renderer: "" };
    const gl = c.getContext("webgl2") as ProbeGl | null;
    if (!gl) return { ok: false, renderer: "" };
    let renderer = "";
    try {
      const info = gl.getExtension?.("WEBGL_debug_renderer_info");
      const v = info?.UNMASKED_RENDERER_WEBGL !== undefined ? gl.getParameter?.(info.UNMASKED_RENDERER_WEBGL) : gl.RENDERER !== undefined ? gl.getParameter?.(gl.RENDERER) : "";
      renderer = typeof v === "string" ? v : "";
    } catch {
      renderer = "";
    }
    // give the test context back at once: browsers allow only a few at a time
    gl.getExtension?.("WEBGL_lose_context")?.loseContext?.();
    return { ok: true, renderer };
  } catch {
    return { ok: false, renderer: "" };
  }
}

export function canDraw3d(make: () => ContextSource | null): boolean {
  return probeWebgl(make).ok;
}

/**
 * A renderer that draws on the processor (SwiftShader, llvmpipe, Windows' basic display driver): the view then
 * draws without multisampling and at one device pixel per CSS pixel, which costs it a quarter or less of the work.
 */
export function isSoftwareRenderer(name: string): boolean {
  return /swiftshader|llvmpipe|softpipe|software rasterizer|basic render/i.test(name);
}
