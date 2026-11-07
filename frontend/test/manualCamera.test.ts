// 对光相机的纯逻辑（src/lib/manual/camera.ts）：/stats 的读法、标记坐标换算、相机 id。
//
//     cd frontend && npm run test:unit

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { cameraIdOf, clampFps, normalizeCameraStats, toRelative } from "../src/lib/manual/camera.ts";

describe("相机 /stats 的读法", () => {
  it("直播时：统计、ROI（含合并）、主次轴、传感器坐标都读出来", () => {
    const s = normalizeCameraStats({
      camera_id: "zelux1",
      state: "live",
      connected: true,
      live: true,
      error: null,
      exposure_us: 2000,
      gain_db: 0,
      fps: 29.8,
      max: 812,
      saturated_frac: 0,
      centroid_px: [806.2, 474.9],
      fwhm_px: [61.2, 66.1],
      centroid_sensor_px: [806.2, 474.9],
      fwhm_major_px: 66.3,
      fwhm_minor_px: 61.0,
      angle_deg: 87,
      roi: { x: 0, y: 0, w: 1440, h: 1080, binning: 1 },
      frame_w: 1440,
      frame_h: 1080,
      frame_seq: 42,
      t: 1760000000.25,
      settled: true,
      image_w: 720,
      image_h: 540,
    })!;
    assert.equal(s.state, "live");
    assert.equal(s.live, true);
    assert.deepEqual(s.centroidPx, { x: 806.2, y: 474.9 });
    assert.deepEqual(s.centroidSensorPx, { x: 806.2, y: 474.9 });
    assert.deepEqual(s.fwhmPx, { x: 61.2, y: 66.1 });
    assert.equal(s.fwhmMajorPx, 66.3);
    assert.equal(s.angleDeg, 87);
    assert.deepEqual(s.roi, { x: 0, y: 0, w: 1440, h: 1080, binning: 1 });
    assert.equal(s.frameW, 1440, "画面在传感器上跨的像素数，不是 JPEG 的 720");
    assert.equal(s.settled, true);
  });

  it("没在直播：照样 200，统计项是 null、状态照实（不是 404）", () => {
    const s = normalizeCameraStats({
      camera_id: "zelux1",
      state: "error",
      connected: true,
      live: false,
      error: "取帧超时：USB 掉线？",
      exposure_us: null,
      centroid_px: null,
      fwhm_px: null,
      roi: null,
      frame_w: null,
    })!;
    assert.equal(s.state, "error");
    assert.equal(s.live, false);
    assert.equal(s.error, "取帧超时：USB 掉线？");
    assert.equal(s.exposureUs, null);
    assert.equal(s.centroidPx, null);
    assert.equal(s.frameW, null);
    assert.equal(normalizeCameraStats("x"), null);
  });

  it("旧写法照认：坐标 {x, y}、roi 数组、单个 FWHM 数", () => {
    const s = normalizeCameraStats({ centroid_px: { x: 1, y: 2 }, roi: [10, 20, 300, 200], fwhm_px: 5 })!;
    assert.deepEqual(s.centroidPx, { x: 1, y: 2 });
    assert.deepEqual(s.roi, { x: 10, y: 20, w: 300, h: 200, binning: 1 });
    assert.equal(s.frameW, 300, "没给 frame_w 时取 roi 的宽");
    assert.deepEqual(s.fwhmPx, { x: 5, y: 5 });
  });
});

describe("标记坐标", () => {
  it("原点是 ROI 左上角像素的中心：先加半个像素，再除以画面跨过的像素数（不是 JPEG 的像素数）", () => {
    const r = toRelative({ x: 0, y: 0 }, { frameW: 1440, frameH: 1080 }, 720, 540)!;
    assert.deepEqual(r, { x: 0.5 / 1440, y: 0.5 / 1080 });
    const c = toRelative({ x: 719.5, y: 539.5 }, { frameW: 1440, frameH: 1080 }, 720, 540)!;
    assert.deepEqual(c, { x: 0.5, y: 0.5 }, "传感器正中");
    assert.equal(toRelative({ x: 1, y: 1 }, { frameW: null, frameH: null }, 0, 0), null);
  });
});

describe("相机 id 与帧率", () => {
  it("相机 id 取动作 fixed 里的 camera_id；都没有才取设备 id 的最后一段", () => {
    assert.equal(cameraIdOf({ id: "local/camera/cam-a", actions: [{ fixed: {} }, { fixed: { camera_id: "zelux1" } }] }), "zelux1");
    assert.equal(cameraIdOf({ id: "local/camera/cam-a", actions: [] }), "cam-a");
  });

  it("帧率 1–10 fps", () => {
    assert.equal(clampFps(0), 1);
    assert.equal(clampFps(30), 10);
    assert.equal(clampFps(Number.NaN), 4);
  });
});
