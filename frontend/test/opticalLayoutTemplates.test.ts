// ════════════════════════════════════════════════════════════════════════════
// Tests for src/lib/opticalLayout/templates.ts: the assemblies of the palette
// (docs/v2/design/optical_layout_v2.md section 8.6).
//
//     cd frontend && npm run test:unit
//
// Inserting is pure: new ids that nothing has, the limits held, the whole group turned about its anchor.
// And each assembly is traced to check that it does what its name says: the expanders expand a beam by 3 and 2
// (beam.ts's Gaussian optics), the 4f relay keeps it, the attenuator sends the s part to its dump, the fold
// pair walks the beam sideways, the coupler ends the beam on the fibre, the collimator collimates.
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { FREE_ANCHOR_TRIES, TEMPLATES, freeAnchor, insertTemplate, templateById, type Template } from "../src/lib/opticalLayout/templates.ts";
import { traceLayoutDetailed } from "../src/lib/opticalLayout/trace.ts";
import { errorsOf, validateLayout } from "../src/lib/opticalLayout/validate.ts";
import { spotDiameterMm, propagateQ } from "../src/lib/opticalLayout/beam.ts";
import { LIMITS } from "../src/lib/opticalLayout/types.ts";
import { comp, layoutOf } from "./helpers/opticalLayout.ts";

const T = (id: string): Template => {
  const t = templateById(id);
  assert.ok(t, id);
  return t;
};

/** A laser (532 nm, Ø1 mm, linear p, 10 mW) at (0, 300) shining along +x, and a detector far away. */
const withLaser = (more: Record<string, unknown> = {}) =>
  layoutOf([comp("laser", "las1", 0, 300, 0, { props: { wavelength_nm: 532, power_mw: 10, beam_diameter_mm: 1, polarization: "linear", pol_angle_deg: 0, ...more } }), comp("detector", "det1", 900, 300, 180, { aperture: 40 })]);

describe("inserting an assembly", () => {
  it("adds every part with an id nothing has, and says which", () => {
    let l = layoutOf([comp("lens", "len1", 0, 0, 0), comp("lens", "len3", 50, 0, 0)]);
    const r1 = insertTemplate(l, T("kepler_3x"), { x: 100, y: 100 });
    assert.equal(r1.refusal, null);
    assert.deepEqual(r1.ids, ["len2", "len4"], "the lowest free numbers");
    l = r1.layout;
    const r2 = insertTemplate(l, T("kepler_3x"), { x: 100, y: 300 });
    assert.deepEqual(r2.ids, ["len5", "len6"]);
    const ids = r2.layout.components.map((c) => c.id);
    assert.equal(new Set(ids).size, ids.length, "no duplicates");
    assert.equal(r2.layout.components.length, 6);
  });

  it("is one new layout (one undo step) and leaves the input alone", () => {
    const l = withLaser();
    const before = JSON.stringify(l);
    const r = insertTemplate(l, T("hwp_pbs_attenuator"), { x: 100, y: 300 });
    assert.notEqual(r.layout, l);
    assert.equal(JSON.stringify(l), before);
    assert.equal(r.layout.components.length, l.components.length + 3);
  });

  it("every assembly makes a layout the server would take", () => {
    for (const t of TEMPLATES) {
      const r = insertTemplate(withLaser(), t, { x: 200, y: 300 }, 30);
      assert.equal(r.refusal, null, t.id);
      assert.deepEqual(errorsOf(validateLayout(r.layout)), [], t.id);
      for (const id of r.ids) assert.ok(r.layout.components.find((c) => c.id === id)!.annotations.function.length > 0, `${t.id}: ${id} says what it is for`);
      assert.ok(t.name().length > 0 && t.hint().length > 0);
    }
    assert.equal(new Set(TEMPLATES.map((t) => t.id)).size, TEMPLATES.length);
  });

  it("turns the whole group about its anchor", () => {
    const r = insertTemplate(layoutOf([]), T("fold_pair"), { x: 100, y: 100 }, 90);
    const [a, b] = r.ids.map((id) => r.layout.components.find((c) => c.id === id)!);
    assert.deepEqual([a!.x_mm, a!.y_mm, a!.angle_deg], [100, 100, 225]);
    assert.deepEqual([b!.x_mm, b!.y_mm, b!.angle_deg], [50, 100, 45], "the 50 mm offset turned from +y to -x, the angle by 90°");
  });

  it("added twice at the same point, the second copy moves on by a hole instead of hiding exactly under the first", () => {
    const l0 = layoutOf([]);
    const pitch = l0.table.hole_pitch_mm;
    const r1 = insertTemplate(l0, T("fold_pair"), { x: 100, y: 300 });
    const r2 = insertTemplate(r1.layout, T("fold_pair"), { x: 100, y: 300 });
    const first = r1.ids.map((id) => r2.layout.components.find((c) => c.id === id)!);
    const second = r2.ids.map((id) => r2.layout.components.find((c) => c.id === id)!);
    for (const b of second) for (const a of first) assert.ok(Math.hypot(a.x_mm - b.x_mm, a.y_mm - b.y_mm) >= 1, `${b.id} on ${a.id}`);
    assert.deepEqual([second[0]!.x_mm, second[0]!.y_mm], [100 + pitch, 300 - pitch], "one hole right and one down, as a single component");
    // a third time: two holes
    const r3 = insertTemplate(r2.layout, T("fold_pair"), { x: 100, y: 300 });
    const third = r2.layout.components.length === r3.layout.components.length - 2 ? r3.layout.components.find((c) => c.id === r3.ids[0])! : null;
    assert.ok(third && third.x_mm === 100 + 2 * pitch && third.y_mm === 300 - 2 * pitch, JSON.stringify(third));
  });

  it("a free anchor stays where it was put; only a part landing on a component moves it", () => {
    const l = layoutOf([comp("mirror", "m1", 0, 0, 45)]);
    assert.deepEqual(freeAnchor(l, T("kepler_3x"), { x: 200, y: 300 }), { x: 200, y: 300 });
    // the second lens of the expander (dx 100) would land on m1
    const moved = freeAnchor(l, T("kepler_3x"), { x: -T("kepler_3x").parts[1]!.dx, y: 0 });
    assert.notDeepEqual(moved, { x: -T("kepler_3x").parts[1]!.dx, y: 0 });
    assert.ok(FREE_ANCHOR_TRIES === 24);
  });

  it("keeps the limits: too many components or light sources, nothing changes", () => {
    const full = layoutOf(Array.from({ length: LIMITS.components - 1 }, (_, i) => comp("mirror", `m${i + 1}`, i, 0, 0)));
    const r = insertTemplate(full, T("kepler_3x"), { x: 0, y: 0 });
    assert.equal(r.refusal, "components");
    assert.equal(r.layout, full);
    assert.deepEqual(r.ids, []);
    const lit = layoutOf(Array.from({ length: LIMITS.sources }, (_, i) => comp("laser", `l${i + 1}`, 0, i * 10, 0, { props: { wavelength_nm: 532 } })));
    assert.equal(insertTemplate(lit, T("fibre_collimator"), { x: 0, y: 0 }).refusal, "sources");
    assert.equal(insertTemplate(lit, T("kepler_3x"), { x: 0, y: 0 }).refusal, null, "lenses are not sources");
  });
});

describe("each assembly does what its name says", () => {
  /** The beam size (mm) leaving the last component of the trace's first branch path that is a lens, 0 mm after it. */
  function spotAfter(l: ReturnType<typeof withLaser>, x: number): number {
    const tree = traceLayoutDetailed(l)[0]!;
    for (const br of tree.branches) {
      for (const g of br.segments) {
        if (g.x0 <= x + 1e-6 && g.x1 >= x - 1e-6 && Math.abs(g.y0 - 300) < 1e-6 && g.beam?.q) {
          return spotDiameterMm(propagateQ(g.beam.q, x - g.x0), tree.wavelength_nm, tree.beam!.m2)!;
        }
      }
    }
    throw new Error(`no beam at x = ${x}`);
  }

  it("the Keplerian expander makes the beam 3 times wider, the Galilean 2 times, the 4f relay keeps it", () => {
    const kepler = insertTemplate(withLaser(), T("kepler_3x"), { x: 100, y: 300 }).layout;
    const ratioK = spotAfter(kepler, 301) / spotAfter(kepler, 99);
    assert.ok(Math.abs(ratioK - 3) < 0.01, `Kepler: ${ratioK}`);
    const galileo = insertTemplate(withLaser(), T("galilean_2x"), { x: 100, y: 300 }).layout;
    const ratioG = spotAfter(galileo, 151) / spotAfter(galileo, 99);
    assert.ok(Math.abs(ratioG - 2) < 0.01, `Galilean: ${ratioG}`);
    const relay = insertTemplate(withLaser(), T("relay_4f"), { x: 100, y: 300 }).layout;
    const ratioR = spotAfter(relay, 301) / spotAfter(relay, 99);
    assert.ok(Math.abs(ratioR - 1) < 0.01, `4f: ${ratioR}`);
  });

  it("the λ/2 + PBS attenuator lets half of p light through and dumps the other half", () => {
    const r = insertTemplate(withLaser(), T("hwp_pbs_attenuator"), { x: 100, y: 300 });
    const tree = traceLayoutDetailed(r.layout)[0]!;
    const dumpId = r.ids[2]!;
    const toDump = tree.branches.find((b) => b.end === "stop" && b.events.at(-1)?.component === dumpId);
    const toDet = tree.branches.find((b) => b.end === "stop" && b.events.at(-1)?.component === "det1");
    assert.ok(toDump && toDet);
    assert.ok(Math.abs(toDump.events.at(-1)!.beam!.p - 0.5) < 1e-9, `dump: ${toDump.events.at(-1)!.beam!.p}`);
    assert.ok(Math.abs(toDet.events.at(-1)!.beam!.p - 0.5) < 1e-9, `detector: ${toDet.events.at(-1)!.beam!.p}`);
  });

  it("the mirror pair walks the beam 50 mm sideways and sends it on in the same direction", () => {
    const r = insertTemplate(withLaser(), T("fold_pair"), { x: 100, y: 300 });
    const tree = traceLayoutDetailed(r.layout)[0]!;
    const segs = tree.branches[0]!.segments;
    const last = segs.at(-1)!;
    assert.ok(Math.abs(last.y0 - 350) < 1e-6 && Math.abs(last.y1 - 350) < 1e-6 && last.x1 > last.x0, JSON.stringify(last));
  });

  it("the fibre coupler ends the beam on its fibre input, at the lens's focus", () => {
    const r = insertTemplate(withLaser(), T("fibre_coupler"), { x: 100, y: 300 });
    const tree = traceLayoutDetailed(r.layout)[0]!;
    const br = tree.branches[0]!;
    assert.equal(br.end, "stop");
    assert.equal(br.events.at(-1)!.component, r.ids[1]);
    assert.ok(br.events.at(-1)!.beam!.d_mm! < 0.02, `focused to ${br.events.at(-1)!.beam!.d_mm} mm`);
  });

  it("the fibre collimator's output is a beam the lens sends on, nearly parallel", () => {
    const l = insertTemplate(layoutOf([comp("detector", "det1", 500, 0, 180, { aperture: 40 })]), T("fibre_collimator"), { x: 0, y: 0 });
    const fo = l.layout.components.find((c) => c.id === l.ids[0])!;
    const withMode = { ...l.layout, components: l.layout.components.map((c) => (c.id === fo.id ? { ...c, props: { ...c.props, beam_diameter_mm: 0.0035 } } : c)) };
    const tree = traceLayoutDetailed(withMode)[0]!;
    const segs = tree.branches[0]!.segments;
    assert.equal(segs.length, 2, "fibre -> lens -> detector");
    const q = segs[1]!.beam!.q!;
    const d0 = spotDiameterMm(q, tree.wavelength_nm, tree.beam!.m2)!;
    const d1 = spotDiameterMm(propagateQ(q, 400), tree.wavelength_nm, tree.beam!.m2)!;
    assert.ok(d0 > 1 && Math.abs(d1 / d0 - 1) < 0.05, `${d0} mm -> ${d1} mm over 400 mm`);
  });
});
