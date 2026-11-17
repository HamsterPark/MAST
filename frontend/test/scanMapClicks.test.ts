// ════════════════════════════════════════════════════════════════════════════
// src/lib/scanMapClicks.ts — single vs double click, and touch echoes, on the
// scan map.
//
//     cd frontend && npm run test:unit
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { ClickArbiter, GHOST_CLICK_MS, type ClickTimers } from "../src/lib/scanMapClicks.ts";

/** A clock the test moves by hand. */
function fakeTimers() {
  let now = 1_000_000;
  let next = 1;
  const due = new Map<number, { at: number; fn: () => void }>();
  const timers: ClickTimers = {
    now: () => now,
    setTimeout: (fn, ms) => {
      const id = next++;
      due.set(id, { at: now + ms, fn });
      return id;
    },
    clearTimeout: (h) => {
      due.delete(h as number);
    },
  };
  const advance = (ms: number) => {
    now += ms;
    for (const [id, d] of [...due].sort((a, b) => a[1].at - b[1].at)) {
      if (d.at <= now) {
        due.delete(id);
        d.fn();
      }
    }
  };
  return { timers, advance };
}

const WINDOW = 400; // Konva.dblClickWindow

describe("single vs double click", () => {
  it("opens the popup only after the double-click window has passed", () => {
    const { timers, advance } = fakeTimers();
    const clicks = new ClickArbiter(WINDOW, timers);
    let opened = 0;
    clicks.later(() => opened++);
    advance(WINDOW - 1);
    assert.equal(opened, 0, "opened inside the window: its backdrop would eat the second click");
    advance(1);
    assert.equal(opened, 1);
  });

  it("a double click inside the window cancels the popup", () => {
    const { timers, advance } = fakeTimers();
    const clicks = new ClickArbiter(WINDOW, timers);
    let opened = 0;
    clicks.later(() => opened++); // first click
    advance(150);
    clicks.later(() => opened++); // second click (Konva reports it as a click too) …
    clicks.cancel(); //              … and then as the double click
    advance(WINDOW * 3);
    assert.equal(opened, 0);
    assert.equal(clicks.waiting, false);
  });

  it("a second single click replaces the first instead of opening twice", () => {
    const { timers, advance } = fakeTimers();
    const clicks = new ClickArbiter(WINDOW, timers);
    const got: string[] = [];
    clicks.later(() => got.push("a"));
    advance(WINDOW + 200); // a, opened
    clicks.later(() => got.push("b"));
    advance(100);
    clicks.later(() => got.push("c"));
    advance(WINDOW);
    assert.deepEqual(got, ["a", "c"]);
  });

  it("cancel with nothing waiting is harmless", () => {
    const { timers } = fakeTimers();
    const clicks = new ClickArbiter(WINDOW, timers);
    clicks.cancel();
    assert.equal(clicks.waiting, false);
  });
});

describe("touch: one tap acts once", () => {
  it("ignores the click a browser synthesizes after a tap", () => {
    const { timers, advance } = fakeTimers();
    const clicks = new ClickArbiter(WINDOW, timers);
    assert.equal(clicks.accept("tap"), true);
    advance(30);
    assert.equal(clicks.accept("click"), false, "the tap's echo placed a second ruler point");
    advance(GHOST_CLICK_MS);
    assert.equal(clicks.accept("click"), true, "a real click later is a click");
  });

  it("never drops a mouse click when there was no tap", () => {
    const { timers } = fakeTimers();
    const clicks = new ClickArbiter(WINDOW, timers);
    assert.equal(clicks.accept("click"), true);
    assert.equal(clicks.accept("click"), true);
  });

  it("accepts every tap, also two in quick succession", () => {
    const { timers, advance } = fakeTimers();
    const clicks = new ClickArbiter(WINDOW, timers);
    assert.equal(clicks.accept("tap"), true);
    advance(100);
    assert.equal(clicks.accept("tap"), true);
  });
});

describe("a clock that runs backwards", () => {
  it("does not turn every later click into a tap's echo", () => {
    // On the wall clock, a step back (NTP, someone fixing the time) put every
    // click for as long as the step "right after the tap": all dropped.
    const { timers, advance } = fakeTimers();
    const clicks = new ClickArbiter(WINDOW, timers);
    clicks.accept("tap");
    advance(700);
    assert.equal(clicks.accept("click"), true);
    advance(-30_000); // the clock now reads 29.3 s before the tap
    const got: boolean[] = [];
    for (const step of [0, 10_000, 19_000, 299]) {
      advance(step);
      got.push(clicks.accept("click"));
    }
    assert.deepEqual(got, [true, true, true, true]);
  });

  it("still drops the echo that follows its tap", () => {
    const { timers, advance } = fakeTimers();
    const clicks = new ClickArbiter(WINDOW, timers);
    clicks.accept("tap");
    advance(0);
    assert.equal(clicks.accept("click"), false, "dt = 0 is an echo");
    advance(GHOST_CLICK_MS - 1);
    assert.equal(clicks.accept("click"), false);
    advance(1);
    assert.equal(clicks.accept("click"), true, "dt = GHOST_CLICK_MS is not");
  });
});
