// ════════════════════════════════════════════════════════════════════════════
// Pure-logic tests for src/lib/scanMapTime.ts — the scan map's time filter.
//
//     cd frontend && npm run test:unit
//
// The four ways a time filter fails here are all silent (scan_map_upgrade.md
// §2.3 and §0 trap 5): a MISSING stamp that hides work, a BAD stamp that is
// mistaken for a real one, a CLOCK STEP that files the newest work as "the
// future", and an EMPTY window that looks like an empty map. Each has a block.
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  ALL_TIME,
  SLIDER_STEPS,
  TIME_PRESETS,
  browserOffsetMin,
  fmtAgo,
  fmtClock,
  fmtDateTime,
  fmtUtcOffset,
  inWindow,
  isAllTime,
  latestActionIndex,
  parseStamp,
  presetOf,
  rangeFromSlider,
  resolveWindow,
  sliderToTime,
  stampOffsetMin,
  summarize,
  timeDomain,
  timeToSlider,
  wallClockOf,
  windowLabel,
} from "../src/lib/scanMapTime.ts";

const H = 3_600_000;
const local = (y: number, mo: number, d: number, h = 0, mi = 0, s = 0, ms = 0) =>
  new Date(y, mo - 1, d, h, mi, s, ms).getTime();

// ── parsing ─────────────────────────────────────────────────────────────────

describe("parseStamp", () => {
  it("reads the backend's naive local stamp, microseconds and all", () => {
    // storage.log_marker writes datetime.now().isoformat().
    assert.equal(parseStamp("2026-11-12T10:00:00.123456"), local(2026, 11, 12, 10, 0, 0, 123));
    assert.equal(parseStamp("2026-11-12T10:00:00"), local(2026, 11, 12, 10));
    assert.equal(parseStamp("2026-11-12 10:00"), local(2026, 11, 12, 10));
  });

  it("reads an image mtime in UTC, with Z or an offset", () => {
    assert.equal(parseStamp("2023-11-14T22:13:20Z"), 1_700_000_000_000);
    assert.equal(parseStamp("2023-11-15T06:13:20+08:00"), 1_700_000_000_000);
    assert.equal(parseStamp("2023-11-14T16:43:20-05:30"), 1_700_000_000_000);
  });

  it("calls a MISSING stamp undated, not zero", () => {
    for (const v of ["", "   ", null, undefined]) assert.equal(parseStamp(v), null, String(v));
  });

  it("calls a BAD stamp undated instead of guessing", () => {
    for (const v of [
      "not-a-time",
      "2026-13-01T00:00:00", // month 13
      "2026-02-31T10:00:00", // Date() would roll this over to 3 March
      "2026-11-12T24:00:00",
      "2026-11-12T10:61:00",
      "2026-11-12T10:00:00+25:00",
      "12/11/2026 10:00",
      1_700_000_000_000, // a number is not a stamp
      {},
    ]) {
      assert.equal(parseStamp(v), null, JSON.stringify(v));
    }
  });
});

// ── marker stamps with the offset of their own date ─────────────────────────
//
// The backend hands each marker out as the instrument PC's wall clock WITH the
// UTC offset in force on that date. One "current offset" for every stamp was
// wrong for half the year in a zone with daylight saving (and made a browser in
// the same zone wrong too). Every expectation here is built with Date.UTC, so
// these hold in whatever TZ the test runs under (checked with PowerShell under
// TZ=Asia/Shanghai, Europe/Berlin and America/New_York).

/** "+02:00" for 120 minutes east. */
function suffix(min: number): string {
  const a = Math.abs(min);
  return `${min < 0 ? "-" : "+"}${String(Math.floor(a / 60)).padStart(2, "0")}:${String(a % 60).padStart(2, "0")}`;
}

describe("marker stamps carry their own offset", () => {
  it("reads a summer and a winter stamp from one zone as the instants they are", () => {
    // Europe/Berlin: +02:00 in July, +01:00 in January.
    assert.equal(parseStamp("2026-07-01T12:00:00.123456+02:00"), Date.UTC(2026, 6, 1, 10, 0, 0, 123));
    assert.equal(parseStamp("2026-01-15T09:30:00+01:00"), Date.UTC(2026, 0, 15, 8, 30));
  });

  it("puts a marker and the image of the same scan at the same instant", () => {
    assert.equal(parseStamp("2026-11-12T10:00:00.000001+08:00"), parseStamp("2026-11-12T02:00:00Z"));
  });

  it("files a lab marker in 'last 1 h' by the lab's clock, wherever the browser is", () => {
    // Recorded 30 min ago in a lab two hours west of this browser. Without its
    // offset, read as browser-local, it would be 2.5 h old.
    const now = Date.UTC(2026, 10, 12, 2, 30);
    const lab = browserOffsetMin(now) - 120;
    const wall = new Date(now - 30 * 60_000 + lab * 60_000).toISOString().slice(0, 19);
    const lastHour = resolveWindow({ kind: "recent", hours: 1 }, now);
    assert.equal(inWindow(parseStamp(wall + suffix(lab)), lastHour), true);
    assert.equal(inWindow(parseStamp(wall), lastHour), false, "test premise: the naive reading is off");
  });

  it("says which offset a stamp names, if any", () => {
    assert.equal(stampOffsetMin("2026-07-01T12:00:00.5+02:00"), 120);
    assert.equal(stampOffsetMin("2026-07-01T12:00:00+0530"), 330);
    assert.equal(stampOffsetMin("2026-07-01T12:00:00-08:00"), -480);
    assert.equal(stampOffsetMin("2026-07-01T04:00:00Z"), 0);
    assert.equal(stampOffsetMin("2026-07-01T12:00:00"), null, "naive: names none");
    assert.equal(stampOffsetMin("not-a-time"), null);
    assert.equal(stampOffsetMin(null), null);
  });

  it("gives the wall clock as written, without fraction or zone", () => {
    assert.equal(wallClockOf("2026-07-01T12:00:00.123456+02:00"), "2026-07-01 12:00:00");
    assert.equal(wallClockOf("2026-07-01 12:00"), "2026-07-01 12:00:00");
    assert.equal(wallClockOf("not-a-time"), null);
  });

  it("names an offset the way the popup prints it", () => {
    assert.equal(fmtUtcOffset(480), "UTC+8");
    assert.equal(fmtUtcOffset(330), "UTC+5:30");
    assert.equal(fmtUtcOffset(-300), "UTC−5");
    assert.equal(fmtUtcOffset(0), "UTC+0");
    assert.equal(fmtUtcOffset(Number.NaN), "");
  });

  it("knows this browser's own offset at an instant", () => {
    const t = Date.UTC(2026, 10, 12, 2);
    assert.equal(browserOffsetMin(t), -new Date(t).getTimezoneOffset());
  });
});

// ── windows ─────────────────────────────────────────────────────────────────

describe("resolveWindow", () => {
  const now = local(2026, 11, 12, 15, 30);

  it("'all' excludes nothing", () => {
    assert.deepEqual(resolveWindow({ kind: "all" }, now), ALL_TIME);
    assert.ok(isAllTime(ALL_TIME));
  });

  it("'last N h' reaches back from now and leaves the future open", () => {
    assert.deepEqual(resolveWindow({ kind: "recent", hours: 1 }, now), { from: now - H, to: null });
    assert.deepEqual(resolveWindow({ kind: "recent", hours: 3 }, now), { from: now - 3 * H, to: null });
  });

  it("'today' starts at local midnight", () => {
    assert.deepEqual(resolveWindow({ kind: "today" }, now), { from: local(2026, 11, 12), to: null });
  });

  it("reads a range dragged past itself the right way round, not as empty", () => {
    assert.deepEqual(resolveWindow({ kind: "range", from: 20, to: 10 }, now), { from: 10, to: 20 });
  });

  it("degrades nonsense to 'all' rather than to an empty map", () => {
    assert.deepEqual(resolveWindow({ kind: "recent", hours: 0 }, now), ALL_TIME);
    assert.deepEqual(resolveWindow({ kind: "recent", hours: Number.NaN }, now), ALL_TIME);
    assert.deepEqual(resolveWindow({ kind: "today" }, Number.NaN), ALL_TIME);
    assert.deepEqual(resolveWindow({ kind: "range", from: Number.NaN, to: null }, now), ALL_TIME);
  });

  it("maps every chip to its preset id and back", () => {
    for (const p of TIME_PRESETS) assert.equal(presetOf(p.filter), p.id);
    assert.equal(presetOf({ kind: "range", from: null, to: null }), "all");
    assert.equal(presetOf({ kind: "range", from: 1, to: null }), null);
  });
});

describe("inWindow", () => {
  const w = { from: 100, to: 200 };

  it("keeps the inside and the edges, drops the outside", () => {
    assert.equal(inWindow(100, w), true);
    assert.equal(inWindow(150, w), true);
    assert.equal(inWindow(200, w), true);
    assert.equal(inWindow(99, w), false);
    assert.equal(inWindow(201, w), false);
  });

  it("ALWAYS keeps an undated item (scan_map_upgrade.md §0 trap 5)", () => {
    assert.equal(inWindow(null, w), true);
    assert.equal(inWindow(undefined, w), true);
    assert.equal(inWindow(Number.NaN, w), true);
  });
});

// ── the clock stepped backwards ────────────────────────────────────────────

describe("clock steps", () => {
  const now = local(2026, 11, 12, 15, 0);
  // The system clock was stepped back an hour at 14:30: work recorded before
  // the step is stamped up to an hour AHEAD of the corrected clock.
  const before = [local(2026, 11, 12, 15, 10), local(2026, 11, 12, 15, 25)];
  const after = [local(2026, 11, 12, 14, 35), local(2026, 11, 12, 14, 50)];

  it("never hides future-stamped work behind a 'last 1 h' window", () => {
    const w = resolveWindow({ kind: "recent", hours: 1 }, now);
    for (const t of [...before, ...after]) assert.equal(inWindow(t, w), true, fmtDateTime(t));
  });

  it("takes the slider domain by value, not first-and-last", () => {
    const d = timeDomain([...before, ...after])!;
    assert.equal(d.min, after[0]);
    assert.equal(d.max, before[1]);
  });

  it("rings the latest RECORDED action, not the largest stamp", () => {
    const markers = [
      { status: "done", x_m: 0, y_m: 0, timestamp: "2026-11-12T15:25:00" },
      { status: "done", x_m: 1e-8, y_m: 0, timestamp: "2026-11-12T14:35:00" }, // newer row, earlier stamp
      { status: "planned", x_m: 2e-8, y_m: 0 },
    ];
    assert.equal(latestActionIndex(markers), 1);
  });

  it("skips planned steps and position-less rows when picking the latest", () => {
    assert.equal(latestActionIndex([{ status: "done", x_m: 0, y_m: 0 }, { status: "done", x_m: null, y_m: 0 }]), 0);
    assert.equal(latestActionIndex([{ status: "planned", x_m: 0, y_m: 0 }]), -1);
    assert.equal(latestActionIndex([]), -1);
    assert.equal(latestActionIndex(null), -1);
  });

  it("says nothing about 'ago' for a time in the future", () => {
    assert.equal(fmtAgo(now + 60_000, now), null);
  });
});

// ── an empty window ─────────────────────────────────────────────────────────

describe("summarize", () => {
  it("reports an empty window as zero shown out of a non-zero total", () => {
    // What the bar needs to say "nothing in this window" instead of looking
    // like an empty map.
    const s = summarize([100, 200], { from: 1000, to: null });
    assert.deepEqual(s, { total: 2, shown: 0, undated: 0 });
  });

  it("counts undated items as shown, and separately", () => {
    const s = summarize([100, null, null, 5000], { from: 1000, to: null });
    assert.deepEqual(s, { total: 4, shown: 3, undated: 2 });
  });

  it("survives nothing at all", () => {
    assert.deepEqual(summarize([], ALL_TIME), { total: 0, shown: 0, undated: 0 });
  });
});

// ── the slider ──────────────────────────────────────────────────────────────

describe("slider", () => {
  const d = { min: 1_000_000, max: 1_000_000 + 10 * H };

  it("has no domain without a usable time", () => {
    assert.equal(timeDomain([null, undefined, Number.NaN]), null);
    assert.equal(timeDomain([]), null);
  });

  it("maps the track linearly onto the domain", () => {
    assert.equal(sliderToTime(0, d), d.min);
    assert.equal(sliderToTime(SLIDER_STEPS, d), d.max);
    assert.equal(sliderToTime(SLIDER_STEPS / 2, d), d.min + 5 * H);
    assert.equal(timeToSlider(d.min + 5 * H, d, "from"), SLIDER_STEPS / 2);
  });

  it("leaves an end OPEN when its handle rests at the end of the track", () => {
    // With the right handle at the end, work recorded after the range was set
    // must still appear — the map keeps polling.
    const f = rangeFromSlider(0, SLIDER_STEPS, d);
    assert.deepEqual(f, { kind: "range", from: null, to: null });
    const g = rangeFromSlider(250, SLIDER_STEPS, d);
    assert.equal(g.kind === "range" && g.to, null);
    assert.ok(g.kind === "range" && g.from === d.min + 2.5 * H);
  });

  it("puts open ends at the extremes of the track", () => {
    assert.equal(timeToSlider(null, d, "from"), 0);
    assert.equal(timeToSlider(null, d, "to"), SLIDER_STEPS);
  });

  it("orders handles that were dragged past each other", () => {
    const f = rangeFromSlider(800, 200, d);
    assert.ok(f.kind === "range" && f.from! < f.to!);
  });

  it("does not divide by zero on a one-instant domain", () => {
    const one = { min: 5, max: 5 };
    assert.equal(timeToSlider(5, one, "from"), 0);
    assert.equal(timeToSlider(5, one, "to"), SLIDER_STEPS);
  });
});

// ── formatting ──────────────────────────────────────────────────────────────

describe("formatting", () => {
  it("shows a clock time, with the date only when it is another day", () => {
    const t = local(2026, 11, 12, 9, 5);
    assert.equal(fmtClock(t, local(2026, 11, 12, 18)), "09:05");
    assert.equal(fmtClock(t, local(2026, 11, 13, 1)), "11-12 09:05");
  });

  it("prints a full local date-time", () => {
    assert.equal(fmtDateTime(local(2026, 1, 2, 3, 4, 5)), "2026-01-02 03:04:05");
  });

  it("labels the window with 'now' for its open end", () => {
    const now = local(2026, 11, 12, 15);
    const label = windowLabel({ from: local(2026, 11, 12, 14), to: null }, null, now);
    assert.equal(label, "14:00 – 现在");
    assert.equal(windowLabel(ALL_TIME, null, now), "全部时间");
  });

  it("says how long ago in words", () => {
    const now = 10 * H;
    assert.equal(fmtAgo(now - 5_000, now), "刚刚");
    assert.equal(fmtAgo(now - 5 * 60_000, now), "5 分钟前");
    assert.equal(fmtAgo(now - 3 * H, now), "3 小时前");
  });
});
