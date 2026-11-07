// ════════════════════════════════════════════════════════════════════════════
// Tests for the fibre link: which wavelengths arrive at a fibre input
// (trace.ts arrivingWavelengths) and what linking a fibre input to a fibre output does
// to the output (edit.ts linkFibre, setSourceWavelengths).
//
//     cd frontend && npm run test:unit
//
// The backend keeps the rule that a fibre output carries a wavelength of its own, even
// when a fibre input continues at it (docs/v2/design/optical_layout.md section 4: the
// tracer then ignores it and emits what arrives). To make that rule less odd for people,
// linking writes the arriving wavelength into an output nobody has set yet; a value
// somebody typed is never replaced.
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { linkFibre, setSourceWavelengths } from "../src/lib/opticalLayout/edit.ts";
import { arrivingWavelengths, traceLayoutDetailed } from "../src/lib/opticalLayout/trace.ts";
import { errorsOf, validateLayout } from "../src/lib/opticalLayout/validate.ts";
import { comp, layoutOf } from "./helpers/opticalLayout.ts";
import type { OpticalLayout, Props } from "../src/lib/opticalLayout/types.ts";

/** laser -> fibre input fi1 (facing the beam) ... fibre output fo1 -> detector. `outProps` undefined keeps the catalogue default. */
function fibre(laserProps: Props, outProps?: Props, more: { fiAngle?: number; laserY?: number } = {}): OpticalLayout {
  return layoutOf([
    comp("laser", "las1", 50, more.laserY ?? 62.5, 0, { props: laserProps }),
    comp("fiber_in", "fi1", 150, 62.5, more.fiAngle ?? 180),
    comp("fiber_out", "fo1", 300, 400, 0, outProps ? { props: outProps } : {}),
    comp("detector", "det1", 500, 400, 180),
  ]);
}

const propsOf = (l: OpticalLayout, id: string) => l.components.find((c) => c.id === id)!.props;

describe("arrivingWavelengths", () => {
  it("lists what ends on the front of a component, each once, in tree order", () => {
    const l = layoutOf([
      comp("laser", "las1", 50, 62.5, 0, { props: { wavelengths_nm: [532, 1064] } }),
      comp("laser", "las2", 100, 62.5, 0, { props: { wavelength_nm: 532 } }),
      comp("fiber_in", "fi1", 150, 62.5, 180),
    ]);
    assert.deepEqual(arrivingWavelengths(traceLayoutDetailed(l), "fi1"), [532, 1064]);
  });

  it("is empty when nothing reaches it, when the beam meets its back, or when the wavelength is not known", () => {
    assert.deepEqual(arrivingWavelengths(traceLayoutDetailed(fibre({ wavelength_nm: 532 }, undefined, { laserY: 300 })), "fi1"), [], "the beam passes by");
    assert.deepEqual(arrivingWavelengths(traceLayoutDetailed(fibre({ wavelength_nm: 532 }, undefined, { fiAngle: 0 })), "fi1"), [], "the back side of a fibre input is not a coupling");
    const lamp = layoutOf([comp("lamp", "lamp1", 50, 62.5, 0), comp("fiber_in", "fi1", 150, 62.5, 180)]);
    assert.deepEqual(arrivingWavelengths(traceLayoutDetailed(lamp), "fi1"), [], "light of unknown wavelength is not a number");
  });

  it("counts the light that reaches the component after a beam splitter or a mirror, not the light that stops elsewhere", () => {
    const l = layoutOf([
      comp("laser", "las1", 50, 62.5, 0, { props: { wavelength_nm: 532 } }),
      comp("mirror", "m1", 112.5, 62.5, 135),
      comp("fiber_in", "fi1", 112.5, 200, 270),
      comp("fiber_in", "fi2", 400, 400, 180),
    ]);
    const t = traceLayoutDetailed(l);
    assert.deepEqual(arrivingWavelengths(t, "fi1"), [532]);
    assert.deepEqual(arrivingWavelengths(t, "fi2"), []);
    assert.deepEqual(arrivingWavelengths(t, "m1"), [], "a mirror reflects: nothing ends on it");
  });
});

describe("linkFibre", () => {
  it("links and writes the arriving wavelength into an output that still has the catalogue default", () => {
    const l = fibre({ wavelength_nm: 1064 });
    const r = linkFibre(l, "fi1", "fo1", arrivingWavelengths(traceLayoutDetailed(l), "fi1"));
    assert.equal(propsOf(r.layout, "fi1")["continues_at"], "fo1");
    assert.deepEqual(propsOf(r.layout, "fo1"), { wavelength_nm: 1064 });
    assert.deepEqual(r.wavelengths, [1064]);
    assert.deepEqual(errorsOf(validateLayout(r.layout)), []);
  });

  it("several wavelengths go into the list form, and the single form is gone (the server wants exactly one of them)", () => {
    const l = fibre({ wavelengths_nm: [532, 1064] });
    const r = linkFibre(l, "fi1", "fo1", [532, 1064]);
    assert.deepEqual(propsOf(r.layout, "fo1"), { wavelengths_nm: [532, 1064] });
    assert.deepEqual(r.wavelengths, [532, 1064]);
    assert.deepEqual(errorsOf(validateLayout(r.layout)), []);
  });

  it("when what arrives is what the output already says, nothing is written", () => {
    const l = fibre({ wavelength_nm: 532 });
    const r = linkFibre(l, "fi1", "fo1", [532]);
    assert.equal(r.wavelengths, null);
    assert.equal(r.layout.components.find((c) => c.id === "fo1"), l.components.find((c) => c.id === "fo1"), "the output is the same object");
  });

  it("a wavelength somebody set is kept: a number, or a list", () => {
    for (const typed of [{ wavelength_nm: 780 }, { wavelengths_nm: [532, 1064] }]) {
      const l = fibre({ wavelength_nm: 1064 }, typed);
      const r = linkFibre(l, "fi1", "fo1", [1064]);
      assert.deepEqual(propsOf(r.layout, "fo1"), typed, JSON.stringify(typed));
      assert.equal(r.wavelengths, null);
      assert.equal(propsOf(r.layout, "fi1")["continues_at"], "fo1", "the link is made all the same");
    }
  });

  it("the catalogue's number is replaced even when other parameters were set, and those stay", () => {
    const l = fibre({ wavelength_nm: 1064 }, { wavelength_nm: 532, power_mw: 5 });
    const r = linkFibre(l, "fi1", "fo1", [1064]);
    assert.deepEqual(propsOf(r.layout, "fo1"), { wavelength_nm: 1064, power_mw: 5 });
    assert.deepEqual(r.wavelengths, [1064]);
  });

  it("with no light at the input the output is left alone", () => {
    const l = fibre({ wavelength_nm: 1064 }, undefined, { laserY: 300 });
    const r = linkFibre(l, "fi1", "fo1", arrivingWavelengths(traceLayoutDetailed(l), "fi1"));
    assert.equal(r.wavelengths, null);
    assert.deepEqual(propsOf(r.layout, "fo1"), { wavelength_nm: 532 });
    assert.equal(propsOf(r.layout, "fi1")["continues_at"], "fo1");
  });

  it("unlinking removes the link and touches nothing else", () => {
    const linked = linkFibre(fibre({ wavelength_nm: 1064 }), "fi1", "fo1", [1064]).layout;
    const r = linkFibre(linked, "fi1", undefined, [1064]);
    assert.equal("continues_at" in propsOf(r.layout, "fi1"), false);
    assert.equal(r.wavelengths, null);
    assert.equal(r.layout.components.find((c) => c.id === "fo1"), linked.components.find((c) => c.id === "fo1"));
  });

  it("only a fibre output takes a wavelength: pointing at anything else just sets the link", () => {
    const l = fibre({ wavelength_nm: 1064 });
    const r = linkFibre(l, "fi1", "det1", [1064]);
    assert.equal(propsOf(r.layout, "fi1")["continues_at"], "det1");
    assert.equal(r.wavelengths, null);
    assert.equal(r.layout.components.find((c) => c.id === "det1"), l.components.find((c) => c.id === "det1"));
  });

  it("does not change the layout it was given", () => {
    const l = fibre({ wavelength_nm: 1064 });
    const before = JSON.stringify(l);
    linkFibre(l, "fi1", "fo1", [1064]);
    assert.equal(JSON.stringify(l), before);
  });

  it("the list is cut to what the server takes (16)", () => {
    const many = Array.from({ length: 20 }, (_, i) => 400 + i * 10);
    const r = linkFibre(fibre({ wavelength_nm: 1064 }), "fi1", "fo1", many);
    assert.equal((propsOf(r.layout, "fo1")["wavelengths_nm"] as number[]).length, 16);
    assert.deepEqual(r.wavelengths, many.slice(0, 16));
  });
});

describe("setSourceWavelengths", () => {
  it("one value in wavelength_nm, several in wavelengths_nm, never both; none changes nothing", () => {
    const l = fibre({ wavelength_nm: 532 }, { wavelengths_nm: [532, 1064] });
    const one = setSourceWavelengths(l, "fo1", [800]);
    assert.deepEqual(propsOf(one, "fo1"), { wavelength_nm: 800 });
    const two = setSourceWavelengths(one, "fo1", [532, 1064]);
    assert.deepEqual(propsOf(two, "fo1"), { wavelengths_nm: [532, 1064] });
    assert.equal(setSourceWavelengths(l, "fo1", []), l);
  });
});
