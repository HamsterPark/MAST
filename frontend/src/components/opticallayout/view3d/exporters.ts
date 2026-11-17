// The 3D view's exports:
//
//   PNG  the view as it is on the screen, rendered again at 1×, 2× or 4× its pixels (as far as the graphics card
//        allows), with the labels drawn into the picture (on the screen they are HTML over the canvas).
//   GLB  the scene as a glTF 2.0 binary for Blender, CAD and other 3D programs: every component a node named after
//        its label and id (its parts children of it), the beams one mesh per traced tree, the table with its
//        holes. Units are metres and +y is up, as glTF has it (the view's millimetres, z up, are turned on the
//        way out), so Blender imports the table at its real size, lying flat.

import { BoxGeometry, BufferGeometry, CanvasTexture, Color, DoubleSide, Float32BufferAttribute, Group, InstancedMesh, Matrix4, Mesh, MeshStandardMaterial, PlaneGeometry, RepeatWrapping, type Material, type WebGLRenderer } from "three";
import { GLTFExporter } from "three/examples/jsm/exporters/GLTFExporter.js";
import { mergeGeometries } from "three/examples/jsm/utils/BufferGeometryUtils.js";
import type { OpticalLayout } from "../../../lib/opticalLayout/types.ts";
import type { DetailedTrace } from "../../../lib/opticalLayout/trace.ts";
import type { ThemeName } from "../../../lib/opticalLayout/colors.ts";
import type { Layers } from "../../../lib/opticalLayout/layers.ts";
import { PALETTES, tintColor } from "../../../lib/opticalLayout/scene3d/palette.ts";
import { sceneComponents } from "../../../lib/opticalLayout/scene3d/scene.ts";
import { partMatrix, type Part } from "../../../lib/opticalLayout/scene3d/parts.ts";
import { beamEnvelopes, beamTubes, transverseFrame } from "../../../lib/opticalLayout/scene3d/beams3d.ts";
import { multiply, type Vec3 } from "../../../lib/opticalLayout/scene3d/mat4.ts";
import { holePatch, HOLE_SHARE, TABLE_THICKNESS_MM } from "../../../lib/opticalLayout/scene3d/table3d.ts";
import { shapeGeometry, tubeGeometry } from "./geometries.ts";
import { tubeMatrix } from "./LayoutScene3D.ts";

// ── PNG ───────────────────────────────────────────────────────────────────────

export const PNG_SCALES = [1, 2, 4] as const;

/** A label as the PNG draws it: its text, where its anchor is on the screen (px), and its look on the page. */
export interface LabelShot {
  text: string;
  x: number;
  y: number;
  /** the share of its box left of / above the anchor (CSS2DObject.center) */
  cx: number;
  cy: number;
  font: string;
  color: string;
  background: string;
  w: number;
  h: number;
}

/** The largest scale ≤ `want` at which a view of `w` × `h` px still fits the graphics card's limit `maxPx`. */
export function fitScale(want: number, w: number, h: number, maxPx: number): number {
  const fit = Math.floor(maxPx / Math.max(1, w, h));
  return Math.max(1, Math.min(want, fit));
}

/**
 * Renders the view again at `scale` (on the same renderer, put back afterwards) and draws the labels into it.
 * `draw` renders the scene once. The picture is copied out right after the render, in the same task, so the
 * canvas does not need to keep its drawing buffer.
 */
export function snapshotPng(renderer: WebGLRenderer, draw: () => void, w: number, h: number, scale: number, labels: readonly LabelShot[]): Promise<{ blob: Blob; scale: number }> {
  const gl = renderer.getContext();
  const maxPx = Math.min(renderer.capabilities.maxTextureSize, gl.getParameter(gl.MAX_RENDERBUFFER_SIZE) as number, ...((gl.getParameter(gl.MAX_VIEWPORT_DIMS) as Int32Array | number[]) ?? [16384]));
  const s = fitScale(scale, w, h, maxPx);
  const before = renderer.getPixelRatio();
  const out = document.createElement("canvas");
  try {
    renderer.setPixelRatio(s);
    renderer.setSize(w, h, false);
    draw();
    const src = renderer.domElement;
    out.width = src.width;
    out.height = src.height;
    const ctx = out.getContext("2d");
    if (!ctx) throw new Error("no 2D canvas");
    ctx.drawImage(src, 0, 0);
    const k = out.width / w;
    for (const l of labels) drawLabel(ctx, l, k);
  } finally {
    renderer.setPixelRatio(before);
    renderer.setSize(w, h, false);
    draw();
  }
  return new Promise((resolve, reject) => out.toBlob((b) => (b ? resolve({ blob: b, scale: s }) : reject(new Error("the browser made no PNG"))), "image/png"));
}

function drawLabel(ctx: CanvasRenderingContext2D, l: LabelShot, k: number): void {
  if (!l.text) return;
  const w = l.w * k;
  const h = l.h * k;
  const x = l.x * k - l.cx * w;
  const y = l.y * k - l.cy * h;
  ctx.save();
  if (l.background && l.background !== "rgba(0, 0, 0, 0)" && l.background !== "transparent") {
    ctx.fillStyle = l.background;
    ctx.beginPath();
    ctx.roundRect(x, y, w, h, 3 * k);
    ctx.fill();
  }
  ctx.font = scaleFont(l.font, k);
  ctx.fillStyle = l.color;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(l.text, x + w / 2, y + h / 2);
  ctx.restore();
}

/** A computed CSS font ("italic 600 11px / 16px Segoe UI, ...") with its pixel size times k, line height dropped. */
export function scaleFont(font: string, k: number): string {
  return font.replace(/(\d+(?:\.\d+)?)px(\s*\/\s*[^\s]+)?/, (_m, px: string) => `${(parseFloat(px) * k).toFixed(2)}px`);
}

// ── GLB ───────────────────────────────────────────────────────────────────────

/** A beam without a Gaussian beam size in the GLB: a tube this many mm per pixel of its 2D width (Ø0.6–1.9 mm). */
export const GLB_BEAM_MM_PER_PX = 0.6;

export interface GlbOptions {
  /**
   * Keep the instances (EXT_mesh_gpu_instancing): one instanced mesh per shape, material and colour, a smaller file,
   * but not every importer reads the extension and the components are no longer nodes of their own. Off by default:
   * every part a plain mesh under its component's node, which any glTF reader opens.
   */
  instancing?: boolean;
}

const PBR: Partial<Record<Part["tint"], { metalness: number; roughness: number }>> = {
  post: { metalness: 0.8, roughness: 0.35 },
  metal: { metalness: 0.7, roughness: 0.4 },
  mirror: { metalness: 0.95, roughness: 0.08 },
  coating: { metalness: 0.9, roughness: 0.12 },
  grating: { metalness: 0.8, roughness: 0.25 },
};

const SURFACE_SIDES = 16;

/**
 * A beam segment at its real size: the surface of revolution of its 1/e² radius at the given samples (distance from
 * `a` along `dir`, radius), position and normal only, indexed, so it merges with the tubes of the same tree.
 */
export function beamSurface(a: Vec3, dir: Vec3, samples: ReadonlyArray<[number, number]>): BufferGeometry {
  const { eh, ev } = transverseFrame(dir);
  const pos: number[] = [];
  const idx: number[] = [];
  for (const [s, r] of samples) {
    for (let k = 0; k < SURFACE_SIDES; k++) {
      const t = (2 * Math.PI * k) / SURFACE_SIDES;
      const c = Math.cos(t) * r;
      const sn = Math.sin(t) * r;
      pos.push(a[0] + dir[0] * s + eh[0] * c + ev[0] * sn, a[1] + dir[1] * s + eh[1] * c + ev[1] * sn, a[2] + dir[2] * s + eh[2] * c + ev[2] * sn);
    }
  }
  for (let i = 0; i + 1 < samples.length; i++) {
    for (let k = 0; k < SURFACE_SIDES; k++) {
      const p = i * SURFACE_SIDES + k;
      const q = i * SURFACE_SIDES + ((k + 1) % SURFACE_SIDES);
      idx.push(p, q, p + SURFACE_SIDES, q, q + SURFACE_SIDES, p + SURFACE_SIDES);
    }
  }
  const g = new BufferGeometry();
  g.setAttribute("position", new Float32BufferAttribute(pos, 3));
  g.setIndex(idx);
  g.computeVertexNormals();
  // a ring of radius 0 (a beam pinched to a point) has only degenerate triangles round it: no normal, and the
  // exporter warns about a normal that is not unit length. The beam's own direction is the right one for such a tip.
  const nrm = g.getAttribute("normal");
  for (let i = 0; i < nrm.count; i++) {
    if (Math.hypot(nrm.getX(i), nrm.getY(i), nrm.getZ(i)) < 1e-9) nrm.setXYZ(i, dir[0], dir[1], dir[2]);
  }
  return g;
}

/**
 * The scene for the GLB, made from the per-component description (scene3d: parts, beams), never from what the live
 * view draws (its merged meshes and screen-sized tubes mean nothing outside it):
 *   * the table with its holes;
 *   * every component a node named "<label> (<id>)" with its parts as children (or, with `instancing`, the parts
 *     as instanced meshes);
 *   * the beams one mesh per traced tree, at their real size where the trace has a Gaussian beam (the 1/e² radius
 *     along each segment) and as tubes as heavy as their 2D lines where it has not; light that is not there (p below
 *     1e-6) one thin grey mesh.
 * Plain meshes and standard materials: no shader of the live view goes into the file.
 */
export function exportScene(layout: OpticalLayout, trace: DetailedTrace, display: Layers, theme: ThemeName, opts: GlbOptions = {}): Group {
  const pal = PALETTES[theme];
  const root = new Group();
  root.name = layout.title || layout.id;
  root.userData = { mast_layout: layout.id, revision: layout.revision, units: "mm (scaled to m by this node)" };
  // the view's millimetres, z up → glTF's metres, y up
  root.scale.setScalar(0.001);
  root.rotation.x = -Math.PI / 2;

  const mats = new Map<string, Material>();
  const material = (color: string, glass: boolean, pbr?: { metalness: number; roughness: number }, emissive = false): Material => {
    const key = `${color}|${glass}|${pbr?.metalness ?? ""}|${emissive}`;
    let m = mats.get(key);
    if (!m) {
      m = new MeshStandardMaterial({
        color: new Color(color),
        metalness: pbr?.metalness ?? 0.1,
        roughness: pbr?.roughness ?? 0.6,
        ...(glass ? { transparent: true, opacity: pal.glassOpacity + 0.1, side: DoubleSide } : {}),
        ...(emissive ? { emissive: new Color(color), emissiveIntensity: 0.8 } : {}),
      });
      mats.set(key, m);
    }
    return m;
  };

  // the table
  const t = layout.table;
  const table = new Group();
  table.name = "table";
  const slab = new Mesh(new BoxGeometry(t.width_mm, t.height_mm, TABLE_THICKNESS_MM), material(pal.tableTop, false));
  slab.position.set(t.width_mm / 2, t.height_mm / 2, -TABLE_THICKNESS_MM / 2);
  slab.name = "table";
  table.add(slab);
  const patch = holePatch(t);
  if (patch && display.holes && typeof document !== "undefined") {
    const g = new PlaneGeometry(patch.x1 - patch.x0, patch.y1 - patch.y0);
    const uv = g.getAttribute("uv");
    for (let i = 0; i < uv.count; i++) uv.setXY(i, patch.u0 + (patch.u1 - patch.u0) * uv.getX(i), patch.v0 + (patch.v1 - patch.v0) * uv.getY(i));
    const holes = new Mesh(g, new MeshStandardMaterial({ map: holeTile(pal.tableTop, pal.holes), roughness: 0.7, metalness: 0.1 }));
    holes.position.set((patch.x0 + patch.x1) / 2, (patch.y0 + patch.y1) / 2, 0.05);
    holes.name = "holes";
    table.add(holes);
  }
  root.add(table);

  // the components
  const comps = new Group();
  comps.name = "components";
  const nodes = sceneComponents(layout);
  if (!opts.instancing) {
    // each a node with its parts
    for (const n of nodes) {
      const g = new Group();
      g.name = n.component.label && n.component.label !== n.id ? `${n.component.label} (${n.id})` : n.id;
      g.userData = { id: n.id, type: n.component.type, label: n.component.label };
      g.matrixAutoUpdate = false;
      g.matrix.fromArray(n.frame);
      for (const p of n.parts) {
        const m = new Mesh(shapeGeometry(p.shape), material(tintColor(p, theme), p.mat === "glass", PBR[p.tint]));
        m.name = p.name;
        m.matrixAutoUpdate = false;
        m.matrix.fromArray(partMatrix(p));
        g.add(m);
      }
      comps.add(g);
    }
  } else {
    // one instanced mesh per shape, material and colour (the colour in the material, which every reader keeps)
    const groups = new Map<string, { shape: Part["shape"]; glass: boolean; color: string; pbr?: { metalness: number; roughness: number }; matrices: number[][] }>();
    for (const n of nodes) {
      n.parts.forEach((p) => {
        const color = tintColor(p, theme);
        const key = `${p.shape}|${p.mat}|${color}|${PBR[p.tint]?.metalness ?? ""}`;
        let gr = groups.get(key);
        if (!gr) {
          gr = { shape: p.shape, glass: p.mat === "glass", color, ...(PBR[p.tint] ? { pbr: PBR[p.tint] } : {}), matrices: [] };
          groups.set(key, gr);
        }
        gr.matrices.push(multiply(n.frame, partMatrix(p)));
      });
    }
    const m4 = new Matrix4();
    for (const [key, gr] of groups) {
      const mesh = new InstancedMesh(shapeGeometry(gr.shape), material(gr.color, gr.glass, gr.pbr), gr.matrices.length);
      gr.matrices.forEach((m, i) => mesh.setMatrixAt(i, m4.fromArray(m)));
      mesh.name = `parts ${key.split("|").slice(0, 3).join(" ")}`;
      comps.add(mesh);
    }
  }
  root.add(comps);

  // the beams
  if (display.beams) root.add(exportBeams(trace, t, theme, pal, material, opts));
  return root;
}

function exportBeams(
  trace: DetailedTrace,
  t: OpticalLayout["table"],
  theme: ThemeName,
  pal: (typeof PALETTES)[ThemeName],
  material: (color: string, glass: boolean, pbr?: { metalness: number; roughness: number }, emissive?: boolean) => Material,
  opts: GlbOptions,
): Group {
  const beams = new Group();
  beams.name = "beams";
  const unit = tubeGeometry();
  unit.deleteAttribute("uv");
  // the 1/e² radius along every segment that has one (beamEnvelopes: the same samples as the 2D band, lit segments only)
  const sized = new Map(beamEnvelopes(trace, t, theme).map((e) => [e.key, e]));
  const surfaces = new Map<number, BufferGeometry[]>();
  const tubes = new Map<number, Matrix4[]>();
  const colorOf = new Map<number, string>();
  const dark: BufferGeometry[] = [];
  for (const tb of beamTubes(trace, t, theme)) {
    const env = tb.joint ? undefined : sized.get(tb.key);
    if (tb.dark) {
      const m = tubeMatrix(tb.a, tb.b, new Matrix4()).multiply(new Matrix4().makeScale(0.3, 0.3, 1));
      dark.push(unit.clone().applyMatrix4(m));
      continue;
    }
    if (!colorOf.has(tb.ref.tree) && tb.color.startsWith("#")) colorOf.set(tb.ref.tree, tb.color);
    if (env) {
      const list = surfaces.get(tb.ref.tree) ?? [];
      list.push(beamSurface(env.a, env.dir, env.samples));
      surfaces.set(tb.ref.tree, list);
      continue;
    }
    // no beam size: a tube as heavy as the 2D line, its radius in the matrix
    const r = (tb.widthPx / 2) * GLB_BEAM_MM_PER_PX;
    const list = tubes.get(tb.ref.tree) ?? [];
    list.push(tubeMatrix(tb.a, tb.b, new Matrix4()).multiply(new Matrix4().makeScale(r, r, 1)));
    tubes.set(tb.ref.tree, list);
  }
  const trees = new Set([...surfaces.keys(), ...tubes.keys()]);
  for (const ti of trees) {
    const tree = trace[ti];
    const color = colorOf.get(ti) ?? pal.tints.glass;
    const name = tree ? `beam ${tree.source}${tree.wavelength_nm !== null ? ` ${tree.wavelength_nm} nm` : ""}` : `beam ${ti}`;
    const mat = material(color, false, undefined, true);
    const parts: BufferGeometry[] = [...(surfaces.get(ti) ?? [])];
    const ms = tubes.get(ti) ?? [];
    if (opts.instancing && ms.length > 0) {
      const inst = new InstancedMesh(unit, mat, ms.length);
      ms.forEach((m, i) => inst.setMatrixAt(i, m));
      inst.name = `${name} (tubes)`;
      beams.add(inst);
    } else for (const m of ms) parts.push(unit.clone().applyMatrix4(m));
    if (parts.length === 0) continue;
    const merged = mergeGeometries(parts, false);
    for (const g of parts) g.dispose();
    if (!merged) continue;
    // a tube scaled thin and a beam surface leave some normals a hair off unit length, which the exporter warns about
    merged.normalizeNormals();
    const mesh = new Mesh(merged, mat);
    mesh.name = name;
    beams.add(mesh);
  }
  if (dark.length > 0) {
    const merged = mergeGeometries(dark, false);
    for (const g of dark) g.dispose();
    if (merged) {
      merged.normalizeNormals();
      const mesh = new Mesh(merged, material(pal.hover, false));
      mesh.name = "beams without light";
      beams.add(mesh);
    }
  }
  if (!opts.instancing) unit.dispose();
  return beams;
}

/** The hole tile for the GLB: the table colour with a dark hole in the middle (opaque, so no viewer has to blend it). */
function holeTile(top: string, hole: string): CanvasTexture {
  const n = 64;
  const cv = document.createElement("canvas");
  cv.width = n;
  cv.height = n;
  const ctx = cv.getContext("2d")!;
  ctx.fillStyle = top;
  ctx.fillRect(0, 0, n, n);
  ctx.fillStyle = hole;
  ctx.beginPath();
  ctx.arc(n / 2, n / 2, (HOLE_SHARE * n) / 2, 0, Math.PI * 2);
  ctx.fill();
  const tex = new CanvasTexture(cv);
  tex.wrapS = RepeatWrapping;
  tex.wrapT = RepeatWrapping;
  return tex;
}

/** The GLB of a scene. */
export async function glbOf(root: Group): Promise<ArrayBuffer> {
  root.updateMatrixWorld(true);
  const out = await new GLTFExporter().parseAsync(root, { binary: true, onlyVisible: true });
  if (!(out instanceof ArrayBuffer)) throw new Error("the exporter did not make a binary file");
  return out;
}

/** Frees what exportScene made (the shared shape geometries stay). */
export function disposeExportScene(root: Group): void {
  root.traverse((o) => {
    const m = o as Mesh;
    if (!m.isMesh) return;
    if (m.name === "table" || m.name === "holes" || m.parent?.name === "beams") m.geometry.dispose();
    const mat = m.material as Material | Material[];
    for (const x of Array.isArray(mat) ? mat : [mat]) {
      const map = (x as MeshStandardMaterial).map;
      map?.dispose();
      x.dispose();
    }
  });
}
