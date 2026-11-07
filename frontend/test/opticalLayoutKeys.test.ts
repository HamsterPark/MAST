// ════════════════════════════════════════════════════════════════════════════
// Tests for src/lib/opticalLayout/keys.ts — the keyboard map of section 5:
//
//   arrow keys 1 mm (Shift 0.1, Alt 10); [ ] 1 degree (Shift 0.1, Alt 15);
//   Delete; Ctrl+Z / Ctrl+Y.
//
//     cd frontend && npm run test:unit
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { interpretKey, isEditableTarget, shortcutHelp } from "../src/lib/opticalLayout/keys.ts";

const arrow = (key: string, mods: { shiftKey?: boolean; altKey?: boolean } = {}) => interpretKey({ key, ...mods });

describe("arrow keys move the selection", () => {
  it("1 mm; up is +y because the screen is flipped", () => {
    assert.deepEqual(arrow("ArrowRight"), { kind: "move", dx: 1, dy: 0 });
    assert.deepEqual(arrow("ArrowLeft"), { kind: "move", dx: -1, dy: 0 });
    assert.deepEqual(arrow("ArrowUp"), { kind: "move", dx: 0, dy: 1 });
    assert.deepEqual(arrow("ArrowDown"), { kind: "move", dx: 0, dy: -1 });
  });

  it("Shift 0.1 mm", () => {
    assert.deepEqual(arrow("ArrowRight", { shiftKey: true }), { kind: "move", dx: 0.1, dy: 0 });
    assert.deepEqual(arrow("ArrowDown", { shiftKey: true }), { kind: "move", dx: 0, dy: -0.1 });
  });

  it("Alt 10 mm", () => {
    assert.deepEqual(arrow("ArrowLeft", { altKey: true }), { kind: "move", dx: -10, dy: 0 });
    assert.deepEqual(arrow("ArrowUp", { altKey: true }), { kind: "move", dx: 0, dy: 10 });
  });

  it("Shift and Alt together take the small step", () => {
    assert.deepEqual(arrow("ArrowRight", { shiftKey: true, altKey: true }), { kind: "move", dx: 0.1, dy: 0 });
  });

  it("Ctrl with an arrow is not a move (word jumps belong to text boxes)", () => {
    assert.equal(interpretKey({ key: "ArrowLeft", ctrlKey: true }), null);
    assert.equal(interpretKey({ key: "ArrowLeft", metaKey: true }), null);
  });
});

describe("[ and ] rotate", () => {
  it("1 degree: [ counter-clockwise (positive), ] clockwise", () => {
    assert.deepEqual(arrow("["), { kind: "rotate", deg: 1 });
    assert.deepEqual(arrow("]"), { kind: "rotate", deg: -1 });
  });

  it("Shift 0.1 degree, Alt 15 degrees", () => {
    assert.deepEqual(arrow("[", { shiftKey: true }), { kind: "rotate", deg: 0.1 });
    assert.deepEqual(arrow("]", { altKey: true }), { kind: "rotate", deg: -15 });
    assert.deepEqual(arrow("[", { altKey: true }), { kind: "rotate", deg: 15 });
  });

  it("Shift turns [ into { on most layouts: the key code still counts", () => {
    assert.deepEqual(interpretKey({ key: "{", code: "BracketLeft", shiftKey: true }), { kind: "rotate", deg: 0.1 });
    assert.deepEqual(interpretKey({ key: "}", code: "BracketRight", shiftKey: true }), { kind: "rotate", deg: -0.1 });
    assert.deepEqual(interpretKey({ key: "ü", code: "BracketLeft" }), { kind: "rotate", deg: 1 }, "a layout where the physical key prints something else");
  });
});

describe("the other keys", () => {
  it("Delete (and Backspace, for keyboards without Delete) delete the selection", () => {
    assert.deepEqual(arrow("Delete"), { kind: "delete" });
    assert.deepEqual(arrow("Backspace"), { kind: "delete" });
  });

  it("Ctrl+Z undoes, Ctrl+Y and Ctrl+Shift+Z redo, also with Cmd", () => {
    assert.deepEqual(interpretKey({ key: "z", ctrlKey: true }), { kind: "undo" });
    assert.deepEqual(interpretKey({ key: "Z", ctrlKey: true }), { kind: "undo" }, "caps lock");
    assert.deepEqual(interpretKey({ key: "y", ctrlKey: true }), { kind: "redo" });
    assert.deepEqual(interpretKey({ key: "z", ctrlKey: true, shiftKey: true }), { kind: "redo" });
    assert.deepEqual(interpretKey({ key: "z", metaKey: true }), { kind: "undo" });
    assert.deepEqual(interpretKey({ key: "z", metaKey: true, shiftKey: true }), { kind: "redo" });
  });

  it("Ctrl+S saves, Ctrl+A selects all, Escape cancels", () => {
    assert.deepEqual(interpretKey({ key: "s", ctrlKey: true }), { kind: "save" });
    assert.deepEqual(interpretKey({ key: "a", ctrlKey: true }), { kind: "select-all" });
    assert.deepEqual(arrow("Escape"), { kind: "escape" });
  });

  it("anything else is left to the browser", () => {
    assert.equal(arrow("a"), null);
    assert.equal(arrow("Enter"), null);
    assert.equal(arrow("Tab"), null);
    assert.equal(arrow("z"), null, "z without Ctrl is just a letter");
    assert.equal(interpretKey({ key: "z", ctrlKey: true, altKey: true }), null, "Ctrl+Alt is AltGr on some layouts");
    assert.equal(interpretKey({ key: "r", ctrlKey: true }), null, "Ctrl+R reloads the page");
  });
});

describe("typing in a box never moves a component", () => {
  it("inputs, text areas, selects and editable elements are left alone", () => {
    assert.equal(isEditableTarget({ tagName: "INPUT" }), true);
    assert.equal(isEditableTarget({ tagName: "textarea" }), true);
    assert.equal(isEditableTarget({ tagName: "SELECT" }), true);
    assert.equal(isEditableTarget({ tagName: "DIV", isContentEditable: true }), true);
    assert.equal(isEditableTarget({ tagName: "svg" }), false);
    assert.equal(isEditableTarget({ tagName: "BUTTON" }), false);
    assert.equal(isEditableTarget(null), false);
    assert.equal(isEditableTarget(undefined), false);
  });
});

describe("the on-screen help", () => {
  it("lists every shortcut with a text in both languages", () => {
    const help = shortcutHelp();
    assert.ok(help.length >= 5);
    for (const h of help) {
      assert.ok(h.keys.length > 0 && h.zh.length > 0 && h.en.length > 0);
    }
    assert.ok(help.some((h) => h.en.includes("0.1 mm") && h.en.includes("10 mm")));
    assert.ok(help.some((h) => h.en.includes("15°")));
  });
});
