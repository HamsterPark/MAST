// ════════════════════════════════════════════════════════════════════════════
// Tests for src/lib/opticalLayout/edit.ts and catalog.ts — the operations the editor
// applies to a layout, and what a freshly made component starts with.
//
//     cd frontend && npm run test:unit
//
// Three properties matter more than the individual operations:
//   * nothing mutates its input (undo stores the old object, so a mutation corrupts it);
//   * a component an operation does not touch is the SAME object afterwards (React skips
//     it, and the undo stack shares it instead of copying it);
//   * values go in the way the server saves them (0.01 mm, [0, 360) at 0.01 degree).
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  CATEGORY_ORDER,
  CATALOG,
  allowedPropKeys,
  angleMeaning,
  categoryLabel,
  cleanProps,
  nextId,
  propFields,
  typeLabel,
  typesIn,
} from "../src/lib/opticalLayout/catalog.ts";
import {
  addComponent,
  addRefusal,
  addText,
  changeType,
  clampAperture,
  duplicateComponents,
  emptyLayout,
  idProblem,
  lockedAmong,
  moveBy,
  newComponent,
  removeComponents,
  removeText,
  renameComponent,
  rotateBy,
  rotateGroup,
  setAngle,
  setAnnotation,
  setAperture,
  setLabel,
  setLinks,
  setLocked,
  setPosition,
  setPositions,
  setProp,
  setTable,
  updateText,
} from "../src/lib/opticalLayout/edit.ts";
import { validateComponent } from "../src/lib/opticalLayout/validate.ts";
import { COMPONENT_TYPES, LIMITS, isSourceType } from "../src/lib/opticalLayout/types.ts";
import type { OpticalLayout } from "../src/lib/opticalLayout/types.ts";
import { comp, layoutOf } from "./helpers/opticalLayout.ts";

/** Freezes a layout all the way down: any write to it throws in strict mode. */
function deepFreeze<T>(v: T): T {
  if (v !== null && typeof v === "object" && !Object.isFrozen(v)) {
    Object.freeze(v);
    for (const x of Object.values(v as Record<string, unknown>)) deepFreeze(x);
  }
  return v;
}

const sample = (): OpticalLayout =>
  deepFreeze(
    layoutOf([
      comp("laser", "las1", 50, 62.5, 0, { props: { wavelength_nm: 532 } }),
      comp("mirror", "m1", 112.5, 62.5, 135),
      comp("mirror", "m2", 112.5, 162.5, 315, { locked: true }),
      comp("fiber_out", "fo1", 300, 400, 0, { props: { wavelength_nm: 532 } }),
      comp("fiber_in", "fi1", 200, 62.5, 180, { props: { continues_at: "fo1" } }),
    ]),
  );

describe("the catalogue", () => {
  it("covers every type, each once, in a category the palette shows", () => {
    assert.equal(COMPONENT_TYPES.length, 29);
    for (const t of COMPONENT_TYPES) {
      assert.equal(CATALOG[t].type, t);
      assert.ok(CATEGORY_ORDER.includes(CATALOG[t].category), t);
    }
    const listed = CATEGORY_ORDER.flatMap((c) => typesIn(c));
    assert.deepEqual([...listed].sort(), [...COMPONENT_TYPES].sort());
  });

  it("every type and category has a label, and the label is not the raw id", () => {
    for (const t of COMPONENT_TYPES) assert.ok(typeLabel(t).length > 0 && typeLabel(t) !== t.replace("_", " "), t);
    for (const c of CATEGORY_ORDER) assert.ok(categoryLabel(c).length > 0);
    assert.equal(typeLabel("something-new"), "something-new", "an unknown type is shown as it arrived");
    for (const t of COMPONENT_TYPES) assert.ok(angleMeaning(t).length > 2, t);
  });

  it("default apertures and angles follow section 4 and section 2", () => {
    const ap: Record<string, number> = {
      laser: 2, fiber_out: 2, lamp: 5, led: 5, mirror: 25.4, beam_splitter: 25.4, dichroic: 25.4, grating: 25,
      lens: 25.4, objective: 10, waveplate: 12.7, polarizer: 12.7, filter: 25.4, window: 25.4, nd: 25.4, aotf: 25.4,
      prism: 25.4, periscope: 25.4, iris: 25.4, shutter: 12.7, sample: 10, detector: 10, camera: 10, spectrometer: 10,
      power_meter: 10, beam_dump: 25.4, blocker: 25.4, fiber_in: 5, custom: 25.4,
    };
    for (const t of COMPONENT_TYPES) assert.equal(CATALOG[t].defaultAperture, ap[t], t);
    assert.equal(CATALOG.mirror.defaultAngle, 135, "the standard fold mirror");
    assert.equal(CATALOG.detector.defaultAngle, 180, "faces a beam travelling along +x");
    assert.equal(CATALOG.laser.defaultAngle, 0);
  });

  it("a new component of any type passes validation without further input", () => {
    for (const t of COMPONENT_TYPES) {
      const c = newComponent(t, `${CATALOG[t].prefix}1`, { x: 100, y: 100 });
      const errors = validateComponent(c).filter((i) => i.level === "error");
      assert.deepEqual(errors, [], t);
    }
  });

  it("type parameters are exactly the keys of section 4", () => {
    assert.deepEqual(allowedPropKeys("laser"), ["wavelength_nm", "wavelengths_nm", "power_mw"]);
    assert.deepEqual(allowedPropKeys("dichroic"), ["edge_nm", "reflects"]);
    assert.deepEqual(allowedPropKeys("beam_splitter"), ["reflect_fraction"]);
    assert.deepEqual(allowedPropKeys("filter"), ["pass_min_nm", "pass_max_nm"]);
    assert.deepEqual(allowedPropKeys("fiber_in"), ["continues_at"]);
    assert.deepEqual(allowedPropKeys("custom"), ["behavior"]);
    assert.deepEqual(allowedPropKeys("mirror"), []);
    for (const t of COMPONENT_TYPES) {
      for (const f of propFields(t)) assert.ok(f.label.length > 0, `${t}.${f.key}`);
    }
  });

  it("cleanProps drops what the type does not have", () => {
    assert.deepEqual(cleanProps("mirror", { edge_nm: 500 }), {});
    assert.deepEqual(cleanProps("dichroic", { edge_nm: 500, od: 3 }), { edge_nm: 500 });
  });

  it("ids are the lowest unused number after the prefix", () => {
    assert.equal(nextId("m", new Set()), "m1");
    assert.equal(nextId("m", new Set(["m1", "m2", "m4"])), "m3");
  });
});

describe("adding", () => {
  it("makes a component with an id, a label, the defaults, and the position at 0.01 mm", () => {
    const { layout, id } = addComponent(emptyLayout("a", "A"), "mirror", { x: 112.456, y: 62.5 });
    assert.equal(id, "m1");
    const c = layout.components[0]!;
    assert.equal(c.label, "M1");
    assert.equal(c.x_mm, 112.46);
    assert.equal(c.angle_deg, 135);
    assert.equal(c.aperture_mm, 25.4);
    assert.equal(c.locked, false);
    assert.deepEqual(c.links, { device_id: null, slot: null });
    assert.equal(c.annotations.motorized, false);
    assert.deepEqual(c.annotations.extra, []);
  });

  it("ids do not repeat, also against text labels", () => {
    let l = emptyLayout("a", "A");
    for (let i = 0; i < 3; i++) l = addComponent(l, "mirror", { x: i, y: 0 }).layout;
    assert.deepEqual(l.components.map((c) => c.id), ["m1", "m2", "m3"]);
    const noM2 = removeComponents(l, ["m2"]);
    assert.equal(addComponent(noM2, "mirror", { x: 0, y: 0 }).id, "m2", "the gap is reused");
    const t = addText(emptyLayout("a", "A"), { x: 0, y: 0 }, "hi");
    assert.equal(t.id, "t1");
  });

  it("refuses beyond 400 components and 32 sources", () => {
    const many = layoutOf(Array.from({ length: LIMITS.components }, (_, i) => comp("mirror", `m${i + 1}`, i, 0, 0)));
    assert.equal(addRefusal(many, "mirror"), "components");
    const r = addComponent(many, "mirror", { x: 0, y: 0 });
    assert.equal(r.id, null);
    assert.equal(r.layout, many);

    const lasers = layoutOf(Array.from({ length: LIMITS.sources }, (_, i) => comp("laser", `las${i + 1}`, i, 0, 0)));
    assert.equal(addRefusal(lasers, "laser"), "sources");
    assert.equal(addRefusal(lasers, "mirror"), null);
    assert.equal(isSourceType("led"), true);
  });
});

describe("no operation mutates its input", () => {
  it("(the sample layout is frozen all the way down; a write would throw)", () => {
    const l = sample();
    moveBy(l, ["m1"], 1, 2);
    rotateBy(l, ["m1"], 10);
    rotateGroup(l, ["m1", "las1"], 10, { x: 0, y: 0 });
    setPosition(l, "m1", 1, 2);
    setPositions(l, new Map([["m1", { x: 1, y: 2 }]]));
    setAngle(l, "m1", 10);
    setAperture(l, "m1", 20);
    setLabel(l, "m1", "x");
    setLocked(l, ["m1"], true);
    setProp(l, "las1", "power_mw", 5);
    setProp(l, "las1", "wavelength_nm", undefined);
    setAnnotation(l, "m1", { model: "BB1" });
    setLinks(l, "m1", { device_id: "witec" });
    changeType(l, "m1", "dichroic");
    renameComponent(l, "fo1", "fiberout");
    removeComponents(l, ["fo1"]);
    duplicateComponents(l, ["m1", "las1"]);
    setTable(l, { width_mm: 1500 });
    addComponent(l, "lens", { x: 1, y: 1 });
    const t = addText(l, { x: 1, y: 1 }, "a").layout;
    updateText(t, "t1", { text: "b" });
    removeText(t, "t1");
    assert.ok(true);
  });
});

describe("moving and rotating", () => {
  it("a move shifts the components, rounds to 0.01 mm and leaves the others the same object", () => {
    const l = sample();
    const r = moveBy(l, ["m1"], 0.1, -0.2);
    assert.equal(r.components[1]!.x_mm, 112.6);
    assert.equal(r.components[1]!.y_mm, 62.3);
    assert.equal(r.components[0], l.components[0], "untouched components keep their identity");
    assert.equal(r.components[3], l.components[3]);
  });

  it("repeated small steps do not collect floating-point dust", () => {
    let l = layoutOf([comp("mirror", "m1", 0, 0, 0)]);
    for (let i = 0; i < 10; i++) l = moveBy(l, ["m1"], 0.1, 0);
    assert.equal(l.components[0]!.x_mm, 1);
    for (let i = 0; i < 3; i++) l = rotateBy(l, ["m1"], 0.1);
    assert.equal(l.components[0]!.angle_deg, 0.3);
  });

  it("locked components do not move or turn by a gesture", () => {
    const l = sample();
    assert.equal(moveBy(l, ["m2"], 5, 5), l, "nothing to do returns the same layout");
    assert.equal(rotateBy(l, ["m2"], 5), l);
    assert.deepEqual(lockedAmong(l, ["m1", "m2"]), ["m2"]);
    const both = moveBy(l, ["m1", "m2"], 1, 0);
    assert.equal(both.components[1]!.x_mm, 113.5);
    assert.equal(both.components[2], l.components[2]);
  });

  it("a zero move is not a change", () => {
    const l = sample();
    assert.equal(moveBy(l, ["m1"], 0, 0), l);
    assert.equal(rotateBy(l, ["m1"], 0), l);
    assert.equal(setPosition(l, "m1", 112.5, 62.5), l);
    assert.equal(setAngle(l, "m1", 135), l);
    assert.equal(setAperture(l, "m1", 25.4), l);
  });

  it("angles wrap into [0, 360) and are saved at 0.01 degree", () => {
    const l = layoutOf([comp("mirror", "m1", 0, 0, 359)]);
    assert.equal(rotateBy(l, ["m1"], 2).components[0]!.angle_deg, 1);
    assert.equal(rotateBy(l, ["m1"], -360).components[0]!.angle_deg, 359);
    assert.equal(setAngle(l, "m1", -45).components[0]!.angle_deg, 315);
    assert.equal(setAngle(l, "m1", 12.3456).components[0]!.angle_deg, 12.35);
    assert.equal(setAngle(l, "m1", 359.999).components[0]!.angle_deg, 0);
  });

  it("the inspector sets a locked component too", () => {
    const l = sample();
    assert.equal(setPosition(l, "m2", 10, 20).components[2]!.x_mm, 10);
    assert.equal(setAngle(l, "m2", 10).components[2]!.angle_deg, 10);
  });

  it("the aperture is kept within 0.1 to 500 mm at 0.01 mm (the server rounds it to 0.01)", () => {
    assert.equal(clampAperture(0), 0.1);
    assert.equal(clampAperture(-3), 0.1);
    assert.equal(clampAperture(1000), 500);
    assert.equal(clampAperture(25.444), 25.44);
    assert.equal(clampAperture(Number.NaN), 0.1);
    assert.equal(setAperture(sample(), "m1", 50.066).components[1]!.aperture_mm, 50.07);
  });

  it("a group turns as a whole about its pivot", () => {
    const l = layoutOf([comp("mirror", "a", 10, 0, 0), comp("mirror", "b", 20, 0, 90)]);
    const r = rotateGroup(l, ["a", "b"], 90, { x: 0, y: 0 });
    assert.deepEqual([r.components[0]!.x_mm, r.components[0]!.y_mm, r.components[0]!.angle_deg], [0, 10, 90]);
    assert.deepEqual([r.components[1]!.x_mm, r.components[1]!.y_mm, r.components[1]!.angle_deg], [0, 20, 180]);
  });

  it("setPositions moves several at once and skips the locked", () => {
    const l = sample();
    const r = setPositions(
      l,
      new Map([
        ["m1", { x: 1, y: 1 }],
        ["m2", { x: 2, y: 2 }],
      ]),
    );
    assert.equal(r.components[1]!.x_mm, 1);
    assert.equal(r.components[2], l.components[2]);
  });
});

describe("deleting and renaming", () => {
  it("deleting a fibre output unlinks the input that continued there", () => {
    const l = sample();
    const r = removeComponents(l, ["fo1"]);
    assert.equal(r.components.some((c) => c.id === "fo1"), false);
    assert.equal("continues_at" in r.components.find((c) => c.id === "fi1")!.props, false);
    assert.equal(r.components.find((c) => c.id === "m1"), l.components[1], "others are the same objects");
  });

  it("a locked component survives a delete", () => {
    const l = sample();
    assert.equal(removeComponents(l, ["m2"]), l);
    assert.equal(removeComponents(l, ["m1", "m2"]).components.length, 4);
    assert.equal(removeComponents(l, ["nope"]), l);
  });

  it("renaming follows the fibre link and refuses a bad or taken id", () => {
    const l = sample();
    const r = renameComponent(l, "fo1", "fiberout");
    assert.ok(r.components.some((c) => c.id === "fiberout"));
    assert.equal(r.components.find((c) => c.id === "fi1")!.props["continues_at"], "fiberout");
    assert.equal(renameComponent(l, "fo1", "m1"), l, "taken");
    assert.equal(renameComponent(l, "fo1", "has space"), l, "malformed");
    assert.equal(renameComponent(l, "fo1", "fo1"), l);
    assert.equal(renameComponent(l, "nope", "x"), l);
    assert.equal(idProblem(l, "m1", "fo1"), "taken");
    assert.equal(idProblem(l, "", "fo1"), "format");
    assert.equal(idProblem(l, "x".repeat(41), "fo1"), "format");
    assert.equal(idProblem(l, "x".repeat(40), "fo1"), null);
    assert.equal(idProblem(l, "fo1", "fo1"), null, "keeping the id is fine");
  });
});

describe("editing fields", () => {
  it("sets and clears a type parameter; an empty value removes the key", () => {
    const l = sample();
    const set = setProp(l, "las1", "power_mw", 10);
    assert.equal(set.components[0]!.props["power_mw"], 10);
    const cleared = setProp(set, "las1", "power_mw", "");
    assert.equal("power_mw" in cleared.components[0]!.props, false);
    assert.equal(setProp(l, "las1", "power_mw", undefined), l, "clearing what is not there changes nothing");
    assert.equal(setProp(l, "las1", "wavelength_nm", 532), l, "the same value is not a change");
    assert.equal(setProp(l, "las1", "wavelength_nm", false).components[0]!.props["wavelength_nm"], false, "false is a value, not 'empty'");
  });

  it("annotations merge; the same value is not a change", () => {
    const l = sample();
    const r = setAnnotation(l, "m1", { model: "BB1-E02", manufacturer: "Thorlabs" });
    assert.equal(r.components[1]!.annotations.model, "BB1-E02");
    assert.equal(r.components[1]!.annotations.notes, "");
    assert.equal(setAnnotation(r, "m1", { model: "BB1-E02" }), r);
    const withExtra = setAnnotation(l, "m1", { extra: [{ key: "mount", value: "KM100" }] });
    assert.deepEqual(withExtra.components[1]!.annotations.extra, [{ key: "mount", value: "KM100" }]);
  });

  it("links merge; null clears", () => {
    const l = setLinks(sample(), "m1", { device_id: "witec", slot: "excitation_waveplate" });
    assert.deepEqual(l.components[1]!.links, { device_id: "witec", slot: "excitation_waveplate" });
    assert.deepEqual(setLinks(l, "m1", { slot: null }).components[1]!.links, { device_id: "witec", slot: null });
  });

  it("lock and label", () => {
    const l = sample();
    assert.equal(setLocked(l, ["m1"], true).components[1]!.locked, true);
    assert.equal(setLocked(l, ["m2"], true), l);
    assert.equal(setLabel(l, "m1", "M1").components[1]!.label, "M1");
    assert.equal(setLabel(l, "m1", "Fold mirror").components[1]!.label, "Fold mirror");
  });

  it("changing the type drops parameters the new type lacks and adds the ones it needs", () => {
    const l = layoutOf([comp("laser", "x", 0, 0, 0, { props: { wavelength_nm: 532, power_mw: 5 } })]);
    const asDichroic = changeType(l, "x", "dichroic").components[0]!;
    assert.deepEqual(asDichroic.props, { edge_nm: 600 }, "the edge is required; the side defaults to short on the server");
    assert.equal(asDichroic.type, "dichroic");
    assert.equal(asDichroic.aperture_mm, 25.4, "the aperture was the laser's default, so it follows");
    const kept = layoutOf([comp("laser", "y", 0, 0, 0, { aperture: 3, props: { wavelength_nm: 532 } })]);
    assert.equal(changeType(kept, "y", "mirror").components[0]!.aperture_mm, 3, "a chosen aperture stays");
    assert.deepEqual(changeType(l, "x", "mirror").components[0]!.props, {});
    assert.equal(changeType(l, "x", "laser"), l);
  });
});

describe("duplicating", () => {
  it("copies with new ids, shifted, unlocked and independent of the original", () => {
    const l = sample();
    const r = duplicateComponents(l, ["m2", "las1"], { x: 10, y: 10 });
    assert.deepEqual(r.ids, ["las2", "m3"]);
    const copy = r.layout.components.find((c) => c.id === "m3")!;
    assert.equal(copy.locked, false);
    assert.equal(copy.x_mm, 122.5);
    assert.equal(copy.label, "M3");
    const laser = r.layout.components.find((c) => c.id === "las2")!;
    assert.deepEqual(laser.props, { wavelength_nm: 532 });
    assert.notEqual(laser.props, l.components[0]!.props, "props are copied, not shared");
  });

  it("copies nothing when the limit is reached", () => {
    const lasers = layoutOf(Array.from({ length: LIMITS.sources }, (_, i) => comp("laser", `las${i + 1}`, i, 0, 0)));
    const r = duplicateComponents(lasers, ["las1"]);
    assert.deepEqual(r.ids, []);
    assert.equal(r.layout, lasers);
  });
});

describe("texts and table", () => {
  it("add, edit, remove", () => {
    let l = emptyLayout("a", "A");
    const a = addText(l, { x: 300.456, y: 850 }, "enclosure door");
    l = a.layout;
    assert.deepEqual(l.texts[0], { id: "t1", x_mm: 300.46, y_mm: 850, text: "enclosure door", size: 1 });
    const edited = updateText(l, "t1", { text: "door", x_mm: 10.004 });
    assert.equal(edited.texts[0]!.text, "door");
    assert.equal(edited.texts[0]!.x_mm, 10);
    assert.equal(updateText(edited, "t1", { text: "door" }), edited);
    assert.equal(removeText(edited, "t1").texts.length, 0);
    assert.equal(removeText(edited, "nope"), edited);
  });

  it("stops at 100 texts", () => {
    let l = emptyLayout("a", "A");
    for (let i = 0; i < LIMITS.texts; i++) l = addText(l, { x: i, y: 0 }, "t").layout;
    assert.equal(l.texts.length, 100);
    assert.equal(addText(l, { x: 0, y: 0 }, "one more").id, null);
  });

  it("table edits merge; the same value is not a change", () => {
    const l = emptyLayout("a", "A");
    assert.equal(setTable(l, { width_mm: 1500 }).table.width_mm, 1500);
    assert.equal(setTable(l, { width_mm: l.table.width_mm }), l);
    assert.equal(l.table.hole_pitch_mm, 25);
  });
});
