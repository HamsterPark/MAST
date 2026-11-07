// ════════════════════════════════════════════════════════════════════════════
// Tests for src/lib/opticalLayout/history.ts and editorState.ts — undo / redo and what
// saving does to them.
//
//     cd frontend && npm run test:unit
//
// Section 5 asks for at least 100 undo steps, so 100 is a floor the limit cannot be set
// below. The editor-state tests are about the failure the operator would actually meet:
// two tabs editing the same layout, a 409, and the question of what happens to the
// edits they have already made.
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  COALESCE_WINDOW_MS,
  DEFAULT_HISTORY_LIMIT,
  MIN_UNDO_STEPS,
  canRedo,
  canUndo,
  commit,
  createHistory,
  redo,
  replacePresent,
  undo,
} from "../src/lib/opticalLayout/history.ts";
import {
  editorReducer,
  initialEditorState,
  isDirty,
  sameContent,
  stableStringify,
} from "../src/lib/opticalLayout/editorState.ts";
import { moveBy, setTitle } from "../src/lib/opticalLayout/edit.ts";
import type { OpticalLayout } from "../src/lib/opticalLayout/types.ts";
import { foldLayout } from "./helpers/opticalLayout.ts";

describe("undo and redo", () => {
  it("steps back and forward through the edits", () => {
    let h = createHistory(0);
    for (const v of [1, 2, 3]) h = commit(h, v, { now: 0 });
    assert.equal(h.present, 3);
    h = undo(h);
    assert.equal(h.present, 2);
    h = undo(h);
    h = undo(h);
    assert.equal(h.present, 0);
    assert.equal(canUndo(h), false);
    assert.equal(undo(h), h, "nothing left to undo");
    h = redo(h);
    h = redo(h);
    h = redo(h);
    assert.equal(h.present, 3);
    assert.equal(canRedo(h), false);
    assert.equal(redo(h), h);
  });

  it("can undo at least 100 steps, and keeps exactly the limit when there are more", () => {
    let h = createHistory(0);
    assert.equal(h.limit, DEFAULT_HISTORY_LIMIT);
    for (let i = 1; i <= 300; i++) h = commit(h, i, { now: 0 });
    let steps = 0;
    while (canUndo(h)) {
      h = undo(h);
      steps++;
    }
    assert.equal(steps, DEFAULT_HISTORY_LIMIT);
    assert.ok(steps >= 100);
    assert.equal(h.present, 300 - DEFAULT_HISTORY_LIMIT, "the oldest states fell off the end");
  });

  it("the limit cannot be set below 100", () => {
    assert.equal(createHistory(0, 10).limit, MIN_UNDO_STEPS);
    let h = createHistory(0, 10);
    for (let i = 1; i <= 150; i++) h = commit(h, i, { now: 0 });
    let steps = 0;
    while (canUndo(h)) {
      h = undo(h);
      steps++;
    }
    assert.equal(steps, 100);
  });

  it("a new edit after an undo drops the redo stack", () => {
    let h = createHistory(0);
    h = commit(h, 1);
    h = commit(h, 2);
    h = undo(h);
    assert.equal(canRedo(h), true);
    h = commit(h, 9);
    assert.equal(canRedo(h), false);
    assert.equal(h.present, 9);
    h = undo(h);
    assert.equal(h.present, 1);
  });

  it("committing the same snapshot again is not a step", () => {
    const obj = { a: 1 };
    let h = createHistory(obj);
    h = commit(h, obj);
    assert.equal(canUndo(h), false);
    assert.equal(commit(h, obj), h);
  });

  it("replacePresent swaps the snapshot without a step", () => {
    let h = createHistory(0);
    h = commit(h, 1);
    const r = replacePresent(h, 5);
    assert.equal(r.present, 5);
    assert.equal(r.past.length, 1);
    assert.equal(replacePresent(r, 5), r);
  });
});

describe("folding repeated edits into one step", () => {
  it("edits with the same key close together are one undo step", () => {
    let h = createHistory(0);
    h = commit(h, 1, { key: "nudge:m1", now: 1000 });
    h = commit(h, 2, { key: "nudge:m1", now: 1200 });
    h = commit(h, 3, { key: "nudge:m1", now: 1400 });
    assert.equal(h.past.length, 1);
    h = undo(h);
    assert.equal(h.present, 0, "one undo reverts the whole run");
  });

  it("a pause longer than the window starts a new step", () => {
    let h = createHistory(0);
    h = commit(h, 1, { key: "k", now: 1000 });
    h = commit(h, 2, { key: "k", now: 1000 + COALESCE_WINDOW_MS + 1 });
    assert.equal(h.past.length, 2);
  });

  it("a held key keeps folding while each press follows the last within the window", () => {
    let h = createHistory(0);
    for (let i = 1; i <= 50; i++) h = commit(h, i, { key: "k", now: i * 400 });
    assert.equal(h.past.length, 1, "20 seconds of holding the key is one step");
  });

  it("a different key, or none, is a step of its own", () => {
    let h = createHistory(0);
    h = commit(h, 1, { key: "a", now: 0 });
    h = commit(h, 2, { key: "b", now: 10 });
    h = commit(h, 3, { now: 20 });
    h = commit(h, 4, { now: 30 });
    assert.equal(h.past.length, 4);
  });

  it("an undo breaks the fold: the next edit is a new step", () => {
    let h = createHistory(0);
    h = commit(h, 1, { key: "k", now: 0 });
    h = commit(h, 2, { key: "k", now: 10 });
    h = undo(h);
    h = commit(h, 7, { key: "k", now: 20 });
    assert.equal(h.past.length, 1);
    assert.equal(h.present, 7);
    assert.equal(canRedo(h), false);
  });

  it("the first edit of a run always makes a step, even with a key", () => {
    let h = createHistory(0);
    h = commit(h, 1, { key: "k", now: 0 });
    assert.equal(h.past.length, 1);
  });
});

describe("stable comparison", () => {
  it("ignores key order", () => {
    assert.equal(stableStringify({ b: 1, a: [2, { d: 1, c: 2 }] }), stableStringify({ a: [2, { c: 2, d: 1 }], b: 1 }));
    assert.notEqual(stableStringify({ a: 1 }), stableStringify({ a: 2 }));
    assert.equal(stableStringify(undefined), "null");
  });

  it("ignores what the server writes: revision, time, author", () => {
    const a = foldLayout();
    const b: OpticalLayout = { ...a, revision: 9, updated_at: "2026-11-03T12:00:00+00:00", updated_by: "ana" };
    assert.equal(sameContent(a, b), true);
    assert.equal(sameContent(a, moveBy(a, ["m1"], 1, 0)), false);
    assert.equal(sameContent(a, setTitle(a, "other")), false);
  });
});

describe("the editor state", () => {
  const server = (rev: number): OpticalLayout => ({ ...foldLayout(), id: "main", revision: rev, updated_by: "ana" });

  it("starts clean at the revision it was loaded at", () => {
    const s = initialEditorState(server(3));
    assert.equal(isDirty(s), false);
    assert.equal(s.baseRevision, 3);
    assert.equal(s.conflict, null);
  });

  it("an edit makes it dirty, undoing back to the loaded content makes it clean again", () => {
    let s = initialEditorState(server(3));
    s = editorReducer(s, { type: "edit", layout: moveBy(s.history.present, ["m1"], 5, 0) });
    assert.equal(isDirty(s), true);
    s = editorReducer(s, { type: "undo" });
    assert.equal(isDirty(s), false);
    s = editorReducer(s, { type: "redo" });
    assert.equal(isDirty(s), true);
  });

  it("an edit that changes nothing is not a step", () => {
    const s = initialEditorState(server(1));
    assert.equal(editorReducer(s, { type: "edit", layout: s.history.present }), s);
    assert.equal(editorReducer(s, { type: "undo" }), s);
    assert.equal(editorReducer(s, { type: "redo" }), s);
  });

  it("a successful save takes the server's copy and its revision, keeps the undo steps", () => {
    let s = initialEditorState(server(3));
    const edited = moveBy(s.history.present, ["m1"], 5, 0);
    s = editorReducer(s, { type: "edit", layout: edited });
    const saved = { ...edited, revision: 4, updated_by: "ana", updated_at: "2026-11-03T12:00:00+00:00" };
    s = editorReducer(s, { type: "saved", sent: edited, saved });
    assert.equal(s.baseRevision, 4);
    assert.equal(s.history.present, saved);
    assert.equal(isDirty(s), false);
    assert.equal(s.history.past.length, 1, "undo still works after a save");
    // and the next save builds on revision 4, not on the stale revision inside the undo stack
    s = editorReducer(s, { type: "undo" });
    assert.equal(s.baseRevision, 4);
    assert.equal(isDirty(s), true);
  });

  it("edits made while the save was in flight survive the answer", () => {
    let s = initialEditorState(server(3));
    const sent = moveBy(s.history.present, ["m1"], 5, 0);
    s = editorReducer(s, { type: "edit", layout: sent });
    const later = moveBy(sent, ["m1"], 0, 7);
    s = editorReducer(s, { type: "edit", layout: later });
    const saved = { ...sent, revision: 4 };
    s = editorReducer(s, { type: "saved", sent, saved });
    assert.equal(s.baseRevision, 4);
    assert.equal(s.history.present.components[1]!.y_mm, 69.5, "the later edit is still there");
    assert.equal(s.history.present.revision, 4);
    assert.equal(isDirty(s), true, "and it still needs saving");
  });

  it("a refused save (409) keeps everything and records what the server said", () => {
    let s = initialEditorState(server(3));
    const edited = moveBy(s.history.present, ["m1"], 5, 0);
    s = editorReducer(s, { type: "edit", layout: edited });
    s = editorReducer(s, { type: "conflict", conflict: { currentRevision: 5, message: "changed elsewhere" } });
    assert.equal(s.conflict?.currentRevision, 5);
    assert.equal(s.history.present, edited, "the operator's edits are not thrown away");
    assert.equal(s.baseRevision, 3);
    s = editorReducer(s, { type: "dismiss-conflict" });
    assert.equal(s.conflict, null);
    assert.equal(editorReducer(s, { type: "dismiss-conflict" }), s);
  });

  it("loading the latest version after a 409 is one undo away from the local edits", () => {
    let s = initialEditorState(server(3));
    const edited = moveBy(s.history.present, ["m1"], 5, 0);
    s = editorReducer(s, { type: "edit", layout: edited });
    s = editorReducer(s, { type: "conflict", conflict: { currentRevision: 5, message: "x" } });
    const latest = { ...server(5), title: "from the other tab" };
    s = editorReducer(s, { type: "adopt-latest", latest });
    assert.equal(s.history.present, latest);
    assert.equal(s.baseRevision, 5);
    assert.equal(s.conflict, null);
    assert.equal(isDirty(s), false);
    s = editorReducer(s, { type: "undo" });
    assert.equal(s.history.present, edited, "Ctrl+Z brings the local edits back");
    assert.equal(s.baseRevision, 5, "and saving them now builds on the latest revision");
    assert.equal(isDirty(s), true);
  });

  it("load starts over", () => {
    let s = initialEditorState(server(3));
    s = editorReducer(s, { type: "edit", layout: moveBy(s.history.present, ["m1"], 5, 0) });
    s = editorReducer(s, { type: "load", layout: server(9) });
    assert.equal(s.history.past.length, 0);
    assert.equal(s.baseRevision, 9);
    assert.equal(isDirty(s), false);
  });
});
