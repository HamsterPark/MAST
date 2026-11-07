// Alignment camera (contract §13.1): how to read GET /api/camera/{camera_id}/stats and how to
// convert coordinates for the marker drawn on the frame.
//
// stats: state (state / connected / live / error / settled) plus spot statistics of the latest
// frame (exposure_us, gain_db, fps, max, saturated_frac, centroid_px, fwhm_px, roi, frame_seq, t,
// frame_w, frame_h, and centroid_sensor_px, fwhm_major_px / fwhm_minor_px / angle_deg). When not
// live every statistic is null and the response is still 200; 404 only means there is no such camera.
//
// Coordinates (defined by the backend): centroid_px / fwhm_px are in sensor pixels, with the origin
// at the **centre** of the top-left ROI pixel; frame_w / frame_h are the number of sensor pixels
// the frame spans (usually the ROI width and height), not the JPEG's pixel count. A point is
// accepted as [x, y] or {x, y}; roi as [x, y, w, h] or {x, y, w, h, binning}.

import { tr } from "../i18n.ts";

export interface Pt {
  x: number;
  y: number;
}

export interface Roi {
  x: number;
  y: number;
  w: number;
  h: number;
  /** Pixel binning; 1 when absent. */
  binning: number;
}

export interface CameraStats {
  /** disconnected | connected | live | error (unrecognised values pass through); null when absent. */
  state: string | null;
  connected: boolean | null;
  live: boolean | null;
  /** Verbatim error text when the camera is in error. */
  error: string | null;
  /** false = exposure / gain / ROI was just changed, so this frame may still use the old settings. */
  settled: boolean | null;
  exposureUs: number | null;
  gainDb: number | null;
  fps: number | null;
  max: number | null;
  saturatedFrac: number | null;
  centroidPx: Pt | null;
  /** Absolute coordinates on the sensor. */
  centroidSensorPx: Pt | null;
  /** FWHM in both directions (pixels); when only one number is given both directions are equal. */
  fwhmPx: Pt | null;
  fwhmMajorPx: number | null;
  fwhmMinorPx: number | null;
  angleDeg: number | null;
  roi: Roi | null;
  /** Width and height of the frame coordinate system (the pixel grid the centroid is in); falls back to the ROI size. */
  frameW: number | null;
  frameH: number | null;
  frameSeq: number | null;
  t: number | null;
}

function isObj(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}
function num(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}
function bool(v: unknown): boolean | null {
  return typeof v === "boolean" ? v : null;
}
function str(v: unknown): string | null {
  return typeof v === "string" && v.trim() ? v.trim() : null;
}

function pt(v: unknown): Pt | null {
  if (Array.isArray(v) && v.length >= 2 && num(v[0]) !== null && num(v[1]) !== null) return { x: v[0] as number, y: v[1] as number };
  if (isObj(v) && num(v.x) !== null && num(v.y) !== null) return { x: v.x as number, y: v.y as number };
  return null;
}

function roi(v: unknown): Roi | null {
  if (Array.isArray(v) && v.length >= 4 && v.slice(0, 4).every((n) => num(n) !== null)) {
    const [x, y, w, h] = v as number[];
    return { x: x!, y: y!, w: w!, h: h!, binning: num(v[4]) ?? 1 };
  }
  if (isObj(v)) {
    const x = num(v.x) ?? num(v.x0);
    const y = num(v.y) ?? num(v.y0);
    const w = num(v.w) ?? num(v.width);
    const h = num(v.h) ?? num(v.height);
    if (x !== null && y !== null && w !== null && h !== null) return { x, y, w, h, binning: num(v.binning) ?? 1 };
  }
  return null;
}

export function normalizeCameraStats(raw: unknown): CameraStats | null {
  if (!isObj(raw)) return null;
  const f = raw.fwhm_px;
  const r = roi(raw.roi);
  return {
    state: str(raw.state),
    connected: bool(raw.connected),
    live: bool(raw.live),
    error: str(raw.error),
    settled: bool(raw.settled),
    exposureUs: num(raw.exposure_us),
    gainDb: num(raw.gain_db),
    fps: num(raw.fps),
    max: num(raw.max),
    saturatedFrac: num(raw.saturated_frac),
    centroidPx: pt(raw.centroid_px),
    centroidSensorPx: pt(raw.centroid_sensor_px),
    fwhmPx: num(f) !== null ? { x: f as number, y: f as number } : pt(f),
    fwhmMajorPx: num(raw.fwhm_major_px),
    fwhmMinorPx: num(raw.fwhm_minor_px),
    angleDeg: num(raw.angle_deg),
    roi: r,
    frameW: num(raw.frame_w) ?? num(raw.width) ?? r?.w ?? null,
    frameH: num(raw.frame_h) ?? num(raw.height) ?? r?.h ?? null,
    frameSeq: num(raw.frame_seq),
    t: num(raw.t),
  };
}

export const CAMERA_STATE_LABEL: Record<string, string> = {
  disconnected: tr("未连接", "Disconnected"),
  connected: tr("已连接（没在直播）", "Connected (not live)"),
  live: tr("直播中", "Live"),
  error: tr("出错", "Error"),
};

/**
 * Sensor pixels -> relative position in the display box (0...1). The origin is the centre of the
 * top-left ROI pixel, so add half a pixel first and then divide by the number of pixels the frame
 * spans. When frameW/H are unknown, fall back to the image's own pixel size (naturalW/H), which is
 * only correct if the backend did not downscale, hence stats should carry the frame size.
 */
export function toRelative(p: Pt, s: Pick<CameraStats, "frameW" | "frameH">, naturalW: number, naturalH: number): Pt | null {
  const w = s.frameW ?? naturalW;
  const h = s.frameH ?? naturalH;
  if (!(w > 0) || !(h > 0)) return null;
  return { x: (p.x + 0.5) / w, y: (p.y + 0.5) / h };
}

/** Camera id: fixed.camera_id of an action in the description; otherwise the last segment of the device id (local/camera/<id>). */
export function cameraIdOf(d: { id: string; actions: ReadonlyArray<{ fixed: Record<string, unknown> }> }): string {
  for (const a of d.actions) {
    const v = a.fixed.camera_id;
    if (typeof v === "string" && v.trim()) return v.trim();
  }
  return d.id.split("/").pop() || d.id;
}

/** Frame-rate choice: 1-10 fps (contract §13.1). */
export function clampFps(v: number): number {
  if (!Number.isFinite(v)) return 4;
  return Math.min(10, Math.max(1, Math.round(v)));
}
