// ════════════════════════════════════════════════════════════════════════════
// The beam props of docs/v2/design/optical_layout_v2.md section 3 as the editor handles them:
// what the catalogue offers, how values are rounded, what early validation refuses (the
// server's rules in MASTv2/mast/optical_layout/model.py, including the two cross-field ones),
// what the setters drop so that a layout can still be saved, how a trace with beam keys is read,
// and the round trip the inspector form makes: edit -> check -> body of the PUT -> read back.
//
//     cd frontend && npm run test:unit
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it } from "node:test";

import { CATALOG, allowedPropKeys, dependencyMet, propField, propFields, typeLabel, typeNote, visiblePropFields } from "../src/lib/opticalLayout/catalog.ts";
import { changeType, normalizePropValue, setProp } from "../src/lib/opticalLayout/edit.ts";
import { errorsOf, propNumberInRange, validateLayout } from "../src/lib/opticalLayout/validate.ts";
import { layoutForWrite, parseLayout, parseTrace } from "../src/lib/opticalLayout/parse.ts";
import { traceLayout } from "../src/lib/opticalLayout/trace.ts";
import { COMPONENT_TYPES, POLARIZATIONS, THROUGHPUT_TYPES } from "../src/lib/opticalLayout/types.ts";
import type { ComponentType, OpticalLayout } from "../src/lib/opticalLayout/types.ts";
import { comp, firstDifference, layoutOf } from "./helpers/opticalLayout.ts";

const paths = (l: OpticalLayout) => errorsOf(validateLayout(l)).map((i) => i.path);
const one = (type: ComponentType, props: Record<string, unknown>) => layoutOf([comp(type, "c1", 100, 100, 0, { props })]);
const SOURCE = { wavelength_nm: 532 };

describe("the catalogue has the props of v2 section 3.2", () => {
  it("pbs: a splitter of 25.4 mm with no props, and a note on what it does", () => {
    assert.equal(CATALOG.pbs.role, "splitter");
    assert.equal(CATALOG.pbs.category, "steer");
    assert.equal(CATALOG.pbs.defaultAperture, 25.4);
    assert.deepEqual(CATALOG.pbs.defaultProps(), {});
    assert.deepEqual(allowedPropKeys("pbs"), ["z_mm"], "no parameter of its own; the height every component has (3D contract section 2)");
    assert.equal(typeLabel("pbs"), "偏振分束器");
    assert.match(typeNote("pbs") ?? "", /透射 p/);
    assert.equal(typeNote("mirror"), null);
    assert.ok(COMPONENT_TYPES.indexOf("pbs") === COMPONENT_TYPES.indexOf("beam_splitter") + 1, "next to the beam splitter in the palette");
  });

  it("ranges and rounding exactly as the table says", () => {
    const f = (type: ComponentType, key: string) => {
      const x = propField(type, key);
      assert.ok(x, `${type}.${key}`);
      return { kind: x.kind, min: x.min, max: x.max, minExclusive: x.minExclusive ?? false, decimals: x.decimals ?? 3, dependsOn: x.dependsOn ?? null };
    };
    for (const t of ["laser", "fiber_out"] as const) {
      assert.deepEqual(propField(t, "polarization")?.options?.map((o) => o.value), [...POLARIZATIONS]);
      assert.deepEqual(f(t, "pol_angle_deg"), { kind: "number", min: -360, max: 360, minExclusive: false, decimals: 2, dependsOn: { key: "polarization", value: "linear" } });
      assert.deepEqual(f(t, "beam_diameter_mm"), { kind: "number", min: 0, max: 1000, minExclusive: true, decimals: 6, dependsOn: null });
      assert.deepEqual(f(t, "m2"), { kind: "number", min: 1, max: 1000, minExclusive: false, decimals: 3, dependsOn: { key: "beam_diameter_mm" } });
    }
    assert.deepEqual(f("fiber_in", "coupling"), { kind: "number", min: 0, max: 1, minExclusive: false, decimals: 6, dependsOn: null });
    for (const t of THROUGHPUT_TYPES) assert.deepEqual(f(t, "throughput"), { kind: "number", min: 0, max: 1, minExclusive: false, decimals: 6, dependsOn: null }, t);
    for (const t of COMPONENT_TYPES) {
      if (!(THROUGHPUT_TYPES as readonly string[]).includes(t)) assert.equal(propField(t, "throughput"), undefined, t);
    }
    for (const t of ["lamp", "led"] as const) for (const k of ["polarization", "beam_diameter_mm", "power_mw"]) assert.equal(propField(t, k), undefined, `${t}.${k}`);
  });

  it("every new field has a label and a hint", () => {
    for (const t of COMPONENT_TYPES) {
      for (const fl of propFields(t)) {
        if (!["polarization", "pol_angle_deg", "beam_diameter_mm", "m2", "coupling", "throughput"].includes(fl.key)) continue;
        assert.ok(fl.label.length > 0 && (fl.hint ?? "").length > 0, `${t}.${fl.key}`);
      }
    }
  });
});

describe("rounding of the beam props (the server's decimals)", () => {
  it("angle 0.01, diameter 1e-6, M² 0.001, coupling and throughput 1e-6, no -0", () => {
    assert.equal(normalizePropValue("laser", "pol_angle_deg", 12.345), 12.35);
    assert.equal(normalizePropValue("laser", "beam_diameter_mm", 0.12345678), 0.123457);
    assert.equal(normalizePropValue("fiber_out", "m2", 1.23456), 1.235);
    assert.equal(normalizePropValue("fiber_in", "coupling", 0.6666666), 0.666667);
    assert.equal(normalizePropValue("mirror", "throughput", 0.9999994), 0.999999);
    assert.ok(Object.is(normalizePropValue("laser", "pol_angle_deg", -0.001), 0));
  });
});

describe("early validation of the beam props says what the server would say", () => {
  const bad = (type: ComponentType, props: Record<string, unknown>, ok = false) => {
    const base = type === "laser" || type === "fiber_out" ? SOURCE : {};
    const p = paths(one(type, { ...base, ...props }));
    assert.equal(p.length === 0, ok, `${type} ${JSON.stringify(props)} -> ${p.join(",")}`);
  };

  it("ranges", () => {
    bad("laser", { polarization: "circular" });
    bad("laser", { polarization: "linear" }, true);
    bad("laser", { polarization: "unpolarized" }, true);
    bad("laser", { polarization: "linear", pol_angle_deg: 360 }, true);
    bad("laser", { polarization: "linear", pol_angle_deg: 360.5 });
    bad("laser", { beam_diameter_mm: 0 });
    bad("laser", { beam_diameter_mm: 1000 }, true);
    bad("laser", { beam_diameter_mm: 1000.1 });
    bad("laser", { beam_diameter_mm: 1, m2: 1 }, true);
    bad("laser", { beam_diameter_mm: 1, m2: 0.99 });
    bad("laser", { beam_diameter_mm: 1, m2: 1001 });
    bad("fiber_in", { coupling: 0 }, true);
    bad("fiber_in", { coupling: 1.01 });
    bad("mirror", { throughput: 1 }, true);
    bad("mirror", { throughput: -0.1 });
    bad("polarizer", { throughput: 0 }, true);
    bad("pbs", { reflect_fraction: 0.5 }, false);
    bad("nd", { throughput: 0.5 }, false);
    bad("dichroic", { edge_nm: 600, throughput: 0.5 }, false);
    bad("lamp", { polarization: "linear" }, false);
  });

  it("an open lower bound is checked on the value the server would store: 4e-7 mm of diameter is kept as 0", () => {
    bad("laser", { beam_diameter_mm: 4e-7 });
    bad("laser", { beam_diameter_mm: 6e-7 }, true);
    assert.equal(propNumberInRange(propField("laser", "wavelength_nm")!, 0.0004), false, "0.0004 nm is stored as 0.0");
    assert.equal(propNumberInRange(propField("laser", "wavelength_nm")!, 0.0006), true, "0.0006 nm is stored as 0.001");
  });

  it("pol_angle_deg only with linear polarisation, m2 only with a diameter (otherwise 422)", () => {
    const at = (props: Record<string, unknown>) => paths(one("laser", { ...SOURCE, ...props }));
    assert.deepEqual(at({ pol_angle_deg: 30 }), ["components[c1].props.pol_angle_deg"]);
    assert.deepEqual(at({ polarization: "unpolarized", pol_angle_deg: 30 }), ["components[c1].props.pol_angle_deg"]);
    assert.deepEqual(at({ polarization: null, pol_angle_deg: 30 }), ["components[c1].props.pol_angle_deg"], "null is not given");
    assert.deepEqual(at({ polarization: "linear", pol_angle_deg: 30 }), []);
    assert.deepEqual(at({ polarization: "linear", pol_angle_deg: null }), [], "a null angle is not given");
    assert.deepEqual(at({ m2: 1.2 }), ["components[c1].props.m2"]);
    assert.deepEqual(at({ beam_diameter_mm: null, m2: 1.2 }), ["components[c1].props.m2"]);
    assert.deepEqual(at({ beam_diameter_mm: 2, m2: 1.2 }), []);
    const msg = errorsOf(validateLayout(one("laser", { ...SOURCE, pol_angle_deg: 30 })))[0]!.message;
    assert.match(msg, /线偏振/, "the message names the value it needs");
  });

  it("a new component of every type, the new pbs included, passes without further input", () => {
    for (const t of COMPONENT_TYPES) {
      const l = layoutOf([comp(t, `${CATALOG[t].prefix}1`, 100, 100, CATALOG[t].defaultAngle)]);
      assert.deepEqual(paths(l), [], t);
    }
  });
});

describe("the setters keep a layout saveable", () => {
  const laser = (props: Record<string, unknown>) => layoutOf([comp("laser", "las1", 0, 0, 0, { props: { ...SOURCE, ...props } })]);
  const props = (l: OpticalLayout) => l.components[0]!.props;

  it("switching to unpolarised light, or clearing the polarisation, removes the angle", () => {
    const l = laser({ polarization: "linear", pol_angle_deg: 30, power_mw: 5 });
    assert.deepEqual(props(setProp(l, "las1", "polarization", "unpolarized")), { ...SOURCE, power_mw: 5, polarization: "unpolarized" });
    assert.deepEqual(props(setProp(l, "las1", "polarization", undefined)), { ...SOURCE, power_mw: 5 });
    assert.deepEqual(props(setProp(l, "las1", "polarization", "linear")), props(l), "linear again: nothing changes");
  });

  it("clearing the diameter removes M²; changing it keeps M²", () => {
    const l = laser({ beam_diameter_mm: 2, m2: 1.3 });
    assert.deepEqual(props(setProp(l, "las1", "beam_diameter_mm", null)), { ...SOURCE });
    assert.deepEqual(props(setProp(l, "las1", "beam_diameter_mm", 3)), { ...SOURCE, beam_diameter_mm: 3, m2: 1.3 });
  });

  it("setting a dependent prop itself is kept (validation says what is missing), nothing just set disappears", () => {
    const l = setProp(laser({}), "las1", "pol_angle_deg", 10);
    assert.equal(props(l)["pol_angle_deg"], 10);
    assert.deepEqual(paths(l), ["components[las1].props.pol_angle_deg"]);
  });

  it("changing the type keeps the props the new type has", () => {
    const l = laser({ polarization: "linear", pol_angle_deg: 30, beam_diameter_mm: 1, m2: 1.1 });
    assert.deepEqual(props(changeType(l, "las1", "fiber_out")), props(l), "a fibre output has the same beam props");
    assert.deepEqual(props(changeType(l, "las1", "lamp")), { ...SOURCE }, "a lamp has none of them");
    const m = layoutOf([comp("mirror", "m", 0, 0, 135, { props: { throughput: 0.9 } })]);
    assert.deepEqual(props(changeType(m, "m", "lens")), { throughput: 0.9 });
    assert.deepEqual(props(changeType(m, "m", "pbs")), {});
    assert.deepEqual(props(changeType(m, "m", "beam_splitter")), {}, "a beam splitter has no throughput (its default fraction is not required)");
  });
});

describe("the form shows a dependent field only where it means something", () => {
  const keys = (type: ComponentType, p: Record<string, unknown>) => visiblePropFields(type, p).map((f) => f.key);

  it("the angle with linear light, M² with a diameter", () => {
    assert.deepEqual(keys("laser", { ...SOURCE }), ["wavelength_nm", "wavelengths_nm", "power_mw", "polarization", "beam_diameter_mm"]);
    assert.deepEqual(keys("laser", { ...SOURCE, polarization: "linear", beam_diameter_mm: 1 }), ["wavelength_nm", "wavelengths_nm", "power_mw", "polarization", "pol_angle_deg", "beam_diameter_mm", "m2"]);
    assert.deepEqual(keys("laser", { ...SOURCE, polarization: "unpolarized" }), ["wavelength_nm", "wavelengths_nm", "power_mw", "polarization", "beam_diameter_mm"]);
  });

  it("a value the server would refuse stays visible so that it can be cleared", () => {
    assert.ok(keys("laser", { ...SOURCE, pol_angle_deg: 5 }).includes("pol_angle_deg"));
    assert.equal(dependencyMet(propField("laser", "pol_angle_deg")!, { pol_angle_deg: 5 }), false);
  });
});

describe("reading a trace with beam keys", () => {
  it("the keys of section 4.7 are read; a trace of an older server has none", () => {
    const raw = [
      {
        source: "las1",
        wavelength_nm: 532,
        beam: { source_mw: 50, m2: 1 },
        branches: [
          {
            branch: "0",
            fraction: 1,
            segments: [{ x0: 0, y0: 0, x1: 1, y1: 0, beam: { p: 1, s: [1, 0, 0], q: [0, 1476.3] } }],
            events: [{ component: "d", action: "stop", x: 1, y: 0, offset_mm: 0, incidence_deg: 0, path_mm: 1, beam: { p: 1, mw: 50, d_mm: 1.000034, s: [1, 0, 0] } }],
            end: "stop",
          },
        ],
      },
    ];
    const t = parseTrace(raw)!;
    assert.equal(firstDifference(t, raw), null);
    const old = parseTrace([{ source: "las1", wavelength_nm: 532, branches: [{ branch: "0", fraction: 1, segments: [{ x0: 0, y0: 0, x1: 1, y1: 0 }], events: [], end: "left_table" }] }])!;
    assert.equal("beam" in old[0]!, false);
    assert.equal("beam" in old[0]!.branches[0]!.segments[0]!, false);
  });

  it("a beam value of the wrong shape is not known (null), never a made-up number", () => {
    const t = parseTrace([
      {
        source: "las1",
        wavelength_nm: 532,
        beam: { source_mw: "50", m2: null },
        branches: [
          {
            branch: "0",
            fraction: 1,
            segments: [
              { x0: 0, y0: 0, x1: 1, y1: 0, beam: { p: 0.5, s: [1, 0], q: [0, -3] } },
              { x0: 1, y0: 0, x1: 2, y1: 0, beam: { p: "much" } },
            ],
            events: [],
            end: "left_table",
          },
        ],
      },
    ])!;
    assert.deepEqual(t[0]!.beam, { source_mw: null, m2: null });
    assert.deepEqual(t[0]!.branches[0]!.segments[0]!.beam, { p: 0.5, s: null, q: null }, "a Stokes triple of two, a negative zR");
    assert.equal(t[0]!.branches[0]!.segments[1]!.beam, undefined, "no power: the beam is not read at all");
  });
});

describe("the inspector's round trip: edit -> check -> body of the PUT -> read back", () => {
  it("every new prop survives it, rounded the way the server keeps it", () => {
    let l = layoutOf([
      comp("laser", "las1", 50, 100, 0, { props: { ...SOURCE } }),
      comp("waveplate", "hwp", 150, 100, 0, { props: { retardance: "half" } }),
      comp("pbs", "pbs1", 250, 100, 135),
      comp("mirror", "m1", 350, 100, 135),
      comp("fiber_in", "fi", 350, 300, 270, { props: { continues_at: "fo" } }),
      comp("fiber_out", "fo", 50, 500, 0, { props: { ...SOURCE } }),
      comp("detector", "d", 450, 500, 180),
      comp("beam_dump", "bd", 250, 300, 270),
    ]);
    // what the form commits, field by field
    l = setProp(l, "las1", "power_mw", 50);
    l = setProp(l, "las1", "polarization", "linear");
    l = setProp(l, "las1", "pol_angle_deg", 12.3456);
    l = setProp(l, "las1", "beam_diameter_mm", 0.70000049);
    l = setProp(l, "las1", "m2", 1.10049);
    l = setProp(l, "hwp", "fast_axis_deg", 11.25);
    l = setProp(l, "hwp", "throughput", 0.995);
    l = setProp(l, "m1", "throughput", 0.98);
    l = setProp(l, "fi", "coupling", 0.6);
    l = setProp(l, "fo", "polarization", "unpolarized");
    l = setProp(l, "fo", "beam_diameter_mm", 0.004);
    assert.deepEqual(errorsOf(validateLayout(l)), []);
    const body = JSON.parse(JSON.stringify({ layout: layoutForWrite(l), base_revision: 0, by: "ana" }));
    const back = parseLayout(body.layout);
    const props = Object.fromEntries(back.components.map((c) => [c.id, c.props]));
    assert.deepEqual(props["las1"], { wavelength_nm: 532, power_mw: 50, polarization: "linear", pol_angle_deg: 12.35, beam_diameter_mm: 0.7, m2: 1.1 });
    assert.deepEqual(props["hwp"], { retardance: "half", fast_axis_deg: 11.25, throughput: 0.995 });
    assert.deepEqual(props["pbs1"], {});
    assert.deepEqual(props["m1"], { throughput: 0.98 });
    assert.deepEqual(props["fi"], { continues_at: "fo", coupling: 0.6 });
    assert.deepEqual(props["fo"], { wavelength_nm: 532, polarization: "unpolarized", beam_diameter_mm: 0.004 });
    // normalising what came back changes nothing (the server keeps what it was sent)
    for (const c of back.components) for (const [k, v] of Object.entries(c.props)) assert.deepEqual(normalizePropValue(c.type, k, v), v, `${c.id}.${k}`);
    // and the trace of what was read back carries the physics
    const t = traceLayout(back);
    assert.deepEqual(t.map((x) => x.source), ["las1", "fo"]);
    assert.equal(t[1]!.beam!.source_mw! > 0, true);
  });

  it("switching the form to unpolarised and clearing the diameter leaves a body the server accepts", () => {
    let l = layoutOf([comp("laser", "las1", 50, 100, 0, { props: { ...SOURCE, polarization: "linear", pol_angle_deg: 30, beam_diameter_mm: 1, m2: 1.2 } })]);
    l = setProp(l, "las1", "polarization", "unpolarized");
    l = setProp(l, "las1", "beam_diameter_mm", undefined);
    assert.deepEqual(errorsOf(validateLayout(l)), []);
    assert.deepEqual(layoutForWrite(l).components[0]!.props, { ...SOURCE, polarization: "unpolarized" });
  });
});

// The components are .tsx (Node cannot run JSX): what connects the logic above to the inspector and the
// check panel is pinned from source, in the style of opticalLayoutWiring.test.ts.
describe("wiring of the beam into the inspector and the check panel", () => {
  const SRC = join(fileURLToPath(new URL(".", import.meta.url)), "..", "src");
  const read = (...p: string[]) => readFileSync(join(SRC, ...p), "utf8");

  it("the inspector shows the fields visiblePropFields gives, a type note, and the beam arriving at each hit", () => {
    const form = read("components", "opticallayout", "ComponentForm.tsx");
    assert.match(form, /const fields = visiblePropFields\(c\.type, c\.props\)/);
    assert.match(form, /\{fields\.map\(\(f\) => \(\s*<PropEditor key=\{f\.key\}/);
    assert.match(form, /typeNote\(c\.type\)/);
    assert.match(form, /<BeamLine trace=\{trace\} h=\{h\} \/>/);
    assert.match(form, /powerText\(b\.p, b\.mw\)/);
    assert.match(form, /polarizationText\(b\.s\)/);
    // every edit of a prop goes through setProp, which drops what stops meaning something
    assert.match(form, /edit\(\(l\) => setProp\(l, c\.id, field\.key, value\)\)/);
  });

  it("the check panel lists the beam checks of the layout it is given with the alignment diagnostics, in the summary's order, each selecting its component", () => {
    const panel = read("components", "opticallayout", "CheckPanel.tsx");
    assert.match(panel, /layout \? beamChecks\(layout, trace\) : \[\]/);
    assert.match(panel, /return orderChecks\(\[\.\.\.diagnostics\.map\(fromDiagnostic\), \.\.\.beam\.map\(fromBeamCheck\)\]\)/);
    assert.match(panel, /onClick=\{\(\) => onSelect\(r\.componentId as string\)\}/);
  });
});
