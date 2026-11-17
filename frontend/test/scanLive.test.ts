// ════════════════════════════════════════════════════════════════════════════
// Pure-logic tests for src/lib/scanLive.ts — the scan map's row-by-row live
// scan (design: docs/v2/design/scan_map_live.md).
//
//     cd frontend && npm run test:unit
//
// The live image fails silently in exactly the ways a picture can: rows that
// land upside down, a range that pins everything to one colour, rows that
// never arrive on a slow connection and simply stay blank. The slow-connection
// block drives the REAL LiveStore through a dropped range and its backfill.
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { normalizeStreamMeta } from "../src/lib/live/protocol.ts";
import { LiveStore, MAP2D_LOG_CAPACITY } from "../src/lib/live/store.ts";
import {
  LiveImageModel,
  afmhotLut,
  detrendRow,
  liveFrameStreamId,
  liveScanUsable,
  liveStaleMs,
  needsBackfillKick,
  parseLiveScanMeta,
  rangeMoved,
  readLiveStatus,
  robustRange,
  scanLineWorld,
} from "../src/lib/scanLive.ts";

// ── fixtures ────────────────────────────────────────────────────────────────

function scalar(entries: Record<string, number[]>) {
  return { kind: "scalar", values: new Map(Object.entries(entries)) };
}

function mapStore(id: string, nx: number, ny: number) {
  const s = new LiveStore({ now: () => 1_000 });
  const meta = normalizeStreamMeta({
    id, kind: "map2d", title: id, source: "local/scan", uid: "u1", last_seq: 0,
    meta: { nx, ny, x0: 0, y0: 0, dx: 1, dy: 1, x_unit: "m", y_unit: "m", value_key: "Z" },
  })!;
  s.applyStreams([meta], { full: true, trustLastSeq: false });
  return s;
}

/** A row whose values are a tilted plane plus a bump, so detrending matters. */
function rowValue(ix: number, iy: number): number {
  return 5 * ix + 100 * iy + (ix === 2 ? 7 : 0);
}

function publishRow(s: LiveStore, id: string, nx: number, iy: number, firstSeq: number): number {
  for (let ix = 0; ix < nx; ix++) {
    s.applyFrame(id, firstSeq + ix, { t: 1, ix, iy, values: { Z: rowValue(ix, iy) } });
  }
  return firstSeq + nx;
}

function alphaRow(model: LiveImageModel, canvasRow: number): number[] {
  const out: number[] = [];
  for (let ix = 0; ix < model.nx; ix++) out.push(model.rgba[(canvasRow * model.nx + ix) * 4 + 3]!);
  return out;
}

// ── status and meta ─────────────────────────────────────────────────────────

describe("readLiveStatus", () => {
  it("reads the newest values of an active session", () => {
    const st = readLiveStatus(scalar({
      active: [0, 1], session: [0, 3], rows_done: [0, 42], rows: [0, 256], dir: [0, -1],
      interval_s: [0, 0.8], degraded: [0, 0],
    }));
    assert.deepEqual(st, {
      active: true, session: 3, rowsDone: 42, rows: 256, dir: -1, intervalS: 0.8, degraded: false,
      tooLarge: null, commsHold: false,
    });
    assert.equal(liveFrameStreamId(3), "local/scan/live/3");
  });

  it("an idle status names no session", () => {
    const st = readLiveStatus(scalar({ active: [1, 0], session: [2, Number.NaN] }));
    assert.equal(st?.active, false);
    assert.equal(st?.session, null);
  });

  it("says when a frame is too large to stream, and when the link breaker holds it", () => {
    const big = readLiveStatus(scalar({ active: [1], too_large: [1], src_lines: [2048], src_pixels: [2048], interval_s: [5] }));
    assert.deepEqual(big?.tooLarge, { lines: 2048, pixels: 2048 });
    assert.equal(big?.session, null, "no session for a frame that is not grabbed");
    const held = readLiveStatus(scalar({ active: [1, 1], comms_hold: [null as unknown as number, 1] }));
    assert.equal(held?.commsHold, true);
    // the newest frame decides: a flag from an earlier status does not stick
    const later = readLiveStatus(scalar({ active: [1, 1], too_large: [1, null as unknown as number] }));
    assert.equal(later?.tooLarge, null);
  });

  it("is null without a status at all", () => {
    assert.equal(readLiveStatus(null), null);
    assert.equal(readLiveStatus({ kind: "map2d" }), null);
    assert.equal(readLiveStatus(scalar({})), null);
  });
});

describe("parseLiveScanMeta", () => {
  const good = {
    nx: 256, ny: 128, center_x_m: 1e-7, center_y_m: 2e-7, width_m: 5e-8, height_m: 2.5e-8,
    angle_deg: 15, scan_dir: "up", row0_source: "nan_front", value_key: "Z", units: { Z: "pm" },
    channel: "Z (m)",
  };

  it("reads what the backend declared", () => {
    const m = parseLiveScanMeta(good)!;
    assert.equal(m.nx, 256);
    assert.equal(m.angleDeg, 15);
    assert.equal(m.scanDir, "up");
    assert.equal(m.unit, "pm");
    assert.equal(m.row0Source, "nan_front");
  });

  it("refuses a stream it could not place", () => {
    assert.equal(parseLiveScanMeta({ ...good, width_m: 0 }), null);
    assert.equal(parseLiveScanMeta({ ...good, center_x_m: undefined }), null);
    assert.equal(parseLiveScanMeta({ ...good, nx: 2.5 }), null);
    assert.equal(parseLiveScanMeta(null), null);
  });
});

// ── the picture ─────────────────────────────────────────────────────────────

describe("detrendRow", () => {
  it("removes a row's own line, leaving the feature", () => {
    const src = [0, 5, 17, 15, 20];                 // 5·x plus a bump of 7 at x = 2
    const out = new Float64Array(5);
    detrendRow(src, 0, 5, out, 0);
    const mean = out.reduce((a, b) => a + b, 0) / 5;
    assert.ok(Math.abs(mean) < 1e-9, "a fitted line leaves zero mean");
    assert.ok(out[2]! > out[1]! && out[2]! > out[3]!, "the bump survives");
  });

  it("fits only finite pixels and keeps the holes", () => {
    const out = new Float64Array(4);
    detrendRow([1, Number.NaN, 3, 4], 0, 4, out, 0);
    assert.ok(Number.isNaN(out[1]!));
    assert.ok(out.every((v, i) => i === 1 || Number.isFinite(v)));
  });

  it("survives a row with one pixel or none", () => {
    const one = new Float64Array(3);
    detrendRow([Number.NaN, 7, Number.NaN], 0, 3, one, 0);
    assert.equal(one[1], 0);
    const none = new Float64Array(2);
    detrendRow([Number.NaN, Number.NaN], 0, 2, none, 0);
    assert.ok(none.every(Number.isNaN));
  });
});

describe("robustRange", () => {
  it("ignores the outliers a 1–99 % range is for", () => {
    const v = Array.from({ length: 1000 }, (_, i) => i / 10);
    v[0] = -1e6;
    v[999] = 1e6;
    const [lo, hi] = robustRange(v)!;
    assert.ok(lo > -1 && hi < 101, `${lo}, ${hi}`);
  });

  it("skips NaN and gives a flat image a usable span", () => {
    const [lo, hi] = robustRange([Number.NaN, 3, 3, 3])!;
    assert.ok(hi > lo);
    assert.equal(robustRange([Number.NaN]), null);
  });

  it("samples a big image instead of sorting all of it", () => {
    const big = new Float64Array(512 * 512).map((_, i) => i % 1000);
    const r = robustRange(big)!;
    assert.ok(r[0] < 50 && r[1] > 950);
  });

  it("knows when the range moved enough to repaint everything", () => {
    assert.equal(rangeMoved([0, 100], [2, 101]), false);
    assert.equal(rangeMoved([0, 100], [20, 100]), true);
    assert.equal(rangeMoved(null, [0, 1]), true);
  });
});

describe("afmhotLut", () => {
  it("runs black → red → orange → yellow → white like matplotlib's afmhot", () => {
    const lut = afmhotLut();
    const at = (i: number) => [...lut.slice(i * 3, i * 3 + 3)];
    assert.deepEqual(at(0), [0, 0, 0]);
    assert.deepEqual(at(64), [128, 0, 0], "t = ¼: half red, no green yet");
    assert.deepEqual(at(128), [255, 129, 1], "t = ½: (1, ½, 0)");
    assert.deepEqual(at(255), [255, 255, 255]);
  });
});

describe("LiveImageModel", () => {
  const id = "local/scan/live/1";

  it("paints received rows and leaves the rest transparent, top row at the top", () => {
    const s = mapStore(id, 4, 3);
    publishRow(s, id, 4, 2, 1);                       // iy = 2: the TOP row of the picture
    const model = new LiveImageModel(4, 3);
    const buf = s.get(id)!.buf as never;
    model.update(buf, "Z");
    assert.deepEqual(alphaRow(model, 0), [255, 255, 255, 255], "iy = ny − 1 is canvas row 0");
    assert.deepEqual(alphaRow(model, 1), [0, 0, 0, 0]);
    assert.deepEqual(alphaRow(model, 2), [0, 0, 0, 0]);
  });

  it("repaints only the rows that changed once the range has settled", () => {
    const s = mapStore(id, 4, 6);
    let seq = 1;
    for (let iy = 5; iy >= 2; iy--) seq = publishRow(s, id, 4, iy, seq);
    const model = new LiveImageModel(4, 6);
    const buf = s.get(id)!.buf as never;
    assert.equal(model.update(buf, "Z"), "all", "the first update paints everything");
    publishRow(s, id, 4, 1, seq);
    const rep = model.update(buf, "Z");
    assert.ok(rep === "all" || (Array.isArray(rep) && rep.includes(1)), `repainted ${String(rep)}`);
    assert.deepEqual(alphaRow(model, 6 - 1 - 1), [255, 255, 255, 255]);
  });
});

// ── slow connections (docs/v2/design/scan_map_live.md) ────────────────
//
// Over a slow tailnet link the server's per-connection backlog overflows and it
// reports `dropped`; the client backfills those seqs over REST, out of order.
// The live image must end up complete — not stay blank where the rows were
// dropped — and a view that fell further behind than the store's write log
// must repaint the whole picture rather than miss rows.

describe("slow connection: dropped rows come back", () => {
  const id = "local/scan/live/7";

  it("a backfilled row is painted, and nothing stays blank", () => {
    const nx = 4;
    const s = mapStore(id, nx, 3);
    let seq = publishRow(s, id, nx, 2, 1);           // seq 1–4, the top row
    // The server reports seq 5–8 (row iy = 1) were dropped from our backlog …
    s.applyDropped(id, seq, seq + nx - 1);
    seq += nx;
    publishRow(s, id, nx, 0, seq);                   // … and row 0 arrives (seq 9–12)
    const model = new LiveImageModel(nx, 3);
    const buf = s.get(id)!.buf as never;
    model.update(buf, "Z");
    assert.equal(s.get(id)!.missing.isEmpty(), false);
    assert.deepEqual(alphaRow(model, 1), [0, 0, 0, 0], "the dropped row is blank before the backfill");

    // The REST backfill delivers the dropped seqs late and out of order.
    for (const ix of [3, 1, 0, 2]) {
      s.applyFrame(id, 5 + ix, { t: 1, ix, iy: 1, values: { Z: rowValue(ix, 1) } });
    }
    const rep = model.update(buf, "Z");
    assert.ok(rep === "all" || (Array.isArray(rep) && rep.includes(1)), `row 1 not repainted: ${String(rep)}`);
    assert.equal(s.get(id)!.missing.isEmpty(), true);
    assert.deepEqual(model.coverage(), { rowsWithData: 3, partial: [] });
    for (let r = 0; r < 3; r++) assert.deepEqual(alphaRow(model, r), [255, 255, 255, 255], `canvas row ${r}`);
  });

  it("a view further behind than the write log repaints the whole picture", () => {
    const nx = 512;
    const ny = Math.ceil((MAP2D_LOG_CAPACITY + 1000) / nx);
    const s = mapStore(id, nx, ny);
    const model = new LiveImageModel(nx, ny);
    const buf = s.get(id)!.buf as never;
    let seq = publishRow(s, id, nx, ny - 1, 1);
    model.update(buf, "Z");                          // the view is now current …
    for (let iy = ny - 2; iy >= 0; iy--) seq = publishRow(s, id, nx, iy, seq);
    // … and then misses more writes than the log holds (a background tab, a burst).
    assert.equal(model.update(buf, "Z"), "all");
    assert.deepEqual(model.coverage(), { rowsWithData: ny, partial: [] });
  });

  it("asks for the gap again once the stream has gone quiet", () => {
    const gap = { missing: { isEmpty: () => false }, lastRecvMs: 10_000 };
    assert.equal(needsBackfillKick(gap, 11_000), false, "frames still arriving re-trigger it themselves");
    assert.equal(needsBackfillKick(gap, 13_500), true, "quiet with a gap left: kick it");
    assert.equal(needsBackfillKick({ missing: { isEmpty: () => true }, lastRecvMs: 0 }, 1e9), false);
    assert.equal(needsBackfillKick(null, 1e9), false);
  });
});

describe("row streams (map2d_rows): one frame per row", () => {
  const id = "local/scan/live/9";

  function rowStore(nx: number, ny: number) {
    const s = new LiveStore({ now: () => 1_000 });
    const meta = normalizeStreamMeta({
      id, kind: "map2d_rows", title: id, source: "local/scan", uid: "u9", last_seq: 0,
      meta: { nx, ny, x0: 0, y0: 0, dx: 1, dy: 1, x_unit: "m", y_unit: "m", value_key: "Z" },
    })!;
    s.applyStreams([meta], { full: true, trustLastSeq: false });
    return s;
  }
  const row = (iy: number, nx: number) => ({
    t: 1, iy, ix0: 0, values: { Z: Array.from({ length: nx }, (_, ix) => rowValue(ix, iy)) },
  });

  it("paints rows the same way as pixel frames, top row at the top", () => {
    const s = rowStore(4, 3);
    s.applyFrame(id, 1, row(2, 4));
    const model = new LiveImageModel(4, 3);
    model.update(s.get(id)!.buf as never, "Z");
    assert.deepEqual(alphaRow(model, 0), [255, 255, 255, 255]);
    assert.deepEqual(alphaRow(model, 1), [0, 0, 0, 0]);
  });

  it("a dropped row comes back by backfill and is painted", () => {
    const s = rowStore(4, 3);
    s.applyFrame(id, 1, row(2, 4));
    s.applyDropped(id, 2, 2);                   // the server says row frame 2 was dropped …
    s.applyFrame(id, 3, row(0, 4));             // … and row 0 arrives
    const model = new LiveImageModel(4, 3);
    const buf = s.get(id)!.buf as never;
    model.update(buf, "Z");
    assert.deepEqual(alphaRow(model, 1), [0, 0, 0, 0]);
    s.applyFrame(id, 2, row(1, 4));             // the backfill, late
    const rep = model.update(buf, "Z");
    assert.ok(rep === "all" || (Array.isArray(rep) && rep.includes(1)), `row 1 not repainted: ${String(rep)}`);
    assert.equal(s.get(id)!.missing.isEmpty(), true);
    assert.deepEqual(model.coverage(), { rowsWithData: 3, partial: [] });
  });
});

// ── the scan line ───────────────────────────────────────────────────────────

describe("scanLineWorld", () => {
  const frame = { cx: 0, cy: 0, w: 2, h: 4, angleDeg: 0 };

  it("starts at the top of a down-scan and moves down", () => {
    const l = scanLineWorld(frame, 1, 4, -1)!;
    assert.equal(l.a.y, 1);
    assert.equal(l.b.y, 1);
    assert.deepEqual([l.a.x, l.b.x], [-1, 1]);
  });

  it("starts at the bottom of an up-scan and moves up", () => {
    assert.equal(scanLineWorld(frame, 1, 4, 1)!.a.y, -1);
    assert.equal(scanLineWorld(frame, 4, 4, 1)!.a.y, 2);
  });

  it("turns with the frame", () => {
    const l = scanLineWorld({ ...frame, angleDeg: 90 }, 0, 4, -1)!;
    // A 90° CCW frame: its top edge is on the LEFT, running bottom to top.
    assert.ok(Math.abs(l.a.x - -2) < 1e-12 && Math.abs(l.a.y - -1) < 1e-12, JSON.stringify(l.a));
    assert.ok(Math.abs(l.b.x - -2) < 1e-12 && Math.abs(l.b.y - 1) < 1e-12, JSON.stringify(l.b));
  });

  it("has nothing to draw without rows or a direction", () => {
    assert.equal(scanLineWorld(frame, 1, 0, -1), null);
    assert.equal(scanLineWorld(frame, 1, 4, 0), null);
  });
});

// ── live or fallback ────────────────────────────────────────────────────────

describe("liveScanUsable", () => {
  const on = {
    active: true, session: 2, rowsDone: 10, rows: 64, dir: -1 as const, intervalS: 0.8, degraded: false,
    tooLarge: null, commsHold: false,
  };

  it("draws the stream when it is active, has pixels and the status is recent", () => {
    assert.equal(liveScanUsable(on, { filled: 640, lastStatusMs: 10_000 }, 11_000), true);
  });

  it("falls back to the milestone PNG otherwise", () => {
    assert.equal(liveScanUsable({ ...on, active: false }, { filled: 640, lastStatusMs: 10_000 }, 11_000), false);
    assert.equal(liveScanUsable(on, { filled: 0, lastStatusMs: 10_000 }, 11_000), false, "no row yet");
    assert.equal(liveScanUsable(on, null, 11_000), false, "no stream (WS and polling both down)");
    const stale = 10_000 + liveStaleMs(0.8) + 1;
    assert.equal(liveScanUsable(on, { filled: 640, lastStatusMs: 10_000 }, stale), false, "a stalled feed");
    assert.equal(liveScanUsable(null, { filled: 640, lastStatusMs: 10_000 }, 11_000), false);
  });

  it("a slow scan stays live while its status keeps ticking, rows or not", () => {
    // Line time 60 s: the service ticks every 10 s (its ceiling) and a row
    // completes only every sixth tick. Freshness is the status, not the rows.
    const slow = { ...on, intervalS: 10 };
    assert.equal(liveScanUsable(slow, { filled: 640, lastStatusMs: 100_000 }, 100_000 + 10_000), true);
    assert.ok(liveStaleMs(10) > liveStaleMs(0.5));
  });
});
