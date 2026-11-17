// ════════════════════════════════════════════════════════════════════════════
// A lost WebGL context (src/components/opticallayout/view3d/sceneHolder.ts): the scene's GPU objects are invalid
// from then on, so nothing may call dispose() on them (the browser warns "delete: object does not belong to this
// context" for each); after the restore a new scene is built from the last input.
//
//     cd frontend && npm run test:unit
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import type { BufferGeometry, Material, Mesh, Object3D } from "three";
import { DEFAULT_LAYERS } from "../src/lib/opticalLayout/layers.ts";
import { traceLayoutDetailed } from "../src/lib/opticalLayout/trace.ts";
import { demo } from "../src/lib/opticalLayout/devMock.ts";
import { LayoutScene3D, type SceneInput } from "../src/components/opticallayout/view3d/LayoutScene3D.ts";
import { SceneHolder, type HeldScene } from "../src/components/opticallayout/view3d/sceneHolder.ts";

class FakeScene implements HeldScene {
  static made: FakeScene[] = [];
  updates: SceneInput[] = [];
  disposed = 0;
  abandoned = 0;
  detail: "high" | "low" = "high";
  constructor() {
    FakeScene.made.push(this);
  }
  update(i: SceneInput): void {
    this.updates.push(i);
  }
  setDetail(d: "high" | "low"): void {
    this.detail = d;
  }
  getDetail(): "high" | "low" {
    return this.detail;
  }
  dispose(): void {
    this.disposed++;
  }
  abandon(): void {
    this.abandoned++;
  }
}

const layout = demo();
const INPUT: SceneInput = { layout, trace: traceLayoutDetailed(layout), display: { ...DEFAULT_LAYERS }, theme: "dark", selected: new Set(), hover: null, beamPick: null, commentCounts: undefined, interactive: true };

describe("the scene across a lost context", () => {
  it("a lost context abandons the old scene (never disposes it); the restore builds a new one from the last input", () => {
    FakeScene.made = [];
    const h = new SceneHolder(() => new FakeScene());
    h.update(INPUT);
    h.current.setDetail("low");
    const old = h.current;
    h.contextLost();
    assert.equal(old.abandoned, 1);
    assert.equal(old.disposed, 0);
    h.contextRestored();
    assert.notEqual(h.current, old, "a new scene");
    assert.equal(FakeScene.made.length, 2);
    const fresh = h.current as FakeScene;
    assert.deepEqual(fresh.updates, [INPUT], "it shows what was shown last");
    assert.equal(fresh.detail, "low", "the coarse/fine state carries over");
    assert.equal(old.disposed, 0, "the old one is still not disposed");
  });

  it("closing the view while the context is lost disposes nothing", () => {
    FakeScene.made = [];
    const h = new SceneHolder(() => new FakeScene());
    h.update(INPUT);
    const s = h.current as FakeScene;
    h.contextLost();
    h.dispose();
    assert.equal(s.disposed, 0);
  });

  it("closing a view with a restored context disposes the current scene, once, and never the old one", () => {
    FakeScene.made = [];
    const h = new SceneHolder(() => new FakeScene());
    h.update(INPUT);
    h.contextLost();
    h.contextRestored();
    const s = h.current as FakeScene;
    h.dispose();
    assert.equal(s.disposed, 1);
    assert.equal((FakeScene.made[0] as FakeScene).disposed, 0);
  });

  it("a second lost, or a restored without a loss, changes nothing", () => {
    FakeScene.made = [];
    const h = new SceneHolder(() => new FakeScene());
    h.contextRestored();
    assert.equal(FakeScene.made.length, 1);
    h.contextLost();
    h.contextLost();
    assert.equal((FakeScene.made[0] as FakeScene).abandoned, 1);
  });
});

/** Just enough of a page for the labels to be made in Node (as in opticalLayout3dScene.test.ts). */
function withFakeDocument<T>(fn: () => T): T {
  const g = globalThis as { document?: unknown; getComputedStyle?: unknown };
  const saved = { document: g.document, getComputedStyle: g.getComputedStyle };
  const page: { documentElement: object; defaultView: { Element: unknown }; createElement: () => unknown } = { documentElement: {}, defaultView: { Element: null }, createElement: () => null };
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
    getContext(): null {
      return null;
    }
  }
  page.defaultView.Element = FakeElement;
  page.createElement = () => new FakeElement();
  g.document = page;
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

/** Every geometry and material of a scene, with its dispose() counted. */
function countDisposals(root: Object3D): { calls: () => number; objects: number } {
  let n = 0;
  const seen = new Set<{ dispose: () => void }>();
  root.traverse((o) => {
    const m = o as Mesh;
    const things: Array<{ dispose: () => void } | undefined> = [m.geometry as BufferGeometry | undefined, ...(Array.isArray(m.material) ? (m.material as Material[]) : [m.material as Material | undefined])];
    for (const t of things) if (t) seen.add(t);
  });
  for (const t of seen) {
    const orig = t.dispose.bind(t);
    t.dispose = () => {
      n++;
      orig();
    };
  }
  return { calls: () => n, objects: seen.size };
}

describe("the real scene across a lost context", () => {
  it("abandon() and the holder's dispose() touch no geometry or material; dispose() of a live scene does (the control)", () => {
    withFakeDocument(() => {
      const live = new LayoutScene3D("dark");
      live.update(INPUT);
      const liveCount = countDisposals(live.scene);
      assert.ok(liveCount.objects > 10, "the scene has GPU objects to free");
      live.dispose();
      assert.ok(liveCount.calls() > 0, "a live scene frees them");

      const h = new SceneHolder(() => new LayoutScene3D("dark"));
      h.update(INPUT);
      const c = countDisposals(h.current.scene);
      assert.ok(c.objects > 10);
      h.contextLost();
      h.dispose();
      assert.equal(c.calls(), 0, "nothing of a lost context is disposed");
    });
  });

  it("after the restore the new scene draws the layout again, and closing frees the new objects only", () => {
    withFakeDocument(() => {
      const h = new SceneHolder(() => new LayoutScene3D("dark"));
      h.update(INPUT);
      const before = h.current.nodes.length;
      assert.ok(before > 0);
      const old = countDisposals(h.current.scene);
      h.contextLost();
      h.contextRestored();
      assert.equal(h.current.nodes.length, before, "the same components, rebuilt");
      const fresh = countDisposals(h.current.scene);
      assert.ok(fresh.objects > 10);
      h.dispose();
      assert.ok(fresh.calls() > 0, "the new scene is freed");
      assert.equal(old.calls(), 0, "the old one never");
    });
  });
});
