import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { newestFirst } from "../src/lib/recordsOrder.ts";

describe("newestFirst", () => {
  it("puts the latest record first, whatever order it came in", () => {
    const got = newestFirst([
      { id: "b", timestamp: "2026-11-06T10:00:02" },
      { id: "a", timestamp: "2026-11-06T10:00:01" },
      { id: "c", timestamp: "2026-11-06T10:00:03" },
    ]);
    assert.deepEqual(got.map((x) => x.id), ["c", "b", "a"]);
  });

  it("compares instants, not text, when time formats differ", () => {
    const got = newestFirst([
      { id: "later", timestamp: "2026-11-06T02:30:00Z" },
      { id: "earlier", timestamp: "2026-11-06T09:00:00+08:00" },
    ]);
    assert.deepEqual(got.map((x) => x.id), ["later", "earlier"]);
  });

  it("keeps a same-time burst in reverse execution order", () => {
    const t = "2026-11-06T10:00:00";
    const got = newestFirst([{ id: "1", timestamp: t }, { id: "2", timestamp: t }]);
    assert.deepEqual(got.map((x) => x.id), ["2", "1"]);
  });

  it("sends entries without a readable time last and leaves the input alone", () => {
    const input = [
      { id: "none", timestamp: null },
      { id: "bad", timestamp: "not a time" },
      { id: "ok", timestamp: "2026-11-06T10:00:00" },
    ];
    const got = newestFirst(input);
    assert.equal(got[0].id, "ok");
    assert.deepEqual(input.map((x) => x.id), ["none", "bad", "ok"]);
  });
});
