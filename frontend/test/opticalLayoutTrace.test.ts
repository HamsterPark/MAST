// ════════════════════════════════════════════════════════════════════════════
// Tests for src/lib/opticalLayout/trace.ts on layouts written out in this file.
//
//     cd frontend && npm run test:unit
//
// The shared fixtures from the backend (tests/fixtures/optical_layout/) are checked in
// test/opticalLayoutFixtures.test.ts; THESE are the cases whose answer can be worked out
// by hand, so a failure here says which rule broke rather than which fixture differs (the
// rules are section 4 and section 11.2 of docs/v2/design/optical_layout.md):
//
//   * a 45 degree mirror at angle_deg 135 turns +x into +y (the one the spec names)
//   * offset_mm is signed along the LOCAL +y axis, incidence_deg is 0..90
//   * mirrors have a front and a back; a beam splitter splits from either side
//   * the dichroic edge itself is transmitted
//   * every limit of section 4: 64 events, 1 m margin, 10 m segment, 1e-3 intensity
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  TRACE_LIMITS,
  branchPoints,
  eventsByComponent,
  ownWavelengths,
  toContractTrace,
  traceLayout,
  traceLayoutDetailed,
} from "../src/lib/opticalLayout/trace.ts";
import { comp, foldLayout, layoutOf } from "./helpers/opticalLayout.ts";

const near = (a: number, b: number, tol = 1e-6) => assert.ok(Math.abs(a - b) <= tol, `${a} is not within ${tol} of ${b}`);

/** The only branch of the only source. */
function single(trace: ReturnType<typeof traceLayout>) {
  assert.equal(trace.length, 1, `expected one source entry, got ${trace.length}`);
  const b = trace[0]!.branches;
  assert.equal(b.length, 1, `expected one branch, got ${b.map((x) => x.branch).join(",")}`);
  return b[0]!;
}

describe("a fold mirror", () => {
  it("angle_deg 135 turns a +x beam into +y", () => {
    const t = traceLayout(foldLayout());
    const b = single(t);
    assert.equal(b.end, "stop");
    assert.equal(b.fraction, 1);
    assert.deepEqual(b.segments, [
      { x0: 50, y0: 62.5, x1: 112.5, y1: 62.5 },
      { x0: 112.5, y0: 62.5, x1: 112.5, y1: 200 },
    ]);
    const [m, d] = b.events;
    assert.equal(m?.component, "m1");
    assert.equal(m?.action, "reflect");
    assert.equal(m?.offset_mm, 0);
    assert.equal(m?.incidence_deg, 45);
    assert.equal(m?.path_mm, 62.5);
    assert.equal(d?.component, "det1");
    assert.equal(d?.action, "stop");
    assert.equal(d?.incidence_deg, 0);
    assert.equal(d?.path_mm, 200);
  });

  it("the source entry names the source and its wavelength", () => {
    const t = traceLayout(foldLayout());
    assert.equal(t[0]?.source, "las1");
    assert.equal(t[0]?.wavelength_nm, 532);
    assert.equal(t[0]?.branches[0]?.branch, "0");
  });

  it("the back of the mirror ends the beam and says so", () => {
    const b = single(traceLayout(foldLayout({ mirrorAngle: 315 })));
    assert.equal(b.end, "back_hit");
    assert.equal(b.events.length, 1);
    assert.equal(b.events[0]?.component, "m1");
    assert.equal(b.events[0]?.action, "back_hit");
    assert.equal(b.segments.length, 1);
  });

  it("a mirror facing the wrong way at 0 degrees is a back hit too", () => {
    assert.equal(single(traceLayout(foldLayout({ mirrorAngle: 0 }))).end, "back_hit");
  });

  it("offset_mm is signed along the local +y axis", () => {
    // local +y of a 135 degree mirror points to (-sin 135, cos 135) = (-0.707, -0.707): down-left.
    // A beam 3 mm above the centre meets the mirror up-right of it: offset -3 * sqrt(2).
    const up = single(traceLayout(foldLayout({ laserY: 65.5 })));
    near(up.events[0]!.offset_mm, -3 * Math.SQRT2, 0.001);
    const down = single(traceLayout(foldLayout({ laserY: 59.5 })));
    near(down.events[0]!.offset_mm, 3 * Math.SQRT2, 0.001);
    // the hit point is where the beam meets the surface, not the centre
    near(up.events[0]!.x, 115.5, 0.001);
    near(up.events[0]!.y, 65.5, 0.001);
  });

  it("a beam outside the aperture misses and flies on to the edge of the table", () => {
    // 22.5 mm above the centre meets the line 31.8 mm from it; the aperture reaches 12.7
    const b = single(traceLayout(foldLayout({ laserY: 85 })));
    assert.equal(b.events.length, 0);
    assert.equal(b.end, "left_table");
    assert.deepEqual(b.segments, [{ x0: 50, y0: 85, x1: 2200, y1: 85 }]);
  });

  it("the edge of the aperture still counts as a hit", () => {
    // offset exactly -12.7: the beam is 12.7 / sqrt(2) above the centre
    const y = 62.5 + 12.7 / Math.SQRT2;
    const b = single(traceLayout(foldLayout({ laserY: y })));
    assert.equal(b.events[0]?.component, "m1");
  });

  it("incidence is the angle to the normal and runs from 0 to 90", () => {
    const l = layoutOf([
      comp("laser", "las1", 0, 0, 0, { props: { wavelength_nm: 532 } }),
      comp("mirror", "m1", 100, 0, 180),
      comp("mirror", "m2", 0, 100, 330),
    ]);
    const first = traceLayout(l)[0]!.branches[0]!.events[0]!;
    assert.equal(first.incidence_deg, 0);
    const slanted = layoutOf([comp("laser", "las1", 0, 0, 0, { props: { wavelength_nm: 532 } }), comp("mirror", "m1", 100, 0, 150)]);
    near(traceLayout(slanted)[0]!.branches[0]!.events[0]!.incidence_deg, 30);
  });
});

describe("two mirrors (a periscope seen from above)", () => {
  it("fold up, then back along +x", () => {
    const l = layoutOf([
      comp("laser", "las1", 50, 62.5, 0, { props: { wavelength_nm: 532 } }),
      comp("mirror", "mA", 100, 62.5, 135),
      comp("mirror", "mB", 100, 162.5, 315),
      comp("detector", "det1", 200, 162.5, 180),
    ]);
    const b = single(traceLayout(l));
    assert.deepEqual(
      b.events.map((e) => [e.component, e.action, e.path_mm]),
      [
        ["mA", "reflect", 50],
        ["mB", "reflect", 150],
        ["det1", "stop", 250],
      ],
    );
    assert.equal(b.end, "stop");
  });
});

describe("a beam splitter", () => {
  const bs = (r: number) =>
    layoutOf([
      comp("laser", "las1", 50, 62.5, 0, { props: { wavelength_nm: 532 } }),
      comp("beam_splitter", "bs1", 112.5, 62.5, 135, { props: { reflect_fraction: r } }),
      comp("detector", "detT", 250, 62.5, 180),
      comp("detector", "detR", 112.5, 200, 270),
    ]);

  it("makes a reflected and a transmitted branch with intensities R and 1 - R", () => {
    const t = traceLayout(bs(0.3))[0]!;
    assert.deepEqual(
      t.branches.map((b) => [b.branch, b.fraction]),
      [
        ["0", 1],
        ["0.r", 0.3],
        ["0.t", 0.7],
      ],
    );
    const root = t.branches[0]!;
    assert.equal(root.events.at(-1)?.component, "bs1");
    assert.equal(root.events.at(-1)?.action, "split");
    assert.equal(root.end, "split", "the parent ends at the splitter, the children follow");
    assert.equal(root.segments.length, 1);
    const r = t.branches[1]!;
    assert.deepEqual(r.segments, [{ x0: 112.5, y0: 62.5, x1: 112.5, y1: 200 }]);
    assert.equal(r.events[0]?.component, "detR");
    assert.equal(r.events[0]?.path_mm, 200, "the path goes on from the parent");
    const tt = t.branches[2]!;
    assert.deepEqual(tt.segments, [{ x0: 112.5, y0: 62.5, x1: 250, y1: 62.5 }]);
    assert.equal(tt.events[0]?.component, "detT");
  });

  it("the default is a 50/50 split", () => {
    const l = bs(0.5);
    delete l.components[1]!.props["reflect_fraction"];
    assert.deepEqual(
      traceLayout(l)[0]!.branches.map((b) => b.fraction),
      [1, 0.5, 0.5],
    );
  });

  it("a branch below 1e-3 is not created, and the other one keeps its name", () => {
    const t = traceLayout(bs(0.0005))[0]!;
    assert.deepEqual(
      t.branches.map((b) => b.branch),
      ["0", "0.t"],
    );
    const all = traceLayout(bs(1))[0]!;
    assert.deepEqual(
      all.branches.map((b) => b.branch),
      ["0", "0.r"],
    );
  });

  it("splits from the back as well, and the fractions of the leaves add up to one", () => {
    const l = layoutOf([
      comp("laser", "las1", 50, 62.5, 0, { props: { wavelength_nm: 532 } }),
      comp("beam_splitter", "bs1", 112.5, 62.5, 315, { props: { reflect_fraction: 0.4 } }),
      comp("detector", "detT", 250, 62.5, 180),
      comp("detector", "detR", 112.5, 200, 270),
    ]);
    const t = traceLayout(l)[0]!;
    const leaves = t.branches.filter((b) => b.end === "stop" && b.events.at(-1)?.action === "stop");
    near(
      leaves.reduce((s, b) => s + b.fraction, 0),
      1,
    );
    assert.equal(t.branches.length, 3);
  });

  it("branch names nest: the transmitted branch of the reflected branch is 0.r.t", () => {
    const l = layoutOf([
      comp("laser", "las1", 50, 62.5, 0, { props: { wavelength_nm: 532 } }),
      comp("beam_splitter", "bs1", 100, 62.5, 135, { props: { reflect_fraction: 0.5 } }),
      comp("beam_splitter", "bs2", 100, 150, 135, { props: { reflect_fraction: 0.5 } }),
      comp("detector", "d1", 100, 250, 270),
      comp("detector", "d2", 300, 150, 180),
      comp("detector", "d3", 300, 62.5, 180),
    ]);
    const names = traceLayout(l)[0]!.branches.map((b) => b.branch);
    assert.deepEqual(names, ["0", "0.r", "0.r.r", "0.r.t", "0.t"]);
  });
});

describe("a dichroic mirror", () => {
  const dm = (reflects: string, wavelengths: number[]) =>
    layoutOf([
      comp("laser", "las1", 50, 62.5, 0, { props: { wavelengths_nm: wavelengths } }),
      comp("dichroic", "dm1", 100, 62.5, 135, { props: { edge_nm: 550, reflects } }),
      comp("detector", "detT", 200, 62.5, 180),
      comp("detector", "detR", 100, 150, 270),
    ]);
  const where = (t: ReturnType<typeof traceLayout>) => t.map((s) => [s.wavelength_nm, s.branches[0]!.events.at(-1)!.component]);

  it("reflects the short wavelengths and transmits the rest; the edge itself is transmitted", () => {
    assert.deepEqual(where(traceLayout(dm("short", [450, 550, 650]))), [
      [450, "detR"],
      [550, "detT"],
      [650, "detT"],
    ]);
  });

  it("or the long ones", () => {
    assert.deepEqual(where(traceLayout(dm("long", [450, 550, 650]))), [
      [450, "detT"],
      [550, "detT"],
      [650, "detR"],
    ]);
  });

  it("each wavelength is its own entry, in the order listed", () => {
    assert.deepEqual(
      traceLayout(dm("short", [650, 450])).map((s) => s.wavelength_nm),
      [650, 450],
    );
  });
});

describe("blocking components", () => {
  it("a filter passes its band and blocks the rest", () => {
    const f = (wl: number) =>
      layoutOf([
        comp("laser", "las1", 50, 62.5, 0, { props: { wavelength_nm: wl } }),
        comp("filter", "flt1", 100, 62.5, 0, { props: { pass_min_nm: 600, pass_max_nm: 700 } }),
        comp("detector", "det1", 200, 62.5, 180),
      ]);
    const blocked = single(traceLayout(f(532)));
    assert.equal(blocked.end, "blocked");
    assert.equal(blocked.events.at(-1)?.action, "blocked");
    assert.equal(blocked.events.at(-1)?.component, "flt1");
    const passed = single(traceLayout(f(650)));
    assert.equal(passed.end, "stop");
    assert.deepEqual(
      passed.events.map((e) => e.action),
      ["transmit", "stop"],
    );
    assert.equal(single(traceLayout(f(600))).end, "stop", "the band edges pass");
    assert.equal(single(traceLayout(f(700))).end, "stop");
  });

  it("a filter with no band passes everything", () => {
    const l = layoutOf([
      comp("laser", "las1", 50, 62.5, 0, { props: { wavelength_nm: 532 } }),
      comp("filter", "flt1", 100, 62.5, 0, { props: {} }),
      comp("detector", "det1", 200, 62.5, 180),
    ]);
    assert.equal(single(traceLayout(l)).end, "stop");
  });

  it("an iris stops a beam further than half the opening from its centre", () => {
    const iris = (y: number) =>
      layoutOf([
        comp("laser", "las1", 50, y, 0, { props: { wavelength_nm: 532 } }),
        comp("iris", "ir1", 100, 62.5, 0, { props: { open_mm: 4 } }),
        comp("detector", "det1", 200, y, 180),
      ]);
    assert.equal(single(traceLayout(iris(65.5))).end, "blocked");
    assert.equal(single(traceLayout(iris(64.5))).end, "stop", "exactly half the opening passes");
    assert.equal(single(traceLayout(iris(63.5))).end, "stop");
  });

  it("an iris without an opening only transmits", () => {
    const l = layoutOf([
      comp("laser", "las1", 50, 65, 0, { props: { wavelength_nm: 532 } }),
      comp("iris", "ir1", 100, 62.5, 0, { props: {} }),
      comp("detector", "det1", 200, 65, 180),
    ]);
    assert.equal(single(traceLayout(l)).end, "stop");
  });

  it("a shutter blocks only while closed", () => {
    const sh = (closed: boolean) =>
      layoutOf([
        comp("laser", "las1", 50, 62.5, 0, { props: { wavelength_nm: 532 } }),
        comp("shutter", "sh1", 100, 62.5, 0, { props: { closed } }),
        comp("detector", "det1", 200, 62.5, 180),
      ]);
    assert.equal(single(traceLayout(sh(true))).end, "blocked");
    assert.equal(single(traceLayout(sh(false))).end, "stop");
  });

  it("a sample reflects only when told to, the others end the beam", () => {
    const smp = (reflects: boolean) =>
      layoutOf([
        comp("laser", "las1", 50, 62.5, 0, { props: { wavelength_nm: 532 } }),
        comp("sample", "smp1", 112.5, 62.5, 135, { props: { reflects } }),
        comp("detector", "det1", 112.5, 200, 270),
      ]);
    assert.equal(single(traceLayout(smp(true))).events.at(-1)?.component, "det1");
    // a non-reflecting sample facing the beam: the beam ends on it
    const stopped = single(traceLayout(smp(false)));
    assert.equal(stopped.events.at(-1)?.component, "smp1");
    assert.equal(stopped.events.at(-1)?.action, "stop");
    assert.equal(stopped.end, "stop");
    // and from behind it is a back hit, like any receiving face
    const behind = layoutOf([
      comp("laser", "las1", 50, 62.5, 0, { props: { wavelength_nm: 532 } }),
      comp("sample", "smp1", 112.5, 62.5, 0, { props: { reflects: false } }),
    ]);
    assert.equal(single(traceLayout(behind)).end, "back_hit");
  });

  it("a terminating component reports the back hit when the beam comes from behind", () => {
    const l = layoutOf([comp("laser", "las1", 50, 62.5, 0, { props: { wavelength_nm: 532 } }), comp("detector", "det1", 100, 62.5, 0)]);
    const b = single(traceLayout(l));
    assert.equal(b.end, "back_hit");
    assert.equal(b.events[0]?.action, "back_hit");
  });

  it("custom components do what their behavior says", () => {
    const cu = (behavior: string) =>
      layoutOf([
        comp("laser", "las1", 50, 62.5, 0, { props: { wavelength_nm: 532 } }),
        comp("custom", "c1", 112.5, 62.5, 135, { props: { behavior } }),
        comp("detector", "det1", 112.5, 200, 270),
        comp("detector", "det2", 250, 62.5, 180),
      ]);
    assert.equal(single(traceLayout(cu("reflect"))).events.at(-1)?.component, "det1");
    assert.equal(single(traceLayout(cu("transmit"))).events.at(-1)?.component, "det2");
    assert.equal(single(traceLayout(cu("stop"))).events.at(-1)?.component, "c1");
  });
});

describe("transmitting components", () => {
  it("record the pass but do not bend the beam", () => {
    for (const type of ["lens", "objective", "waveplate", "polarizer", "window", "nd", "aotf", "prism", "periscope"] as const) {
      const l = layoutOf([
        comp("laser", "las1", 50, 62.5, 0, { props: { wavelength_nm: 532 } }),
        comp(type, "x1", 100, 62.5, 0),
        comp("detector", "det1", 200, 62.5, 180),
      ]);
      const b = single(traceLayout(l));
      assert.deepEqual(
        b.events.map((e) => [e.component, e.action]),
        [
          ["x1", "transmit"],
          ["det1", "stop"],
        ],
        type,
      );
      assert.equal(b.segments.length, 2, `${type}: one segment per leg`);
    }
  });

  it("a grating is a zero-order mirror (v1)", () => {
    const l = layoutOf([
      comp("laser", "las1", 50, 62.5, 0, { props: { wavelength_nm: 532 } }),
      comp("grating", "gr1", 112.5, 62.5, 135),
      comp("detector", "det1", 112.5, 200, 270),
    ]);
    const b = single(traceLayout(l));
    assert.equal(b.events[0]?.action, "reflect");
    assert.equal(b.events.at(-1)?.component, "det1");
  });
});

describe("limits", () => {
  it("a beam that leaves the table ends at the edge of the 1 m margin", () => {
    const l = layoutOf([comp("laser", "las1", 50, 62.5, 180, { props: { wavelength_nm: 532 } })]);
    const b = single(traceLayout(l));
    assert.equal(b.end, "left_table");
    assert.deepEqual(b.segments, [{ x0: 50, y0: 62.5, x1: -1000, y1: 62.5 }]);
    const up = single(traceLayout(layoutOf([comp("laser", "las1", 50, 62.5, 90, { props: { wavelength_nm: 532 } })])));
    near(up.segments[0]!.y1, 1900);
  });

  it("a single segment never passes 10 m", () => {
    const l = layoutOf([comp("laser", "las1", 0, 62.5, 0, { props: { wavelength_nm: 532 } })], { width_mm: 30000 });
    const b = single(traceLayout(l));
    assert.equal(b.end, "left_table");
    assert.equal(b.segments[0]?.x1, 10000);
  });

  it("two mirrors facing each other stop at 64 events", () => {
    const l = layoutOf([
      comp("laser", "las1", 50, 62.5, 0, { props: { wavelength_nm: 532 } }),
      comp("mirror", "mA", 0, 62.5, 0),
      comp("mirror", "mB", 100, 62.5, 180),
    ]);
    const b = single(traceLayout(l));
    assert.equal(b.events.length, 64);
    assert.equal(b.segments.length, 64);
    assert.equal(b.end, "max_events");
    assert.equal(b.events[0]?.component, "mB");
    assert.equal(b.events[1]?.component, "mA");
    near(b.events[0]!.path_mm, 50);
    near(b.events[63]!.path_mm, 50 + 63 * 100);
  });

  it("a beam parallel to the active surface hits nothing", () => {
    const l = layoutOf([comp("laser", "las1", 50, 62.5, 0, { props: { wavelength_nm: 532 } }), comp("mirror", "m1", 100, 62.5, 90)]);
    const b = single(traceLayout(l));
    assert.equal(b.events.length, 0);
    assert.equal(b.end, "left_table");
  });

  it("a component behind the source is not hit", () => {
    const l = layoutOf([comp("laser", "las1", 100, 62.5, 0, { props: { wavelength_nm: 532 } }), comp("detector", "det1", 50, 62.5, 0)]);
    assert.equal(single(traceLayout(l)).events.length, 0);
  });

  it("light sources never block a beam", () => {
    const l = layoutOf([
      comp("laser", "las1", 50, 62.5, 0, { props: { wavelength_nm: 532 } }),
      comp("lamp", "lamp1", 100, 62.5, 0, { props: { wavelength_nm: 600 } }),
      comp("detector", "det1", 200, 62.5, 180),
    ]);
    const t = traceLayout(l);
    assert.equal(t.length, 2);
    assert.equal(t[0]!.branches[0]!.events.at(-1)?.component, "det1");
    assert.equal(t[1]!.branches[0]!.events.at(-1)?.component, "det1");
  });
});

describe("sources and wavelengths", () => {
  it("one entry per source and wavelength, in layout order", () => {
    const l = layoutOf([
      comp("laser", "b", 50, 100, 0, { props: { wavelengths_nm: [800, 400] } }),
      comp("led", "a", 50, 200, 0, { props: { wavelength_nm: 450 } }),
    ]);
    assert.deepEqual(
      traceLayout(l).map((s) => [s.source, s.wavelength_nm]),
      [
        ["b", 800],
        ["b", 400],
        ["a", 450],
      ],
    );
  });

  it("a lamp with no wavelength emits one ray with wavelength null", () => {
    const l = layoutOf([comp("lamp", "lamp1", 50, 62.5, 0, { props: {} }), comp("filter", "flt1", 100, 62.5, 0, { props: { pass_min_nm: 600 } })]);
    const t = traceLayout(l);
    assert.equal(t.length, 1);
    assert.equal(t[0]?.wavelength_nm, null);
    assert.equal(t[0]?.branches[0]?.events[0]?.action, "transmit", "an unknown wavelength passes a filter");
  });

  it("wavelength_nm and wavelengths_nm are merged without repeats", () => {
    const c = comp("laser", "las1", 0, 0, 0, { props: { wavelength_nm: 532, wavelengths_nm: [532, 1064] } });
    assert.deepEqual(ownWavelengths(c), [532, 1064]);
    assert.deepEqual(ownWavelengths(comp("laser", "las2", 0, 0, 0, { props: {} })), [null]);
  });

  it("a layout without sources has an empty trace", () => {
    assert.deepEqual(traceLayout(layoutOf([comp("mirror", "m1", 10, 10, 0)])), []);
  });
});

describe("fibre links", () => {
  const link = (fiberOutProps: Record<string, unknown>) =>
    layoutOf([
      comp("laser", "las1", 50, 62.5, 0, { props: { wavelengths_nm: [532, 1064] } }),
      comp("fiber_in", "fi1", 150, 62.5, 180, { props: { continues_at: "fo1" } }),
      comp("fiber_out", "fo1", 300, 400, 0, { props: fiberOutProps }),
      comp("detector", "det1", 500, 400, 180),
    ]);

  it("the fibre output emits the wavelengths that reach the fibre input, after all direct sources", () => {
    const t = traceLayout(link({ wavelength_nm: 532 }));
    assert.deepEqual(
      t.map((s) => [s.source, s.wavelength_nm]),
      [
        ["las1", 532],
        ["las1", 1064],
        ["fo1", 532],
        ["fo1", 1064],
      ],
    );
    assert.equal(t[2]?.branches[0]?.events.at(-1)?.component, "det1");
    assert.equal(t[2]?.branches[0]?.events.at(-1)?.path_mm, 200, "the path starts again at the fibre output");
  });

  it("a fibre output that a fibre input continues at never emits from its own settings", () => {
    const t = traceLayout(link({ wavelength_nm: 800 }));
    assert.deepEqual(
      t.filter((s) => s.source === "fo1").map((s) => s.wavelength_nm),
      [532, 1064],
      "800 nm is its own setting and is not used",
    );
    // and with nothing reaching the fibre input it is dark
    const dark = layoutOf([
      comp("fiber_in", "fi1", 150, 62.5, 180, { props: { continues_at: "fo1" } }),
      comp("fiber_out", "fo1", 300, 400, 0, { props: { wavelength_nm: 800 } }),
    ]);
    assert.deepEqual(traceLayout(dark), []);
  });

  it("continuation trees come after every direct tree, in the order they were found", () => {
    const l = layoutOf([
      comp("fiber_out", "fo1", 300, 400, 0, { props: { wavelength_nm: 532 } }),
      comp("laser", "las1", 50, 62.5, 0, { props: { wavelengths_nm: [532, 1064] } }),
      comp("fiber_in", "fi1", 150, 62.5, 180, { props: { continues_at: "fo1" } }),
      comp("laser", "las2", 50, 200, 0, { props: { wavelength_nm: 633 } }),
    ]);
    assert.deepEqual(
      traceLayout(l).map((s) => [s.source, s.wavelength_nm]),
      [
        ["las1", 532],
        ["las1", 1064],
        ["las2", 633],
        ["fo1", 532],
        ["fo1", 1064],
      ],
    );
  });

  it("chains of fibres follow all the way", () => {
    const l = layoutOf([
      comp("laser", "las1", 50, 62.5, 0, { props: { wavelength_nm: 532 } }),
      comp("fiber_in", "fi1", 150, 62.5, 180, { props: { continues_at: "fo1" } }),
      comp("fiber_out", "fo1", 300, 400, 0, { props: {} }),
      comp("fiber_in", "fi2", 400, 400, 180, { props: { continues_at: "fo2" } }),
      comp("fiber_out", "fo2", 300, 700, 0, { props: {} }),
      comp("detector", "det1", 500, 700, 180),
    ]);
    const t = traceLayout(l);
    assert.deepEqual(
      t.map((s) => [s.source, s.wavelength_nm]),
      [
        ["las1", 532],
        ["fo1", 532],
        ["fo2", 532],
      ],
    );
    assert.equal(t[2]?.branches[0]?.events.at(-1)?.component, "det1");
  });

  it("a fibre loop does not run away: one tree per fibre output and wavelength", () => {
    const l = layoutOf([
      comp("laser", "las1", 50, 62.5, 0, { props: { wavelength_nm: 532 } }),
      comp("fiber_in", "fi1", 150, 62.5, 180, { props: { continues_at: "fo1" } }),
      comp("fiber_out", "fo1", 300, 400, 0, { props: { wavelength_nm: 532 } }),
      comp("fiber_in", "fi2", 500, 400, 180, { props: { continues_at: "fo1" } }),
    ]);
    assert.deepEqual(
      traceLayout(l).map((s) => [s.source, s.wavelength_nm]),
      [
        ["las1", 532],
        ["fo1", 532],
      ],
    );
  });

  it("a fibre input that continues nowhere just ends the beam", () => {
    const l = layoutOf([
      comp("laser", "las1", 50, 62.5, 0, { props: { wavelength_nm: 532 } }),
      comp("fiber_in", "fi1", 150, 62.5, 180, { props: { continues_at: "nothing" } }),
    ]);
    assert.equal(traceLayout(l).length, 1);
    assert.equal(single(traceLayout(l)).end, "stop");
  });
});

describe("the contract form", () => {
  it("carries exactly the keys of section 4, rounded to 0.001", () => {
    const l = layoutOf([
      comp("laser", "las1", 50, 62.5, 0, { props: { wavelength_nm: 532 } }),
      comp("grating", "gr1", 112.5004, 62.5, 135),
      comp("detector", "det1", 112.5, 200, 270),
    ]);
    const t = traceLayout(l);
    const b = t[0]!.branches[0]!;
    assert.deepEqual(Object.keys(t[0]!).sort(), ["branches", "source", "wavelength_nm"]);
    assert.deepEqual(Object.keys(b).sort(), ["branch", "end", "events", "fraction", "segments"]);
    assert.deepEqual(Object.keys(b.segments[0]!).sort(), ["x0", "x1", "y0", "y1"]);
    assert.deepEqual(Object.keys(b.events[0]!).sort(), ["action", "component", "incidence_deg", "offset_mm", "path_mm", "x", "y"]);
    assert.deepEqual(Object.keys(b.events[1]!).sort(), ["action", "component", "incidence_deg", "offset_mm", "path_mm", "x", "y"]);
    for (const e of b.events) for (const v of [e.x, e.y, e.offset_mm, e.incidence_deg, e.path_mm]) assert.equal(v, Math.round(v * 1000) / 1000);
  });

  it("the detailed form keeps the exact numbers and the incoming direction", () => {
    const d = traceLayoutDetailed(foldLayout({ laserY: 65.5 }));
    const e = d[0]!.branches[0]!.events[0]!;
    near(e.dir.x, 1, 1e-12);
    near(e.dir.y, 0, 1e-12);
    near(e.pos.x, 115.5, 1e-9);
    assert.notEqual(e.offset_mm, Math.round(e.offset_mm * 1000) / 1000, "not rounded");
    assert.deepEqual(toContractTrace(d), traceLayout(foldLayout({ laserY: 65.5 })));
  });

  it("survives a trip through JSON unchanged (no -0, no NaN, no undefined)", () => {
    const t = traceLayout(foldLayout());
    assert.deepEqual(JSON.parse(JSON.stringify(t)), t);
    assert.ok(Object.is(t[0]!.branches[0]!.events[0]!.offset_mm, 0), "a hit on the centre has offset +0");
  });
});

describe("lookups over a trace", () => {
  it("events are grouped by component", () => {
    const m = eventsByComponent(traceLayoutDetailed(foldLayout()));
    assert.deepEqual([...m.keys()].sort(), ["det1", "m1"]);
    assert.equal(m.get("m1")?.[0]?.source, "las1");
    assert.equal(m.get("m1")?.[0]?.index, 0);
  });

  it("a branch becomes a polyline", () => {
    const d = traceLayoutDetailed(foldLayout());
    const pts = branchPoints(d[0]!.branches[0]!);
    assert.equal(pts.length, 3);
    assert.deepEqual(pts[0], { x: 50, y: 62.5 });
    near(pts[2]!.y, 200, 1e-9);
    assert.deepEqual(branchPoints({ segments: [] }), []);
  });
});

describe("the tracer on a big layout", () => {
  it("handles 400 components and 32 sources without noticeable delay", () => {
    const comps = [];
    for (let i = 0; i < 32; i++) comps.push(comp("laser", `las${i}`, 20, 20 + i * 25, 0, { props: { wavelengths_nm: [532, 1064] } }));
    for (let i = 0; i < 368; i++) {
      const col = i % 23;
      const row = Math.floor(i / 23);
      comps.push(comp(i % 3 === 0 ? "beam_splitter" : "mirror", `c${i}`, 100 + col * 45, 20 + row * 48, 135, { aperture: 12 }));
    }
    const l = layoutOf(comps);
    const started = performance.now();
    const t = traceLayoutDetailed(l);
    const ms = performance.now() - started;
    assert.equal(t.length, 64);
    assert.ok(ms < 1500, `took ${ms.toFixed(0)} ms`);
  });
});

describe("a tie in nearest hit (section 11.2 point 6)", () => {
  // Two components whose working surfaces are collinear and overlap are crossed by a ray at one and the same
  // point, so their t are equal in mathematics and differ in the last bit of a double: which one is "nearer"
  // was rounding noise (Python and JavaScript disagree even on sin(45 deg) to the last bit). The rule is
  // explicit: a later candidate replaces the best only when it is nearer by more than 1e-9 mm, so among ties
  // the component listed first wins.
  const u = { x: -Math.sin(225 * (Math.PI / 180)), y: Math.cos(225 * (Math.PI / 180)) };
  const detectorAt = (id: string, x: number, y: number) => ({ ...comp("detector", id, 0, 0, 225, { aperture: 40 }), x_mm: x, y_mm: y });
  const a = detectorAt("a", 100, 100);
  const b = detectorAt("b", 100 + 10 * u.x, 100 + 10 * u.y); // 10 mm along the same surface
  const laserAt = (y: number) => ({ ...comp("laser", "las1", 50, 0, 0, { props: { wavelength_nm: 532 } }), y_mm: y });
  const hit = (parts: ReturnType<typeof detectorAt>[], y: number) => traceLayout(layoutOf([laserAt(y), ...parts]))[0]!.branches[0]!.events[0]?.component;

  it("is 1e-9 mm", () => {
    assert.equal(TRACE_LIMITS.tieEps, 1e-9);
  });

  it("collinear overlapping surfaces: the component listed first is hit, at every beam position and in both orders", () => {
    const wrong: string[] = [];
    for (let i = 0; i <= 1000; i++) {
      const y = Math.round((95 + i * 0.01) * 100) / 100; // a beam that crosses the surfaces of both
      if (hit([a, b], y) !== "a") wrong.push(`a,b at y=${y}`);
      if (hit([b, a], y) !== "b") wrong.push(`b,a at y=${y}`);
    }
    assert.deepEqual(wrong.slice(0, 6), [], `${wrong.length} beam positions named the one listed later`);
  });

  it("two detectors at the very same place: the one listed first", () => {
    const same = detectorAt("s2", 100, 100);
    assert.equal(hit([a, same], 100), "a");
    assert.equal(hit([same, a], 100), "s2");
  });

  // a beam along +x at y = 50 meets detectors that face it; 'near' is nearer than 'far' by delta
  const facing = (id: string, x: number) => ({ ...comp("detector", id, 0, 50, 180), x_mm: x });
  const onAxis = (parts: ReturnType<typeof facing>[]) => traceLayout(layoutOf([comp("laser", "las1", 50, 50, 0, { props: { wavelength_nm: 532 } }), ...parts]))[0]!.branches[0]!.events[0]?.component;

  it("nearer by less than 1e-9 mm is a tie: the one listed first wins even when the other is nearer", () => {
    const far = facing("far", 100);
    const near = facing("near", 100 - 5e-10);
    assert.equal(onAxis([far, near]), "far");
    assert.equal(onAxis([near, far]), "near");
  });

  it("nearer by more than 1e-9 mm is a real difference: the nearer one wins whichever is listed first", () => {
    const far = facing("far", 100);
    const near = facing("near", 100 - 3e-9);
    assert.equal(onAxis([far, near]), "near");
    assert.equal(onAxis([near, far]), "near");
    // and a difference nobody would call noise
    assert.equal(onAxis([far, facing("near", 99.99)]), "near");
  });
});
