// ════════════════════════════════════════════════════════════════════════════
// Pure-logic tests for src/lib/scanMapView.ts — which scan-map layers are
// drawn, when the analysis is refetched, and what area is in view.
//
// Same house rules as monitoring.test.ts: NO test framework is installed in
// this frontend and adding one is not our decision, so these run on
// `node --test` with Node's native TypeScript stripping:
//
//     cd frontend && npm run test:unit
//
// What that buys: the map's failure modes here are all silent. A view that
// still fits switched-off content looks like a map that "just won't zoom in";
// an analysis that never refetches looks like a keep-out zone that simply is
// not there; a persisted layer set missing a newly added key hides an overlay
// with no control to bring it back. None of those throw, and none of them are
// obvious in a screenshot.
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  DEFAULT_LAYERS,
  MAP_LAYER_IDS,
  MAP_PRESETS,
  MIN_AUTO_ANALYSIS_INTERVAL_MS,
  SCAN_IMAGE_OPACITY,
  STALE_EPOCH_OPACITY,
  analysisAutoEnabled,
  analysisSignature,
  boundsForMode,
  fmtNm,
  markerDetailRows,
  inverseStageTransform,
  isStaleEpoch,
  matchPreset,
  nextAnalysisDelayMs,
  niceStepNm,
  pinnedLabel,
  drawnHistory,
  drawnScanImages,
  exportFileName,
  filterMarkerRows,
  fmtCount,
  kindCounts,
  markerKeys,
  markerKindOptions,
  scanImageDetailRows,
  scanImageOpacity,
  stageToScreen,
  visibleStepNm,
  withLayerDefaults,
  type MapLayers,
} from "../src/lib/scanMapView.ts";
import { MAX_ZOOM } from "../src/lib/scanMapGeometry.ts";
import { browserOffsetMin, fmtUtcOffset } from "../src/lib/scanMapTime.ts";

// ── fixtures ────────────────────────────────────────────────────────────────

const nm = (v: number) => v * 1e-9;

function layers(over: Partial<MapLayers> = {}): MapLayers {
  return { ...DEFAULT_LAYERS, ...over };
}

function mapWith(over: Record<string, unknown> = {}) {
  return {
    frame: null,
    tip_xyz: null,
    markers: [],
    scan_images: [],
    current_epoch: 0,
    marker_count: 0,
    ...over,
  };
}

// ── layer sets and presets ──────────────────────────────────────────────────

describe("layer defaults", () => {
  it("starts with every layer on", () => {
    assert.equal(MAP_LAYER_IDS.every((id) => DEFAULT_LAYERS[id]), true);
  });

  it("fills in a layer the stored state predates", () => {
    // Exactly what an older localStorage looks like after a layer is added.
    const restored = withLayerDefaults({ underlay: false } as Partial<MapLayers>);
    assert.equal(restored.underlay, false, "the stored choice is kept");
    assert.equal(restored.upcoming, true, "the unknown layer defaults to visible");
  });

  it("ignores junk in stored state rather than reading it as off", () => {
    const restored = withLayerDefaults({ grid: "yes" } as unknown as Partial<MapLayers>);
    assert.equal(restored.grid, true);
  });

  it("survives no stored state at all", () => {
    assert.deepEqual(withLayerDefaults(null), DEFAULT_LAYERS);
    assert.deepEqual(withLayerDefaults(undefined), DEFAULT_LAYERS);
  });
});

describe("presets", () => {
  it("every preset assigns every layer", () => {
    for (const p of MAP_PRESETS) {
      for (const id of MAP_LAYER_IDS) {
        assert.equal(typeof p.layers[id], "boolean", `${p.id} is missing ${id}`);
      }
    }
  });

  it("精简 drops the crowding overlays but keeps the surface", () => {
    const p = MAP_PRESETS.find((x) => x.id === "minimal")!;
    assert.equal(p.layers.underlay, true);
    assert.equal(p.layers.history, true);
    assert.equal(p.layers.grid, false);
    assert.equal(p.layers.plan, false);
    assert.equal(p.layers.upcoming, false);
    assert.equal(p.layers.stale, false);
  });

  it("规划聚焦 keeps both planning channels and what they must avoid", () => {
    const p = MAP_PRESETS.find((x) => x.id === "planning")!;
    assert.equal(p.layers.plan, true);
    assert.equal(p.layers.upcoming, true);
    assert.equal(p.layers.next, true);
    assert.equal(p.layers.avoid, true, "planning without the keep-out zones is a lie");
    assert.equal(p.layers.history, true, "already-scanned ground is planning input");
    assert.equal(p.layers.underlay, false);
  });

  it("highlights the preset whose layer set is exactly the current one", () => {
    assert.equal(matchPreset(DEFAULT_LAYERS), "all");
    for (const p of MAP_PRESETS) assert.equal(matchPreset(p.layers), p.id);
  });

  it("shows no preset once a single switch is flipped", () => {
    assert.equal(matchPreset(layers({ grid: false })), null);
  });
});

// ── analysis refresh ────────────────────────────────────────────────────────

describe("analysis signature", () => {
  it("changes when an operation is recorded", () => {
    const before = analysisSignature(mapWith({ marker_count: 3 }));
    assert.notEqual(before, analysisSignature(mapWith({ marker_count: 4 })));
  });

  it("changes when a coarse move invalidates every coordinate", () => {
    const before = analysisSignature(mapWith({ marker_count: 3, current_epoch: 0 }));
    assert.notEqual(
      before,
      analysisSignature(mapWith({ marker_count: 3, current_epoch: 1 })),
    );
  });

  it("changes when a route is published, and again when it advances", () => {
    const none = analysisSignature(mapWith({ markers: [{ status: "done" }] }));
    const published = analysisSignature(
      mapWith({ markers: [{ status: "done" }, { status: "planned" }, { status: "planned" }] }),
    );
    const advanced = analysisSignature(
      mapWith({ markers: [{ status: "done" }, { status: "planned" }] }),
    );
    assert.notEqual(none, published);
    assert.notEqual(published, advanced);
  });

  it("does not change when nothing that matters moved", () => {
    assert.equal(analysisSignature(mapWith({ marker_count: 7 })),
                 analysisSignature(mapWith({ marker_count: 7 })));
  });

  it("is empty with no map yet", () => {
    assert.equal(analysisSignature(null), "");
  });
});

describe("analysis auto-enable", () => {
  it("is off when no layer needs the payload", () => {
    assert.equal(
      analysisAutoEnabled(layers({ avoid: false, next: false, upcoming: false })),
      false,
      "with all three analysis layers off the endpoint must not be called at all",
    );
  });

  it("is on if any one of the three is showing", () => {
    for (const id of ["avoid", "next", "upcoming"] as const) {
      const only = layers({ avoid: false, next: false, upcoming: false, [id]: true });
      assert.equal(analysisAutoEnabled(only), true, `${id} alone should enable it`);
    }
  });
});

describe("analysis throttle", () => {
  it("goes immediately on the first call", () => {
    assert.equal(nextAnalysisDelayMs(1_000_000, null), 0);
  });

  it("goes immediately once the interval has passed", () => {
    const t = 1_000_000;
    assert.equal(nextAnalysisDelayMs(t + MIN_AUTO_ANALYSIS_INTERVAL_MS, t), 0);
  });

  it("defers rather than drops inside the interval", () => {
    // A burst of markers must not be lost — the LAST state has to land.
    const t = 1_000_000;
    const wait = nextAnalysisDelayMs(t + 2_000, t);
    assert.equal(wait, MIN_AUTO_ANALYSIS_INTERVAL_MS - 2_000);
    assert.ok(wait > 0);
  });

  it("never asks for a negative wait when the clock jumps", () => {
    assert.equal(nextAnalysisDelayMs(0, 1_000_000), 0);
  });
});

// ── view bounds ─────────────────────────────────────────────────────────────

describe("fit mode", () => {
  it("returns nothing to fit on an empty map", () => {
    assert.equal(boundsForMode("fit", mapWith(), null, DEFAULT_LAYERS).bounds, null);
  });

  it("includes a marker's whole footprint, not just its centre", () => {
    const { bounds } = boundsForMode(
      "fit",
      mapWith({ markers: [{ status: "done", x_m: 0, y_m: 0, w_m: nm(100), h_m: nm(100) }] }),
      null,
      DEFAULT_LAYERS,
    );
    assert.ok(bounds);
    assert.equal(bounds!.minX, nm(-50));
    assert.equal(bounds!.maxX, nm(50));
  });

  it("stops fitting around a layer that was switched off", () => {
    // The whole point of 精简: a far-away underlay must not keep the view
    // zoomed out around a thumbnail nobody is drawing.
    const map = mapWith({
      markers: [{ status: "done", x_m: 0, y_m: 0, w_m: nm(20), h_m: nm(20) }],
      scan_images: [{ center_x_m: nm(5000), center_y_m: 0, width_m: nm(100), height_m: nm(100) }],
    });
    const on = boundsForMode("fit", map, null, DEFAULT_LAYERS).bounds!;
    const off = boundsForMode("fit", map, null, layers({ underlay: false })).bounds!;
    assert.ok(on.maxX > nm(4000));
    assert.ok(off.maxX < nm(100));
  });

  it("drops stale-generation markers from the fit when they are hidden", () => {
    const map = mapWith({
      current_epoch: 1,
      markers: [
        { status: "done", coord_epoch: 1, x_m: 0, y_m: 0, w_m: nm(20), h_m: nm(20) },
        { status: "done", coord_epoch: 0, x_m: nm(9000), y_m: 0, w_m: nm(20), h_m: nm(20) },
      ],
    });
    assert.ok(boundsForMode("fit", map, null, DEFAULT_LAYERS).bounds!.maxX > nm(8000));
    assert.ok(boundsForMode("fit", map, null, layers({ stale: false })).bounds!.maxX < nm(100));
  });

  it("keeps the route ahead inside the view, or it would point off-canvas", () => {
    const map = mapWith({ tip_xyz: { x_m: 0, y_m: 0 } });
    const analysis = {
      frame_size_m: nm(100),
      upcoming: [{ x_m: nm(800), y_m: 0 }, { x_m: nm(-800), y_m: 0 }],
    };
    const on = boundsForMode("fit", map, analysis, DEFAULT_LAYERS).bounds!;
    assert.ok(on.maxX >= nm(850));
    assert.ok(on.minX <= nm(-850));
    const off = boundsForMode("fit", map, analysis, layers({ upcoming: false })).bounds!;
    assert.equal(off.maxX, 0);
  });
});

describe("follow mode", () => {
  it("puts a window around the live scan frame", () => {
    const map = mapWith({
      frame: { center_x_m: nm(200), center_y_m: nm(-100), width_m: nm(100), height_m: nm(100) },
      markers: [{ status: "done", x_m: nm(9000), y_m: 0, w_m: nm(20), h_m: nm(20) }],
    });
    const { bounds, fellBack } = boundsForMode("follow", map, null, DEFAULT_LAYERS);
    assert.equal(fellBack, false);
    // Centred on the frame, and unaffected by far-away history.
    assert.equal((bounds!.minX + bounds!.maxX) / 2, nm(200));
    assert.equal((bounds!.minY + bounds!.maxY) / 2, nm(-100));
    assert.ok(bounds!.maxX - bounds!.minX > nm(100), "some context around the frame");
    assert.ok(bounds!.maxX - bounds!.minX < nm(200), "but not the whole record");
  });

  it("falls back to fit — and says so — when nothing is being scanned", () => {
    const map = mapWith({
      markers: [{ status: "done", x_m: 0, y_m: 0, w_m: nm(100), h_m: nm(100) }],
    });
    const { bounds, fellBack } = boundsForMode("follow", map, null, DEFAULT_LAYERS);
    assert.equal(fellBack, true, "the UI has to be able to explain the mismatch");
    assert.deepEqual(bounds, boundsForMode("fit", map, null, DEFAULT_LAYERS).bounds);
  });

  it("falls back when the frame has no size yet", () => {
    const map = mapWith({
      frame: { center_x_m: 0, center_y_m: 0, width_m: 0, height_m: 0 },
      markers: [{ status: "done", x_m: 0, y_m: 0, w_m: nm(100), h_m: nm(100) }],
    });
    assert.equal(boundsForMode("follow", map, null, DEFAULT_LAYERS).fellBack, true);
  });
});

describe("full mode", () => {
  it("shows the whole reachable piezo area regardless of content", () => {
    const { bounds, fellBack } = boundsForMode(
      "full", mapWith(), null, DEFAULT_LAYERS, 1.5e-6,
    );
    assert.equal(fellBack, false);
    assert.deepEqual(bounds, { minX: -1.5e-6, maxX: 1.5e-6, minY: -1.5e-6, maxY: 1.5e-6 });
  });

  it("falls back to fit when the range is unknown", () => {
    const map = mapWith({
      markers: [{ status: "done", x_m: 0, y_m: 0, w_m: nm(100), h_m: nm(100) }],
    });
    const { bounds, fellBack } = boundsForMode("full", map, null, DEFAULT_LAYERS, null);
    assert.equal(fellBack, true);
    assert.deepEqual(bounds, boundsForMode("fit", map, null, DEFAULT_LAYERS).bounds);
  });
});

describe("stale epoch", () => {
  it("is only stale when the generation is genuinely behind", () => {
    assert.equal(isStaleEpoch({ coord_epoch: 0 }, 1), true);
    assert.equal(isStaleEpoch({ coord_epoch: 1 }, 1), false);
    assert.equal(isStaleEpoch({ coord_epoch: null }, 1), false);
    assert.equal(isStaleEpoch({ coord_epoch: 0 }, null), false);
  });
});

// ── stale-coordinate thumbnails (scan_map_upgrade.md §1) ─────────────────────
//
// The backend has always flagged a .sxm written before the last coarse move
// (`stale_epoch`), and the canvas never read the flag: the image was drawn at
// 0.92 on surface it never imaged, exactly as bright as a current one, and the
// "old coordinates" switch did nothing to it.

describe("scanImageOpacity", () => {
  const current = { stale_epoch: false };
  const old = { stale_epoch: true };
  const FADED = SCAN_IMAGE_OPACITY * STALE_EPOCH_OPACITY;

  // underlay × stale layer × image → opacity (null = not drawn)
  const cases: [boolean, boolean, "current" | "old", number | null][] = [
    [true, true, "current", SCAN_IMAGE_OPACITY],
    [true, true, "old", FADED],
    [true, false, "current", SCAN_IMAGE_OPACITY],
    [true, false, "old", null],
    [false, true, "current", null],
    [false, true, "old", null],
    [false, false, "current", null],
    [false, false, "old", null],
  ];
  for (const [underlay, stale, which, want] of cases) {
    it(`underlay ${underlay ? "on" : "off"} · old coordinates ${stale ? "on" : "off"} · ${which} image → ${want ?? "hidden"}`, () => {
      const got = scanImageOpacity(which === "old" ? old : current, layers({ underlay, stale }));
      assert.equal(got, want);
    });
  }

  it("fades an old image by exactly the factor an old marker gets", () => {
    const got = scanImageOpacity(old, DEFAULT_LAYERS)!;
    assert.ok(Math.abs(got / SCAN_IMAGE_OPACITY - STALE_EPOCH_OPACITY) < 1e-12);
    assert.ok(got < 0.25, `an old image at ${got} still reads as current`);
  });

  it("treats a missing flag as current, not as old", () => {
    // Older backends and the in-progress frame send no flag at all.
    assert.equal(scanImageOpacity({}, DEFAULT_LAYERS), SCAN_IMAGE_OPACITY);
    assert.equal(scanImageOpacity({ stale_epoch: null }, DEFAULT_LAYERS), SCAN_IMAGE_OPACITY);
  });

  it("stops fitting the view around an old image the layer hides", () => {
    const map = mapWith({
      markers: [{ status: "done", x_m: 0, y_m: 0, w_m: nm(20), h_m: nm(20) }],
      scan_images: [
        { center_x_m: 0, center_y_m: 0, width_m: nm(50), height_m: nm(50) },
        { center_x_m: nm(6000), center_y_m: 0, width_m: nm(100), height_m: nm(100), stale_epoch: true },
      ],
    });
    assert.ok(boundsForMode("fit", map, null, DEFAULT_LAYERS).bounds!.maxX > nm(6000));
    const hidden = boundsForMode("fit", map, null, layers({ stale: false })).bounds!;
    assert.ok(hidden.maxX < nm(100), `the hidden old image still holds the view open to ${hidden.maxX}`);
  });

  it("keeps fitting a current image when only the old ones are hidden", () => {
    const map = mapWith({
      scan_images: [{ center_x_m: nm(3000), center_y_m: 0, width_m: nm(100), height_m: nm(100) }],
    });
    assert.ok(boundsForMode("fit", map, null, layers({ stale: false })).bounds!.maxX > nm(3000));
  });
});

// ── map chrome under wheel zoom  ───────────────────────────────────────

describe("visibleStepNm / niceStepNm", () => {
  it("is unchanged at 1× — the whole-map behaviour is the baseline", () => {
    assert.equal(visibleStepNm(500, 1), niceStepNm(500));
    assert.equal(niceStepNm(500), 100);
  });

  it("subdivides as the operator zooms in, instead of holding whole-map spacing", () => {
    assert.ok(visibleStepNm(500, 10) < visibleStepNm(500, 1));
    assert.ok(visibleStepNm(500, 25) < visibleStepNm(500, 10));
  });

  it("keeps the scale bar a roughly constant fraction of the canvas", () => {
    // Screen length = step · (px per nm) · k. Because the step tracks 1/k, that
    // product must not run away with zoom — the old code multiplied by k with a
    // fixed step, so at 25× the bar was 25× too long and left the canvas.
    const spanNm = 500;
    const pxPerNm = 532 / spanNm; // CANVAS_W - 2·PAD over the span
    const lengths = [1, 2, 5, 10, 25].map((k) => visibleStepNm(spanNm, k) * pxPerNm * k);
    for (const L of lengths) {
      assert.ok(L > 40 && L < 300, `scale bar ${L}px must stay on a 620px canvas`);
    }
  });

  it("survives a nonsense zoom rather than producing NaN spacing", () => {
    assert.equal(visibleStepNm(500, 0), niceStepNm(500));
    assert.equal(visibleStepNm(500, Number.NaN), niceStepNm(500));
  });
});

describe("inverseStageTransform / stageToScreen", () => {
  it("composes with the stage transform to the identity", () => {
    const zoom = { k: 7.3, x: -211, y: 88 };
    const inv = inverseStageTransform(zoom);
    for (const p of [{ x: 0, y: 0 }, { x: 310, y: 230 }, { x: 619, y: 459 }]) {
      // stage(layer(p)) — what Konva actually paints for a layer carrying inv.
      const lx = p.x * inv.scaleX + inv.x;
      const ly = p.y * inv.scaleY + inv.y;
      const out = stageToScreen({ x: lx, y: ly }, zoom);
      assert.ok(Math.abs(out.x - p.x) < 1e-9, `${out.x} ≈ ${p.x}`);
      assert.ok(Math.abs(out.y - p.y) < 1e-9, `${out.y} ≈ ${p.y}`);
    }
  });

  it("is the identity at 1× so an un-zoomed map is untouched", () => {
    assert.deepEqual(inverseStageTransform({ k: 1, x: 0, y: 0 }), {
      scaleX: 1, scaleY: 1, x: 0, y: 0,
    });
    assert.deepEqual(stageToScreen({ x: 12, y: 34 }, { k: 1, x: 0, y: 0 }), { x: 12, y: 34 });
  });

  it("moves a world anchor off-canvas when it really is off-canvas", () => {
    // The culling the tick labels rely on: at 10× with a big pan, a tick that
    // used to sit at x=300 is nowhere near the 620px canvas.
    const s = stageToScreen({ x: 300, y: 100 }, { k: 10, x: -2800, y: 0 });
    assert.equal(s.x, 200);
    const far = stageToScreen({ x: 300, y: 100 }, { k: 10, x: -3400, y: 0 });
    assert.ok(far.x < 0, `${far.x} must be culled`);
  });

  it("degrades a bad zoom to the identity instead of dividing by zero", () => {
    assert.deepEqual(inverseStageTransform({ k: 0, x: 5, y: 5 }), {
      scaleX: 1, scaleY: 1, x: -5, y: -5,
    });
  });
});

describe("fmtNm", () => {
  it("rounds to whole nm when the grid step is whole nm", () => {
    assert.equal(fmtNm(137.4, 50), "137");
    assert.equal(fmtNm(-0.2, 50), "0"); // never a stray "-0"
  });

  it("keeps sub-nm precision once the zoom makes the step sub-nm", () => {
    // The bug this replaces: Math.round() printed "0" for every tick on a
    // zoomed-in map, and "0 nm" on the scale bar.
    assert.equal(fmtNm(0.25, 0.05), "0.25");
    assert.equal(fmtNm(1.5, 0.5), "1.5");
    assert.notEqual(fmtNm(0.25, 0.05), "0");
  });

  it("is never more precise than the step it labels", () => {
    assert.equal(fmtNm(1.23456, 1), "1");
    assert.equal(fmtNm(1.23456, 0.1), "1.2");
  });

  it("returns empty for a non-finite value rather than 'NaN'", () => {
    assert.equal(fmtNm(Number.NaN, 1), "");
    assert.equal(fmtNm(1.0, Number.NaN), "1");
  });
});

// ── world-anchored labels under zoom ───────────────────────────────────────
//
// #52 pinned the GLOBAL chrome (ticks, scale bar) with inverseStageTransform.
// The five labels that belong to a moving feature stayed in the zoomed layer,
// so they kept scaling with the stage: at 25× a 10 px caption paints 250 px
// tall over the surface it names — the same complaint as #52:
// captions don't stay put when the wheel zooms.

describe("pinnedLabel", () => {
  it("renders at constant screen size whatever the zoom", () => {
    for (const k of [1, 2.5, 25]) {
      const p = pinnedLabel({ x: 100, y: 100 }, 8, -6, k);
      // Konva paints glyphs at fontSize · stageScale · nodeScale.
      assert.ok(Math.abs(k * p.scaleX - 1) < 1e-12, `${k}× 下字号被放大了 ${k * p.scaleX}`);
      assert.ok(Math.abs(k * p.scaleY - 1) < 1e-12);
    }
  });

  it("keeps the gap between feature and caption constant on screen", () => {
    // The label's screen offset from its anchor must not depend on k.
    for (const k of [1, 4, 25]) {
      const p = pinnedLabel({ x: 100, y: 100 }, 8, -6, k);
      assert.ok(Math.abs((p.x - 100) * k - 8) < 1e-12, `${k}× 下横向间距变成 ${(p.x - 100) * k}`);
      assert.ok(Math.abs((p.y - 100) * k - -6) < 1e-12);
    }
  });

  it("stays anchored to the feature — the anchor is NOT counter-scaled", () => {
    // The whole reason this is not just inverseStageTransform on the layer:
    // the anchor has to keep riding the world, or the caption drifts off the
    // thing it names as soon as the operator pans.
    const p = pinnedLabel({ x: 640, y: 480 }, 0, 0, 25);
    assert.deepEqual({ x: p.x, y: p.y }, { x: 640, y: 480 });
  });

  it("is a no-op offset at 1× so an un-zoomed map is untouched", () => {
    assert.deepEqual(pinnedLabel({ x: 10, y: 20 }, 8, -6, 1), {
      x: 18, y: 14, scaleX: 1, scaleY: 1,
    });
  });

  it("degrades a bad zoom to 1× instead of dividing by zero", () => {
    for (const bad of [0, -3, Number.NaN, Number.POSITIVE_INFINITY]) {
      const p = pinnedLabel({ x: 10, y: 20 }, 8, -6, bad);
      assert.deepEqual(p, { x: 18, y: 14, scaleX: 1, scaleY: 1 }, `k=${bad}`);
    }
  });
});

// ── marker detail popup  ──────────────────────────────────────

describe("markerDetailRows", () => {
  const row = (rows: { label: string; value: string }[], label: string) =>
    rows.find((r) => r.label === label);

  it("carries what the one-line tooltip could not", () => {
    // The hover tip is capped at 200 px with wrap="none" — a label and a clock
    // time and nothing else. Skill, outcome and frame size had nowhere to go.
    const rows = markerDetailRows({
      kind: "scan",
      skill_name: "StartScan",
      status: "done",
      timestamp: "2026-08-05T13:42:11",
      x_m: 1.2e-7,
      y_m: -3.4e-8,
      w_m: 1e-7,
      h_m: 1e-7,
      coord_epoch: 2,
    }, 2);
    assert.equal(row(rows, "技能")?.value, "StartScan");
    assert.equal(row(rows, "状态")?.value, "已完成");
    assert.match(row(rows, "时间")!.value, /2026-08-05 13:42:11/);
    assert.match(row(rows, "位置")!.value, /120\.00 nm/);
    assert.match(row(rows, "尺寸")!.value, /100\.00 nm × 100\.00 nm/);
  });

  it("says a stale epoch IS stale, in words", () => {
    // Fading the marker says "something is different"; it does not say the
    // coordinates no longer point at where the operation happened. That is the
    // one thing here that changes what the operator should do next.
    const stale = markerDetailRows({ coord_epoch: 1 }, 3);
    assert.match(row(stale, "坐标代次")!.value, /旧坐标系/);
    const fresh = markerDetailRows({ coord_epoch: 3 }, 3);
    assert.equal(row(fresh, "坐标代次")!.value, "#3");
  });

  it("omits what it does not have rather than printing a zero", () => {
    // A marker with no size is a POINT operation (a pulse, a spectrum), not a
    // zero-sized scan. 「尺寸 0 × 0」 would be a false statement.
    const rows = markerDetailRows({ kind: "pulse", x_m: 0, y_m: 0 });
    assert.equal(row(rows, "尺寸"), undefined);
    assert.equal(row(rows, "时间"), undefined);
    assert.equal(row(rows, "技能"), undefined);
    assert.ok(row(rows, "位置"), "a point operation still has a position");
  });

  it("survives a marker with nothing on it at all", () => {
    assert.deepEqual(markerDetailRows({}), []);
    assert.deepEqual(markerDetailRows({ x_m: null, y_m: undefined }), []);
  });

  it("drops a non-finite coordinate instead of rendering NaN", () => {
    const rows = markerDetailRows({ x_m: Number.NaN, y_m: 1e-9 });
    assert.equal(row(rows, "位置"), undefined);
  });
});

// ── what is drawn: layers × time window (scan_map_upgrade.md §2.3) ───────────

describe("drawnHistory", () => {
  const T = (h: number) => `2026-11-12T${String(h).padStart(2, "0")}:00:00`;
  const at = (h: number) => new Date(2026, 10, 12, h).getTime();
  const markers = [
    { kind: "scan", status: "done", coord_epoch: 1, x_m: 0, y_m: 0, timestamp: T(9) },
    { kind: "sts", status: "done", coord_epoch: 1, x_m: 0, y_m: 0, timestamp: T(11) },
    { kind: "sts", status: "done", coord_epoch: 1, x_m: 0, y_m: 0, timestamp: "" }, // undated
    { kind: "sts", status: "done", coord_epoch: 1, x_m: 0, y_m: 0, timestamp: "garbage" }, // bad
    { kind: "pulse", status: "done", coord_epoch: 0, x_m: 0, y_m: 0, timestamp: T(8) }, // stale
    { kind: "scan", status: "planned", x_m: 0, y_m: 0 }, // the route, not history
    { kind: "move", status: "done", coord_epoch: 1, x_m: null, y_m: 0, timestamp: T(12) }, // no position
  ];

  it("keeps executed, positioned markers and their indexes", () => {
    const got = drawnHistory(markers, 1, DEFAULT_LAYERS).map((d) => d.index);
    assert.deepEqual(got, [0, 1, 2, 3, 4]);
  });

  it("splits live and old coordinates by their two layers", () => {
    assert.deepEqual(drawnHistory(markers, 1, layers({ stale: false })).map((d) => d.index), [0, 1, 2, 3]);
    assert.deepEqual(drawnHistory(markers, 1, layers({ history: false })).map((d) => d.index), [4]);
  });

  it("drops what is outside the window but ALWAYS keeps the undated", () => {
    const w = { from: at(10), to: null };
    assert.deepEqual(drawnHistory(markers, 1, DEFAULT_LAYERS, w).map((d) => d.index), [1, 2, 3]);
  });

  it("an empty window leaves only the undated, not a crash", () => {
    const w = { from: at(23), to: null };
    assert.deepEqual(drawnHistory(markers, 1, DEFAULT_LAYERS, w).map((d) => d.index), [2, 3]);
  });

  it("survives no markers", () => {
    assert.deepEqual(drawnHistory(null, 0, DEFAULT_LAYERS), []);
  });
});

describe("drawnScanImages", () => {
  const imgs = [
    { name: "old", stale_epoch: true, mtime: "2026-11-12T00:00:00Z" },
    { name: "a", mtime: "2026-11-12T01:00:00Z" },
    { name: "b", mtime: "2026-11-12T03:00:00Z" },
    { name: "undated", mtime: "" },
  ];

  it("applies the stale rule and carries the opacity", () => {
    const all = drawnScanImages(imgs, DEFAULT_LAYERS);
    assert.deepEqual(all.map((d) => d.img.name), ["old", "a", "b", "undated"]);
    assert.ok(all[0]!.opacity < all[1]!.opacity);
    assert.deepEqual(drawnScanImages(imgs, layers({ stale: false })).map((d) => d.img.name), ["a", "b", "undated"]);
  });

  it("filters by mtime and keeps an image whose mtime could not be read", () => {
    const w = { from: Date.UTC(2026, 10, 12, 2), to: null };
    assert.deepEqual(drawnScanImages(imgs, DEFAULT_LAYERS, w).map((d) => d.img.name), ["b", "undated"]);
  });

  it("never filters the scan in progress out by time", () => {
    // Its mtime is its newest milestone's. A window whose upper end closed
    // before that hid the very scan the operator was watching.
    const withLive = [...imgs, { name: "in-progress", mtime: "2026-11-12T05:00:00Z", live: true }];
    const closed = { from: Date.UTC(2026, 10, 12, 0, 30), to: Date.UTC(2026, 10, 12, 1, 30) };
    assert.deepEqual(drawnScanImages(withLive, DEFAULT_LAYERS, closed).map((d) => d.img.name), ["a", "undated", "in-progress"]);
    const before = { from: Date.UTC(2026, 10, 12, 6), to: null };
    assert.deepEqual(drawnScanImages(withLive, DEFAULT_LAYERS, before).map((d) => d.img.name), ["undated", "in-progress"]);
  });

  it("still filters a saved scan that merely claims nothing about being live", () => {
    const w = { from: Date.UTC(2026, 10, 12, 2), to: null };
    const notLive = [{ name: "saved", mtime: "2026-11-12T01:00:00Z", live: false }, { name: "old", mtime: "2026-11-12T01:00:00Z", live: null }];
    assert.deepEqual(drawnScanImages(notLive, DEFAULT_LAYERS, w), []);
  });
});

describe("marker stamps with their own offset", () => {
  const at = (ts: string) => ({ kind: "sts", status: "done", coord_epoch: 0, x_m: 0, y_m: 0, timestamp: ts });
  /** "+02:00" for 120 minutes east. */
  const suffix = (min: number) => {
    const a = Math.abs(min);
    return `${min < 0 ? "-" : "+"}${String(Math.floor(a / 60)).padStart(2, "0")}:${String(a % 60).padStart(2, "0")}`;
  };
  /** The wall clock at `offMin`, as the backend writes it: "2026-11-12T10:00:00.000+08:00". */
  const stamp = (ms: number, offMin: number) => new Date(ms + offMin * 60_000).toISOString().slice(0, 23) + suffix(offMin);

  it("files markers into the time window by the lab's clock", () => {
    // A lab two hours WEST of wherever this test runs: without the offset, read
    // as browser-local, its stamps land two hours early and fall out of a "last
    // 30 min" window (whose upper end is open, so a lab to the east would not
    // show the difference).
    const ref = Date.UTC(2026, 10, 12, 2);
    const lab = browserOffsetMin(ref) - 120;
    const markers = [at(stamp(ref, lab)), at(stamp(ref - 3 * 3_600_000, lab))];
    const recent = { from: ref - 30 * 60_000, to: null };
    assert.deepEqual(drawnHistory(markers, 0, DEFAULT_LAYERS, recent).map((d) => d.index), [0]);
    const naive = markers.map((m) => ({ ...m, timestamp: m.timestamp.slice(0, 23) }));
    assert.deepEqual(drawnHistory(naive, 0, DEFAULT_LAYERS, recent).map((d) => d.index), [], "test premise: the naive reading is two hours off");
  });

  it("shows a marker's time in the popup the way the image's time is shown", () => {
    // Both through the browser's clock: one popup, one time zone.
    const rows = markerDetailRows({ timestamp: "2026-11-12T10:00:00.123456+08:00" }, null);
    const want = new Date(Date.UTC(2026, 10, 12, 2));
    const pad = (n: number) => String(n).padStart(2, "0");
    const local = `${want.getFullYear()}-${pad(want.getMonth() + 1)}-${pad(want.getDate())} ${pad(want.getHours())}:${pad(want.getMinutes())}:${pad(want.getSeconds())}`;
    assert.equal(rows.find((r) => r.label === "时间")?.value, local);
    const img = scanImageDetailRows({ name: "a.sxm", mtime: "2026-11-12T02:00:00Z" });
    assert.equal(img.find((r) => r.label === "修改时间")?.value, local, "marker and image of the same instant print the same");
  });

  it("adds the instrument's own wall clock, at the stamp's own offset, only when the zones differ", () => {
    const t0 = Date.UTC(2026, 10, 12, 2);
    const here = browserOffsetMin(t0);
    const pc = (rows: { label: string; value: string }[]) => rows.find((r) => r.label === "仪器时钟");
    // A PC one hour east of this browser at that instant: its wall clock, as
    // written, with the offset the stamp names (not any "current" one).
    const east = stamp(t0, here + 60);
    assert.equal(pc(markerDetailRows({ timestamp: east }, null))?.value, `${east.slice(0, 10)} ${east.slice(11, 19)} (${fmtUtcOffset(here + 60)})`);
    // Same zone as the browser, or a stamp that names no offset: nothing to add.
    assert.equal(pc(markerDetailRows({ timestamp: stamp(t0, here) }, null)), undefined);
    assert.equal(pc(markerDetailRows({ timestamp: "2026-11-12T10:00:00.123456" }, null)), undefined);
  });

  it("prints an unreadable stamp as recorded instead of dropping it", () => {
    assert.equal(markerDetailRows({ timestamp: "not-a-time" }, null).find((r) => r.label === "时间")?.value, "not-a-time");
  });
});

describe("legend counts", () => {
  it("counts by kind, case-insensitively", () => {
    assert.deepEqual(kindCounts([{ kind: "sts" }, { kind: "STS" }, { kind: "scan" }]), { sts: 2, scan: 1 });
  });

  it("shows one number when everything is drawn, shown/total otherwise", () => {
    assert.equal(fmtCount(14, 14), "14");
    assert.equal(fmtCount(5, 14), "5/14");
  });
});

describe("markerKeys", () => {
  const m = (label: string, ts = "2026-11-12T10:00:00.1") => ({ kind: "sts", timestamp: ts, label, x_m: 1e-9, y_m: 0 });

  it("does not change when older rows drop off the front", () => {
    // The backend returns the newest 2000 rows: once a scope outgrows that,
    // every array index shifts on each new operation. Keys must not.
    const a = markerKeys([m("one"), m("two"), m("three")]);
    const b = markerKeys([m("two"), m("three"), m("four")]);
    assert.equal(a[1], b[0]);
    assert.equal(a[2], b[1]);
  });

  it("tells exact duplicates apart", () => {
    const k = markerKeys([m("same"), m("same")]);
    assert.notEqual(k[0], k[1]);
  });
});

describe("scanImageDetailRows", () => {
  const row = (rows: { label: string; value: string }[], label: string) => rows.find((r) => r.label === label);
  const img = {
    name: "Au111_003.sxm",
    path: "D:/data/Au111_003.sxm",
    center_x_m: 1.5e-7,
    center_y_m: -2e-8,
    width_m: 1.5e-7,
    height_m: 1.5e-7,
    angle_deg: 0,
    mtime: "2023-11-14T22:13:20Z",
  };

  it("names the file, its place, size and time", () => {
    const rows = scanImageDetailRows(img, 1_700_000_000_000 + 2 * 3_600_000);
    assert.equal(row(rows, "文件")?.value, "Au111_003.sxm");
    assert.equal(row(rows, "路径")?.value, "D:/data/Au111_003.sxm");
    assert.match(row(rows, "中心")!.value, /150\.00 nm/);
    assert.match(row(rows, "尺寸")!.value, /150\.00 nm × 150\.00 nm/);
    assert.match(row(rows, "修改时间")!.value, /2 小时前/);
    assert.equal(row(rows, "角度"), undefined, "a zero angle is not worth a row");
    assert.equal(row(rows, "坐标")?.value, "当前坐标系");
  });

  it("says in words what an old-coordinate image means", () => {
    const rows = scanImageDetailRows({ ...img, stale_epoch: true });
    assert.match(row(rows, "坐标")!.value, /粗动之前/);
    assert.match(row(rows, "坐标")!.value, /淡显/);
  });

  it("says an unreadable mtime is unknown, rather than 1970 or nothing", () => {
    const rows = scanImageDetailRows({ ...img, mtime: "" });
    assert.match(row(rows, "修改时间")!.value, /读不到/);
  });
});

describe("fmtNm minus sign", () => {
  it("prints axis numbers with U+2212, not a hyphen", () => {
    assert.equal(fmtNm(-200, 100), "\u2212200");
    assert.equal(fmtNm(-0.25, 0.05), "\u22120.25");
    assert.equal(fmtNm(200, 100), "200");
  });
});

// ── the marker list, the export name, the 200× ceiling ──────────────────────

describe("filterMarkerRows", () => {
  const rows = [
    { index: 0, m: { kind: "scan", label: "scan #1", skill_name: "ScanFrame" } },
    { index: 3, m: { kind: "sts", label: "STS #1", skill_name: "AcquireSTS" } },
    { index: 7, m: { kind: "STS", label: "", skill_name: "GridSTS" } },
    { index: 9, m: { kind: "pulse", label: "pulse 3 V", skill_name: "BiasPulse" } },
  ];
  const names: Record<string, string> = { sts: "STS 点谱", scan: "扫图", pulse: "电脉冲" };
  const kindName = (k?: string | null) => names[(k ?? "").toLowerCase()] ?? "";

  it("lists newest first, by record order", () => {
    assert.deepEqual(filterMarkerRows(rows, {}).map((r) => r.index), [9, 7, 3, 0]);
  });

  it("narrows by kind, case-insensitively", () => {
    assert.deepEqual(filterMarkerRows(rows, { kind: "sts" }).map((r) => r.index), [7, 3]);
  });

  it("matches the query against label, skill and the kind's display name", () => {
    assert.deepEqual(filterMarkerRows(rows, { query: "grid" }).map((r) => r.index), [7]);
    assert.deepEqual(filterMarkerRows(rows, { query: " 3 V " }).map((r) => r.index), [9]);
    assert.deepEqual(filterMarkerRows(rows, { query: "点谱" }, kindName).map((r) => r.index), [7, 3]);
  });

  it("returns nothing rather than everything when nothing matches", () => {
    assert.deepEqual(filterMarkerRows(rows, { query: "zzz" }), []);
  });

  it("does not reorder the caller's array", () => {
    const copy = rows.map((r) => r.index);
    filterMarkerRows(rows, {});
    assert.deepEqual(rows.map((r) => r.index), copy);
  });
});

describe("markerKindOptions", () => {
  const rows = [{ m: { kind: "scan" } }, { m: { kind: "STS" } }, { m: { kind: "pulse" } }, { m: { kind: "" } }];

  it("offers the kinds the rows have, once each, lower-cased and sorted", () => {
    assert.deepEqual(markerKindOptions(rows, ""), ["pulse", "scan", "sts"]);
  });

  it("keeps the selected kind as an option after its rows have left", () => {
    // "Last 1 h" took the STS rows away. Without the option the select showed
    // "All kinds" while the list stayed narrowed to STS — to nothing.
    const later = rows.filter((r) => (r.m.kind ?? "").toLowerCase() !== "sts");
    assert.deepEqual(markerKindOptions(later, "sts"), ["pulse", "scan", "sts"]);
    assert.deepEqual(filterMarkerRows(later.map((r, index) => ({ ...r, index })), { kind: "sts" }), []);
  });

  it("does not add an option for no selection", () => {
    assert.deepEqual(markerKindOptions([], ""), []);
    assert.deepEqual(markerKindOptions([], "  "), []);
  });
});

describe("exportFileName", () => {
  const at = new Date(2026, 10, 12, 15, 42, 10);

  it("carries the sample and the local time", () => {
    assert.equal(exportFileName("Au(111) #3", at), "scanmap_Au(111)_#3_20261112-154210.png");
  });

  it("replaces what Windows forbids in a file name", () => {
    assert.equal(exportFileName('a<b>c:"d/e\\f|g?h*', at), "scanmap_a_b_c_d_e_f_g_h_20261112-154210.png");
    assert.ok(!exportFileName("C:\\data\\x", at).includes("\\"), "a backslash must not survive");
  });

  it("keeps a Chinese sample name", () => {
    assert.equal(exportFileName("金 111 样品", at), "scanmap_金_111_样品_20261112-154210.png");
  });

  it("falls back to 'map' without a usable sample name", () => {
    assert.equal(exportFileName("", at), "scanmap_map_20261112-154210.png");
    assert.equal(exportFileName(null, at), "scanmap_map_20261112-154210.png");
    assert.equal(exportFileName("???", at), "scanmap_map_20261112-154210.png");
  });
});

describe("sub-nanometre grid at the 200× ceiling", () => {
  it("raised the ceiling to 200×", () => {
    assert.equal(MAX_ZOOM, 200);
  });

  it("steps go sub-nm and the labels keep the digits they need", () => {
    // A 300 nm map at 200× shows 1.5 nm across the canvas.
    const step = visibleStepNm(300, 200);
    assert.ok(step < 1 && step > 0, `step ${step}`);
    assert.equal(step, 0.2);
    assert.equal(fmtNm(0.4, step), "0.4");
    assert.equal(fmtNm(-0.6000000000000001, step), "\u22120.6", "float noise from i·step must not leak");
    // A 20 nm map at 200× shows 0.1 nm: two decimals.
    const fine = visibleStepNm(20, 200);
    assert.equal(fine, 0.02);
    assert.equal(fmtNm(0.04, fine), "0.04");
  });

  it("keeps the scale bar on the canvas all the way to 200×", () => {
    const spanNm = 500;
    const pxPerNm = 532 / spanNm;
    for (const k of [25, 50, 100, 200]) {
      const L = visibleStepNm(spanNm, k) * pxPerNm * k;
      assert.ok(L > 40 && L < 300, `scale bar ${L}px at ${k}×`);
    }
  });

  it("never prints a tick label with more than a few decimals", () => {
    for (const k of [1, 10, 50, 200]) {
      const step = visibleStepNm(437, k);
      for (let i = -3; i <= 3; i++) {
        const label = fmtNm(i * step + 1234.5678 * (k === 1 ? 1 : 0), step);
        assert.ok(!/\d\.\d{5,}/.test(label), `${label} at step ${step}`);
      }
    }
  });
});

// ── the live scan layer (docs/v2/design/scan_map_live.md) ───────────────────

describe("live scan layer", () => {
  it("comes on for a browser whose saved layers predate it", () => {
    // An old localStorage has every other key and not this one.
    const old = { ...DEFAULT_LAYERS } as Partial<MapLayers>;
    delete old.live;
    assert.equal(withLayerDefaults(old).live, true);
  });

  it("is on in All and Minimal, off with the thumbnails in Planning focus", () => {
    const by = (id: string) => MAP_PRESETS.find((p) => p.id === id)!.layers;
    assert.equal(by("all").live, true);
    assert.equal(by("minimal").live, true);
    assert.equal(by("planning").live, false);
  });

  it("the 1/8 milestone image steps aside for the stream, and goes with the layer", () => {
    const imgs = [{ name: "saved.sxm" }, { name: "milestone.png", live: true }];
    const names = (l: Partial<MapLayers>, shown = false) =>
      drawnScanImages(imgs, layers(l), undefined, shown).map((d) => d.img.name);
    assert.deepEqual(names({}), ["saved.sxm", "milestone.png"], "no stream: the PNG is the fallback");
    assert.deepEqual(names({}, true), ["saved.sxm"], "the stream is drawing that frame");
    assert.deepEqual(names({ live: false }), ["saved.sxm"], "live layer off");
  });
});
