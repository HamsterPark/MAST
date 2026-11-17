// The parts of one (shape, material) baked into ONE plain mesh: every instance's vertices moved to where the instance
// is, its normals turned with it, its colour written per vertex.
//
// Why not draw them as an instanced mesh: a software renderer (SwiftShader, which headless Chromium and machines
// without a graphics driver use) runs an instanced draw as one internal draw per instance, and with two thousand
// parts that fixed cost per instance was most of the frame (docs/v2/design/optical_layout_3d_view.md section 7.1).
// One plain mesh per bucket has the same draw calls and no per-instance cost. A GPU does not mind either way.
//
// The instanced meshes stay, out of the scene, for picking (three.js raycasts an instanced mesh instance by
// instance, against the small canonical shape); this file only draws.

import { BufferAttribute, BufferGeometry, Matrix3, Matrix4, Mesh, type Material } from "three";

const _m = new Matrix4();
const _n = new Matrix3();

export class MergedBucket {
  readonly mesh: Mesh;
  private base: BufferGeometry;
  /** vertices and indices of one instance */
  private vpi = 0;
  private ipi = 0;
  private srcPos: ArrayLike<number> = [];
  private srcNrm: ArrayLike<number> = [];
  private srcIdx: ArrayLike<number> = [];
  private count = 0;
  private matrices: Float32Array = new Float32Array(0);
  private colors: Float32Array = new Float32Array(0);

  constructor(base: BufferGeometry, material: Material, name: string) {
    this.base = base;
    this.mesh = new Mesh(new BufferGeometry(), material);
    this.mesh.name = name;
    this.mesh.frustumCulled = false;
    this.readBase();
  }

  private readBase(): void {
    const pos = this.base.getAttribute("position");
    const nrm = this.base.getAttribute("normal");
    this.vpi = pos.count;
    this.srcPos = pos.array as ArrayLike<number>;
    this.srcNrm = nrm.array as ArrayLike<number>;
    if (this.base.index) {
      this.srcIdx = this.base.index.array as ArrayLike<number>;
    } else {
      const idx = new Uint32Array(this.vpi);
      for (let i = 0; i < this.vpi; i++) idx[i] = i;
      this.srcIdx = idx;
    }
    this.ipi = this.srcIdx.length;
  }

  /** Every instance anew: its matrix (16 numbers, column-major, at i * 16) and its colour (linear r, g, b at i * 3). */
  setAll(matrices: Float32Array, colors: Float32Array, count: number): void {
    this.count = count;
    this.matrices = matrices;
    this.colors = colors;
    this.rebuild();
  }

  /** Bakes the instances with another canonical shape (a finer or coarser one), as they are. */
  setBase(base: BufferGeometry): void {
    if (base === this.base) return;
    this.base = base;
    this.readBase();
    this.rebuild();
  }

  private rebuild(): void {
    const n = this.count;
    const g = new BufferGeometry();
    const pos = new Float32Array(n * this.vpi * 3);
    const nrm = new Float32Array(n * this.vpi * 3);
    const col = new Float32Array(n * this.vpi * 3);
    const idx = new Uint32Array(n * this.ipi);
    g.setAttribute("position", new BufferAttribute(pos, 3));
    g.setAttribute("normal", new BufferAttribute(nrm, 3));
    g.setAttribute("color", new BufferAttribute(col, 3));
    g.setIndex(new BufferAttribute(idx, 1));
    const old = this.mesh.geometry;
    this.mesh.geometry = g;
    old.dispose();
    for (let i = 0; i < n; i++) {
      const off = i * this.vpi;
      for (let k = 0; k < this.ipi; k++) idx[i * this.ipi + k] = (this.srcIdx[k] as number) + off;
      this.writePose(i);
      this.writeColor(i);
    }
    g.computeBoundingSphere();
  }

  private writePose(i: number): void {
    const g = this.mesh.geometry;
    const pos = g.getAttribute("position").array as Float32Array;
    const nrm = g.getAttribute("normal").array as Float32Array;
    _m.fromArray(this.matrices, i * 16);
    _n.getNormalMatrix(_m);
    const e = _m.elements;
    const ne = _n.elements;
    const sp = this.srcPos;
    const sn = this.srcNrm;
    let o = i * this.vpi * 3;
    for (let v = 0; v < this.vpi; v++, o += 3) {
      const x = sp[v * 3] as number;
      const y = sp[v * 3 + 1] as number;
      const z = sp[v * 3 + 2] as number;
      pos[o] = e[0]! * x + e[4]! * y + e[8]! * z + e[12]!;
      pos[o + 1] = e[1]! * x + e[5]! * y + e[9]! * z + e[13]!;
      pos[o + 2] = e[2]! * x + e[6]! * y + e[10]! * z + e[14]!;
      const a = sn[v * 3] as number;
      const b = sn[v * 3 + 1] as number;
      const c = sn[v * 3 + 2] as number;
      let nx = ne[0]! * a + ne[3]! * b + ne[6]! * c;
      let ny = ne[1]! * a + ne[4]! * b + ne[7]! * c;
      let nz = ne[2]! * a + ne[5]! * b + ne[8]! * c;
      const l = Math.hypot(nx, ny, nz) || 1;
      nx /= l;
      ny /= l;
      nz /= l;
      nrm[o] = nx;
      nrm[o + 1] = ny;
      nrm[o + 2] = nz;
    }
  }

  private writeColor(i: number): void {
    const col = this.mesh.geometry.getAttribute("color").array as Float32Array;
    const r = this.colors[i * 3]!;
    const g = this.colors[i * 3 + 1]!;
    const b = this.colors[i * 3 + 2]!;
    let o = i * this.vpi * 3;
    for (let v = 0; v < this.vpi; v++, o += 3) {
      col[o] = r;
      col[o + 1] = g;
      col[o + 2] = b;
    }
  }

  /** One instance moved (a drag): only its vertices are written again. */
  setMatrix(i: number, m: ArrayLike<number>): void {
    for (let k = 0; k < 16; k++) this.matrices[i * 16 + k] = m[k] as number;
    this.writePose(i);
    const g = this.mesh.geometry;
    const start = i * this.vpi * 3;
    const n = this.vpi * 3;
    for (const name of ["position", "normal"]) {
      const a = g.getAttribute(name) as BufferAttribute;
      a.addUpdateRange(start, n);
      a.needsUpdate = true;
    }
    g.boundingSphere = null;
  }

  /** One instance's colour (the selection tint). */
  setColor(i: number, r: number, g: number, b: number): void {
    if (this.colors[i * 3] === r && this.colors[i * 3 + 1] === g && this.colors[i * 3 + 2] === b) return;
    this.colors[i * 3] = r;
    this.colors[i * 3 + 1] = g;
    this.colors[i * 3 + 2] = b;
    this.writeColor(i);
    const a = this.mesh.geometry.getAttribute("color") as BufferAttribute;
    a.addUpdateRange(i * this.vpi * 3, this.vpi * 3);
    a.needsUpdate = true;
  }

  get instances(): number {
    return this.count;
  }

  /** The colour drawn for instance i, read back from its first vertex (linear r, g, b). */
  colorAt(i: number): [number, number, number] {
    const col = this.mesh.geometry.getAttribute("color").array as Float32Array;
    const o = i * this.vpi * 3;
    return [col[o]!, col[o + 1]!, col[o + 2]!];
  }

  dispose(): void {
    this.mesh.geometry.dispose();
  }
}
