// 合帧时钟（src/lib/live/frameClock.ts）。
//
//     cd frontend && npm run test:unit
//
// 20 Hz × 好几个流的「有新数据」不许变成每次都重画：同一个视图一帧最多画一次。

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { createFrameClock } from "../src/lib/live/frameClock.ts";

function fakeRaf() {
  let next = 1;
  const pending = new Map<number, (t: number) => void>();
  return {
    raf: (cb: (t: number) => void) => {
      const h = next++;
      pending.set(h, cb);
      return h;
    },
    caf: (h: number) => {
      pending.delete(h);
    },
    /** 浏览器画一帧。 */
    tick() {
      const cbs = [...pending.values()];
      pending.clear();
      for (const cb of cbs) cb(0);
    },
    size: () => pending.size,
  };
}

describe("frameClock", () => {
  it("同一个视图一帧里登记多次只画一次，用最后那次登记", () => {
    const r = fakeRaf();
    const clock = createFrameClock({ raf: r.raf, caf: r.caf });
    const view = {};
    const drawn: number[] = [];
    for (let i = 0; i < 20; i++) clock.request(view, () => drawn.push(i));
    assert.equal(r.size(), 1, "只要一次 requestAnimationFrame");
    r.tick();
    assert.deepEqual(drawn, [19]);
  });

  it("不同视图同一帧各画一次；一个抛错不挡别的", () => {
    const r = fakeRaf();
    const clock = createFrameClock({ raf: r.raf, caf: r.caf });
    const drawn: string[] = [];
    clock.request({}, () => {
      throw new Error("boom");
    });
    clock.request({}, () => drawn.push("b"));
    const warn = console.warn;
    console.warn = () => {};
    try {
      r.tick();
    } finally {
      console.warn = warn;
    }
    assert.deepEqual(drawn, ["b"]);
  });

  it("后台标签页（浏览器不调 rAF）：登记只是覆盖，不堆积", () => {
    const r = fakeRaf();
    const clock = createFrameClock({ raf: r.raf, caf: r.caf });
    const view = {};
    let n = 0;
    for (let i = 0; i < 1000; i++) clock.request(view, () => (n += 1));
    assert.equal(clock.pending(), 1);
    r.tick(); // 回到前台的第一帧
    assert.equal(n, 1);
  });

  it("卸载时取消，最后一个取消掉的连 rAF 也撤回", () => {
    const r = fakeRaf();
    const clock = createFrameClock({ raf: r.raf, caf: r.caf });
    const view = {};
    let n = 0;
    clock.request(view, () => (n += 1));
    clock.cancel(view);
    assert.equal(r.size(), 0);
    r.tick();
    assert.equal(n, 0);
  });
});
