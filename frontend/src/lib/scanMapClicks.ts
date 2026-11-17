// ════════════════════════════════════════════════════════════════════════════
// Single click, double click and touch on the scan map — told apart.
//
// Two ways one gesture could do the wrong thing:
//
//   • A single click on a thumbnail opens its details; a double click anywhere
//     resets the zoom. Opened at once, the popup's backdrop caught the second
//     click of a double click, so the canvas never saw a double click and the
//     zoom stayed. The popup now waits out the double-click window, and a
//     double click inside it cancels the popup and resets the zoom instead.
//   • On a touch screen one tap fires Konva's `tap` AND, from the browser's
//     compatibility mouse events, a `click` a moment later: the ruler dropped
//     A and B on the same spot. A click within GHOST_CLICK_MS of a tap is that
//     tap's echo and is ignored. (The tap handler stays: on a marker the
//     browser does not always follow with a click.)
//
// Pure (timers injected), so `node --test` drives it with a fake clock.
// ════════════════════════════════════════════════════════════════════════════

/** A click this soon after a tap is the tap's synthesized echo. */
export const GHOST_CLICK_MS = 600;

export interface ClickTimers {
  now(): number;
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

// A MONOTONIC clock: Date.now() follows the wall clock, and
// after it is stepped back — an NTP correction, someone fixing the time — every
// click for as long as the step measured as "just after the tap" and was dropped.
const realTimers: ClickTimers = {
  now: () => performance.now(),
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: (h) => clearTimeout(h as ReturnType<typeof setTimeout>),
};

export class ClickArbiter {
  private pending: unknown = null;
  private lastTapMs = Number.NEGATIVE_INFINITY;
  private readonly delayMs: number;
  private readonly timers: ClickTimers;

  /**
   * @param delayMs how long a single click's action waits for a second click —
   *   the double-click window the canvas library uses (Konva.dblClickWindow),
   *   so any second click it would count as a double click arrives in time.
   */
  constructor(delayMs: number, timers: ClickTimers = realTimers) {
    this.delayMs = delayMs;
    this.timers = timers;
  }

  /** A pointer event arrived. False when it is a tap's echo and must be ignored. */
  accept(kind: "click" | "tap"): boolean {
    const t = this.timers.now();
    if (kind === "tap") {
      this.lastTapMs = t;
      return true;
    }
    // An echo FOLLOWS its tap: only 0 ≤ dt < GHOST_CLICK_MS is one. A clock that
    // reads earlier than the tap (dt < 0) says nothing about an echo.
    const dt = t - this.lastTapMs;
    return !(dt >= 0 && dt < GHOST_CLICK_MS);
  }

  /** Run `action` once the double-click window has passed, unless a double click (or another single click) comes first. */
  later(action: () => void): void {
    this.cancel();
    this.pending = this.timers.setTimeout(() => {
      this.pending = null;
      action();
    }, this.delayMs);
  }

  /** A double click, an unmount: drop whatever a single click scheduled. */
  cancel(): void {
    if (this.pending === null) return;
    this.timers.clearTimeout(this.pending);
    this.pending = null;
  }

  /** For tests and diagnostics: a single click's action is waiting. */
  get waiting(): boolean {
    return this.pending !== null;
  }
}
