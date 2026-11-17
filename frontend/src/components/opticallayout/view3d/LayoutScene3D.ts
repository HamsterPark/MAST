// The three.js scene of the 3D view: the table, the components (one instanced mesh per shape and material),
// the beams (one instanced mesh of tubes), the envelope, the polarisation marks, the outlines of the selection,
// and the labels (CSS2D objects in a scene of their own). It is told what to show (update) and works out what
// changed by identity: the editor's edits keep the objects they do not touch (edit.ts), so a drag that moves one
// component rebuilds the instances and the beams, never the table or the labels' elements.
//
// Everything it draws comes from lib/opticalLayout/scene3d (where things are, what they look like); this file
// only turns that data into three.js objects.

import {
  BoxGeometry,
  BufferGeometry,
  CanvasTexture,
  Color,
  DirectionalLight,
  DynamicDrawUsage,
  Float32BufferAttribute,
  Group,
  HemisphereLight,
  InstancedMesh,
  LineSegments,
  Matrix4,
  Mesh,
  MeshBasicMaterial,
  MeshLambertMaterial,
  PlaneGeometry,
  Quaternion,
  RepeatWrapping,
  Scene,
  Vector3,
  type Camera,
  type Material,
  type Raycaster,
} from "three";
import { CSS2DObject } from "three/examples/jsm/renderers/CSS2DRenderer.js";
import type { OpticalLayout, TableSpec } from "../../../lib/opticalLayout/types.ts";
import type { DetailedTrace } from "../../../lib/opticalLayout/trace.ts";
import type { ThemeName } from "../../../lib/opticalLayout/colors.ts";
import type { SegRef } from "../../../lib/opticalLayout/beamDraw.ts";
import type { Layers } from "../../../lib/opticalLayout/layers.ts";
import { branchPath } from "../../../lib/opticalLayout/beam.ts";
import { tr } from "../../../lib/i18n.ts";
import { PALETTES, mixHex, tintColor, type Palette3D } from "../../../lib/opticalLayout/scene3d/palette.ts";
import { instanceBuckets, labelAnchor, outlineEdges, sceneComponents, type BucketKey, type ComponentNode } from "../../../lib/opticalLayout/scene3d/scene.ts";
import { beamEnvelopes, beamTubes, passMarks, polarizationMarks, powerTags3d, transverseFrame, type EnvelopeSpec, type TubeSpec } from "../../../lib/opticalLayout/scene3d/beams3d.ts";
import { gridLines, holePatch, sceneBox, TABLE_THICKNESS_MM, HOLE_SHARE } from "../../../lib/opticalLayout/scene3d/table3d.ts";
import { declutter, type LabelBox } from "../../../lib/opticalLayout/scene3d/labels3d.ts";
import type { Box3, Vec3 } from "../../../lib/opticalLayout/scene3d/mat4.ts";
import type { Shape3, Mat3 } from "../../../lib/opticalLayout/scene3d/parts.ts";
import { shapeGeometry, tubeGeometry, type Detail } from "./geometries.ts";
import { MergedBucket } from "./merged.ts";
import { beamMaterial, beamUniforms, ensureBeamAttribute, envelopeMaterial, glassMaterial, linesMaterial, solidMaterial, type BeamUniforms } from "./materials.ts";

export interface SceneInput {
  layout: OpticalLayout;
  trace: DetailedTrace;
  display: Layers;
  theme: ThemeName;
  selected: ReadonlySet<string>;
  hover: string | null;
  beamPick: SegRef | null;
  commentCounts: ReadonlyMap<string, number> | undefined;
  /** the live editor (locks shown); false in an old revision */
  interactive: boolean;
}

/** Colours that come from the page's design tokens when there is a page, else from the palette. */
export interface ThemeColors {
  pal: Palette3D;
  text: string;
  faint: string;
  selection: string;
  background: string;
  tableTop: string;
}

function cssVar(name: string): string | null {
  if (typeof document === "undefined") return null;
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return /^#[0-9a-f]{6}$/i.test(v) ? v : null;
}

export function themeColors(theme: ThemeName): ThemeColors {
  const pal = PALETTES[theme];
  return {
    pal,
    text: cssVar("--mast-text") ?? (theme === "dark" ? pal.tints.coating : pal.tints.ink),
    faint: cssVar("--mast-faint") ?? pal.hover,
    selection: cssVar("--mast-accent") ?? pal.selection,
    background: cssVar("--mast-bg") ?? pal.background,
    tableTop: pal.tableTop,
  };
}

/** A beam colour as a hex value: a design token (the grey of "no light") is read from the page. */
function resolveColor(c: string, colors: ThemeColors): string {
  const m = /^var\((--[A-Za-z0-9-]+)\)$/.exec(c.trim());
  if (!m) return c;
  return cssVar(m[1]!) ?? colors.faint;
}

const BUCKET_ORDER: Array<[Shape3, Mat3]> = [];
for (const shape of ["box", "cyl", "frustum", "cone", "lensVex", "lensCave", "prism", "torus", "sphere"] as const) for (const mat of ["solid", "glass"] as const) BUCKET_ORDER.push([shape, mat]);

/**
 * The parts of one (shape, material): drawn by `drawn`, every instance baked into one plain mesh (merged.ts), and
 * picked through `mesh`, an instanced mesh that is never drawn (three.js raycasts it instance by instance).
 */
interface Bucket {
  shape: Shape3;
  mesh: InstancedMesh;
  drawn: MergedBucket;
  owners: string[];
  /** which part of its component each instance is */
  parts: number[];
  /** each instance's own colour (linear r, g, b), before the selection tints it */
  base: Float32Array;
  capacity: number;
}

/** How far a selected component's colour goes towards the accent (the 2D glyph's glow), and a hovered one's. */
export const SELECTED_TINT = 0.5;
export const HOVER_TINT = 0.22;

const _m = new Matrix4();
const _q = new Quaternion();
const _v = new Vector3();
const _s = new Vector3();
const _z = new Vector3(0, 0, 1);
const _c = new Color();

/** Extra pixels of the halo round the beam the beam inspector shows. */
const PICK_HALO_PX = 5;

export class LayoutScene3D {
  readonly scene = new Scene();
  /** the CSS2D labels, apart so the label renderer walks only them */
  readonly labelScene = new Scene();
  readonly uniforms: BeamUniforms = beamUniforms();
  colors: ThemeColors;
  private theme: ThemeName;

  private table = new Group();
  private parts = new Group();
  private light = new Group();
  private overlay = new Group();
  private buckets = new Map<BucketKey, Bucket>();
  private solid = solidMaterial();
  private glass: MeshLambertMaterial;
  private beams: InstancedMesh | null = null;
  private beamMat: MeshBasicMaterial;
  private haloStrong: InstancedMesh | null = null;
  private haloWeak: InstancedMesh | null = null;
  private envelope: Mesh | null = null;
  private marks: LineSegments | null = null;
  private outlines: LineSegments | null = null;
  private hemi = new HemisphereLight(0xffffff, 0x404850, 1.25);
  private sun = new DirectionalLight(0xffffff, 1.9);

  private prev: Partial<SceneInput> = {};
  nodes: ComponentNode[] = [];
  private nodeById = new Map<string, ComponentNode>();
  /** the drawn beam tubes, in instance order */
  tubes: TubeSpec[] = [];
  private envelopes: EnvelopeSpec[] = [];
  /** the box round everything (for fitting the view) */
  box: Box3 = { min: [0, 0, 0], max: [1, 1, 1] };

  private detail: Detail = "high";
  private labels = new Map<string, CSS2DObject>();
  private labelSizes = new Map<string, { w: number; h: number; text: string }>();
  private holeTexture: CanvasTexture | null = null;
  private readonly anisotropy: number;

  constructor(theme: ThemeName, opts: { anisotropy?: number } = {}) {
    this.theme = theme;
    this.colors = themeColors(theme);
    this.anisotropy = opts.anisotropy ?? 1;
    this.glass = glassMaterial(this.colors.pal.glassOpacity);
    this.beamMat = beamMaterial(this.uniforms);
    this.table.name = "table";
    this.parts.name = "components";
    this.light.name = "beams";
    this.overlay.name = "overlay";
    this.scene.add(this.table, this.parts, this.light, this.overlay, this.hemi, this.sun, this.sun.target);
  }

  // ── updating ───────────────────────────────────────────────────────────────

  update(input: SceneInput): void {
    const p = this.prev;
    const themeChanged = p.theme !== input.theme;
    if (themeChanged) {
      this.theme = input.theme;
      this.colors = themeColors(input.theme);
      this.glass.opacity = this.colors.pal.glassOpacity;
    }
    const tableChanged = themeChanged || p.layout?.table !== input.layout.table;
    const compsChanged = themeChanged || tableChanged || p.layout?.components !== input.layout.components;
    const d = input.display;
    const pd = p.display;
    const beamLayersChanged = !pd || pd.beams !== d.beams || pd.envelope !== d.envelope || pd.polarization !== d.polarization || pd.power !== d.power;
    const traceChanged = themeChanged || tableChanged || p.trace !== input.trace || beamLayersChanged;
    if (tableChanged) this.buildTable(input.layout.table);
    // a new theme recolours everything; a new table (its beam height) moves everything
    if (compsChanged) this.buildComponents(input.layout, themeChanged || tableChanged);
    if (traceChanged) this.buildBeams(input.trace, input.layout.table, input.theme, d);
    const selChanged = compsChanged || p.selected !== input.selected || p.hover !== input.hover;
    if (selChanged) {
      this.buildOutlines(input.selected, input.hover);
      this.paint(input.selected, input.hover);
    }
    if (traceChanged || p.beamPick !== input.beamPick) this.buildPickHalo(input.trace, input.layout.table, input.theme, input.beamPick, d.beams);
    // the labels follow their layers too (names, text notes, power tags, comment pins)
    if (compsChanged || p.display !== input.display || p.layout?.texts !== input.layout.texts || p.commentCounts !== input.commentCounts || traceChanged || p.interactive !== input.interactive || selChanged) this.buildLabels(input);
    // visibility of the layers
    const holes = this.table.getObjectByName("holes");
    if (holes) holes.visible = d.holes;
    const grid = this.table.getObjectByName("grid");
    if (grid) grid.visible = d.grid;
    this.light.visible = d.beams;
    this.prev = input;
  }

  // ── the table ──────────────────────────────────────────────────────────────

  private buildTable(t: TableSpec): void {
    disposeChildren(this.table);
    const pal = this.colors.pal;
    const slab = new Mesh(new BoxGeometry(t.width_mm, t.height_mm, TABLE_THICKNESS_MM), new MeshLambertMaterial({ color: new Color(pal.tableSide) }));
    slab.position.set(t.width_mm / 2, t.height_mm / 2, -TABLE_THICKNESS_MM / 2 - 0.05);
    slab.name = "table slab";
    const top = new Mesh(new PlaneGeometry(t.width_mm, t.height_mm), new MeshLambertMaterial({ color: new Color(pal.tableTop) }));
    top.position.set(t.width_mm / 2, t.height_mm / 2, 0);
    top.name = "table top";
    this.table.add(slab, top);
    const patch = holePatch(t);
    const tex = patch ? this.holeMap() : null;
    if (patch && tex) {
      const g = new PlaneGeometry(patch.x1 - patch.x0, patch.y1 - patch.y0);
      const uv = g.getAttribute("uv");
      for (let i = 0; i < uv.count; i++) uv.setXY(i, patch.u0 + (patch.u1 - patch.u0) * uv.getX(i), patch.v0 + (patch.v1 - patch.v0) * uv.getY(i));
      const holes = new Mesh(g, new MeshBasicMaterial({ color: new Color(pal.holes), map: tex, transparent: true, depthWrite: false, polygonOffset: true, polygonOffsetFactor: -1, polygonOffsetUnits: -1 }));
      holes.position.set((patch.x0 + patch.x1) / 2, (patch.y0 + patch.y1) / 2, 0.05);
      holes.name = "holes";
      this.table.add(holes);
    }
    const lines = gridLines(t);
    if (lines.length > 0) {
      const grid = new LineSegments(lineGeometry(lines.map(([a, b]) => [a, b, pal.grid] as const)), linesMaterial());
      (grid.material as Material).transparent = true;
      (grid.material as Material).opacity = 0.55;
      grid.name = "grid";
      this.table.add(grid);
    }
  }

  /** One tile of the hole pattern: a disc in the middle of a transparent square, repeated once per pitch. */
  private holeMap(): CanvasTexture | null {
    if (this.holeTexture) return this.holeTexture;
    if (typeof document === "undefined") return null;
    const n = 64;
    const cv = document.createElement("canvas");
    cv.width = n;
    cv.height = n;
    const ctx = cv.getContext("2d");
    if (!ctx) return null;
    ctx.clearRect(0, 0, n, n);
    ctx.fillStyle = "white";
    ctx.beginPath();
    ctx.arc(n / 2, n / 2, (HOLE_SHARE * n) / 2, 0, Math.PI * 2);
    ctx.fill();
    const tex = new CanvasTexture(cv);
    tex.wrapS = RepeatWrapping;
    tex.wrapT = RepeatWrapping;
    tex.anisotropy = this.anisotropy;
    this.holeTexture = tex;
    return tex;
  }

  // ── the components ─────────────────────────────────────────────────────────

  private buildComponents(layout: OpticalLayout, rebuild: boolean): void {
    const before = this.nodeById;
    this.nodes = sceneComponents(layout);
    this.nodeById = new Map(this.nodes.map((n) => [n.id, n]));
    this.box = sceneBox(layout.table, layout.components);
    const groups = instanceBuckets(this.nodes);
    const theme = this.theme;
    // The same components in the same order, with the same parts: only the ones that changed (a drag moves one, edit.ts
    // keeps the others' objects) are written again, in place. Anything else rebuilds every bucket.
    const inPlace = !rebuild && BUCKET_ORDER.every(([shape, mat]) => {
      const list = groups.get(`${shape}:${mat}`) ?? [];
      const b = this.buckets.get(`${shape}:${mat}`);
      const had = b ? b.owners.length : 0;
      return had === list.length && list.every((inst, i) => b!.owners[i] === inst.id && b!.parts[i] === inst.part);
    });
    for (const [shape, mat] of BUCKET_ORDER) {
      const key: BucketKey = `${shape}:${mat}`;
      const list = groups.get(key) ?? [];
      let b = this.buckets.get(key);
      if (inPlace) {
        if (!b) continue;
        const bucket = b;
        list.forEach((inst, i) => {
          if (before.get(inst.id)?.component === this.nodeById.get(inst.id)?.component) return;
          _m.fromArray(inst.matrix);
          bucket.mesh.setMatrixAt(i, _m);
          bucket.drawn.setMatrix(i, inst.matrix);
          _c.set(tintColor(inst, theme));
          bucket.base[i * 3] = _c.r;
          bucket.base[i * 3 + 1] = _c.g;
          bucket.base[i * 3 + 2] = _c.b;
        });
        bucket.mesh.boundingSphere = null;
        bucket.mesh.boundingBox = null;
        continue;
      }
      if (list.length === 0) {
        if (b) {
          b.mesh.count = 0;
          b.owners = [];
          b.parts = [];
          b.drawn.setAll(new Float32Array(0), new Float32Array(0), 0);
        }
        continue;
      }
      if (!b || b.capacity < list.length) {
        if (b) {
          b.mesh.dispose();
          this.parts.remove(b.drawn.mesh);
          b.drawn.dispose();
        }
        const capacity = Math.max(16, 2 ** Math.ceil(Math.log2(list.length)));
        const material = mat === "glass" ? this.glass : this.solid;
        const mesh = new InstancedMesh(shapeGeometry(shape, "high"), material, capacity);
        mesh.name = key;
        const drawn = new MergedBucket(shapeGeometry(shape, this.detail), material, key);
        if (mat === "glass") drawn.mesh.renderOrder = 2;
        b = { shape, mesh, drawn, owners: [], parts: [], base: new Float32Array(0), capacity };
        this.buckets.set(key, b);
        this.parts.add(drawn.mesh);
      }
      const mesh = b.mesh;
      b.owners = list.map((i) => i.id);
      b.parts = list.map((i) => i.part);
      b.base = new Float32Array(list.length * 3);
      const base = b.base;
      const matrices = new Float32Array(list.length * 16);
      list.forEach((inst, i) => {
        _m.fromArray(inst.matrix);
        mesh.setMatrixAt(i, _m);
        matrices.set(inst.matrix, i * 16);
        _c.set(tintColor(inst, theme));
        base[i * 3] = _c.r;
        base[i * 3 + 1] = _c.g;
        base[i * 3 + 2] = _c.b;
      });
      mesh.count = list.length;
      // picking computes these again from the new instances
      mesh.boundingSphere = null;
      mesh.boundingBox = null;
      b.drawn.setAll(matrices, new Float32Array(base), list.length);
    }
    // light from above, a little from the front left, whatever the size of the table
    const cx = (this.box.min[0] + this.box.max[0]) / 2;
    const cy = (this.box.min[1] + this.box.max[1]) / 2;
    const r = Math.hypot(this.box.max[0] - this.box.min[0], this.box.max[1] - this.box.min[1]);
    this.sun.position.set(cx - 0.35 * r, cy - 0.6 * r, 1.2 * r);
    this.sun.target.position.set(cx, cy, 0);
  }

  // ── the light ──────────────────────────────────────────────────────────────

  private buildBeams(trace: DetailedTrace, table: TableSpec, theme: ThemeName, d: Layers): void {
    const tubes = beamTubes(trace, table, theme);
    this.tubes = tubes;
    const n = tubes.length;
    if (!this.beams || (this.beams.instanceMatrix.count as number) < n) {
      if (this.beams) {
        this.light.remove(this.beams);
        this.beams.geometry.dispose();
        this.beams.dispose();
      }
      const capacity = Math.max(64, 2 ** Math.ceil(Math.log2(Math.max(1, n))));
      const mesh = new InstancedMesh(tubeGeometry(), this.beamMat, capacity);
      mesh.instanceMatrix.setUsage(DynamicDrawUsage);
      mesh.frustumCulled = false;
      mesh.name = "beam tubes";
      ensureBeamAttribute(mesh, capacity);
      this.beams = mesh;
      this.light.add(mesh);
    }
    const mesh = this.beams;
    const attr = ensureBeamAttribute(mesh, mesh.instanceMatrix.count);
    const arr = attr.array as Float32Array;
    const table3 = this.colors.tableTop;
    tubes.forEach((t, i) => {
      tubeMatrix(t.a, t.b, _m);
      mesh.setMatrixAt(i, _m);
      // a fainter beam is drawn in its colour mixed towards the table, as the 2D line's opacity makes it look
      _c.set(mixHex(table3, resolveColor(t.color, this.colors), t.opacity));
      mesh.setColorAt(i, _c);
      arr[i * 4] = t.widthPx / 2;
      arr[i * 4 + 1] = t.dash ? t.dash[0] : 0;
      arr[i * 4 + 2] = t.dash ? t.dash[1] : 0;
      arr[i * 4 + 3] = Math.hypot(t.b[0] - t.a[0], t.b[1] - t.a[1], t.b[2] - t.a[2]);
    });
    mesh.count = n;
    mesh.instanceMatrix.needsUpdate = true;
    if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
    attr.needsUpdate = true;

    // the envelope
    if (this.envelope) {
      this.light.remove(this.envelope);
      this.envelope.geometry.dispose();
      (this.envelope.material as Material).dispose();
      this.envelope = null;
    }
    this.envelopes = d.envelope ? beamEnvelopes(trace, table, theme) : [];
    if (this.envelopes.length > 0) {
      this.envelope = new Mesh(envelopeGeometry(this.envelopes, (c) => resolveColor(c, this.colors)), envelopeMaterial());
      this.envelope.name = "beam envelope";
      this.envelope.renderOrder = 3;
      this.light.add(this.envelope);
    }

    // polarisation marks and waist ticks
    if (this.marks) {
      this.light.remove(this.marks);
      this.marks.geometry.dispose();
      (this.marks.material as Material).dispose();
      this.marks = null;
    }
    const segs: Array<readonly [Vec3, Vec3, string]> = [];
    const size = markSize(table);
    if (d.polarization) for (const m of polarizationMarks(trace, table, { sizeMm: size, minLengthMm: size * 3 })) for (const [a, b] of m.lines) segs.push([a, b, this.colors.text]);
    for (const e of this.envelopes) {
      if (e.waist === null) continue;
      const at: Vec3 = [e.a[0] + e.dir[0] * e.waist, e.a[1] + e.dir[1] * e.waist, e.a[2]];
      const { eh, ev } = transverseFrame(e.dir);
      const r = Math.max(size * 0.3, 1.5 * (e.samples.reduce((m, s) => Math.min(m, s[1]), Infinity) || 0));
      const col = resolveColor(e.color, this.colors);
      segs.push([[at[0] - eh[0] * r, at[1] - eh[1] * r, at[2]], [at[0] + eh[0] * r, at[1] + eh[1] * r, at[2]], col]);
      segs.push([[at[0], at[1], at[2] - ev[2] * r], [at[0], at[1], at[2] + ev[2] * r], col]);
    }
    // where a beam goes over or under a component (heights): a thin line down or up to the component's axis
    for (const m of passMarks(trace, table)) segs.push([m.at, m.axis, this.colors.faint]);
    if (segs.length > 0) {
      this.marks = new LineSegments(lineGeometry(segs), linesMaterial());
      this.marks.name = "polarisation marks";
      this.light.add(this.marks);
    }
  }

  /** The halo round the beam the beam inspector shows: the segment strongly, the rest of its path lightly. */
  private buildPickHalo(trace: DetailedTrace, table: TableSpec, theme: ThemeName, pick: SegRef | null, beams: boolean): void {
    for (const h of [this.haloStrong, this.haloWeak]) {
      if (!h) continue;
      this.light.remove(h);
      h.geometry.dispose();
      disposeMat(h.material);
    }
    this.haloStrong = null;
    this.haloWeak = null;
    if (!pick || !beams) return;
    const tree = trace[pick.tree];
    if (!tree) return;
    const path = new Set(branchPath(tree, pick.branch).map((b) => b.branch));
    const strong: TubeSpec[] = [];
    const weak: TubeSpec[] = [];
    for (const t of this.tubes.length > 0 ? this.tubes : beamTubes(trace, table, theme)) {
      if (t.ref.tree !== pick.tree || !path.has(t.ref.branch)) continue;
      if (t.ref.branch === pick.branch && t.ref.segment === pick.segment && !t.joint) strong.push(t);
      else weak.push(t);
    }
    const make = (list: TubeSpec[], opacity: number, name: string): InstancedMesh | null => {
      if (list.length === 0) return null;
      const mat = beamMaterial(this.uniforms);
      mat.transparent = true;
      mat.opacity = opacity;
      mat.depthWrite = false;
      mat.color = new Color(this.colors.selection);
      const mesh = new InstancedMesh(tubeGeometry(), mat, list.length);
      const attr = ensureBeamAttribute(mesh, list.length);
      const arr = attr.array as Float32Array;
      list.forEach((t, i) => {
        tubeMatrix(t.a, t.b, _m);
        mesh.setMatrixAt(i, _m);
        arr[i * 4] = t.widthPx / 2 + PICK_HALO_PX;
        arr[i * 4 + 3] = Math.hypot(t.b[0] - t.a[0], t.b[1] - t.a[1], t.b[2] - t.a[2]);
      });
      mesh.frustumCulled = false;
      mesh.renderOrder = 4;
      mesh.name = name;
      this.light.add(mesh);
      return mesh;
    };
    this.haloStrong = make(strong, 0.55, "picked beam");
    this.haloWeak = make(weak, 0.22, "picked beam path");
  }

  // ── selection, hover ───────────────────────────────────────────────────────

  /**
   * The colours of the parts: their own, a selected component's turned towards the accent and a hovered one's a
   * little, so the selection reads at any zoom (an outline alone is a few pixels when the whole table is in view).
   */
  private paint(selected: ReadonlySet<string>, hover: string | null): void {
    const accent = new Color(this.colors.selection);
    for (const b of this.buckets.values()) {
      for (let i = 0; i < b.owners.length; i++) {
        const id = b.owners[i]!;
        const t = selected.has(id) ? SELECTED_TINT : id === hover ? HOVER_TINT : 0;
        const r = b.base[i * 3]!;
        const g = b.base[i * 3 + 1]!;
        const bl = b.base[i * 3 + 2]!;
        b.drawn.setColor(i, r + (accent.r - r) * t, g + (accent.g - g) * t, bl + (accent.b - bl) * t);
      }
    }
  }

  private buildOutlines(selected: ReadonlySet<string>, hover: string | null): void {
    if (this.outlines) {
      this.overlay.remove(this.outlines);
      this.outlines.geometry.dispose();
      disposeMat(this.outlines.material);
      this.outlines = null;
    }
    const segs: Array<readonly [Vec3, Vec3, string]> = [];
    for (const id of selected) {
      const n = this.nodeById.get(id);
      if (n) for (const [a, b] of outlineEdges(n)) segs.push([a, b, this.colors.selection]);
    }
    if (hover && !selected.has(hover)) {
      const n = this.nodeById.get(hover);
      if (n) for (const [a, b] of outlineEdges(n, 2.5)) segs.push([a, b, this.colors.pal.hover]);
    }
    if (segs.length === 0) return;
    this.outlines = new LineSegments(lineGeometry(segs), linesMaterial(true));
    this.outlines.renderOrder = 10;
    this.outlines.name = "outlines";
    this.overlay.add(this.outlines);
  }

  // ── labels ─────────────────────────────────────────────────────────────────

  private buildLabels(input: SceneInput): void {
    // labels are HTML: nothing to make without a page (the unit tests build the scene in Node)
    if (typeof document === "undefined") return;
    /** `center`: which point of the label sits on `at`, as shares of its width and height (CSS2DObject.center) */
    const want = new Map<string, { at: Vec3; text: string; cls: string; color?: string; html?: string; center: [number, number] }>();
    const d = input.display;
    if (d.labels) {
      for (const n of this.nodes) {
        const sel = input.selected.has(n.id);
        const label = n.component.label || n.id;
        want.set(`c:${n.id}`, { at: labelAnchor(n), text: n.component.type === "custom" ? `${label} · ${tr("自定义", "custom")}` : label, cls: sel ? LABEL_SELECTED : LABEL, center: [0.5, 1] });
      }
    }
    if (d.texts) for (const t of input.layout.texts) want.set(`t:${t.id}`, { at: [t.x_mm, t.y_mm, 0.5], text: t.text, cls: TEXT_NOTE, center: [0.5, 0.5] });
    if (d.beams && d.power) {
      for (const p of powerTags3d(input.trace, input.layout.table, input.theme)) {
        // to the right of where the light arrives, the second beam on the same component one line below the first
        want.set(`p:${p.key}`, { at: [p.at[0], p.at[1], p.at[2]], text: tr(p.zh, p.en), cls: POWER_TAG, color: resolveColor(p.color, this.colors), center: [-0.08, 0.5 - 1.1 * p.slot] });
      }
    }
    if (d.comments && input.commentCounts) {
      for (const [id, count] of input.commentCounts) {
        const n = this.nodeById.get(id);
        if (n && count > 0) {
          const a = labelAnchor(n);
          want.set(`m:${id}`, { at: [a[0], a[1], a[2]], text: String(count), cls: COMMENT_PIN, center: [0.5, 2.3] });
        }
      }
    }
    if (input.interactive) {
      for (const n of this.nodes) {
        if (!n.component.locked) continue;
        const a = labelAnchor(n);
        want.set(`l:${n.id}`, { at: [a[0], a[1], a[2]], text: "", cls: LOCK_BADGE, html: LOCK_SVG, center: [-1.6, 1.1] });
      }
    }
    for (const [key, obj] of this.labels) {
      if (want.has(key)) continue;
      this.labelScene.remove(obj);
      obj.element.remove();
      this.labels.delete(key);
      this.labelSizes.delete(key);
    }
    for (const [key, w] of want) {
      let obj = this.labels.get(key);
      if (!obj) {
        const el = document.createElement("div");
        obj = new CSS2DObject(el);
        obj.name = key;
        this.labels.set(key, obj);
        this.labelScene.add(obj);
      }
      const el = obj.element;
      if (el.className !== w.cls) el.className = w.cls;
      if (w.html !== undefined) {
        if (el.innerHTML !== w.html) el.innerHTML = w.html;
        el.title = tr("已锁定", "Locked");
      } else if (el.textContent !== w.text) {
        el.textContent = w.text;
        this.labelSizes.delete(key);
      }
      if (w.color !== undefined) el.style.color = w.color;
      el.style.background = w.cls === LABEL || w.cls === POWER_TAG ? TRANSLUCENT_PANEL : "";
      el.dataset["ol3dLabel"] = key;
      obj.position.set(w.at[0], w.at[1], w.at[2]);
      obj.center.set(w.center[0], w.center[1]);
    }
  }

  /**
   * Shows the component labels that fit (labels3d.ts declutter), the selected and hovered first, and returns
   * how many are shown. Called before every render of the labels.
   */
  declutterLabels(camera: Camera, width: number, height: number, selected: ReadonlySet<string>, hover: string | null): number {
    const boxes: LabelBox[] = [];
    camera.updateMatrixWorld();
    const camPos = new Vector3().setFromMatrixPosition(camera.matrixWorld);
    for (const [key, obj] of this.labels) {
      if (!key.startsWith("c:")) continue;
      _v.copy(obj.position).project(camera);
      if (_v.z < -1 || _v.z > 1) {
        obj.visible = false;
        continue;
      }
      let size = this.labelSizes.get(key);
      const el = obj.element;
      if (!size || size.text !== el.textContent) {
        const w = el.offsetWidth;
        const h = el.offsetHeight;
        size = w > 0 ? { w, h, text: el.textContent ?? "" } : { w: (el.textContent?.length ?? 4) * 7 + 8, h: 16, text: "" };
        if (w > 0) this.labelSizes.set(key, size);
      }
      const id = key.slice(2);
      const sx = (_v.x * 0.5 + 0.5) * width;
      const sy = (-_v.y * 0.5 + 0.5) * height;
      boxes.push({ id: key, x: sx, y: sy - size.h / 2, w: size.w, h: size.h, priority: selected.has(id) ? 2 : hover === id ? 1 : 0, depth: camPos.distanceToSquared(obj.position) });
    }
    const keep = declutter(boxes, width, height);
    let shown = 0;
    for (const b of boxes) {
      const obj = this.labels.get(b.id);
      if (!obj) continue;
      obj.visible = keep.has(b.id);
      if (obj.visible) shown++;
    }
    return shown;
  }

  /** The labels that are on the screen now, with where they are, for the PNG export to draw them. */
  visibleLabels(): CSS2DObject[] {
    return [...this.labels.values()].filter((o) => o.visible && o.element.style.display !== "none" && o.element.isConnected);
  }

  // ── per frame ──────────────────────────────────────────────────────────────

  /** Swaps every part's geometry for its finer or coarser version (the instances stay as they are). */
  setDetail(detail: Detail): void {
    if (detail === this.detail) return;
    this.detail = detail;
    for (const b of this.buckets.values()) b.drawn.setBase(shapeGeometry(b.shape, detail));
  }

  getDetail(): Detail {
    return this.detail;
  }

  /** The beam width uniforms for a camera on a view `heightPx` tall. */
  setPixelScale(kind: "perspective" | "orthographic", value: number): void {
    if (kind === "perspective") {
      this.uniforms.uOrtho.value = 0;
      this.uniforms.uMmPerPxPerDepth.value = value;
    } else {
      this.uniforms.uOrtho.value = 1;
      this.uniforms.uOrthoMmPerPx.value = value;
    }
  }

  // ── picking ────────────────────────────────────────────────────────────────

  /** The component under a ray, and where the ray met it. */
  pickComponent(raycaster: Raycaster): { id: string; point: Vec3 } | null {
    const meshes = [...this.buckets.values()].filter((b) => b.mesh.count > 0).map((b) => b.mesh);
    const hits = raycaster.intersectObjects(meshes, false);
    for (const h of hits) {
      const b = this.buckets.get(h.object.name as BucketKey);
      const id = h.instanceId !== undefined ? b?.owners[h.instanceId] : undefined;
      if (id) return { id, point: [h.point.x, h.point.y, h.point.z] };
    }
    return null;
  }

  node(id: string): ComponentNode | undefined {
    return this.nodeById.get(id);
  }

  /** The colours a component's parts are drawn in now, and their own colours (linear r, g, b), for checking. */
  partColors(id: string): Array<{ drawn: [number, number, number]; own: [number, number, number] }> {
    const out: Array<{ drawn: [number, number, number]; own: [number, number, number] }> = [];
    for (const b of this.buckets.values()) {
      b.owners.forEach((o, i) => {
        if (o === id) out.push({ drawn: b.drawn.colorAt(i), own: [b.base[i * 3]!, b.base[i * 3 + 1]!, b.base[i * 3 + 2]!] });
      });
    }
    return out;
  }

  // ── cleaning up ────────────────────────────────────────────────────────────

  /**
   * The WebGL context was lost: every GPU object of this scene is invalid, and calling dispose() on them makes the
   * browser warn for each. Only the page side is cleaned up (the label elements); the scene is not to be used again.
   */
  abandon(): void {
    for (const o of this.labels.values()) o.element.remove();
    this.labels.clear();
    this.scene.clear();
    this.labelScene.clear();
  }

  dispose(): void {
    disposeChildren(this.table);
    for (const b of this.buckets.values()) {
      b.mesh.dispose();
      b.drawn.dispose();
    }
    this.buckets.clear();
    for (const m of [this.beams, this.haloStrong, this.haloWeak]) {
      if (!m) continue;
      m.geometry.dispose();
      // the halos have materials of their own; the beams' is beamMat, freed below
      if (m !== this.beams) disposeMat(m.material);
      m.dispose();
    }
    for (const m of [this.envelope, this.marks, this.outlines]) {
      if (!m) continue;
      m.geometry.dispose();
      disposeMat(m.material);
    }
    this.solid.dispose();
    this.glass.dispose();
    this.beamMat.dispose();
    this.holeTexture?.dispose();
    for (const o of this.labels.values()) o.element.remove();
    this.labels.clear();
  }
}

// ── helpers ───────────────────────────────────────────────────────────────────

/** The matrix that stretches the unit tube (z from 0 to 1) from a to b. */
export function tubeMatrix(a: Vec3, b: Vec3, out: Matrix4): Matrix4 {
  _v.set(b[0] - a[0], b[1] - a[1], b[2] - a[2]);
  const len = _v.length();
  if (len > 0) _v.divideScalar(len);
  else _v.set(0, 0, 1);
  _q.setFromUnitVectors(_z, _v);
  _s.set(1, 1, Math.max(len, 1e-6));
  return out.compose(new Vector3(a[0], a[1], a[2]), _q, _s);
}

/** Line segments with a colour each (vertex colours, linear). */
export function lineGeometry(segs: ReadonlyArray<readonly [Vec3, Vec3, string]>): BufferGeometry {
  const pos = new Float32Array(segs.length * 6);
  const col = new Float32Array(segs.length * 6);
  segs.forEach(([a, b, color], i) => {
    pos.set([a[0], a[1], a[2], b[0], b[1], b[2]], i * 6);
    _c.set(color);
    col.set([_c.r, _c.g, _c.b, _c.r, _c.g, _c.b], i * 6);
  });
  const g = new BufferGeometry();
  g.setAttribute("position", new Float32BufferAttribute(pos, 3));
  g.setAttribute("color", new Float32BufferAttribute(col, 3));
  return g;
}

const ENVELOPE_SIDES = 16;

/** The envelopes as one surface of revolution per segment, coloured per vertex with their opacity. */
export function envelopeGeometry(envs: readonly EnvelopeSpec[], colorOf: (c: string) => string): BufferGeometry {
  const pos: number[] = [];
  const col: number[] = [];
  const idx: number[] = [];
  for (const e of envs) {
    const { eh, ev } = transverseFrame(e.dir);
    _c.set(colorOf(e.color));
    const base = pos.length / 3;
    for (const [s, r] of e.samples) {
      const cx = e.a[0] + e.dir[0] * s;
      const cy = e.a[1] + e.dir[1] * s;
      const cz = e.a[2] + e.dir[2] * s;
      for (let k = 0; k < ENVELOPE_SIDES; k++) {
        const t = (2 * Math.PI * k) / ENVELOPE_SIDES;
        const c = Math.cos(t) * r;
        const sn = Math.sin(t) * r;
        pos.push(cx + eh[0] * c + ev[0] * sn, cy + eh[1] * c + ev[1] * sn, cz + eh[2] * c + ev[2] * sn);
        col.push(_c.r, _c.g, _c.b, e.opacity);
      }
    }
    for (let i = 0; i + 1 < e.samples.length; i++) {
      for (let k = 0; k < ENVELOPE_SIDES; k++) {
        const a = base + i * ENVELOPE_SIDES + k;
        const b = base + i * ENVELOPE_SIDES + ((k + 1) % ENVELOPE_SIDES);
        const c = a + ENVELOPE_SIDES;
        const d = b + ENVELOPE_SIDES;
        idx.push(a, c, b, b, c, d);
      }
    }
  }
  const g = new BufferGeometry();
  g.setAttribute("position", new Float32BufferAttribute(pos, 3));
  g.setAttribute("color", new Float32BufferAttribute(col, 4));
  g.setIndex(idx);
  return g;
}

/** The length (mm) of a polarisation mark: about a hundredth of the table, at least 6 mm. */
export function markSize(t: TableSpec): number {
  return Math.min(40, Math.max(6, Math.hypot(t.width_mm, t.height_mm) * 0.012));
}

function disposeMat(m: Material | Material[]): void {
  for (const x of Array.isArray(m) ? m : [m]) x.dispose();
}

function disposeChildren(g: Group): void {
  for (const ch of [...g.children]) {
    g.remove(ch);
    const m = ch as Mesh;
    m.geometry?.dispose();
    if (m.material) disposeMat(m.material as Material | Material[]);
  }
}

// ── label styles (design tokens only) ─────────────────────────────────────────

const LABEL_BASE = "pointer-events-none whitespace-nowrap rounded px-1 text-[11px] leading-4 shadow-mast";
const LABEL = `${LABEL_BASE} text-mast-text`;
const LABEL_SELECTED = `${LABEL_BASE} bg-mast-panel font-semibold text-mast-accent ring-1 ring-mast-accent`;
const TEXT_NOTE = "pointer-events-none whitespace-nowrap rounded px-1 text-[12px] italic text-mast-muted";
const POWER_TAG = "pointer-events-none whitespace-nowrap rounded px-1 font-mono text-[10px] leading-4";
/** Tailwind cannot put an opacity on a var() colour (LayoutCanvas READOUT_BG does the same) */
const TRANSLUCENT_PANEL = "color-mix(in srgb, var(--mast-panel) 82%, transparent)";
const COMMENT_PIN = "pointer-events-none rounded-full bg-mast-accent px-1.5 text-[10px] font-semibold leading-4 text-mast-accent-ink";
const LOCK_BADGE = "pointer-events-none flex h-4 w-4 items-center justify-center rounded-full border border-mast-border-strong bg-mast-panel text-mast-muted";
/** the padlock of the 2D canvas (LayoutSvg Lock), in currentColor */
const LOCK_SVG =
  '<svg width="10" height="11" viewBox="-5 -5 10 11" aria-hidden="true"><path d="M-2 0v-1.6a2 2 0 0 1 4 0v1.6" fill="none" stroke="currentColor" stroke-width="1.2"/><rect x="-3" y="0" width="6" height="4.4" rx="0.8" fill="currentColor"/></svg>';
