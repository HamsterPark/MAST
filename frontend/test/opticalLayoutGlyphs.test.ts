// ════════════════════════════════════════════════════════════════════════════
// Tests for src/lib/opticalLayout/glyphs.ts: the drawings of the components (v2 section 8.1).
//
//     cd frontend && npm run test:unit
//
// What the redesign must not lose is the geometry: every component a ray can meet draws its
// active surface as one exact segment, (0, -a/2) to (0, a/2) in its local frame, last, so no
// decoration covers it. The rest pins that the drawing follows the component's state (focal
// sign, coating side, pass band, density, retardance, axis) and the screen box the labels use.
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { PAPER_DIAL_MIN_PX, PAPER_TEXT_MIN_PX, dichroicHue, faceOf, filterHue, glyphShapes, ndDensity, paperShapes, sampleHasTip, screenBox, shapeBounds, sourceWavelengths, type Shape } from "../src/lib/opticalLayout/glyphs.ts";
import { COMPONENT_TYPES, isSourceType, type LayoutComponent } from "../src/lib/opticalLayout/types.ts";
import { newComponent } from "../src/lib/opticalLayout/edit.ts";

type Opts = Parameters<typeof newComponent>[3];
const comp = (type: string, opts: Opts = {}): LayoutComponent => newComponent(type as LayoutComponent["type"], "x1", { x: 0, y: 0 }, opts);
/** A component of any type, also one this build's catalogue may not list yet (a PBS is a splitter cube). */
const anyComp = (type: string, opts: Opts = {}): LayoutComponent => {
  try {
    return comp(type, opts);
  } catch {
    return { ...comp("beam_splitter", opts), type } as LayoutComponent;
  }
};
/** The types a ray can meet: everything but the light sources (and `pbs`, which v2's type list adds). */
const RECEIVING = [...COMPONENT_TYPES.filter((t) => !isSourceType(t)), "pbs"];

describe("the active surface", () => {
  it("every component a ray can meet draws it once, exactly (0, ±a/2), and last", () => {
    for (const type of RECEIVING) {
      for (const a of [3, 10, 25.4, 50]) {
        const c = anyComp(type, { aperture_mm: a });
        const shapes = glyphShapes(c);
        const faces = shapes.filter((s) => s.role === "face");
        assert.equal(faces.length, 1, `${type} @ ${a}: one face`);
        const f = faces[0]!;
        assert.equal(f.k, "line", type);
        if (f.k !== "line") continue;
        assert.deepEqual([f.x1, f.x2], [0, 0], `${type}: on the local y axis`);
        assert.equal(Math.min(f.y1, f.y2), -a / 2, `${type} @ ${a}`);
        assert.equal(Math.max(f.y1, f.y2), a / 2, `${type} @ ${a}`);
        assert.equal(shapes[shapes.length - 1], f, `${type}: drawn last, over every decoration`);
        assert.equal(f.min, undefined, `${type}: never left out at any zoom`);
        assert.equal(faceOf(shapes), f);
      }
    }
  });

  it("light sources have no surface and draw their body behind the emission point", () => {
    for (const type of ["laser", "fiber_out", "led"]) {
      const shapes = glyphShapes(comp(type));
      assert.equal(shapes.filter((s) => s.role === "face").length, 0, type);
      assert.equal(faceOf(shapes), undefined);
      const b = shapeBounds(shapes);
      assert.ok(b.maxX <= 1e-9 && b.minX < -5, `${type}: ${JSON.stringify(b)}`);
    }
  });

  it("reflecting and receiving surfaces are the heavy line; a transmitting optic's is the thin mid-plane", () => {
    for (const type of ["mirror", "grating", "detector", "camera", "fiber_in"]) assert.equal(faceOf(glyphShapes(comp(type)))?.weight, "thick", type);
    for (const type of ["lens", "waveplate", "polarizer", "window", "filter", "prism"]) {
      const f = faceOf(glyphShapes(comp(type)));
      assert.ok(f && (f.weight === "fine" || f.weight === "hair"), type);
    }
  });
});

describe("details", () => {
  it("only details are left out when zoomed out, and each says from which zoom it shows", () => {
    for (const type of [...COMPONENT_TYPES, "pbs"]) {
      for (const s of glyphShapes(anyComp(type))) {
        if (s.min === undefined) continue;
        assert.ok(s.min > 0 && Number.isFinite(s.min), `${type}: ${JSON.stringify(s)}`);
        assert.ok(s.role === "detail" || s.role === "hatch" || s.role === "teeth" || s.k === "text" || s.k === "dial" || s.weight === "hair", `${type}: ${s.k} ${s.role}`);
      }
    }
  });

  it("a text shows once it is about 6.5 px tall", () => {
    const t = glyphShapes(comp("waveplate", { props: { retardance: "half" } })).find((s): s is Extract<Shape, { k: "text" }> => s.k === "text")!;
    assert.ok(Math.abs(t.size * t.min! - 6.5) < 1e-9);
  });
});

describe("the figure keeps every text and dial (paperShapes)", () => {
  type Text = Extract<Shape, { k: "text" }>;
  type Dial = Extract<Shape, { k: "dial" }>;
  const texts = (s: readonly Shape[]) => s.filter((x): x is Text => x.k === "text");
  const dials = (s: readonly Shape[]) => s.filter((x): x is Dial => x.k === "dial");
  const shown = (s: readonly Shape[], k: number) => s.filter((x) => !(x.min !== undefined && k < x.min));

  it("at a zoom where the canvas leaves them out, the figure still has the P, the λ/2, the OD and the dials", () => {
    const k = 1.2; // a 900 mm table framed to 1100 px
    const cases: Array<[LayoutComponent, string | null, number]> = [
      [anyComp("pbs", { aperture_mm: 12.7 }), "P", 0],
      [comp("waveplate", { aperture_mm: 12.7, props: { retardance: "half", fast_axis_deg: 22.5 } }), "λ/2", 1],
      [comp("waveplate", { aperture_mm: 12.7, props: { retardance: "quarter" } }), "λ/4", 1],
      [comp("polarizer", { aperture_mm: 12.7, props: { axis_deg: 30 } }), null, 1],
      [comp("nd", { aperture_mm: 12.7, props: { od: 2 } }), "OD 2", 0],
    ];
    for (const [c, label, nDials] of cases) {
      const screen = shown(glyphShapes(c), k);
      const paper = shown(paperShapes(glyphShapes(c), k), k);
      if (label) {
        assert.ok(!texts(screen).some((t) => t.text === label), `${c.type}: the canvas leaves ${label} out at k ${k}`);
        assert.ok(texts(paper).some((t) => t.text === label), `${c.type}: the figure has ${label}`);
      }
      assert.equal(dials(paper).length, nDials, `${c.type}: dials in the figure`);
    }
  });

  it("a text is at least PAPER_TEXT_MIN_PX high, never larger than its room, and moves out by what it grew", () => {
    const k = 1.2;
    const wp = comp("waveplate", { aperture_mm: 12.7, props: { retardance: "half" } });
    const before = texts(glyphShapes(wp))[0]!;
    const after = texts(paperShapes(glyphShapes(wp), k))[0]!;
    assert.ok(Math.abs(after.size * k - PAPER_TEXT_MIN_PX) < 1e-9, `${after.size * k} px`);
    assert.equal(after.min, undefined);
    // it sits beyond the -y end: its centre moves out (down in the glyph's frame) by 0.55 of what it grew
    assert.ok(Math.abs(after.y - (before.y - (after.size - before.size) * 0.55)) < 1e-9);
    // a large zoom changes nothing: the text is already larger than the minimum
    const big = texts(paperShapes(glyphShapes(wp), 20))[0]!;
    assert.equal(big.size, before.size);
    assert.equal(big.y, before.y);
    // the P of a PBS sits inside the cube: it grows up to its room and no further, and does not move
    const pbs = anyComp("pbs", { aperture_mm: 12.7 });
    const p0 = texts(glyphShapes(pbs))[0]!;
    const tiny = texts(paperShapes(glyphShapes(pbs), 0.2))[0]!;
    assert.ok(p0.fit !== undefined && tiny.size === p0.fit, `${tiny.size} vs room ${p0.fit}`);
    assert.equal(tiny.y, p0.y);
    assert.equal(tiny.x, p0.x);
  });

  it("a dial is at least PAPER_DIAL_MIN_PX in radius, moved out by what it grew; everything else is left as it is", () => {
    const k = 0.5;
    const pol = comp("polarizer", { aperture_mm: 12.7, props: { axis_deg: 30 } });
    const shapes = glyphShapes(pol);
    const out = paperShapes(shapes, k);
    const d0 = dials(shapes)[0]!;
    const d1 = dials(out)[0]!;
    assert.ok(Math.abs(d1.r * k - PAPER_DIAL_MIN_PX) < 1e-9);
    assert.ok(Math.abs(d1.cy - (d0.cy + (d1.r - d0.r))) < 1e-9, "beyond the +y end, it moves up by what it grew");
    assert.equal(d1.min, undefined);
    for (let i = 0; i < shapes.length; i++) if (shapes[i]!.k !== "text" && shapes[i]!.k !== "dial") assert.equal(out[i], shapes[i], "the same object");
  });
});

describe("the drawing follows the state", () => {
  const pathOf = (c: LayoutComponent) => glyphShapes(c).find((s): s is Extract<Shape, { k: "path" }> => s.k === "path")!;

  it("a lens is bi-convex for a positive focal length, bi-concave for a negative one, dashed when it has none", () => {
    const pos = pathOf(comp("lens", { props: { focal_mm: 100 } }));
    const neg = pathOf(comp("lens", { props: { focal_mm: -50 } }));
    const none = pathOf(comp("lens"));
    assert.ok(pos.bw > neg.bw * 0.5, "the convex lens bulges");
    assert.notEqual(pos.d, neg.d);
    assert.equal(pos.dash, undefined);
    assert.equal(neg.dash, undefined);
    assert.ok(none.dash, "a lens without a focal length is drawn dashed");
    // the concave one is thinner in the middle than at the rim: the control points of its curves sit inside
    assert.match(neg.d, /Q /);
  });

  it("a dichroic's coating takes the colour of what it reflects", () => {
    assert.equal(dichroicHue(comp("dichroic", { props: { edge_nm: 600, reflects: "short" } })), 540);
    assert.equal(dichroicHue(comp("dichroic", { props: { edge_nm: 600, reflects: "long" } })), 660);
    assert.equal(dichroicHue({ ...comp("dichroic"), props: {} }), null);
    const face = faceOf(glyphShapes(comp("dichroic", { props: { edge_nm: 600, reflects: "short" } })));
    assert.equal(face?.tone, "wl");
    assert.equal(face?.wl, 540);
  });

  it("a filter is tinted with its pass band, and neutral glass without one", () => {
    assert.equal(filterHue(comp("filter", { props: { pass_min_nm: 500, pass_max_nm: 560 } })), 530);
    assert.equal(filterHue(comp("filter", { props: { pass_min_nm: 1000 } })), 1040);
    assert.equal(filterHue(comp("filter", { props: { pass_max_nm: 450 } })), 410);
    assert.equal(filterHue(comp("filter")), null);
    const tinted = glyphShapes(comp("filter", { props: { pass_min_nm: 500, pass_max_nm: 560 } }));
    assert.ok(tinted.some((s) => s.fill === "wl" && s.wl === 530));
    assert.ok(!glyphShapes(comp("filter")).some((s) => s.fill === "wl"));
  });

  it("an ND plate is greyer the denser it is, and says its density", () => {
    assert.ok(ndDensity(0) < ndDensity(1) && ndDensity(1) < ndDensity(2) && ndDensity(2) < ndDensity(4));
    assert.ok(ndDensity(20) <= 0.88 && ndDensity(null) > 0);
    const shapes = glyphShapes(comp("nd", { props: { od: 1 } }));
    assert.ok(shapes.some((s) => s.k === "text" && s.text === "OD 1"));
  });

  it("a wave plate names its retardance and shows its fast axis on a dial", () => {
    const text = (c: LayoutComponent) => glyphShapes(c).find((s) => s.k === "text");
    assert.equal(text(comp("waveplate", { props: { retardance: "half" } }))?.k === "text" && text(comp("waveplate", { props: { retardance: "half" } }))!.text, "λ/2");
    const quarter = glyphShapes(comp("waveplate", { props: { retardance: "quarter", fast_axis_deg: 22.5 } }));
    assert.ok(quarter.some((s) => s.k === "text" && s.text === "λ/4"));
    const dial = quarter.find((s): s is Extract<Shape, { k: "dial" }> => s.k === "dial")!;
    assert.equal(dial.deg, 22.5);
    assert.equal(dial.mark, "axis");
    const unset = glyphShapes({ ...comp("waveplate"), props: {} });
    assert.ok(unset.some((s) => s.k === "text" && s.text === "λ/?" && s.tone === "faint"), "no retardance: said, faintly");
  });

  it("a polariser shows its axis as the lines of a grid", () => {
    const dial = glyphShapes(comp("polarizer", { props: { axis_deg: 45 } })).find((s): s is Extract<Shape, { k: "dial" }> => s.k === "dial")!;
    assert.equal(dial.deg, 45);
    assert.equal(dial.mark, "grid");
    const none = glyphShapes(comp("polarizer")).find((s): s is Extract<Shape, { k: "dial" }> => s.k === "dial")!;
    assert.equal(none.deg, 0, "no axis given is 0° (v2 section 3.2)");
  });

  it("a PBS is the splitter cube marked P", () => {
    const shapes = glyphShapes({ ...comp("beam_splitter"), type: "pbs" } as unknown as LayoutComponent);
    assert.ok(shapes.some((s) => s.k === "text" && s.text === "P"));
    assert.ok(!glyphShapes(comp("beam_splitter")).some((s) => s.k === "text"));
  });

  it("a sample gets a tip when its label or function says STM / TERS / tip", () => {
    const plain = comp("sample", { label: "Si wafer" });
    assert.equal(sampleHasTip(plain), false);
    assert.equal(sampleHasTip(comp("sample", { label: "TERS sample" })), true);
    assert.equal(sampleHasTip({ ...plain, annotations: { ...plain.annotations, function: "under the STM tip" } }), true);
    assert.equal(sampleHasTip(comp("sample", { label: "针尖下的样品" })), true);
    assert.equal(sampleHasTip(comp("sample", { label: "multiple" })), false, "a word, not a substring");
    assert.ok(glyphShapes(comp("sample", { label: "TERS" })).length > glyphShapes(plain).length);
  });

  it("a laser shows a coloured band per wavelength, at most three", () => {
    const bands = (c: LayoutComponent) => glyphShapes(c).filter((s) => s.fill === "wl").map((s) => s.wl);
    assert.deepEqual(bands(comp("laser", { props: { wavelength_nm: 633 } })), [633]);
    assert.deepEqual(bands(comp("laser", { props: { wavelengths_nm: [405, 488, 561, 640] } })), [405, 488, 561]);
    assert.deepEqual(sourceWavelengths(comp("laser", { props: { wavelengths_nm: [532, "x", -1, 1064] } })), [532, 1064]);
    assert.deepEqual(bands({ ...comp("laser"), props: {} }), []);
  });

  it("a shutter and an iris show their state", () => {
    const closed = glyphShapes(comp("shutter", { props: { closed: true } }));
    const open = glyphShapes(comp("shutter", { props: { closed: false } }));
    assert.equal(faceOf(closed)?.weight, "normal", "closed: the blade is in the beam");
    assert.ok(faceOf(open)?.dash, "open: the aperture is clear");
    const wide = glyphShapes(comp("iris", { props: { open_mm: 20 } }));
    const narrow = glyphShapes(comp("iris", { props: { open_mm: 2 } }));
    const gap = (s: Shape[]) => {
      const blade = s.filter((x): x is Extract<Shape, { k: "poly" }> => x.k === "poly")[0]!;
      return Math.max(...blade.pts.map(([, y]) => y));
    };
    assert.ok(gap(wide) < gap(narrow), "the blades of a wider opening stop further out");
  });
});

describe("the screen box of a glyph", () => {
  it("turns with the component and flips y", () => {
    const b = { minX: -40, maxX: 0, minY: -8, maxY: 8 }; // a laser: the body behind the emission point
    const at0 = screenBox(b, 0, 2);
    assert.deepEqual([at0.dx, at0.dy, at0.ex, at0.ey].map((v) => Math.round(v * 1e9) / 1e9 + 0), [-40, 0, 40, 16]);
    const at180 = screenBox(b, 180, 2);
    assert.ok(Math.abs(at180.dx - 40) < 1e-9 && Math.abs(at180.dy) < 1e-9);
    // facing up the table (+y): the body is below the emission point on the table, so below it on the screen
    const at90 = screenBox(b, 90, 2);
    assert.ok(Math.abs(at90.dx) < 1e-9 && Math.abs(at90.dy - 40) < 1e-9, JSON.stringify(at90));
    assert.ok(Math.abs(at90.ex - 16) < 1e-9 && Math.abs(at90.ey - 40) < 1e-9);
    const at45 = screenBox({ minX: -1, maxX: 1, minY: -1, maxY: 1 }, 45, 1);
    assert.ok(Math.abs(at45.ex - Math.SQRT2) < 1e-9 && Math.abs(at45.ey - Math.SQRT2) < 1e-9);
  });
});
