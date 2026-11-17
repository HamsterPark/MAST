// ════════════════════════════════════════════════════════════════════════════
// Tests for the interaction math of the 3D view (docs/v2/design/optical_layout_3d_view.md section 5):
// src/lib/opticalLayout/scene3d/{pick,screenPick,views,labels3d,files}.ts.
//
//     cd frontend && npm run test:unit
//
// The rules that matter: a drag moves components ACROSS the table with the 2D canvas's own rule (the lead
// component follows the pointer and is snapped with geometry.ts snapPoint in the editor's mode, Ctrl or Cmd
// switch snapping off, the others keep their offsets, locked ones stay); a click selects exactly as on the 2D
// canvas (Shift / Ctrl add and take out, a press on a selected component keeps the group, a click without a drag
// on one member of a group selects it alone), so the inspector shows the same thing whichever view is open.
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import type { Vec } from "../src/lib/opticalLayout/types.ts";
import { snapPoint, type SnapMode } from "../src/lib/opticalLayout/geometry.ts";
import { DRAG_THRESHOLD_PX, LEVEL_EPS, dragPositions, dragSnapMode, mmPerPxOrthographic, mmPerPxPerspective, rayAtHeight, selectionOnClick, selectionOnPress, startDrag } from "../src/lib/opticalLayout/scene3d/pick.ts";
import { nearestOnScreen, pickTube } from "../src/lib/opticalLayout/scene3d/screenPick.ts";
import { view3dKey } from "../src/lib/opticalLayout/scene3d/keys3d.ts";
import { LAYERS_2D_ONLY, drawnIn3d } from "../src/lib/opticalLayout/scene3d/layers3d.ts";
import { LAYER_IDS } from "../src/lib/opticalLayout/layers.ts";
import { interpretKey } from "../src/lib/opticalLayout/keys.ts";
import { VIEW_PRESETS, boxRadius, fitBoxDistance, orthoBoxSpan, presetDirection, presetPose, screenAxes } from "../src/lib/opticalLayout/scene3d/views.ts";
import { MAX_LABELS, declutter, type LabelBox } from "../src/lib/opticalLayout/scene3d/labels3d.ts";
import { canDraw3d, export3dFileName, isSoftwareRenderer, probeWebgl } from "../src/lib/opticalLayout/scene3d/files.ts";
import type { Box3, Vec3 } from "../src/lib/opticalLayout/scene3d/mat4.ts";
import { comp, layoutOf } from "./helpers/opticalLayout.ts";

const near = (a: number, b: number, tol = 1e-9) => assert.ok(Math.abs(a - b) <= tol, `${a} is not within ${tol} of ${b}`);
const nearVec = (a: Vec3, b: Vec3, tol = 1e-9) => a.forEach((v, i) => near(v, b[i]!, tol));

// ── the pointer's ray ───────────────────────────────────────────────────────

describe("where the pointer's ray meets a level plane", () => {
  it("straight and slanting rays", () => {
    nearVec(rayAtHeight([10, 20, 500], [0, 0, -1], 75)!, [10, 20, 75]);
    nearVec(rayAtHeight([0, 0, 100], [1, 1, -1], 0)!, [100, 100, 0]);
    nearVec(rayAtHeight([0, 0, -50], [0, 0.6, 0.8], 30)!, [0, 60, 30], 1e-12);
  });

  it("no point for a level ray, a plane behind the camera, or one at the horizon", () => {
    assert.equal(rayAtHeight([0, 0, 100], [1, 0, 0], 0), null);
    assert.equal(rayAtHeight([0, 0, 100], [1, 0, LEVEL_EPS / 2], 0), null);
    assert.equal(rayAtHeight([0, 0, 100], [0, 0, 1], 0), null, "the plane is behind");
    assert.equal(rayAtHeight([0, 0, 100], [1, 0, -1e-5], 0), null, "10 km away");
  });
});

// ── dragging ────────────────────────────────────────────────────────────────

describe("a drag moves components across the table, as on the 2D canvas", () => {
  const l = layoutOf([comp("mirror", "m1", 112.5, 62.5, 135), comp("lens", "l1", 200.3, 70.1, 0), comp("mirror", "m2", 300, 300, 135, { locked: true })]);
  const at = (x: number, y: number): Vec => ({ x, y });

  it("the components that move: the selected ones that are not locked", () => {
    const d = startDrag(l, ["m1", "l1", "m2"], "m1", at(110, 60), 75)!;
    assert.deepEqual([...d.origin.keys()].sort(), ["l1", "m1"]);
    assert.equal(d.planeZ, 75);
    assert.equal(startDrag(l, ["m2"], "m2", at(300, 300), 75), null, "a locked lead does not move");
    assert.equal(startDrag(l, ["m1"], "nope", at(0, 0), 75), null);
    assert.equal(startDrag(l, ["l1"], "m1", at(0, 0), 75), null, "the pressed component is not in the selection");
  });

  for (const mode of ["hole", "half", "mm"] as SnapMode[]) {
    it(`snaps the lead component the way the 2D canvas does (${mode})`, () => {
      const d = startDrag(l, ["m1", "l1"], "m1", at(110, 60), 75)!;
      for (const now of [at(150.2, 61), at(37, 333.3), at(110, 60)]) {
        const pos = dragPositions(d, now, l.table, mode);
        const want = snapPoint(l.table, { x: 112.5 + (now.x - 110), y: 62.5 + (now.y - 60) }, mode);
        near(pos.get("m1")!.x, want.x);
        near(pos.get("m1")!.y, want.y);
        // the other one keeps its offset to the lead
        near(pos.get("l1")!.x - pos.get("m1")!.x, 200.3 - 112.5, 1e-9);
        near(pos.get("l1")!.y - pos.get("m1")!.y, 70.1 - 62.5, 1e-9);
        assert.equal(pos.has("m2"), false);
      }
    });
  }

  it("follows the pointer exactly with snapping off", () => {
    const d = startDrag(l, ["m1"], "m1", at(110, 60), 75)!;
    const pos = dragPositions(d, at(123.456, 71.234), l.table, "off");
    near(pos.get("m1")!.x, 112.5 + 13.456, 1e-9);
    near(pos.get("m1")!.y, 62.5 + 11.234, 1e-9);
  });

  it("Ctrl or Cmd switch snapping off", () => {
    assert.equal(dragSnapMode("hole", {}), "hole");
    assert.equal(dragSnapMode("half", { ctrlKey: true }), "off");
    assert.equal(dragSnapMode("mm", { metaKey: true }), "off");
  });

  it("the same threshold as the 2D canvas", () => {
    assert.equal(DRAG_THRESHOLD_PX, 3);
  });
});

// ── selecting ───────────────────────────────────────────────────────────────

describe("a click selects as on the 2D canvas", () => {
  it("a press", () => {
    assert.deepEqual(selectionOnPress([], "a", false), ["a"]);
    assert.deepEqual(selectionOnPress(["b"], "a", false), ["a"], "another component replaces the selection");
    assert.deepEqual(selectionOnPress(["a", "b"], "a", false), ["a", "b"], "a selected one keeps the group, so it can be dragged");
    assert.deepEqual(selectionOnPress(["b"], "a", true), ["b", "a"], "Shift / Ctrl adds");
    assert.deepEqual(selectionOnPress(["a", "b"], "a", true), ["b"], "and takes out");
  });

  it("a release without a drag", () => {
    assert.deepEqual(selectionOnClick(["a", "b"], "a", false), ["a"], "one member of a group, alone");
    assert.equal(selectionOnClick(["a"], "a", false), null);
    assert.equal(selectionOnClick(["a", "b"], "a", true), null);
    assert.equal(selectionOnClick(["b", "c"], "a", false), null);
  });
});

// ── beams on the screen ─────────────────────────────────────────────────────

describe("the beam under the pointer, by distance on the screen", () => {
  it("the nearest within the tolerance, the earlier of two as near", () => {
    const segs = [{ ax: 0, ay: 0, bx: 100, by: 0 }, null, { ax: 0, ay: 10, bx: 100, by: 10 }, { ax: 0, ay: 10, bx: 100, by: 10 }];
    const h = nearestOnScreen(segs, 50, 7, 6)!;
    assert.equal(h.index, 2);
    near(h.t, 0.5);
    near(h.dist, 3);
    assert.equal(nearestOnScreen(segs, 50, 30, 6), null);
    assert.equal(nearestOnScreen(segs, 50, 1, 6)!.index, 0);
    near(nearestOnScreen(segs, -20, 0, 30)!.t, 0, 0);
  });
});

// ── views ───────────────────────────────────────────────────────────────────

describe("the preset views", () => {
  const box: Box3 = { min: [0, 0, -60], max: [1200, 900, 150] };

  it("look from where they say", () => {
    for (const v of VIEW_PRESETS) near(Math.hypot(...presetDirection(v)), 1, 1e-12);
    const top = presetDirection("top");
    assert.ok(top[2] > 0.9999 && top[1] < 0 && top[0] === 0, "top: down, +y up the screen");
    assert.deepEqual(presetDirection("front"), [0, -1, 0]);
    assert.deepEqual(presetDirection("side"), [1, 0, 0]);
    const iso = presetDirection("iso");
    near(Math.asin(iso[2]) * (180 / Math.PI), 35.26438968, 1e-6);
    assert.ok(iso[0] < 0 && iso[1] < 0, "from the front left");
  });

  const corners = (b: Box3): Vec3[] => [0, 1, 2, 3, 4, 5, 6, 7].map((i) => [i & 1 ? b.max[0] : b.min[0], i & 2 ? b.max[1] : b.min[1], i & 4 ? b.max[2] : b.min[2]]);
  const dot = (a: Vec3, b: Vec3) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];

  it("hold the whole box, and no more than they must (every corner inside, one on the edge)", () => {
    for (const aspect of [0.5, 1, 2.5]) {
      for (const v of VIEW_PRESETS) {
        const p = presetPose(v, box, 40, aspect);
        nearVec(p.target, [600, 450, 45]);
        const dir = presetDirection(v);
        const { right, up } = screenAxes(dir);
        const tv = Math.tan((40 * Math.PI) / 360);
        const th = tv * aspect;
        let worst = 0;
        for (const c of corners(box)) {
          const rel: Vec3 = [c[0] - p.position[0], c[1] - p.position[1], c[2] - p.position[2]];
          const depth = -dot(rel, dir);
          assert.ok(depth > 0, `${v}: a corner behind the camera`);
          const fill = Math.max(Math.abs(dot(rel, right)) / (depth * th), Math.abs(dot(rel, up)) / (depth * tv));
          assert.ok(fill <= 1 / 1.06 + 1e-9, `${v} at ${aspect}: a corner at ${fill} of the half picture`);
          worst = Math.max(worst, fill);
        }
        near(worst, 1 / 1.06, 1e-6);
        const half = Math.min((20 * Math.PI) / 180, Math.atan(Math.tan((20 * Math.PI) / 180) * aspect));
        assert.ok(fitBoxDistance(box, dir, 40, aspect) <= (1.06 * boxRadius(box)) / Math.sin(half) + 1e-9, `${v} at ${aspect}: looser than the sphere round the box`);
      }
    }
  });

  it("the screen's axes: top has +x right and +y up, front +x right and z up, side +y right", () => {
    const top = screenAxes(presetDirection("top"));
    nearVec(top.right, [1, 0, 0], 1e-6);
    nearVec(top.up, [0, 1, 0], 1e-3);
    nearVec(screenAxes(presetDirection("front")).right, [1, 0, 0]);
    nearVec(screenAxes(presetDirection("front")).up, [0, 0, 1]);
    nearVec(screenAxes(presetDirection("side")).right, [0, 1, 0]);
  });

  it("an orthographic view of the whole box", () => {
    // looking straight down at a 1200 x 900 table on a 2:1 view: 900 tall would do, 1200 / 2 = 600 is less
    near(orthoBoxSpan({ min: [0, 0, 0], max: [1200, 900, 0] }, [0, 0, 1], 2, 1), 900, 1e-6);
    near(orthoBoxSpan({ min: [0, 0, 0], max: [1200, 900, 0] }, [0, 0, 1], 1, 1), 1200, 1e-6);
  });

  it("millimetres per pixel", () => {
    near(mmPerPxPerspective(1000, 90, 500), 4, 1e-12);
    near(mmPerPxOrthographic(800, 400), 2);
  });
});

// ── labels ──────────────────────────────────────────────────────────────────

describe("which labels are shown", () => {
  const box = (id: string, x: number, y: number, priority = 0, depth = 1): LabelBox => ({ id, x, y, w: 60, h: 16, priority, depth });

  it("of two that overlap, the nearer; the selected one always", () => {
    const keep = declutter([box("far", 100, 100, 0, 9), box("near", 110, 104, 0, 1), box("apart", 400, 100)], 800, 600);
    assert.deepEqual([...keep].sort(), ["apart", "near"]);
    const sel = declutter([box("near", 110, 104, 0, 1), box("sel", 100, 100, 2, 9)], 800, 600);
    assert.ok(sel.has("sel"));
    assert.ok(!sel.has("near"));
    const both = declutter([box("sel", 100, 100, 2), box("hover", 102, 101, 1)], 800, 600);
    assert.deepEqual([...both].sort(), ["hover", "sel"], "the selected and the hovered ones are both shown");
  });

  it("not off the view, and not more than the cap", () => {
    assert.equal(declutter([box("out", -200, 100)], 800, 600).size, 0);
    const many = Array.from({ length: 400 }, (_, i) => box(`l${i}`, (i % 20) * 70 + 30, Math.floor(i / 20) * 20 + 10));
    const keep = declutter(many, 1400, 400);
    assert.ok(keep.size <= MAX_LABELS);
    assert.ok(keep.size > 100);
  });
});

// ── files, WebGL ────────────────────────────────────────────────────────────

describe("export file names and the WebGL check", () => {
  it("names", () => {
    assert.equal(export3dFileName({ id: "shg-demo", revision: 3 }, "png"), "shg-demo-r3-3d.png");
    assert.equal(export3dFileName({ id: "shg-demo", revision: 3 }, "png", true, 2), "shg-demo-r3-unsaved-3d@2x.png");
    assert.equal(export3dFileName({ id: "a b/c", revision: 0 }, "glb"), "a_b_c-r0-3d.glb");
    assert.equal(export3dFileName({ id: "big", revision: 7 }, "glb", false, 1, "-instanced"), "big-r7-3d-instanced.glb");
    assert.equal(export3dFileName({ id: "big", revision: 7 }, "glb", false, 1, "/../x"), "big-r7-3dx.glb", "a suffix is cleaned too");
  });

  it("WebGL 2 or a message", () => {
    let lost = 0;
    assert.equal(canDraw3d(() => ({ getContext: () => ({ getExtension: () => ({ loseContext: () => lost++ }) }) })), true);
    assert.equal(lost, 1, "the test context is given back");
    assert.equal(canDraw3d(() => ({ getContext: () => null })), false);
    assert.equal(canDraw3d(() => null), false);
    const UNMASKED = 0x9246;
    const named = probeWebgl(() => ({ getContext: () => ({ getExtension: (n: string) => (n === "WEBGL_debug_renderer_info" ? { UNMASKED_RENDERER_WEBGL: UNMASKED } : {}), getParameter: (p: number) => (p === UNMASKED ? "ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero)), SwiftShader driver)" : null) }) }));
    assert.equal(named.ok, true);
    assert.match(named.renderer, /SwiftShader/);
    assert.equal(probeWebgl(() => ({ getContext: () => ({}) })).renderer, "", "a browser that does not say");
    assert.equal(
      canDraw3d(() => ({
        getContext: () => {
          throw new Error("blocked");
        },
      })),
      false,
    );
  });

  it("a software renderer gets the cheap settings", () => {
    for (const n of ["ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero) (0x0000C0DE)), SwiftShader driver)", "llvmpipe (LLVM 15.0.7, 256 bits)", "ANGLE (Microsoft, Microsoft Basic Render Driver Direct3D11 vs_5_0 ps_5_0, D3D11)"]) assert.equal(isSoftwareRenderer(n), true, n);
    for (const n of ["ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Direct3D11 vs_5_0 ps_5_0, D3D11)", "ANGLE (Intel, Intel(R) UHD Graphics 770 Direct3D11 vs_5_0 ps_5_0, D3D11)", "Apple M2", ""]) assert.equal(isSoftwareRenderer(n), false, n);
  });
});

// ── the keys of the 3D view ─────────────────────────────────────────────────

describe("the keys of the 3D view", () => {
  it("bare digits pick the views; with a modifier they are the browser's", () => {
    assert.deepEqual(view3dKey({ key: "1" }), { kind: "preset", preset: "top" });
    assert.deepEqual(view3dKey({ key: "2" }), { kind: "preset", preset: "front" });
    assert.deepEqual(view3dKey({ key: "3" }), { kind: "preset", preset: "side" });
    assert.deepEqual(view3dKey({ key: "4" }), { kind: "preset", preset: "iso" });
    assert.deepEqual(view3dKey({ key: "0" }), { kind: "fit" });
    assert.deepEqual(view3dKey({ key: "5" }), { kind: "projection" });
    for (const mod of [{ ctrlKey: true }, { metaKey: true }, { altKey: true }, { shiftKey: true }]) {
      for (const key of ["1", "2", "3", "4", "0", "5"]) assert.equal(view3dKey({ key, ...mod }), null, `${JSON.stringify(mod)} ${key}`);
    }
    assert.equal(view3dKey({ key: "6" }), null);
    assert.equal(view3dKey({ key: "v" }), null, "V is the editor's (keys.ts)");
  });

  it("the editor's keys and the view's do not overlap", () => {
    for (const key of ["0", "1", "2", "3", "4", "5"]) assert.equal(interpretKey({ key }), null, key);
    assert.deepEqual(interpretKey({ key: "v" }), { kind: "view3d" });
  });
});

// ── the layers in 3D ────────────────────────────────────────────────────────

describe("the layers the 3D view draws", () => {
  it("all but the rulers and the alignment read-outs", () => {
    assert.deepEqual([...LAYERS_2D_ONLY].sort(), ["alignment", "rulers"]);
    for (const id of LAYER_IDS) assert.equal(drawnIn3d(id), id !== "rulers" && id !== "alignment", id);
  });
});

// ── picking a beam tube ─────────────────────────────────────────────────────

describe("the beam tube under the pointer", () => {
  // a top view, 2 px per mm: (x, y) on the table is (2x, 600 - 2y) on the screen
  const top = (p: Vec3): [number, number] => [2 * p[0], 600 - 2 * p[1]];
  const tubes = [
    { a: [0, 100, 75] as Vec3, b: [200, 100, 75] as Vec3, joint: false, name: "first" },
    { a: [200, 100, 75] as Vec3, b: [200, 100, 160] as Vec3, joint: true, name: "joint" },
    { a: [200, 100, 160] as Vec3, b: [200, 300, 160] as Vec3, joint: false, name: "up" },
  ];

  it("maps the screen back to the tube and the point on it", () => {
    const hit = pickTube(tubes, top, 2 * 150, 600 - 2 * 101, 6)!;
    assert.equal(hit.tube.name, "first");
    assert.equal(hit.index, 0);
    nearVec(hit.point, [150, 100, 75], 1e-9);
    const up = pickTube(tubes, top, 2 * 199, 600 - 2 * 250, 6)!;
    assert.equal(up.tube.name, "up");
    nearVec(up.point, [200, 250, 160], 1e-9);
  });

  it("never picks a vertical joint, a tube behind the camera, or one too far away", () => {
    // seen from the side (x across, z up) the joint is a line of its own, from z 75 to z 160 at x 200: a click on its
    // middle is 80 px and more from the two tubes it joins, so nothing is picked
    const side = (p: Vec3): [number, number] => [2 * p[0], 600 - 2 * p[2]];
    assert.equal(pickTube(tubes, side, 400, 600 - 2 * 117, 6), null);
    assert.equal(pickTube(tubes, side, 300, 600 - 2 * 75, 6)!.tube.name, "first", "the tube beside it is still picked");
    const behind = (p: Vec3): [number, number] | null => (p[1] > 200 ? null : top(p));
    assert.equal(pickTube(tubes, behind, 2 * 199, 600 - 2 * 250, 6), null);
    assert.equal(pickTube(tubes, top, 100, 100, 6), null);
  });
});
