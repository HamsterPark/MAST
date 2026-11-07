// ════════════════════════════════════════════════════════════════════════════
// seriesGaps.ts — where a time series stops being **continuous**.
//
// A straight line is the shape of "measured". If the daemon was down for twenty
// minutes and the two ends are joined, those twenty minutes look exactly like a
// steady signal — and the two situations call for opposite actions. Auxiliary
// channels must therefore show an interruption as a gap instead of forcing a line.
//
// There is one rule, written in one place. The environment-history page used to
// inline its own copy (`envHistory.toAlignedData`) and the monitoring page had
// none; two copies of one rule drift apart, so it is extracted here and the
// environment-history page calls it.
//
// ── Implementation basis (uPlot 1.6.32, verified against its source) ───────
// · A series value of `null` breaks the line — `spanGaps` defaults to false,
//   `findGaps` collects null runs into `_paths.gaps`, and `clipGaps` turns them
//   into a clip path with the holes cut out.
// · **Band fills break too**: when drawing a band, `fillStroke`/`strokeFill`
//   apply both the upper edge's `gapsClip` and the lower edge's `gapsClip2`. So
//   filling both edges with null breaks the shaded area between them as well.
//   (This had to be verified before relying on it: a broken line with an
//   unbroken fill would still claim "there is data here".)
// ════════════════════════════════════════════════════════════════════════════

/**
 * The **median** of adjacent timestamp gaps — a self-calibrating answer to "how
 * dense is this curve supposed to be".
 *
 * Median rather than mean: the mean is inflated by the very gap being looked
 * for, so the bigger a gap the less it looks like one.
 *
 * When to use it: **use the declared step when there is one** (the environment
 * history's bucket width is computed and reported by the server). Auxiliary
 * channels have no declarable step — the configured value and the measured one
 * never match (sampling is opportunistic and the setting is only a throttle
 * ceiling; with a 1 s setting the measured interval was 1.30/1.34 s), and
 * `/aux/series` decimates server-side to `max_points`, after which the spacing
 * of drawn points has nothing to do with the sampling interval. Using the
 * configured value as the step would classify a 24-hour curve as **a gap at
 * every point**.
 *
 * Because the median is self-calibrating, a change in the sampling rate (e.g. to
 * 5 Hz) needs no change here — that is the benefit of not hard-coding 1.33.
 *
 * Returns 0 when it cannot be computed (too few points / non-increasing
 * timestamps); callers then treat that as "draw no gaps".
 */
export function medianStep(t: readonly number[]): number {
  const d: number[] = [];
  for (let i = 1; i < t.length; i += 1) {
    const a = t[i - 1];
    const b = t[i];
    if (typeof a === "number" && typeof b === "number" && b > a) d.push(b - a);
  }
  if (!d.length) return 0;
  d.sort((x, y) => x - y);
  const m = d.length >> 1;
  return d.length % 2 ? d[m]! : ((d[m - 1]! + d[m]!) / 2);
}

/**
 * A gap larger than this multiple of the step counts as an **interruption**.
 *
 * 3 rather than 1.x: sampling is opportunistic (a tick is skipped while the role
 * lock is held by a scan), so missing a beat or two is normal operation, not an
 * interruption — the panel already reports "skipped N times because the channel
 * was busy" elsewhere. At a 1.33 s cadence the cut-off lands at 4 s: missing one
 * beat (2.7 s) or two (4.0 s) does not break the line, missing three does.
 *
 * The cost on the other side is that a real interruption shorter than 4 s cannot
 * be drawn. That direction is **chosen deliberately**: a spurious gap sends
 * someone looking for a fault that does not exist, while the information it
 * would add is only "these 4 seconds were not measured".
 */
export const GAP_FACTOR = 3;

function num(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

/**
 * Insert `null` rows at every acquisition interruption so uPlot draws a hole.
 *
 * `step` **is supplied by the caller; no guess is made here**. A 0 or negative
 * value means "I do not know how dense this curve should be" ⇒ no gap is drawn.
 * This is pinned by a test (environment history must not invent gaps when the
 * bucket width is 0), and the direction is right: when the normal interval is
 * unknown, any gap decision would be made up. A caller that wants
 * self-calibration passes {@link medianStep}(t) itself, so the choice is
 * **visible**.
 *
 * The gap's timestamp is `previous point + step`, right after the last real
 * sample rather than in the middle of the hole: the left edge of the hole should
 * be the moment the data stopped.
 */
export function spliceGaps(
  t: readonly number[],
  cols: ReadonlyArray<ReadonlyArray<number | null | undefined>>,
  opts: { step: number; factor?: number },
): { t: number[]; cols: (number | null)[][]; gaps: number } {
  const factor = Number.isFinite(opts.factor) ? (opts.factor as number) : GAP_FACTOR;
  const step = Number.isFinite(opts.step) && opts.step > 0 ? opts.step : 0;
  const outT: number[] = [];
  const outCols: (number | null)[][] = cols.map(() => []);
  let gaps = 0;

  for (let i = 0; i < t.length; i += 1) {
    const ts = num(t[i]);
    if (ts == null) continue;
    const prev = outT.length ? outT[outT.length - 1]! : null;
    if (prev != null && step > 0 && ts - prev > step * factor) {
      // The gap marker must fall strictly between two real samples, otherwise
      // the x axis stops being monotonic and uPlot draws a line running
      // backwards. With factor ≥ 1, prev+step is naturally in between, but
      // factor is a parameter, so this does not rely on callers behaving.
      outT.push(Math.min(prev + step, (prev + ts) / 2));
      for (const c of outCols) c.push(null);
      gaps += 1;
    }
    outT.push(ts);
    for (let k = 0; k < cols.length; k += 1) outCols[k]!.push(num(cols[k]![i]));
  }

  return { t: outT, cols: outCols, gaps };
}
