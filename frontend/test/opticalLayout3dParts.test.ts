// ════════════════════════════════════════════════════════════════════════════
// Tests for the 3D view's layout → scene mapping (docs/v2/design/optical_layout_3d_view.md sections 2 and 3):
// src/lib/opticalLayout/scene3d/{heights,mat4,parts,scene,table3d}.ts.
//
//     cd frontend && npm run test:unit
//
// What is pinned: where heights come from (the contract of docs/v2/design/optical_layout_3d.md, with every key
// optional), how a component is placed and turned (its local x along angle_deg, as in 2D), that every one of the
// 30 types has a 3D shape whose active surface is where the 2D drawing and the tracer put it (x = 0, the whole
// aperture), that the stands reach the table, and that the parts of any number of components go into a fixed
// number of instanced meshes.
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { COMPONENT_TYPES, type ComponentType, type LayoutComponent, type Segment } from "../src/lib/opticalLayout/types.ts";
import { CATALOG } from "../src/lib/opticalLayout/catalog.ts";
import { newComponent } from "../src/lib/opticalLayout/edit.ts";
import { filterHue, ndDensity } from "../src/lib/opticalLayout/glyphs.ts";
import { DEFAULT_BEAM_HEIGHT_MM, componentHeight, eventHeight, periscopeOutHeight, segmentHeight, tableBeamHeight } from "../src/lib/opticalLayout/scene3d/heights.ts";
import { applyDir, applyPoint, compose, emptyBox, growBox, multiply, rotation3, unitCubeCorners, type Box3, type Vec3 } from "../src/lib/opticalLayout/scene3d/mat4.ts";
import { MATERIALS, SHAPES, partMatrix, partsBox, partsOf, type Part } from "../src/lib/opticalLayout/scene3d/parts.ts";
import { componentFrame, componentNode, instanceBuckets, outlineEdges, sceneComponents, worldBox } from "../src/lib/opticalLayout/scene3d/scene.ts";
import { gridLines, gridStep, holePatch, sceneBox } from "../src/lib/opticalLayout/scene3d/table3d.ts";
import { comp, layoutOf } from "./helpers/opticalLayout.ts";

const EPS = 1e-9;
const near = (a: number, b: number, tol = 1e-9) => assert.ok(Math.abs(a - b) <= tol, `${a} is not within ${tol} of ${b}`);
const nearVec = (a: Vec3, b: Vec3, tol = 1e-9) => a.forEach((v, i) => near(v, b[i]!, tol));
const TABLE = layoutOf([]).table;

function boxOf(p: Part): Box3 {
  const b = emptyBox();
  for (const q of unitCubeCorners(partMatrix(p))) growBox(b, q);
  return b;
}
const contains = (b: Box3, p: Vec3, tol = EPS) => p.every((v, i) => v >= b.min[i]! - tol && v <= b.max[i]! + tol);
const fresh = (type: ComponentType, props?: Record<string, unknown>, label?: string) =>
  newComponent(type, `${CATALOG[type].prefix}1`, { x: 300, y: 200 }, { ...(props ? { props } : {}), ...(label ? { label } : {}) });
const ctx = (z = DEFAULT_BEAM_HEIGHT_MM, zOut = z) => ({ z, zOut });

// ── heights ─────────────────────────────────────────────────────────────────

describe("heights: every key optional, absent means the table's beam height, 75 mm by default", () => {
  it("the table's beam height", () => {
    assert.equal(DEFAULT_BEAM_HEIGHT_MM, 75);
    assert.equal(tableBeamHeight(TABLE), 75);
    assert.equal(tableBeamHeight({ ...TABLE, beam_height_mm: 100 } as typeof TABLE), 100);
    for (const bad of [null, "100", Number.NaN, Infinity, undefined]) assert.equal(tableBeamHeight({ ...TABLE, beam_height_mm: bad } as unknown as typeof TABLE), 75, String(bad));
  });

  it("a component: props.z_mm, else the table's", () => {
    const t = { ...TABLE, beam_height_mm: 120 } as typeof TABLE;
    assert.equal(componentHeight({ props: {} }, TABLE), 75);
    assert.equal(componentHeight({ props: {} }, t), 120);
    assert.equal(componentHeight({ props: { z_mm: 40 } }, t), 40);
    assert.equal(componentHeight({ props: { z_mm: 0 } }, t), 0, "0 is a height, not a missing one");
    assert.equal(componentHeight({ props: { z_mm: "40" } }, t), 120);
  });

  it("a periscope's output: props.z_out_mm, else its own height (no change)", () => {
    assert.equal(periscopeOutHeight({ props: { z_mm: 60, z_out_mm: 150 } }, TABLE), 150);
    assert.equal(periscopeOutHeight({ props: { z_mm: 60 } }, TABLE), 60);
    assert.equal(periscopeOutHeight({ props: {} }, { ...TABLE, beam_height_mm: 90 } as typeof TABLE), 90);
  });

  it("a segment: segment.z_mm, else the table's (not the component's)", () => {
    const seg: Segment = { x0: 0, y0: 0, x1: 1, y1: 0 };
    assert.equal(segmentHeight(seg, TABLE), 75);
    assert.equal(segmentHeight(seg, { ...TABLE, beam_height_mm: 110 } as typeof TABLE), 110);
    assert.equal(segmentHeight({ ...seg, z_mm: 33 } as Segment, { ...TABLE, beam_height_mm: 110 } as typeof TABLE), 33);
    assert.equal(segmentHeight({ ...seg, z_mm: null } as unknown as Segment, TABLE), 75);
    assert.equal(eventHeight({ z_mm: 12 }), 12);
    assert.equal(eventHeight({}), null);
  });

  it("the components sit at their height in the scene", () => {
    const l = layoutOf([comp("mirror", "m1", 100, 100, 135, { props: {} }), { ...comp("mirror", "m2", 200, 100, 135), props: { z_mm: 140 } }], { beam_height_mm: 90 } as Partial<typeof TABLE>);
    const [a, b] = sceneComponents(l);
    assert.equal(a!.z, 90);
    assert.equal(b!.z, 140);
    nearVec(applyPoint(a!.frame, [0, 0, 0]), [100, 100, 90]);
    nearVec(applyPoint(b!.frame, [0, 0, 0]), [200, 100, 140]);
  });
});

// ── placing and turning ─────────────────────────────────────────────────────

describe("a component's frame: local x along angle_deg, local y along its face, z up", () => {
  for (const deg of [0, 45, 90, 135, 210, 300]) {
    it(`${deg}°`, () => {
      const f = componentFrame({ x_mm: 10, y_mm: -20, angle_deg: deg }, 75);
      const t = (deg * Math.PI) / 180;
      nearVec(applyPoint(f, [0, 0, 0]), [10, -20, 75]);
      nearVec(applyDir(f, [1, 0, 0]), [Math.cos(t), Math.sin(t), 0]);
      nearVec(applyDir(f, [0, 1, 0]), [-Math.sin(t), Math.cos(t), 0]);
      nearVec(applyDir(f, [0, 0, 1]), [0, 0, 1]);
    });
  }

  it("rotation3 is Rz·Ry·Rx and compose is T·R·S", () => {
    nearVec(applyDir(compose([0, 0, 0], rotation3asVec(0, 0, Math.PI / 2), [1, 1, 1]), [1, 0, 0]), [0, 1, 0]);
    nearVec(applyDir(compose([0, 0, 0], rotation3asVec(Math.PI / 2, 0, 0), [1, 1, 1]), [0, 1, 0]), [0, 0, 1], 1e-12);
    nearVec(applyDir(compose([0, 0, 0], rotation3asVec(0, Math.PI / 2, 0), [1, 1, 1]), [0, 0, 1]), [1, 0, 0], 1e-12);
    // x first, then y: (0, 1, 0) -Rx(90°)-> (0, 0, 1) -Ry(90°)-> (1, 0, 0)
    nearVec(applyDir(compose([0, 0, 0], [Math.PI / 2, Math.PI / 2, 0], [1, 1, 1]), [0, 1, 0]), [1, 0, 0], 1e-12);
    const m = compose([1, 2, 3], [0.3, -0.4, 1.1], [2, 3, 4]);
    nearVec(applyPoint(m, [0.5, 0, 0]), add(applyDir(compose([0, 0, 0], [0.3, -0.4, 1.1], [1, 1, 1]), [1, 0, 0]), [1, 2, 3]), 1e-12);
    const r = rotation3(0.3, -0.4, 1.1);
    nearVec([r[0]!, r[3]!, r[6]!], applyDir(compose([0, 0, 0], [0.3, -0.4, 1.1], [1, 1, 1]), [1, 0, 0]), 1e-12);
    // multiply applies b first
    const ab = multiply(compose([5, 0, 0], undefined, [1, 1, 1]), compose([0, 0, 0], [0, 0, Math.PI / 2], [1, 1, 1]));
    nearVec(applyPoint(ab, [1, 0, 0]), [5, 1, 0]);
  });
});

function rotation3asVec(rx: number, ry: number, rz: number): Vec3 {
  return [rx, ry, rz];
}
function add(a: Vec3, b: Vec3): Vec3 {
  return [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
}

// ── the shapes of the 30 types ──────────────────────────────────────────────

const SOURCES: ComponentType[] = ["laser", "fiber_out", "lamp", "led"];
/** the active surface is the FRONT of the part: it ends at x = 0 (mirrors, receivers, blockers, a fibre input) */
const FRONT_AT_ZERO: ComponentType[] = ["mirror", "grating", "dichroic", "sample", "detector", "camera", "spectrometer", "power_meter", "beam_dump", "blocker", "fiber_in"];
/** the active surface is the MID-PLANE of the part: it is centred on x = 0 (transmitting and splitting optics) */
const CENTRED: ComponentType[] = ["beam_splitter", "pbs", "lens", "objective", "waveplate", "polarizer", "filter", "window", "nd", "aotf", "prism", "periscope", "iris", "shutter", "custom"];

describe("every type has a 3D shape", () => {
  it("the lists above name all 30 types once", () => {
    assert.deepEqual([...SOURCES, ...FRONT_AT_ZERO, ...CENTRED].sort(), [...COMPONENT_TYPES].sort());
    assert.equal(COMPONENT_TYPES.length, 30);
  });

  for (const type of COMPONENT_TYPES) {
    it(`${type}: valid parts`, () => {
      const parts = partsOf(fresh(type), ctx());
      assert.ok(parts.length >= 2, `${type} has ${parts.length} parts`);
      for (const p of parts) {
        assert.ok((SHAPES as readonly string[]).includes(p.shape), p.shape);
        assert.ok((MATERIALS as readonly string[]).includes(p.mat), p.mat);
        for (const v of [...p.at, ...p.size, ...(p.rot ?? [])]) assert.ok(Number.isFinite(v), `${type}/${p.name}`);
        for (const v of p.size) assert.ok(v > 0, `${type}/${p.name} has a size of ${v}`);
        assert.ok(p.name.length > 0);
      }
    });
  }

  for (const type of [...FRONT_AT_ZERO, ...CENTRED]) {
    it(`${type}: one face, at x = 0, across the whole aperture`, () => {
      const c = fresh(type);
      const faces = partsOf(c, ctx()).filter((p) => p.role === "face");
      assert.equal(faces.length, 1, `${type} has ${faces.length} faces`);
      const b = boxOf(faces[0]!);
      const h = c.aperture_mm / 2;
      assert.ok(contains(b, [0, -h, 0]) && contains(b, [0, h, 0]), `${type}: the face ${JSON.stringify(b)} does not hold the segment x = 0, |y| <= ${h}`);
      if (FRONT_AT_ZERO.includes(type)) near(b.max[0], 0, 1e-9);
      else near(b.min[0] + b.max[0], 0, 1e-9);
    });
  }

  for (const type of SOURCES) {
    it(`${type}: one port, at the emission point`, () => {
      const parts = partsOf(fresh(type), ctx());
      assert.equal(parts.filter((p) => p.role === "face").length, 0);
      const ports = parts.filter((p) => p.role === "port");
      assert.equal(ports.length, 1);
      const b = boxOf(ports[0]!);
      assert.ok(contains(b, [0, 0, 0]), JSON.stringify(b));
      if (type !== "lamp") near(b.max[0], 0, 1e-9);
    });
  }

  for (const type of COMPONENT_TYPES) {
    it(`${type}: stands on the table at any beam height`, () => {
      for (const z of [40, 75, 160]) {
        const parts = partsOf(fresh(type), ctx(z));
        const lowest = Math.min(...parts.map((p) => boxOf(p).min[2]));
        near(lowest, -z, 1e-9);
        // and nothing but the stand reaches below the part it carries
        for (const p of parts) if (p.role !== "stand") assert.ok(boxOf(p).min[2] > -z, `${type}/${p.name} goes into the table`);
      }
    });
  }

  it("the size follows the aperture", () => {
    const small = boxOf(partsOf({ ...fresh("mirror"), aperture_mm: 12.7 }, ctx()).find((p) => p.role === "face")!);
    const big = boxOf(partsOf({ ...fresh("mirror"), aperture_mm: 50.8 }, ctx()).find((p) => p.role === "face")!);
    near(small.max[1] - small.min[1], 12.7, 1e-9);
    near(big.max[1] - big.min[1], 50.8, 1e-9);
  });

  it("a lens is bi-convex, and bi-concave for a negative focal length", () => {
    const shape = (props: Record<string, unknown>) => partsOf(fresh("lens", props), ctx()).find((p) => p.role === "face")!.shape;
    assert.equal(shape({}), "lensVex");
    assert.equal(shape({ focal_mm: 100 }), "lensVex");
    assert.equal(shape({ focal_mm: -50 }), "lensCave");
  });

  it("a wave plate's and a polariser's axis is drawn where it points: from local +y towards straight up", () => {
    for (const [type, key] of [["waveplate", "fast_axis_deg"], ["polarizer", "axis_deg"]] as const) {
      for (const deg of [0, 30, 90, 135]) {
        const bar = partsOf(fresh(type, { [key]: deg, ...(type === "waveplate" ? { retardance: "half" } : {}) }), ctx()).find((p) => p.name === "axis")!;
        const t = (deg * Math.PI) / 180;
        const along = applyDir(compose([0, 0, 0], bar.rot, [1, 1, 1]), [0, 1, 0]);
        nearVec(along, [0, Math.cos(t), Math.sin(t)], 1e-12);
      }
    }
  });

  it("an ND filter is as grey as on the 2D canvas, a filter has the colour of its pass band", () => {
    const nd = partsOf(fresh("nd", { od: 2 }), ctx()).find((p) => p.role === "face")!;
    assert.equal(nd.tint, "nd");
    assert.equal(nd.density, ndDensity(2));
    const f = fresh("filter", { pass_min_nm: 500, pass_max_nm: 560 });
    const plate = partsOf(f, ctx()).find((p) => p.role === "face")!;
    assert.equal(plate.tint, "wl");
    assert.equal(plate.wl, filterHue(f));
  });

  it("an iris opens as far as open_mm says", () => {
    for (const open of [4, 10, 20]) {
      const c = fresh("iris", { open_mm: open });
      const blades = partsOf(c, ctx()).filter((p) => p.name === "iris blade");
      assert.equal(blades.length, 6);
      for (const b of blades) near(Math.hypot(b.at[1], b.at[2]) - b.size[1] / 2, open / 2, 1e-9);
    }
  });

  it("a shutter's blade covers the aperture when closed and is beside it when open", () => {
    const blade = (closed: boolean) => boxOf(partsOf(fresh("shutter", { closed }), ctx()).find((p) => p.name.startsWith("blade"))!);
    const h = CATALOG.shutter.defaultAperture / 2;
    assert.ok(contains(blade(true), [0, 0, 0]));
    assert.ok(blade(false).min[1] > h, "the open blade is clear of the aperture");
  });

  it("a sample for STM / TERS work has a tip that points at its face", () => {
    assert.equal(partsOf(fresh("sample"), ctx()).some((p) => p.name === "tip"), false);
    const tip = partsOf(fresh("sample", { reflects: false }, "TERS sample"), ctx()).find((p) => p.name === "tip")!;
    assert.equal(tip.shape, "cone");
    // the cone's apex (canonical +z) is the end nearest the face
    const apex = applyPoint(partMatrix(tip), [0, 0, 0.5]);
    near(apex[0], 0.25, 1e-9);
  });

  it("a periscope that does not change the height is a housing; one that does has two parallel mirrors at its two heights", () => {
    const c = fresh("periscope");
    assert.deepEqual(partsOf(c, ctx(75, 75)).filter((p) => p.name.includes("mirror")), []);
    for (const [zOut, normal] of [[175, [1, 0, -1]], [25, [1, 0, 1]]] as const) {
      const parts = partsOf(c, ctx(75, zOut));
      const mirrors = parts.filter((p) => p.name === "input mirror" || p.name === "output mirror");
      assert.equal(mirrors.length, 2);
      near(mirrors.find((m) => m.name === "input mirror")!.at[2], 0);
      near(mirrors.find((m) => m.name === "output mirror")!.at[2], zOut - 75);
      for (const m of mirrors) {
        const n = applyDir(compose([0, 0, 0], m.rot, [1, 1, 1]), [1, 0, 0]);
        nearVec(n, [normal[0] / Math.SQRT2, normal[1] / Math.SQRT2, normal[2] / Math.SQRT2], 1e-12);
      }
      // the post reaches from the table up past the higher mirror
      const post = parts.find((p) => p.name === "periscope post")!;
      near(boxOf(post).min[2], -75, 1e-9);
      assert.ok(boxOf(post).max[2] >= Math.max(0, zOut - 75));
    }
  });

  it("a type this build does not know still gets a plate with a face", () => {
    const odd = { ...fresh("mirror"), type: "hologram" } as unknown as LayoutComponent;
    const parts = partsOf(odd, ctx());
    assert.equal(parts.filter((p) => p.role === "face").length, 1);
  });
});

// ── the scene ───────────────────────────────────────────────────────────────

describe("the components in the scene", () => {
  const all = layoutOf(COMPONENT_TYPES.map((t, i) => comp(t, `${CATALOG[t].prefix}${i}`, 50 + (i % 6) * 120, 50 + Math.floor(i / 6) * 120, (i * 37) % 360)));

  it("every part of every component is one instance, its pose the frame times the part's", () => {
    const nodes = sceneComponents(all);
    const buckets = instanceBuckets(nodes);
    let n = 0;
    for (const [key, list] of buckets) {
      for (const inst of list) {
        n++;
        const node = nodes.find((x) => x.id === inst.id)!;
        const part = node.parts[inst.part]!;
        assert.equal(key, `${part.shape}:${part.mat}`);
        const want = multiply(node.frame, partMatrix(part));
        inst.matrix.forEach((v, i) => near(v, want[i]!, 1e-9));
        nearVec(applyPoint(inst.matrix, [0, 0, 0]), applyPoint(node.frame, part.at), 1e-9);
      }
    }
    assert.equal(n, nodes.reduce((s, x) => s + x.parts.length, 0));
  });

  it("however many components, at most one instanced mesh per (shape, material)", () => {
    const many = layoutOf(Array.from({ length: 400 }, (_, i) => comp(COMPONENT_TYPES[i % 30]!, `c${i}`, (i % 20) * 60, Math.floor(i / 20) * 45, i % 360)));
    const buckets = instanceBuckets(sceneComponents(many));
    assert.ok(buckets.size <= SHAPES.length * MATERIALS.length, `${buckets.size} meshes`);
    assert.ok([...buckets.values()].reduce((s, l) => s + l.length, 0) > 2000);
  });

  it("the outline goes round the component (without its stand), turned with it", () => {
    const n = componentNode(comp("mirror", "m1", 500, 400, 135), TABLE);
    const edges = outlineEdges(n, 0);
    assert.equal(edges.length, 12);
    const wb = worldBox(n);
    for (const [a, b] of edges) for (const p of [a, b]) assert.ok(contains(wb, p, 1e-9));
    // the stand is left out: the outline does not reach the table
    assert.ok(wb.min[2] > 0, `the outline reaches ${wb.min[2]}`);
    const local = partsBox(n.parts);
    near(local.max[0], 0, 1e-9);
  });

  it("the box round the scene holds the table, the components and their heights", () => {
    const l = layoutOf([{ ...comp("periscope", "per1", 1500, 950, 0), props: { z_mm: 75, z_out_mm: 300 } }]);
    const b = sceneBox(l.table, l.components);
    assert.ok(b.min[0] <= 0 && b.min[1] <= 0 && b.max[0] >= 1500 && b.max[1] >= 950);
    assert.ok(b.max[2] >= 300, "the raised output is in the box");
    assert.ok(b.min[2] <= -50, "the table's thickness is in the box");
  });
});

// ── the table ───────────────────────────────────────────────────────────────

describe("the holes and the grid", () => {
  it("the hole texture covers the holes, a hole in the middle of every tile", () => {
    const p = holePatch(TABLE)!;
    assert.deepEqual([p.x0, p.y0, p.x1, p.y1], [0, 0, 1200, 900]);
    // u counts pitches from half a pitch before the first hole: hole k is at u = k + 0.5
    for (const k of [0, 1, 10, 47]) {
      const x = TABLE.hole_offset_mm + k * TABLE.hole_pitch_mm;
      near(p.u0 + ((x - p.x0) / (p.x1 - p.x0)) * (p.u1 - p.u0), k + 0.5, 1e-9);
    }
  });

  it("an offset larger than half a pitch starts the patch at the first hole, not at the edge", () => {
    const t = { ...TABLE, hole_offset_mm: 40, hole_pitch_mm: 25 };
    const p = holePatch(t)!;
    near(p.x0, 27.5);
    near(p.u0, 0);
  });

  it("no holes: no patch", () => {
    assert.equal(holePatch({ ...TABLE, grid: "none" }), null);
    assert.equal(holePatch({ ...TABLE, hole_pitch_mm: 0 }), null);
    assert.equal(holePatch({ ...TABLE, hole_offset_mm: 2000 }), null);
  });

  it("grid lines at a round step inside the table", () => {
    assert.equal(gridStep(TABLE), 100);
    const lines = gridLines(TABLE);
    assert.equal(lines.length, 11 + 8);
    for (const [a, b] of lines) for (const p of [a, b]) assert.ok(p[0] >= 0 && p[0] <= 1200 && p[1] >= 0 && p[1] <= 900);
    assert.deepEqual(gridLines({ ...TABLE, grid: "none" }), []);
  });
});
