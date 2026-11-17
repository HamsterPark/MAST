// ════════════════════════════════════════════════════════════════════════════
// Tests for the three.js side of the 3D view (src/components/opticallayout/view3d/{geometries,materials,
// LayoutScene3D,exporters}.ts), built in Node: three.js makes geometry, matrices and the GLB without a GPU.
//
//     cd frontend && npm run test:unit
//
// What is pinned: the canonical shapes fill the unit cube (the rule parts.ts sizes its parts by); the matrices of
// scene3d/mat4.ts are three's own (Euler order ZYX); a beam tube runs from its segment's start to its end; the
// scene puts every part into one of a handful of instanced meshes (a few dozen draw calls for 400 components),
// picks the component under a ray, and keeps what an edit did not touch; the beam shader's anchors exist in
// three's shaders; the GLB is a binary glTF in metres, y up, with a node per component.
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { Box3, BufferGeometry, Color, Euler, InstancedMesh, LineSegments, Matrix4, Mesh, Quaternion, Raycaster, ShaderLib, Texture, Vector3, type Material, type Object3D } from "three";
import { GLTFLoader, type GLTF } from "three/examples/jsm/loaders/GLTFLoader.js";
import { COMPONENT_TYPES } from "../src/lib/opticalLayout/types.ts";
import { CATALOG } from "../src/lib/opticalLayout/catalog.ts";
import { DEFAULT_LAYERS } from "../src/lib/opticalLayout/layers.ts";
import { traceLayoutDetailed } from "../src/lib/opticalLayout/trace.ts";
import { setPositions } from "../src/lib/opticalLayout/edit.ts";
import { demo } from "../src/lib/opticalLayout/devMock.ts";
import { SHAPES } from "../src/lib/opticalLayout/scene3d/parts.ts";
import { beamTubes } from "../src/lib/opticalLayout/scene3d/beams3d.ts";
import { compose } from "../src/lib/opticalLayout/scene3d/mat4.ts";
import { shapeGeometry, tubeGeometry } from "../src/components/opticallayout/view3d/geometries.ts";
import { beamMaterial, beamUniforms } from "../src/components/opticallayout/view3d/materials.ts";
import { HOVER_TINT, LayoutScene3D, SELECTED_TINT, tubeMatrix, type SceneInput } from "../src/components/opticallayout/view3d/LayoutScene3D.ts";
import { GLB_BEAM_MM_PER_PX, exportScene, fitScale, glbOf, scaleFont } from "../src/components/opticallayout/view3d/exporters.ts";
import { comp, layoutOf } from "./helpers/opticalLayout.ts";

const near = (a: number, b: number, tol = 1e-6) => assert.ok(Math.abs(a - b) <= tol, `${a} is not within ${tol} of ${b}`);

// As the editor passes them: the same objects from one update to the next unless they changed (its state, the
// trace memoised per layout). Fresh objects on every update would rebuild everything each time and hide a missing
// rebuild.
const NO_SELECTION: ReadonlySet<string> = new Set();
const LAYERS_ON = { ...DEFAULT_LAYERS };
const traces = new WeakMap<object, SceneInput["trace"]>();
function traceOf(layout: SceneInput["layout"]): SceneInput["trace"] {
  let t = traces.get(layout);
  if (!t) traces.set(layout, (t = traceLayoutDetailed(layout)));
  return t;
}

function input(over: Partial<SceneInput> & Pick<SceneInput, "layout">): SceneInput {
  return { trace: traceOf(over.layout), display: LAYERS_ON, theme: "dark", selected: NO_SELECTION, hover: null, beamPick: null, commentCounts: undefined, interactive: true, ...over };
}

function drawn(root: Object3D): Object3D[] {
  const out: Object3D[] = [];
  root.traverseVisible((o) => {
    const m = o as Mesh;
    if ((m.isMesh || (o as LineSegments).isLineSegments) && !((o as InstancedMesh).isInstancedMesh && (o as InstancedMesh).count === 0)) out.push(o);
  });
  return out;
}

describe("the canonical shapes", () => {
  for (const detail of ["high", "low"] as const) {
    for (const shape of SHAPES) {
      it(`${shape} fills the unit cube (${detail} detail)`, () => {
        const g = shapeGeometry(shape, detail);
        const b = g.boundingBox!;
        for (const v of [b.min.x, b.min.y, b.min.z]) near(v, -0.5, 1e-6);
        for (const v of [b.max.x, b.max.y, b.max.z]) near(v, 0.5, 1e-6);
        assert.ok(g.getAttribute("normal"), "it has normals (it is lit)");
      });
    }
  }

  it("the coarse shapes have far fewer triangles", () => {
    const tris = (shape: (typeof SHAPES)[number], detail: "high" | "low") => {
      const g = shapeGeometry(shape, detail);
      return (g.index ? g.index.count : g.getAttribute("position").count) / 3;
    };
    for (const shape of ["cyl", "torus", "lensVex", "sphere"] as const) assert.ok(tris(shape, "low") * 2 < tris(shape, "high"), shape);
  });

  it("a beam tube runs from z = 0 to z = 1, radius 1", () => {
    const g = tubeGeometry();
    g.computeBoundingBox();
    near(g.boundingBox!.min.z, 0);
    near(g.boundingBox!.max.z, 1);
    near(g.boundingBox!.max.x, 1);
  });
});

describe("the matrices are three's", () => {
  it("compose(at, rot, size) = Matrix4.compose with the Euler order ZYX", () => {
    for (const [rot, at, size] of [
      [[0.3, -1.2, 2.1], [1, 2, 3], [2, 0.5, 7]],
      [[Math.PI / 2, 0, 0], [0, 0, 0], [1, 1, 1]],
      [[0, -Math.PI / 2, Math.PI / 4], [-5, 10, 75], [12.7, 12.7, 40]],
    ] as const) {
      const ours = compose([at[0], at[1], at[2]], [rot[0], rot[1], rot[2]], [size[0], size[1], size[2]]);
      const theirs = new Matrix4().compose(new Vector3(...at), new Quaternion().setFromEuler(new Euler(rot[0], rot[1], rot[2], "ZYX")), new Vector3(...size));
      ours.forEach((v, i) => near(v, theirs.elements[i]!, 1e-12));
    }
  });

  it("a tube's matrix takes z = 0 to its start and z = 1 to its end, keeping the radius", () => {
    for (const [a, b] of [
      [[0, 0, 75], [100, 0, 75]],
      [[10, 20, 75], [10, 20, 160]],
      [[5, 5, 30], [-40, 80, 30]],
      [[0, 0, 0], [0, 0, -50]],
    ] as const) {
      const m = tubeMatrix([a[0], a[1], a[2]], [b[0], b[1], b[2]], new Matrix4());
      const p0 = new Vector3(0, 0, 0).applyMatrix4(m);
      const p1 = new Vector3(0, 0, 1).applyMatrix4(m);
      [p0.x, p0.y, p0.z].forEach((v, i) => near(v, a[i]!, 1e-9));
      [p1.x, p1.y, p1.z].forEach((v, i) => near(v, b[i]!, 1e-9));
      const r = new Vector3(1, 0, 0).transformDirection(m);
      near(r.length(), 1, 1e-12);
      near(r.dot(new Vector3(b[0] - a[0], b[1] - a[1], b[2] - a[2]).normalize()), 0, 1e-9);
    }
  });
});

describe("the beam shader", () => {
  it("finds its anchors in three's own shaders and adds the per-instance width and dashes", () => {
    for (const anchor of ["#include <common>", "#include <begin_vertex>"]) assert.ok(ShaderLib.basic.vertexShader.includes(anchor), anchor);
    for (const anchor of ["#include <common>", "void main() {"]) assert.ok(ShaderLib.basic.fragmentShader.includes(anchor), anchor);
    const u = beamUniforms();
    const m = beamMaterial(u);
    const shader = { uniforms: {} as Record<string, unknown>, vertexShader: ShaderLib.basic.vertexShader, fragmentShader: ShaderLib.basic.fragmentShader };
    m.onBeforeCompile(shader as never, null as never);
    assert.match(shader.vertexShader, /attribute vec4 aBeam;/);
    assert.match(shader.vertexShader, /transformed\.xy \*= aBeam\.x \* mmPerPx;/);
    assert.match(shader.fragmentShader, /discard;/);
    assert.equal(shader.uniforms["uMmPerPxPerDepth"], u.uMmPerPxPerDepth, "the uniforms are shared, so one update reaches every tube");
  });
});

describe("the scene", () => {
  it("draws the demo with one plain mesh per shape and material in use, every part baked in once", () => {
    const l = demo();
    const s = new LayoutScene3D("dark");
    s.update(input({ layout: l, selected: new Set(["m2"]), hover: "las1" }));
    const parts = s.scene.getObjectByName("components")!;
    const meshes = parts.children as Mesh[];
    assert.ok(meshes.every((m) => m.isMesh && !(m as InstancedMesh).isInstancedMesh), "nothing instanced is drawn (merged.ts)");
    assert.equal(
      meshes.reduce((n, m) => n + m.geometry.getAttribute("position").count / shapeGeometry(m.name.split(":")[0] as (typeof SHAPES)[number]).getAttribute("position").count, 0),
      s.nodes.reduce((n, x) => n + x.parts.length, 0),
    );
    const tubes = s.scene.getObjectByName("beam tubes") as InstancedMesh;
    assert.equal(tubes.count, s.tubes.length);
    assert.ok(s.tubes.length > 10);
    // the outline of the selected and the hovered component: 12 edges each
    const outline = s.scene.getObjectByName("outlines") as LineSegments;
    assert.equal(outline.geometry.getAttribute("position").count, 2 * 12 * 2);
    s.dispose();
  });

  it("400 components (the stress layout) in a few dozen draw calls", () => {
    const types = COMPONENT_TYPES;
    const comps = Array.from({ length: 400 }, (_, i) => comp(types[i % types.length]!, `${CATALOG[types[i % types.length]!].prefix}x${i}`, 40 + (i % 25) * 110, 40 + Math.floor(i / 25) * 110, (i * 45) % 360));
    const l = layoutOf(comps, { width_mm: 3000, height_mm: 2000 });
    const s = new LayoutScene3D("light");
    s.update(input({ layout: l }));
    const calls = drawn(s.scene).length;
    assert.ok(calls <= 32, `${calls} draw calls`);
    assert.equal(s.nodes.length, 400);
    s.dispose();
  });

  it("swaps every part between the fine and the coarse shapes, keeping the instances", () => {
    const l = demo();
    const s = new LayoutScene3D("dark");
    s.update(input({ layout: l }));
    const meshes = () => s.scene.getObjectByName("components")!.children as Mesh[];
    const vertices = () => meshes().map((m) => m.geometry.getAttribute("position").count);
    const high = vertices();
    s.setDetail("low");
    assert.equal(s.getDetail(), "low");
    const low = vertices();
    assert.ok(low.reduce((a, b) => a + b, 0) * 2 < high.reduce((a, b) => a + b, 0), `${low} vs ${high}`);
    // the same instances: each mesh holds as many copies of its coarse shape as it did of its fine one
    meshes().forEach((m, i) => {
      const shape = m.name.split(":")[0] as (typeof SHAPES)[number];
      assert.equal(low[i]! / shapeGeometry(shape, "low").getAttribute("position").count, high[i]! / shapeGeometry(shape, "high").getAttribute("position").count, m.name);
    });
    s.setDetail("high");
    assert.deepEqual(vertices(), high);
    s.dispose();
  });

  it("a selected component turns towards the accent, a hovered one a little, the others keep their colours", () => {
    const l = demo();
    const s = new LayoutScene3D("dark");
    s.update(input({ layout: l, selected: new Set(["m2"]), hover: "las1" }));
    const accent = new Color(s.colors.selection);
    const share = (c: { drawn: number[]; own: number[] }) => {
      // how far along own -> accent the drawn colour is, from the channel that moved most
      const k = [0, 1, 2].reduce((best, ch) => (Math.abs([accent.r, accent.g, accent.b][ch]! - c.own[ch]!) > Math.abs([accent.r, accent.g, accent.b][best]! - c.own[best]!) ? ch : best), 0);
      return (c.drawn[k]! - c.own[k]!) / ([accent.r, accent.g, accent.b][k]! - c.own[k]!);
    };
    for (const c of s.partColors("m2")) near(share(c), SELECTED_TINT, 1e-5);
    for (const c of s.partColors("las1")) near(share(c), HOVER_TINT, 1e-5);
    for (const c of s.partColors("m9")) assert.deepEqual(c.drawn.map((v) => Math.round(v * 1e5)), c.own.map((v) => Math.round(v * 1e5)));
    // the selection moves: the old one gets its own colours back
    s.update(input({ layout: l, selected: new Set(["m9"]), hover: null }));
    for (const c of s.partColors("m2")) assert.deepEqual(c.drawn.map((v) => Math.round(v * 1e5)), c.own.map((v) => Math.round(v * 1e5)));
    for (const c of s.partColors("m9")) near(share(c), SELECTED_TINT, 1e-5);
    s.dispose();
  });

  it("picks the component under a ray", () => {
    const l = layoutOf([comp("laser", "las1", 100, 100, 0, { props: { wavelength_nm: 532 } }), comp("mirror", "m1", 300, 100, 135), comp("detector", "d1", 300, 300, 270)]);
    const s = new LayoutScene3D("dark");
    s.update(input({ layout: l }));
    s.scene.updateMatrixWorld(true);
    const ray = (x: number, y: number) => new Raycaster(new Vector3(x, y, 1000), new Vector3(0, 0, -1));
    assert.equal(s.pickComponent(ray(300, 100))?.id, "m1");
    assert.equal(s.pickComponent(ray(80, 100))?.id, "las1");
    assert.equal(s.pickComponent(ray(300, 296))?.id, "d1");
    assert.equal(s.pickComponent(ray(700, 700)), null, "the bare table");
    const hit = s.pickComponent(ray(300, 100))!;
    assert.ok(hit.point[2] > 75, "the ray meets the top of the mount, above the beam");
    s.dispose();
  });

  it("an edit that moves one component moves its instances and the beams, and leaves the table alone", () => {
    const l = layoutOf([comp("laser", "las1", 100, 100, 0, { props: { wavelength_nm: 532 } }), comp("mirror", "m1", 300, 100, 135), comp("detector", "d1", 300, 300, 270)]);
    const s = new LayoutScene3D("dark");
    s.update(input({ layout: l }));
    const table = s.scene.getObjectByName("table")!.children.slice();
    const drawn = (s.scene.getObjectByName("components")!.children as Mesh[]).map((m) => m.geometry);
    const before = (s.scene.getObjectByName("components")!.children as Mesh[]).map((m) => Float32Array.from(m.geometry.getAttribute("position").array as Float32Array));
    const moved = setPositions(l, new Map([["d1", { x: 320, y: 300 }]]));
    assert.equal(moved.components[0], l.components[0], "edit.ts keeps what it does not touch");
    s.update(input({ layout: moved }));
    assert.deepEqual(s.scene.getObjectByName("table")!.children, table, "the table was not rebuilt");
    const now = s.scene.getObjectByName("components")!.children as Mesh[];
    now.forEach((m, i) => assert.equal(m.geometry, drawn[i], "written in place, not rebuilt"));
    // exactly the detector's vertices moved, by 20 mm in x
    let movedVerts = 0;
    now.forEach((m, i) => {
      const a = before[i]!;
      const b = m.geometry.getAttribute("position").array as Float32Array;
      for (let k = 0; k < a.length; k += 3) {
        if (a[k] === b[k] && a[k + 1] === b[k + 1] && a[k + 2] === b[k + 2]) continue;
        movedVerts++;
        near(b[k]! - a[k]!, 20, 1e-3);
        near(b[k + 1]!, a[k + 1]!, 1e-3);
      }
    });
    const detectorVerts = s.node("d1")!.parts.reduce((n, p) => n + shapeGeometry(p.shape).getAttribute("position").count, 0);
    assert.equal(movedVerts, detectorVerts);
    // picking follows too
    s.scene.updateMatrixWorld(true);
    assert.equal(s.pickComponent(new Raycaster(new Vector3(320, 296, 1000), new Vector3(0, 0, -1)))?.id, "d1");
    const box = new Box3().setFromObject(s.scene.getObjectByName("components")!);
    assert.ok(box.max.x > 320);
    s.dispose();
  });

  it("a new beam height of the table raises every component that has no height of its own", () => {
    const l = layoutOf([comp("laser", "las1", 100, 100, 0, { props: { wavelength_nm: 532 } }), comp("mirror", "m1", 300, 100, 135), { ...comp("detector", "d1", 300, 300, 270), props: { z_mm: 60 } }]);
    const s = new LayoutScene3D("dark");
    s.update(input({ layout: l }));
    const top = () => new Box3().setFromObject(s.scene.getObjectByName("components")!).max.z;
    const before = top();
    const raised = { ...l, table: { ...l.table, beam_height_mm: 175 } as typeof l.table };
    assert.equal(raised.components, l.components, "the same components: only the table is new");
    s.update(input({ layout: raised }));
    assert.equal(s.node("m1")!.z, 175);
    assert.equal(s.node("d1")!.z, 60, "its own height stays");
    near(top() - before, 100, 1e-3);
    s.dispose();
  });

  it("the layers switch what they say: holes, grid, beams, envelope, polarisation", () => {
    const l = layoutOf([comp("laser", "las1", 0, 100, 0, { props: { wavelength_nm: 532, beam_diameter_mm: 1, polarization: "linear", pol_angle_deg: 0 } }), comp("lens", "len1", 200, 100, 0, { props: { focal_mm: 50 } }), comp("beam_dump", "bd1", 500, 100, 180)]);
    const s = new LayoutScene3D("dark");
    s.update(input({ layout: l }));
    assert.ok(s.scene.getObjectByName("beam envelope"));
    assert.ok(s.scene.getObjectByName("polarisation marks"));
    s.update(input({ layout: l, display: { ...DEFAULT_LAYERS, envelope: false, polarization: false } }));
    assert.equal(s.scene.getObjectByName("beam envelope"), undefined);
    assert.equal(s.scene.getObjectByName("polarisation marks"), undefined);
    s.update(input({ layout: l, display: { ...DEFAULT_LAYERS, beams: false, grid: false } }));
    assert.equal(s.scene.getObjectByName("beams")!.visible, false);
    assert.equal(s.scene.getObjectByName("grid")!.visible, false);
    s.dispose();
  });

  it("the beam the beam inspector shows has a halo", () => {
    const l = demo();
    const s = new LayoutScene3D("dark");
    const trace = traceLayoutDetailed(l);
    s.update(input({ layout: l, trace, beamPick: { tree: 0, branch: trace[0]!.branches[0]!.branch, segment: 0 } }));
    assert.equal((s.scene.getObjectByName("picked beam") as InstancedMesh).count, 1);
    s.update(input({ layout: l, trace, beamPick: null }));
    assert.equal(s.scene.getObjectByName("picked beam"), undefined);
    s.dispose();
  });
});

describe("the exports", () => {
  it("a GLB: binary glTF, metres, y up, a node per component, a mesh per beam tree", async () => {
    (globalThis as { FileReader?: unknown }).FileReader ??= class {
      result: ArrayBuffer | null = null;
      onloadend: (() => void) | null = null;
      readAsArrayBuffer(b: Blob) {
        void b.arrayBuffer().then((r) => {
          this.result = r;
          this.onloadend?.();
        });
      }
    };
    const l = demo();
    const trace = traceLayoutDetailed(l);
    const root = exportScene(l, trace, { ...DEFAULT_LAYERS }, "light");
    near(root.scale.x, 0.001);
    near(root.rotation.x, -Math.PI / 2);
    const comps = root.getObjectByName("components")!;
    assert.equal(comps.children.length, l.components.length);
    assert.ok(comps.children.every((g, i) => g.name.includes(l.components[i]!.id)));
    const lit = new Set(beamTubes(trace, l.table, "light").filter((t) => !t.dark).map((t) => t.ref.tree));
    const beamMeshes = root.getObjectByName("beams")!.children.filter((c) => c.name.startsWith("beam ") && !c.name.startsWith("beam envelope"));
    assert.equal(beamMeshes.length, lit.size);
    assert.equal(root.getObjectByName("beam envelope (1/e²)"), undefined, "no separate envelope: the Gaussian beams themselves are at their 1/e² size");
    // a point 1 m along the table's x is 1 m along glTF's x; the table's +z (up) is glTF's +y
    root.updateMatrixWorld(true);
    const up = new Vector3(0, 0, 1000).applyMatrix4(root.matrixWorld);
    near(up.y, 1, 1e-9);
    near(up.z, 0, 1e-9);
    const glb = await glbOf(root);
    const head = new DataView(glb);
    assert.equal(head.getUint32(0, true), 0x46546c67, "magic glTF");
    assert.equal(head.getUint32(4, true), 2, "version 2");
    assert.equal(head.getUint32(8, true), glb.byteLength);
    const jsonLen = head.getUint32(12, true);
    const json = JSON.parse(new TextDecoder().decode(new Uint8Array(glb, 20, jsonLen))) as { nodes: Array<{ name?: string }> };
    assert.ok(json.nodes.some((n) => n.name?.includes("(las1)")), "the laser is a named node");
  });

  it("a PNG at the scale the graphics card allows, the labels' fonts scaled with it", () => {
    assert.equal(fitScale(4, 1600, 900, 16384), 4);
    assert.equal(fitScale(4, 1600, 900, 4096), 2);
    assert.equal(fitScale(4, 5000, 900, 4096), 1);
    assert.equal(scaleFont("italic 600 11px / 16px Segoe UI, sans-serif", 2), "italic 600 22.00px Segoe UI, sans-serif");
    assert.equal(scaleFont("11px Microsoft YaHei", 4), "44.00px Microsoft YaHei");
  });
});

// ── picking: instance → component ────────────────────────────────────────────

describe("picking maps a hit instance back to its component", () => {
  const down = (x: number, y: number) => new Raycaster(new Vector3(x, y, 1000), new Vector3(0, 0, -1));

  it("many components of the same kind share instanced meshes: every hit still names the right one", () => {
    const row = Array.from({ length: 8 }, (_, i) => comp("mirror", `m${i}`, 100 + i * 75, 200, 135));
    const l = layoutOf([...row, comp("lens", "len1", 400, 400, 0), comp("detector", "d1", 600, 400, 180)]);
    const s = new LayoutScene3D("dark");
    s.update(input({ layout: l }));
    for (const c of l.components) assert.equal(s.pickComponent(down(c.x_mm, c.y_mm))?.id, c.id, c.id);
    // after a change of the set (one removed: every bucket rebuilt) and after an edit in place (one moved)
    const fewer = { ...l, components: l.components.filter((c) => c.id !== "m3") };
    s.update(input({ layout: fewer }));
    assert.equal(s.pickComponent(down(100 + 3 * 75, 200)), null, "the removed one is gone");
    for (const c of fewer.components) assert.equal(s.pickComponent(down(c.x_mm, c.y_mm))?.id, c.id, c.id);
    const moved = setPositions(fewer, new Map([["m5", { x: 475, y: 250 }]]));
    s.update(input({ layout: moved }));
    assert.equal(s.pickComponent(down(475, 250))?.id, "m5");
    assert.equal(s.pickComponent(down(100 + 5 * 75, 200)), null, "not where it was");
    s.dispose();
  });

  it("the meshes used for picking are not drawn (no second copy of the parts on the GPU)", () => {
    const s = new LayoutScene3D("dark");
    s.update(input({ layout: demo() }));
    const instanced: string[] = [];
    s.scene.traverse((o) => {
      if ((o as InstancedMesh).isInstancedMesh && o.name !== "beam tubes") instanced.push(o.name);
    });
    assert.deepEqual(instanced, [], "only the beam tubes are an instanced mesh in the scene");
    s.dispose();
  });
});

// ── labels ───────────────────────────────────────────────────────────────────

/** Just enough of a page for the labels (HTML elements) to be made in Node. */
function withFakeDocument<T>(fn: () => T): T {
  const g = globalThis as { document?: unknown; getComputedStyle?: unknown };
  const saved = { document: g.document, getComputedStyle: g.getComputedStyle };
  const page: { documentElement: object; defaultView: { Element: unknown }; createElement: () => unknown } = { documentElement: {}, defaultView: { Element: null }, createElement: () => null };
  // what CSS2DObject and the scene touch on an element; CSS2DObject's "removed" listener asks
  // element.ownerDocument.defaultView.Element and parentNode
  class FakeElement {
    style: Record<string, string> = {};
    dataset: Record<string, string> = {};
    className = "";
    textContent = "";
    innerHTML = "";
    title = "";
    parentNode = null;
    isConnected = false;
    ownerDocument = page;
    setAttribute(): void {}
    remove(): void {}
    /** a canvas (the hole texture) without a 2D context: the scene goes without the texture */
    getContext(): null {
      return null;
    }
  }
  page.defaultView.Element = FakeElement;
  page.createElement = () => new FakeElement();
  g.document = page;
  // no design tokens: the palette's own colours
  g.getComputedStyle = () => ({ getPropertyValue: () => "" });
  try {
    return fn();
  } finally {
    for (const k of ["document", "getComputedStyle"] as const) {
      if (saved[k] === undefined) delete g[k];
      else g[k] = saved[k];
    }
  }
}

describe("the labels follow their layers", () => {
  it("names, text notes, comment pins and power tags come and go with the Layers menu, at once", () => {
    withFakeDocument(() => {
      const base = demo();
      const l = { ...base, texts: [{ id: "note1", x_mm: 100, y_mm: 100, text: "注释", size: 12 }] };
      const trace = traceLayoutDetailed(l);
      const counts = new Map([[l.components[0]!.id, 2]]);
      // one selection object throughout, as the editor passes it: a new Set on every update would count as a new
      // selection and rebuild the labels anyway
      const selected = new Set<string>();
      const s = new LayoutScene3D("dark");
      const keys = (prefix: string) => s.labelScene.children.map((o) => o.name).filter((k) => k.startsWith(prefix));
      const on = { ...DEFAULT_LAYERS };
      s.update(input({ layout: l, trace, display: on, commentCounts: counts, selected }));
      assert.equal(keys("c:").length, l.components.length, "a name per component");
      assert.deepEqual(keys("t:"), ["t:note1"]);
      assert.deepEqual(keys("m:"), [`m:${l.components[0]!.id}`]);
      assert.ok(keys("p:").length > 0, "a power tag");
      // names, notes and comment pins: only the layers change (the same layout, trace and selection), so nothing but
      // the layer can bring the labels down
      s.update(input({ layout: l, trace, display: { ...on, labels: false, texts: false, comments: false }, commentCounts: counts, selected }));
      assert.deepEqual(keys("c:"), [], "the names go");
      assert.deepEqual(keys("t:"), [], "the text notes go");
      assert.deepEqual(keys("m:"), [], "the comment pins go");
      assert.ok(keys("p:").length > 0, "the power tags stay");
      s.update(input({ layout: l, trace, display: { ...on, labels: false, texts: false, comments: false, power: false }, commentCounts: counts, selected }));
      assert.deepEqual(keys("p:"), [], "the power tags go");
      s.update(input({ layout: l, trace, display: { ...on }, commentCounts: counts, selected }));
      assert.equal(keys("c:").length, l.components.length);
      assert.deepEqual(keys("t:"), ["t:note1"]);
      assert.equal(keys("m:").length, 1);
      assert.ok(keys("p:").length > 0);
      s.dispose();
    });
  });
});

// ── cleaning up ──────────────────────────────────────────────────────────────

describe("dispose frees what the scene made", () => {
  it("every geometry, material and texture of the scene, but not the shapes it shares with other scenes", () => {
    const s = new LayoutScene3D("light");
    const l = demo();
    const trace = traceLayoutDetailed(l);
    s.update(input({ layout: l, trace, selected: new Set(["m2"]), hover: "las1", beamPick: { tree: 0, branch: trace[0]!.branches[0]!.branch, segment: 0 } }));
    const owned = new Set<BufferGeometry | Material | Texture>();
    s.scene.traverse((o) => {
      const m = o as Mesh;
      if (m.geometry) owned.add(m.geometry);
      for (const x of [m.material].flat()) {
        if (!x) continue;
        owned.add(x as Material);
        const map = (x as { map?: Texture | null }).map;
        if (map) owned.add(map);
      }
    });
    const shared = new Set(SHAPES.flatMap((sh) => [shapeGeometry(sh, "high"), shapeGeometry(sh, "low")]));
    const freed = new Set<unknown>();
    for (const r of [...owned, ...shared]) r.addEventListener("dispose", () => freed.add(r));
    s.dispose();
    const left = [...owned].filter((r) => !shared.has(r as BufferGeometry) && !freed.has(r));
    assert.deepEqual(left.map((r) => `${r.constructor.name} ${(r as { name?: string }).name ?? ""}`), [], "not freed");
    assert.equal([...shared].filter((g) => freed.has(g)).length, 0, "the shared shapes stay");
    assert.ok(owned.size > 20, `${owned.size} resources`);
  });
});

// ── the GLB, read back ───────────────────────────────────────────────────────

(globalThis as { FileReader?: unknown }).FileReader ??= class {
  result: ArrayBuffer | null = null;
  onloadend: (() => void) | null = null;
  readAsArrayBuffer(b: Blob) {
    void b.arrayBuffer().then((r) => {
      this.result = r;
      this.onloadend?.();
    });
  }
};

function readBack(glb: ArrayBuffer): Promise<GLTF> {
  return new Promise((resolve, reject) => new GLTFLoader().parse(glb, "", resolve, reject));
}

describe("the GLB, read back with three's GLTFLoader", () => {
  it("in metres with y up: the table is its real size, lying flat; every component a node of its own", async () => {
    const l = demo();
    const trace = traceLayoutDetailed(l);
    const root = exportScene(l, trace, { ...DEFAULT_LAYERS }, "light");
    const gltf = await readBack(await glbOf(root));
    const scene = gltf.scene;
    scene.updateMatrixWorld(true);
    const json = gltf.parser.json as { extensionsUsed?: string[] };
    assert.ok(!(json.extensionsUsed ?? []).includes("EXT_mesh_gpu_instancing"), "plain meshes by default");
    const table = new Box3().setFromObject(scene.getObjectByName("table")!);
    const size = table.getSize(new Vector3());
    near(size.x, 1.2, 1e-6);
    near(size.y, 0.06, 1e-6);
    near(size.z, 0.9, 1e-6);
    near(table.max.y, 0, 1e-6);
    // the components: one node each, every part a mesh under it
    const comps = scene.getObjectByName("components")!;
    assert.equal(comps.children.length, l.components.length);
    let meshes = 0;
    comps.traverse((o) => {
      if ((o as Mesh).isMesh) meshes++;
    });
    const s = new LayoutScene3D("dark");
    s.update(input({ layout: l, trace }));
    assert.equal(meshes, s.nodes.reduce((n, x) => n + x.parts.length, 0));
    // a component at the beam height (75 mm) is 0.075 m up
    const laser = comps.children.find((c) => c.name.includes("(las1)"))!;
    near(new Vector3().setFromMatrixPosition(laser.matrixWorld).y, 0.075, 1e-6);
    s.dispose();
  });

  it("keeping the instances (EXT_mesh_gpu_instancing): the same parts, fewer meshes", async () => {
    const l = demo();
    const trace = traceLayoutDetailed(l);
    const gltf = await readBack(await glbOf(exportScene(l, trace, { ...DEFAULT_LAYERS }, "light", { instancing: true })));
    const json = gltf.parser.json as { extensionsUsed?: string[] };
    assert.ok((json.extensionsUsed ?? []).includes("EXT_mesh_gpu_instancing"));
    let instances = 0;
    gltf.scene.getObjectByName("components")!.traverse((o) => {
      if ((o as InstancedMesh).isInstancedMesh) instances += (o as InstancedMesh).count;
    });
    const s = new LayoutScene3D("dark");
    s.update(input({ layout: l, trace }));
    assert.equal(instances, s.nodes.reduce((n, x) => n + x.parts.length, 0));
    s.dispose();
    const size = new Box3().setFromObject(gltf.scene.getObjectByName("table")!).getSize(new Vector3());
    near(size.x, 1.2, 1e-6);
    near(size.z, 0.9, 1e-6);
  });

  it("the beams at their size: a Gaussian beam at its 1/e² radius, the others as heavy as their 2D line", async () => {
    // a collimated Ø2 mm beam and a beam without a size, side by side, along +x
    const l = layoutOf([
      comp("laser", "las1", 0, 100, 0, { props: { wavelength_nm: 532, beam_diameter_mm: 2 } }),
      comp("beam_dump", "bd1", 300, 100, 180),
      comp("laser", "las2", 0, 300, 0, { props: { wavelength_nm: 633 } }),
      comp("beam_dump", "bd2", 300, 300, 180),
    ]);
    const trace = traceLayoutDetailed(l);
    const gltf = await readBack(await glbOf(exportScene(l, trace, { ...DEFAULT_LAYERS }, "dark")));
    gltf.scene.updateMatrixWorld(true);
    const beams = gltf.scene.getObjectByName("beams")!;
    const sized = beams.children.find((c) => c.name.startsWith("beam_las1") || c.name.startsWith("beam las1"))!;
    const plain = beams.children.find((c) => c.name.startsWith("beam_las2") || c.name.startsWith("beam las2"))!;
    // in the file (metres, y up) the table's z is y: a beam along the table's x is as thick in y as it is across
    const across = (o: Object3D) => new Box3().setFromObject(o).getSize(new Vector3());
    const a = across(sized);
    // a 2 mm beam whose waist is at the laser: 2.0 mm at the start, a hair more 300 mm on
    assert.ok(a.y >= 0.002 - 1e-6 && a.y < 0.0021, `Gaussian beam ${a.y} m thick`);
    const tubes = beamTubes(trace, l.table, "dark");
    const width = tubes.find((t) => t.ref.tree === 1)!.widthPx;
    near(across(plain).y, width * GLB_BEAM_MM_PER_PX * 1e-3, 1e-6);
  });

  it("is made from the components' own description, not from what the live view draws", () => {
    const l = demo();
    const root = exportScene(l, traceLayoutDetailed(l), { ...DEFAULT_LAYERS }, "dark");
    const names: string[] = [];
    root.traverse((o) => names.push(o.name));
    // the live view's merged meshes are named after their bucket ("cyl:solid"); none of that is in the file
    assert.deepEqual(names.filter((n) => /^[a-zA-Z]+:(solid|glass)$/.test(n)), []);
    const comps = root.getObjectByName("components")!;
    assert.deepEqual(comps.children.map((c) => (c.userData as { id: string }).id), l.components.map((c) => c.id));
  });
});
