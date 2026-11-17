// ════════════════════════════════════════════════════════════════════════════
// Tests for src/lib/opticalLayout/layers.ts: the Layers menu and how a stored choice is read back
// (docs/v2/design/optical_layout_v2.md section 8.5).
//
//     cd frontend && npm run test:unit
//
// The rule that matters: a layer added after the browser stored its choice must come up ON, not
// read as `undefined` = off (then nobody would ever see it or know it exists).
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { BEAM_LAYERS, DEFAULT_LAYERS, LAYER_GROUPS, LAYER_IDS, layerLabel, layerShown, withLayerDefaults } from "../src/lib/opticalLayout/layers.ts";

describe("reading the stored layers", () => {
  it("a v1 choice (seven layers) keeps its values and the layers v2 added come up on", () => {
    const v1 = { holes: false, grid: true, rulers: false, labels: true, beams: true, alignment: false, comments: true };
    const read = withLayerDefaults(v1);
    assert.equal(read.holes, false);
    assert.equal(read.rulers, false);
    assert.equal(read.alignment, false);
    for (const id of ["texts", "envelope", "polarization", "power"] as const) assert.equal(read[id], true, id);
  });

  it("nothing stored, or something that is not an object, is the defaults", () => {
    for (const v of [null, undefined, 3, "x", [], [true]]) assert.deepEqual(withLayerDefaults(v), DEFAULT_LAYERS, JSON.stringify(v));
  });

  it("a malformed value is the default for that key only; unknown keys are dropped", () => {
    const read = withLayerDefaults({ grid: "no", beams: false, sparkles: true, envelope: 0 });
    assert.equal(read.grid, true);
    assert.equal(read.beams, false);
    assert.equal(read.envelope, true);
    assert.deepEqual(Object.keys(read).sort(), [...LAYER_IDS].sort());
  });

  it("returns a fresh object: changing it does not change the defaults", () => {
    const read = withLayerDefaults(null);
    read.holes = false;
    assert.equal(DEFAULT_LAYERS.holes, true);
    assert.ok(Object.isFrozen(DEFAULT_LAYERS));
  });
});

describe("the layers", () => {
  it("everything starts on", () => {
    for (const id of LAYER_IDS) assert.equal(DEFAULT_LAYERS[id], true, id);
  });

  it("the beam layers draw only while the beams do", () => {
    const off = { ...DEFAULT_LAYERS, beams: false };
    for (const id of BEAM_LAYERS) {
      assert.equal(layerShown(DEFAULT_LAYERS, id), true, id);
      assert.equal(layerShown(off, id), false, id);
    }
    assert.equal(layerShown(off, "labels"), true);
    assert.equal(layerShown({ ...DEFAULT_LAYERS, envelope: false }, "envelope"), false);
  });

  it("the menu lists every layer once, each with a name in both languages' code path", () => {
    const listed = LAYER_GROUPS.flatMap((g) => g.layers);
    assert.deepEqual([...listed].sort(), [...LAYER_IDS].sort());
    assert.equal(new Set(listed).size, listed.length);
    for (const id of LAYER_IDS) assert.ok(layerLabel(id).length > 0, id);
    for (const g of LAYER_GROUPS) assert.ok(g.label().length > 0);
  });

  it("the contract's layers are all there: holes / grid, names, alignment, envelope, polarisation, power, text", () => {
    for (const id of ["holes", "grid", "labels", "alignment", "envelope", "polarization", "power", "texts"]) assert.ok((LAYER_IDS as readonly string[]).includes(id), id);
  });
});
