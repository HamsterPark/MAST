// ════════════════════════════════════════════════════════════════════════════
// Tests for src/lib/opticalLayout/frame.ts, glyphs.ts and exportSvg.ts.
//
//     cd frontend && npm run test:unit
//
// frame.ts is the answer to "dragging must not freeze the page on a big layout": however
// many pointer events arrive, the trace runs at most once per animation frame. Here the
// frames are driven by hand.
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { createFrameThrottle } from "../src/lib/opticalLayout/frame.ts";
import { glyphRadius, glyphShapes, shapeBounds, type Shape } from "../src/lib/opticalLayout/glyphs.ts";
import { exportFileName, exportSize, resolveCssVars, standaloneSvg, xmlEscape } from "../src/lib/opticalLayout/exportSvg.ts";
import { COMPONENT_TYPES } from "../src/lib/opticalLayout/types.ts";
import { CATALOG } from "../src/lib/opticalLayout/catalog.ts";
import { newComponent } from "../src/lib/opticalLayout/edit.ts";

/** A manual animation-frame clock. */
function manualFrames() {
  const queue = new Map<number, () => void>();
  let next = 1;
  return {
    request: (cb: () => void) => {
      queue.set(next, cb);
      return next++;
    },
    cancel: (id: number) => void queue.delete(id),
    tick: () => {
      const jobs = [...queue.values()];
      queue.clear();
      for (const j of jobs) j();
    },
    get scheduled() {
      return queue.size;
    },
  };
}

describe("the frame throttle", () => {
  it("applies only the latest value, once per frame, however many arrive", () => {
    const f = manualFrames();
    const seen: number[] = [];
    const t = createFrameThrottle<number>((v) => seen.push(v), f.request, f.cancel);
    for (let i = 1; i <= 500; i++) t.push(i);
    assert.equal(f.scheduled, 1, "one frame is requested, not 500");
    assert.deepEqual(seen, [], "nothing runs before the frame");
    f.tick();
    assert.deepEqual(seen, [500]);
    assert.equal(t.pending, false);
    f.tick();
    assert.deepEqual(seen, [500], "and nothing runs on its own afterwards");
  });

  it("the next burst gets the next frame", () => {
    const f = manualFrames();
    const seen: string[] = [];
    const t = createFrameThrottle<string>((v) => seen.push(v), f.request, f.cancel);
    t.push("a");
    t.push("b");
    f.tick();
    t.push("c");
    f.tick();
    assert.deepEqual(seen, ["b", "c"]);
  });

  it("flush applies the pending value now, so the last position of a drag is never lost, and cancels the frame", () => {
    const f = manualFrames();
    const seen: number[] = [];
    const t = createFrameThrottle<number>((v) => seen.push(v), f.request, f.cancel);
    t.push(1);
    t.push(2);
    t.flush();
    assert.deepEqual(seen, [2]);
    assert.equal(f.scheduled, 0);
    f.tick();
    assert.deepEqual(seen, [2], "the cancelled frame does not apply it a second time");
    t.flush();
    assert.deepEqual(seen, [2], "flushing nothing does nothing");
  });

  it("cancel drops the pending value", () => {
    const f = manualFrames();
    const seen: number[] = [];
    const t = createFrameThrottle<number>((v) => seen.push(v), f.request, f.cancel);
    t.push(1);
    assert.equal(t.pending, true);
    t.cancel();
    assert.equal(t.pending, false);
    f.tick();
    assert.deepEqual(seen, []);
  });

  it("a value pushed from inside the apply is kept for the next frame", () => {
    const f = manualFrames();
    const seen: number[] = [];
    const t = createFrameThrottle<number>(
      (v) => {
        seen.push(v);
        if (v === 1) t.push(2);
      },
      f.request,
      f.cancel,
    );
    t.push(1);
    f.tick();
    assert.deepEqual(seen, [1]);
    f.tick();
    assert.deepEqual(seen, [1, 2]);
  });
});

describe("glyphs", () => {
  const finite = (s: Shape): boolean => {
    switch (s.k) {
      case "line":
        return [s.x1, s.y1, s.x2, s.y2].every(Number.isFinite);
      case "rect":
        return [s.x, s.y, s.w, s.h].every(Number.isFinite) && s.w >= 0 && s.h >= 0;
      case "poly":
        return s.pts.length >= 3 && s.pts.every(([x, y]) => Number.isFinite(x) && Number.isFinite(y));
      case "path":
        return [s.bx, s.by, s.bw, s.bh].every(Number.isFinite) && s.d.length > 0 && !s.d.includes("NaN");
      case "circle":
        return [s.cx, s.cy, s.r].every(Number.isFinite) && s.r > 0;
    }
  };

  it("every type has a drawing made of finite shapes", () => {
    for (const type of COMPONENT_TYPES) {
      const shapes = glyphShapes(newComponent(type, "x1", { x: 0, y: 0 }));
      assert.ok(shapes.length > 0, type);
      for (const s of shapes) assert.ok(finite(s), `${type}: ${JSON.stringify(s)}`);
    }
  });

  it("stays within a sane size: no glyph is a speck or a monster", () => {
    for (const type of COMPONENT_TYPES) {
      const c = newComponent(type, "x1", { x: 0, y: 0 });
      const r = glyphRadius(glyphShapes(c));
      assert.ok(r >= 1 && r <= 120, `${type}: radius ${r}`);
    }
  });

  it("the drawing grows with the aperture", () => {
    for (const type of ["mirror", "lens", "beam_splitter", "waveplate", "iris", "filter"] as const) {
      const small = shapeBounds(glyphShapes(newComponent(type, "x", { x: 0, y: 0 }, { aperture_mm: 10 })));
      const big = shapeBounds(glyphShapes(newComponent(type, "x", { x: 0, y: 0 }, { aperture_mm: 50 })));
      assert.ok(big.maxY - big.minY > small.maxY - small.minY, type);
    }
  });

  it("the active surface is where the drawing says: a mirror's face spans the aperture", () => {
    const shapes = glyphShapes(newComponent("mirror", "m", { x: 0, y: 0 }, { aperture_mm: 30 }));
    const face = shapes.find((s) => s.k === "line" && s.weight === "thick" && s.x1 === 0 && s.x2 === 0);
    assert.ok(face && face.k === "line");
    assert.equal(Math.min(face.y1, face.y2), -15);
    assert.equal(Math.max(face.y1, face.y2), 15);
  });

  it("a mirror's hatching is on its back (-x), a grating's teeth are on its front (+x)", () => {
    const m = shapeBounds(glyphShapes(newComponent("mirror", "m", { x: 0, y: 0 })));
    assert.ok(m.minX < -1 && m.maxX <= 1e-9, JSON.stringify(m));
    const g = shapeBounds(glyphShapes(newComponent("grating", "g", { x: 0, y: 0 })));
    assert.ok(g.maxX > 1 && g.minX >= -1e-9, JSON.stringify(g));
  });

  it("receivers and sources have their body behind the face (-x)", () => {
    for (const type of ["detector", "camera", "spectrometer", "power_meter", "beam_dump", "laser", "fiber_in"] as const) {
      const b = shapeBounds(glyphShapes(newComponent(type, "x", { x: 0, y: 0 })));
      assert.ok(b.minX < -5 && Math.abs(b.minX) > Math.abs(b.maxX), `${type}: ${JSON.stringify(b)}`);
    }
  });

  it("shows the state: an iris shows its opening, a shutter whether it is closed", () => {
    const wide = glyphShapes(newComponent("iris", "i", { x: 0, y: 0 }, { props: { open_mm: 20 } }));
    const narrow = glyphShapes(newComponent("iris", "i", { x: 0, y: 0 }, { props: { open_mm: 2 } }));
    assert.notDeepEqual(wide, narrow);
    const closed = glyphShapes(newComponent("shutter", "s", { x: 0, y: 0 }, { props: { closed: true } }));
    const open = glyphShapes(newComponent("shutter", "s", { x: 0, y: 0 }, { props: { closed: false } }));
    assert.notDeepEqual(closed, open);
    const refl = glyphShapes(newComponent("sample", "s", { x: 0, y: 0 }, { props: { reflects: true } }));
    const absorb = glyphShapes(newComponent("sample", "s", { x: 0, y: 0 }, { props: { reflects: false } }));
    assert.notDeepEqual(refl, absorb);
  });

  it("an iris with an opening larger than its aperture is still drawn sanely", () => {
    const shapes = glyphShapes(newComponent("iris", "i", { x: 0, y: 0 }, { props: { open_mm: 900 } }));
    for (const s of shapes) assert.ok(finite(s));
  });

  it("a component of a type this build does not know is still drawn", () => {
    const c = { ...newComponent("mirror", "x", { x: 0, y: 0 }), type: "from-the-future" as never };
    assert.ok(glyphShapes(c).length > 0);
  });

  it("the catalogue and the glyphs agree on what the types are", () => {
    assert.equal(Object.keys(CATALOG).length, COMPONENT_TYPES.length);
  });
});

describe("exporting", () => {
  it("replaces CSS variables by what the page has, falling back when it has nothing", () => {
    const vars: Record<string, string> = { "--mast-text": "#161a21", "--mast-panel": " #ffffff " };
    const out = resolveCssVars('<g stroke="var(--mast-text)" fill="var(--mast-panel)" color="var(--mast-nope, #123456)" x="var(--mast-nope2)"/>', (n) => vars[n]);
    assert.equal(out, '<g stroke="#161a21" fill="#ffffff" color="#123456" x="#888888"/>');
    assert.equal(resolveCssVars("no variables", () => "x"), "no variables");
    assert.equal(resolveCssVars("var( --mast-text )", (n) => vars[n]), "#161a21");
  });

  it("wraps the markup into a file with the XML header and namespace, once", () => {
    const s = standaloneSvg('<svg width="10" height="10"><g/></svg>');
    assert.ok(s.startsWith('<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg" width="10"'));
    assert.equal(standaloneSvg(s.slice(s.indexOf("<svg"))).match(/xmlns=/g)?.length, 1, "already there: not added twice");
  });

  it("names the file after the layout and its revision, safely", () => {
    assert.equal(exportFileName({ id: "main", revision: 12 }, "svg"), "main-r12.svg");
    assert.equal(exportFileName({ id: "a/b c", revision: 0 }, "png"), "a_b_c-r0.png");
    assert.equal(exportFileName({ id: "main", revision: 3 }, "svg", true), "main-r3-unsaved.svg");
    assert.equal(exportFileName({ id: "", revision: 1 }, "svg"), "layout-r1.svg");
  });

  it("escapes text and sizes the picture", () => {
    assert.equal(xmlEscape('a<b>&"c"'), "a&lt;b&gt;&amp;&quot;c&quot;");
    assert.deepEqual(exportSize(1200, 900, 2), { width: 2400, height: 1800 });
    assert.deepEqual(exportSize(0, 0, 2), { width: 1, height: 1 });
  });
});
