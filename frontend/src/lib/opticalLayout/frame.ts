// Frame throttling for drags. A pointer can deliver hundreds of events a second; each one
// that changed React state would re-run the ray trace and redraw the whole canvas, and a
// big layout would fall behind the pointer and freeze the page. The throttle keeps only
// the latest value and applies it once per animation frame, so the trace runs at most
// once per frame however fast the pointer moves (section 8 of
// docs/v2/design/optical_layout.md: "the trace is throttled in requestAnimationFrame").
//
// The scheduler functions are parameters so the unit test can drive frames by hand
// (test/opticalLayoutFrame.test.ts); the page passes the browser's.

export interface FrameThrottle<T> {
  /** Remember the latest value; it is applied on the next frame. Older pending values are dropped. */
  push(value: T): void;
  /** Apply the pending value now (the end of a drag: the last position must not be lost). */
  flush(): void;
  /** Drop the pending value without applying it. */
  cancel(): void;
  readonly pending: boolean;
}

export type RequestFrame = (cb: () => void) => number;
export type CancelFrame = (id: number) => void;

export function createFrameThrottle<T>(apply: (value: T) => void, request: RequestFrame, cancelFrame: CancelFrame): FrameThrottle<T> {
  let latest: { value: T } | null = null;
  let handle: number | null = null;

  const run = () => {
    handle = null;
    const job = latest;
    latest = null;
    if (job) apply(job.value);
  };

  return {
    push(value) {
      latest = { value };
      if (handle === null) handle = request(run);
    },
    flush() {
      if (handle !== null) {
        cancelFrame(handle);
        handle = null;
      }
      const job = latest;
      latest = null;
      if (job) apply(job.value);
    },
    cancel() {
      if (handle !== null) {
        cancelFrame(handle);
        handle = null;
      }
      latest = null;
    },
    get pending() {
      return latest !== null;
    },
  };
}
