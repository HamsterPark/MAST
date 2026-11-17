// ════════════════════════════════════════════════════════════════════════════
// 图库自动增量更新：构建结束后前端该做什么 — src/lib/gallery/autoBuild.ts
//
//     cd frontend && npm run test:unit
//
// 自动构建结束且什么都没变时，什么都不许发生（不弹 toast、不重拉索引、不重读标记）；有变化时
// 索引只「稍后」拉，标记永远不重读。手动构建保持原样。最后两条读源码钉住接线：
//   * GalleryApp 用「构建身份变了」而不是 running 的下降沿；
//   * marksStore.reload 不能拿一份可能不含本地改动的文档去覆盖本地文档。
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it } from "node:test";

import { buildKey, indexChanged, isPristine, planAfterBuild } from "../src/lib/gallery/autoBuild.ts";

const DONE = { running: false, phase: "done", started: "t0", finished: "t1" };
const NOTHING = { n_new: 0, n_changed: 0, n_failed: 0, n_removed: 0, n_render: 0, n_analysis: 0, n_env: 0 };

describe("planAfterBuild", () => {
  it("an automatic build that changed nothing does nothing at all", () => {
    assert.deepEqual(planAfterBuild({ ...DONE, ...NOTHING, trigger: "auto" }), { index: "none", marks: false, toast: "none" });
  });

  it("any change makes it a light toast and a deferred index fetch; marks are never re-read", () => {
    // n_env：只有 sidecar 后来才写成、数据文件一个没变 —— 索引照样变了，不重拉就看不到环境标签
    for (const k of ["n_new", "n_changed", "n_removed", "n_render", "n_analysis", "n_env"] as const) {
      const p = planAfterBuild({ ...DONE, ...NOTHING, [k]: 1, trigger: "auto" });
      assert.deepEqual(p, { index: "later", marks: false, toast: "light" }, k);
    }
  });

  it("failures alone: a toast (the component de-duplicates it) but no index fetch", () => {
    assert.deepEqual(planAfterBuild({ ...DONE, ...NOTHING, n_failed: 3, trigger: "auto" }), {
      index: "none",
      marks: false,
      toast: "failed",
    });
  });

  it("a cancelled automatic build is silent (a manual one follows it)", () => {
    assert.deepEqual(planAfterBuild({ ...DONE, phase: "cancelled", ...NOTHING, n_new: 4, trigger: "auto" }), {
      index: "none",
      marks: false,
      toast: "none",
    });
  });

  it("an automatic build error says so but touches neither index nor marks", () => {
    assert.deepEqual(planAfterBuild({ ...DONE, phase: "error", trigger: "auto" }), { index: "none", marks: false, toast: "error" });
  });

  it("a manual build keeps the old behaviour: index now, marks reloaded, toast", () => {
    assert.deepEqual(planAfterBuild({ ...DONE, ...NOTHING, trigger: "manual" }), { index: "now", marks: true, toast: "updated" });
    assert.deepEqual(planAfterBuild({ ...DONE, phase: "cancelled", trigger: "manual" }), { index: "now", marks: true, toast: "cancelled" });
    assert.equal(planAfterBuild({ ...DONE, phase: "error" }).toast, "error"); // no trigger field = manual
  });
});

describe("untouched view applies the new index by itself", () => {
  const CHANGED = { ...DONE, ...NOTHING, n_new: 2, trigger: "auto" };

  it("pristine view: index now, light toast, still no marks reload", () => {
    assert.deepEqual(planAfterBuild(CHANGED, true), { index: "now", marks: false, toast: "light" });
  });

  it("touched view keeps the deferred index (the chip)", () => {
    assert.deepEqual(planAfterBuild(CHANGED, false), { index: "later", marks: false, toast: "light" });
    assert.equal(planAfterBuild(CHANGED).index, "later"); // default = not pristine
  });

  it("pristine changes nothing when nothing changed, on errors or on cancel", () => {
    assert.equal(planAfterBuild({ ...DONE, ...NOTHING, trigger: "auto" }, true).index, "none");
    assert.equal(planAfterBuild({ ...DONE, phase: "error", trigger: "auto" }, true).index, "none");
    assert.equal(planAfterBuild({ ...CHANGED, phase: "cancelled" }, true).index, "none");
    assert.equal(planAfterBuild({ ...DONE, ...NOTHING, n_failed: 2, trigger: "auto" }, true).index, "none");
  });

  const calm = { lightboxOpen: false, scrollTop: 0, viewportHeight: 800, selected: 0, editing: false };

  it("isPristine needs all four conditions", () => {
    assert.equal(isPristine(calm), true);
    assert.equal(isPristine({ ...calm, scrollTop: 800 }), true); // exactly one screen
    assert.equal(isPristine({ ...calm, lightboxOpen: true }), false);
    assert.equal(isPristine({ ...calm, scrollTop: 801 }), false);
    assert.equal(isPristine({ ...calm, selected: 1 }), false);
    assert.equal(isPristine({ ...calm, editing: true }), false);
  });

  it("a manual build is unchanged by the argument", () => {
    assert.deepEqual(planAfterBuild({ ...DONE, ...NOTHING, trigger: "manual" }, true), planAfterBuild({ ...DONE, ...NOTHING, trigger: "manual" }, false));
  });
});

describe("buildKey / indexChanged", () => {
  it("has no identity while running, and a new one for each build", () => {
    assert.equal(buildKey({ ...DONE, running: true }), null);
    assert.notEqual(buildKey(DONE), buildKey({ ...DONE, finished: "t2" }));
    assert.equal(buildKey(DONE), buildKey({ ...DONE }));
  });

  it("indexChanged ignores failures and deferred files", () => {
    assert.equal(indexChanged({ ...NOTHING, n_failed: 5 }), false);
    assert.equal(indexChanged({ ...NOTHING, n_render: 1 }), true);
  });
});

describe("wiring", () => {
  const read = (rel: string) => readFileSync(join(fileURLToPath(new URL(".", import.meta.url)), "..", "src", rel), "utf8");

  it("GalleryApp decides on build identity and asks the server on open / visible", () => {
    const src = read("components/gallery/GalleryApp.tsx");
    assert.match(src, /buildKey\(d\)/);
    assert.match(src, /planAfterBuild\(d, viewIsPristine\(rootRef\.current\)\)/);
    assert.match(read("components/gallery/Lightbox.tsx"), /data-gallery-lightbox/);
    assert.match(src, /\/api\/gallery\/auto/);
    assert.match(src, /visibilitychange", ask/);
    assert.doesNotMatch(src, /wasRunning/);
  });

  it("marksStore.reload waits for in-flight saves and discards a fetch that raced a local edit", () => {
    const src = read("components/gallery/marksStore.ts");
    const body = src.slice(src.indexOf("reload: async"));
    assert.match(body, /get\(\)\.busy/);
    assert.match(body, /localEdits !== before/);
    assert.match(src, /localEdits\+\+/);
  });
});
