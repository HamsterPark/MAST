// ════════════════════════════════════════════════════════════════════════════
// Hold-to-repeat jogging (contract §13.1). The server has no unbounded continuous motion, so
// holding just **sends bounded jogs repeatedly**:
//
//   - The first jog is sent on press; each next one is sent only **after the previous returned and
//     at least intervalMs after the previous started** (a slow reply does not pile requests up, and
//     a fast reply does not push the rate above the limit given in the description);
//   - Release, pointer leave, window blur, page hidden, holding longer than maxHoldMs (30 s), or
//     any failed jog all stop sending immediately;
//   - stopOnRelease: a stop is sent when stopping, provided **the device is still ours and at least
//     one jog in this hold actually ran**:
//       - A jog answered refused (refused_busy, execution entry unavailable, request rejected: it
//         did not run) -> no stop, and earlier jogs no longer count, since a composite skill may
//         be holding the device and a token-free stop would cut its motion short;
//       - A jog failed but was not refused (soft limit, driver error, wait-for-position timeout...)
//         -> treated as possibly moved, so the stop is sent;
//       - skipped (parameters could not be built, nothing was sent) -> does not count as run;
//       - A jog still in flight at the moment of stopping: **wait for it and decide once** (if it
//         is refused, the device is held by someone else and an eager stop would cut their motion).
//         The cost is that this one bounded step runs to completion.
//       - When a stop is due by the rules above, stopGuard is asked once more first (the card uses
//         it to read who holds the instrument token now: if a composite skill or someone else holds
//         it, no stop is sent); if it cannot be asked (throws / rejects) the usual rule applies.
//
// The clock is injectable and unit tests use a fake clock (test/manual.test.ts). This file does not
// touch the DOM: the button component turns pointer / focus / visibility events into
// release(reason).
// ════════════════════════════════════════════════════════════════════════════

import { tr } from "../i18n.ts";

export interface HoldClock {
  now(): number;
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

export const realClock: HoldClock = {
  now: () => Date.now(),
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: (h) => clearTimeout(h as ReturnType<typeof setTimeout>),
};

/** A hold stops by itself after this long (contract §13.1). */
export const HOLD_MAX_MS = 30_000;

export type HoldEndReason = "release" | "leave" | "blur" | "hidden" | "timeout" | "failed" | "refused" | "cancel";

/**
 * Result of one jog. ok = it ran, keep sending; failed = it did not succeed (may have moved);
 * refused = it did not run (the device may be held by someone else); skipped = nothing was sent
 * (parameters could not be built). true / false count as ok / failed.
 */
export type HoldSendResult = "ok" | "failed" | "refused" | "skipped";

export interface HoldDoneInfo {
  /** How many jogs were sent. */
  sent: number;
  /** Whether a stop was sent. */
  stopped: boolean;
  /** Result of the last jog to return (null if none returned). */
  last: HoldSendResult | null;
  /** Why a stop that was due by the usual rule was blocked by stopGuard (e.g. "the instrument token is held by TERSGridMap"). */
  blocked: string | null;
}

/** stopGuard's answer: with allow=false no stop is sent and reason goes into the wind-down notice. */
export interface HoldStopGuard {
  allow: boolean;
  reason?: string;
}

export interface HoldOptions {
  intervalMs: number;
  maxHoldMs?: number;
  /** Sends one jog; only ok (or true) continues, anything else stops. A rejection counts as failed. */
  send: () => Promise<HoldSendResult | boolean>;
  /** Stop (used with stopOnRelease); a failure does not affect the wind-down. */
  stop?: () => Promise<unknown>;
  stopOnRelease: boolean;
  /** Asked right before a trailing stop; absent = send it by the usual rule. Throw / reject = could not be asked, usual rule applies. */
  stopGuard?: () => Promise<HoldStopGuard>;
  /** The moment sending stops (the button returns to normal). */
  onEnd?: (reason: HoldEndReason) => void;
  /** Wind-down complete: the in-flight jog has returned and whether to send a stop is decided. Called once per hold. */
  onDone?: (reason: HoldEndReason, info: HoldDoneInfo) => void;
  clock?: HoldClock;
}

export interface HoldController {
  press(): void;
  release(reason?: HoldEndReason): void;
  active(): boolean;
  /** Number of jogs sent in this hold. */
  sent(): number;
}

function asResult(r: HoldSendResult | boolean): HoldSendResult {
  if (r === true) return "ok";
  if (r === "ok" || r === "refused" || r === "skipped") return r;
  return "failed";
}

export function createHoldRepeat(opts: HoldOptions): HoldController {
  const clock = opts.clock ?? realClock;
  const maxHold = opts.maxHoldMs ?? HOLD_MAX_MS;
  let isActive = false;
  let inFlight = false;
  let count = 0;
  /** How many jogs in this hold ran (or may have run); reset on refused, since the device may now belong to someone else and is not stopped on their behalf. */
  let ran = 0;
  let last: HoldSendResult | null = null;
  let lastStart = 0;
  let nextTimer: unknown = null;
  let capTimer: unknown = null;
  /** Stopped, but a jog is still in flight: wind down once it returns. */
  let doneReason: HoldEndReason | null = null;

  const clearTimers = () => {
    if (nextTimer !== null) clock.clearTimeout(nextTimer);
    if (capTimer !== null) clock.clearTimeout(capTimer);
    nextTimer = null;
    capTimer = null;
  };

  const sendStop = () => {
    if (!opts.stop) return;
    try {
      void opts.stop().catch(() => undefined);
    } catch {
      /* A failed stop does not affect the wind-down: the header "Stop all" and the emergency stop remain available */
    }
  };

  /** Whether to send a stop is decided only here, once per hold. */
  const finish = (reason: HoldEndReason) => {
    const done = (stopped: boolean, blocked: string | null = null) => opts.onDone?.(reason, { sent: count, stopped, last, blocked });
    if (!(opts.stopOnRelease && ran > 0)) {
      done(false);
      return;
    }
    const go = () => {
      sendStop();
      done(true);
    };
    if (!opts.stopGuard) {
      go();
      return;
    }
    let g: Promise<HoldStopGuard>;
    try {
      g = opts.stopGuard();
    } catch (e) {
      g = Promise.reject(e);
    }
    g.then(
      (r) => (r.allow ? go() : done(false, r.reason?.trim() || tr("守卫拦下", "blocked by the guard"))),
      () => go(), // could not be asked: usual rule
    );
  };

  const end = (reason: HoldEndReason) => {
    if (!isActive) return;
    isActive = false;
    clearTimers();
    opts.onEnd?.(reason);
    if (inFlight) doneReason = reason;
    else finish(reason);
  };

  const fire = () => {
    nextTimer = null;
    if (!isActive || inFlight) return;
    inFlight = true;
    lastStart = clock.now();
    count += 1;
    let p: Promise<HoldSendResult | boolean>;
    try {
      p = opts.send();
    } catch {
      p = Promise.resolve("failed");
    }
    p.then(
      (r) => settle(asResult(r)),
      () => settle("failed"),
    );
  };

  const settle = (r: HoldSendResult) => {
    inFlight = false;
    last = r;
    if (r === "refused") ran = 0;
    else if (r !== "skipped") ran += 1;
    if (doneReason !== null) {
      const why = doneReason;
      doneReason = null;
      finish(why);
      return;
    }
    if (!isActive) return;
    if (r !== "ok") {
      end(r === "refused" ? "refused" : "failed");
      return;
    }
    const wait = lastStart + opts.intervalMs - clock.now();
    // Reply slower than the interval: send the next one on return (no catch-up, no backlog)
    if (wait <= 0) fire();
    else nextTimer = clock.setTimeout(fire, wait);
  };

  return {
    press() {
      if (isActive || inFlight) return;
      isActive = true;
      count = 0;
      ran = 0;
      last = null;
      doneReason = null;
      capTimer = clock.setTimeout(() => end("timeout"), maxHold);
      fire();
    },
    release(reason = "release") {
      end(reason);
    },
    active: () => isActive,
    sent: () => count,
  };
}
