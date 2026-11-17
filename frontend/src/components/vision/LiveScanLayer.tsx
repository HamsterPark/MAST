import { useEffect, useRef, useState } from "react";
import { Group, Image as KonvaImage, Line } from "react-konva";
import type Konva from "konva";
import { useLiveDraw, useLiveStream, useLiveTick } from "@/hooks/useLive";
import { ensureBackfill, getLiveStore } from "@/lib/liveClient";
import type { Map2dBuf } from "@/lib/live/store";
import {
  LIVE_STATUS_STREAM,
  LiveImageModel,
  liveFrameStreamId,
  liveScanUsable,
  liveStaleMs,
  needsBackfillKick,
  parseLiveScanMeta,
  readLiveStatus,
  scanLineWorld,
  type LiveScanMeta,
  type LiveStatus,
} from "@/lib/scanLive";

// The scan map's live scan: the frame being acquired, row by row, with a scan
// line — the React half. The decisions (which session, live or fallback, how
// to flatten and colour a row, where the line goes) live in lib/scanLive.ts;
// the backend is mast/monitoring/scan_live.py; the design is
// docs/v2/design/scan_map_live.md.
//
// No text is drawn on the canvas from here: the row count and the up-scan
// caveat go in the chip row above the map, where they are readable at any zoom
// (and where the label-space guard in scanMapLabels.test.ts does not need to
// look for them).

/** After a scan ends, keep its picture this long — until the saved .sxm
 *  (written by Nanonis at the end of the scan, picked up by the next 3 s map
 *  poll) is there to replace it. */
const LIVE_GRACE_MS = 20_000;
/** How often a quiet stream with gaps gets its backfill re-kicked. */
const BACKFILL_KICK_MS = 2_000;

export interface LiveScanState {
  status: LiveStatus | null;
  /** The session stream to draw (the running one, or the one that just ended). */
  frameId: string | null;
  meta: LiveScanMeta | null;
  /** The feed is live: a scan is running and its rows are arriving (scan line, status chip). */
  usable: boolean;
  /** Draw the stream, and not the 1/8 milestone image of the same frame: while
   *  usable, and for a grace period after the scan ended. */
  show: boolean;
  /** Frames confirmed lost (the server no longer has them): rows on a
   *  map2d_rows stream, pixels on an older backend's map2d stream. */
  lost: number;
  lostUnit: "row" | "pixel";
  /** Frames still being backfilled. */
  missing: boolean;
}

export function useLiveScan(enabled: boolean): LiveScanState {
  const statusId = enabled ? LIVE_STATUS_STREAM : null;
  useLiveStream(statusId);
  useLiveTick(statusId, 2);
  const statusSt = statusId ? getLiveStore().get(statusId) : undefined;
  const status = readLiveStatus(statusSt?.buf as never);

  const last = useRef<{ session: number; endedAt: number | null } | null>(null);
  const now = Date.now();
  if (status?.active && status.session !== null) {
    if (!last.current || last.current.session !== status.session || last.current.endedAt !== null) {
      last.current = { session: status.session, endedAt: null };
    }
  } else if (last.current && last.current.endedAt === null) {
    last.current = { ...last.current, endedAt: now };
  }
  const prev = last.current;
  const inGrace = !!prev && prev.endedAt !== null && now - prev.endedAt < LIVE_GRACE_MS;
  const session = status?.active ? status.session : inGrace ? prev!.session : null;
  const frameId = enabled && session !== null ? liveFrameStreamId(session) : null;

  useLiveStream(frameId);
  useLiveTick(frameId, 1);
  const frameSt = frameId ? getLiveStore().get(frameId) : undefined;

  // A slow remote client that fell behind can be left with gaps once the scan
  // ends and no new frame re-triggers the backfill (lib/liveClient.ts).
  useEffect(() => {
    if (!frameId) return;
    const t = setInterval(() => {
      if (needsBackfillKick(getLiveStore().get(frameId), Date.now())) ensureBackfill(frameId);
    }, BACKFILL_KICK_MS);
    return () => clearInterval(t);
  }, [frameId]);

  const meta = frameSt ? parseLiveScanMeta(frameSt.meta.meta) : null;
  const filled = frameSt && frameSt.buf.kind === "map2d" ? (frameSt.buf as Map2dBuf).filled : 0;
  const lastStatusMs = statusSt?.lastRecvMs ?? null;
  const usable = !!meta && liveScanUsable(status, frameSt ? { filled, lastStatusMs } : null, now);

  // Re-render at the next moment the answer changes with no new data: the
  // feed going stale, the grace period running out. Nothing else would wake
  // the map then — that is exactly when the streams have gone quiet.
  const [, wake] = useState(0);
  let deadline: number | null = null;
  if (usable && lastStatusMs !== null) deadline = lastStatusMs + liveStaleMs(status?.intervalS ?? null) + 1;
  if (inGrace && prev?.endedAt != null) {
    const end = prev.endedAt + LIVE_GRACE_MS;
    deadline = deadline === null ? end : Math.min(deadline, end);
  }
  useEffect(() => {
    if (deadline === null) return;
    const t = setTimeout(() => wake((n) => n + 1), Math.max(0, deadline - Date.now()) + 50);
    return () => clearTimeout(t);
  }, [deadline]);

  return {
    status,
    frameId,
    meta,
    usable,
    show: !!meta && filled > 0 && (usable || inGrace),
    lost: frameSt?.lost ?? 0,
    lostUnit: frameSt?.meta.kind === "map2d" ? "pixel" : "row",
    missing: !!frameSt && !frameSt.missing.isEmpty(),
  };
}

interface Pt {
  x: number;
  y: number;
}

/**
 * The frame being acquired, as an image at its stage footprint. Updated off
 * React (useLiveDraw, once per animation frame at most): each new row is
 * flattened and painted into an nx × ny offscreen canvas that Konva draws
 * scaled and rotated with the scan frame.
 */
export function LiveScanImage({
  frameId,
  meta,
  toPx,
  scale,
  opacity = 0.95,
}: {
  frameId: string;
  meta: LiveScanMeta;
  toPx: (x: number, y: number) => Pt;
  scale: number;
  opacity?: number;
}) {
  const imgRef = useRef<Konva.Image | null>(null);
  const model = useRef<{ m: LiveImageModel; ctx: CanvasRenderingContext2D } | null>(null);
  const [el, setEl] = useState<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = document.createElement("canvas");
    canvas.width = meta.nx;
    canvas.height = meta.ny;
    const ctx = canvas.getContext("2d");
    model.current = ctx ? { m: new LiveImageModel(meta.nx, meta.ny), ctx } : null;
    setEl(canvas);
  }, [frameId, meta.nx, meta.ny]);

  useLiveDraw(
    frameId,
    (st) => {
      const cur = model.current;
      if (!cur || !st) return;
      const rep = cur.m.update(st.buf as never, meta.valueKey);
      if (rep === null || (Array.isArray(rep) && rep.length === 0)) return;
      cur.ctx.putImageData(new ImageData(cur.m.rgba, meta.nx, meta.ny), 0, 0);
      imgRef.current?.getLayer()?.batchDraw();
    },
    [el, meta.valueKey],
  );

  if (!el) return null;
  const c = toPx(meta.cx, meta.cy);
  const w = meta.w * scale;
  const h = meta.h * scale;
  return (
    <KonvaImage
      ref={imgRef}
      image={el}
      x={c.x}
      y={c.y}
      width={w}
      height={h}
      offsetX={w / 2}
      offsetY={h / 2}
      rotation={-meta.angleDeg}
      opacity={opacity}
      listening={false}
    />
  );
}

/** The scan line's colour: not the cyan of the frame outline it runs into, and
 *  clear of every afmhot hue it crosses. A dark halo keeps it readable on the
 *  image, on the dark map and on the light one. */
const SCAN_LINE = "#4ade80";
const SCAN_LINE_HALO = "rgba(0,0,0,0.6)";

/**
 * The scan line: across the frame at the scan front, turning with the frame,
 * with a caret just outside each end — so it reads as a moving cursor and not
 * as one more edge of the image it borders.
 */
export function LiveScanLine({
  meta,
  status,
  toPx,
  zoomK,
}: {
  meta: LiveScanMeta;
  status: LiveStatus;
  toPx: (x: number, y: number) => Pt;
  /** The stage zoom: carets keep their screen size. */
  zoomK: number;
}) {
  const seg = scanLineWorld(meta, status.rowsDone, status.rows, status.dir);
  if (!seg) return null;
  const a = toPx(seg.a.x, seg.a.y);
  const b = toPx(seg.b.x, seg.b.y);
  const len = Math.hypot(b.x - a.x, b.y - a.y);
  if (!(len > 0)) return null;
  const k = zoomK > 0 ? zoomK : 1;
  const dx = (b.x - a.x) / len;
  const dy = (b.y - a.y) / len;
  const gap = 2 / k;
  const depth = 9 / k;
  const half = 5 / k;
  // A caret pointing along the line, its tip `gap` outside the end.
  const caret = (p: Pt, s: 1 | -1) => {
    const tx = p.x - s * dx * gap;
    const ty = p.y - s * dy * gap;
    const bx = tx - s * dx * depth;
    const by = ty - s * dy * depth;
    return [tx, ty, bx - dy * half, by + dx * half, bx + dy * half, by - dx * half];
  };
  const pts = [a.x, a.y, b.x, b.y];
  return (
    <Group listening={false}>
      <Line points={pts} stroke={SCAN_LINE_HALO} strokeWidth={5.5} strokeScaleEnabled={false} />
      <Line points={pts} stroke={SCAN_LINE} strokeWidth={2.25} strokeScaleEnabled={false} />
      {([[a, 1], [b, -1]] as const).map(([p, s], i) => (
        <Line
          key={i}
          points={caret(p, s)}
          closed
          fill={SCAN_LINE}
          stroke={SCAN_LINE_HALO}
          strokeWidth={1.25}
          strokeScaleEnabled={false}
        />
      ))}
    </Group>
  );
}
