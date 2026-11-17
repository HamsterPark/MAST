// The GLB exporter warns ("Creating normalized normal attribute from the non-normalized one") for any normal that is
// not unit length; a beam pinched to a point (a ring of radius 0) used to leave zero normals. Pinned: every normal of
// the export scene is unit length, and exporting says nothing to the console.

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import type { BufferGeometry, Object3D } from "three";
import { DEFAULT_LAYERS } from "../src/lib/opticalLayout/layers.ts";
import { traceLayoutDetailed } from "../src/lib/opticalLayout/trace.ts";
import { demo } from "../src/lib/opticalLayout/devMock.ts";
import { beamSurface, exportScene, glbOf } from "../src/components/opticallayout/view3d/exporters.ts";

(globalThis as { FileReader?: unknown }).FileReader ??= class {
  onloadend: (() => void) | null = null;
  result: unknown = null;
  readAsArrayBuffer(b: Blob): void {
    void b.arrayBuffer().then((r) => {
      this.result = r;
      this.onloadend?.();
    });
  }
  readAsDataURL(): void {}
};

function badNormals(root: Object3D): string[] {
  const bad: string[] = [];
  root.traverse((o) => {
    const g = (o as { geometry?: BufferGeometry }).geometry;
    const n = g?.getAttribute("normal");
    if (!n) return;
    for (let i = 0; i < n.count; i++) {
      if (Math.abs(Math.hypot(n.getX(i), n.getY(i), n.getZ(i)) - 1) > 0.0005) {
        bad.push(o.name);
        return;
      }
    }
  });
  return bad;
}

describe("GLB normals", () => {
  it("a beam surface pinched to a point still has unit normals", () => {
    const g = beamSurface([0, 0, 0], [1, 0, 0], [[0, 0], [10, 1], [20, 0]]);
    const n = g.getAttribute("normal");
    for (let i = 0; i < n.count; i++) assert.ok(Math.abs(Math.hypot(n.getX(i), n.getY(i), n.getZ(i)) - 1) < 1e-6, `vertex ${i}`);
  });

  it("every normal of the demo's export scene is unit length, and the export warns of nothing", async () => {
    const l = demo();
    const root = exportScene(l, traceLayoutDetailed(l), { ...DEFAULT_LAYERS }, "light");
    assert.deepEqual(badNormals(root), []);
    const warnings: unknown[][] = [];
    const warn = console.warn;
    console.warn = (...a: unknown[]) => void warnings.push(a);
    try {
      await glbOf(root);
    } finally {
      console.warn = warn;
    }
    assert.deepEqual(warnings, []);
  });
});
