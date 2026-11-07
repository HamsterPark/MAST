// ════════════════════════════════════════════════════════════════════════════
// useLive: React bindings for LiveHub real-time data (transport in lib/liveClient.ts,
// cache in lib/live/store.ts).
//
// Two kinds of updates are kept apart, which is what keeps the UI smooth at 20 Hz:
//   - canvas / uPlot redraws: useLiveDraw registers with the frame-coalescing clock as data
//     arrives, and each view is drawn at most once per requestAnimationFrame, **without going
//     through React rendering**;
//   - React-rendered text (channel names, frame counts, "x s ago"): useLiveTick, throttled to a
//     few updates per second.
// Background tabs get no rAF, so canvases are simply not drawn, and the throttling setTimeout is
// clamped to >= 1 s by the browser. Data is still received (the cache is cheap) and the first
// frame after returning to the foreground draws the latest state.
// ════════════════════════════════════════════════════════════════════════════

import { useEffect, useRef, useState, useSyncExternalStore, type DependencyList } from "react";
import {
  getLiveConnState,
  getLiveStore,
  subscribeLiveStatus,
  watchList,
  watchStream,
  type LiveConnState,
} from "@/lib/liveClient";
import { frameClock } from "@/lib/live/frameClock";
import { tr } from "@/lib/i18n";
import type { StreamState } from "@/lib/live/store";

/** Connection state (open / reconnecting / failed / polling...). */
export function useLiveConnection(): LiveConnState {
  return useSyncExternalStore(subscribeLiveStatus, getLiveConnState, getLiveConnState);
}

function subscribeList(cb: () => void): () => void {
  return getLiveStore().onList(cb);
}

function listVersion(): number {
  return getLiveStore().getListVersion();
}

/**
 * All streams (re-renders when the list or metadata changes, not on single frames). Mounting it
 * keeps the connection open without subscribing to any stream's frames.
 */
export function useLiveStreams(): StreamState[] {
  useEffect(() => watchList(), []);
  const v = useSyncExternalStore(subscribeList, listVersion, listVersion);
  // v only triggers the re-render; states() returns a new array each time
  void v;
  return getLiveStore().states();
}

/** Wants this stream's frames (subscribed while the component is mounted). Returns the current state object (mutable; do not store it in state). */
export function useLiveStream(id: string | null): StreamState | undefined {
  useEffect(() => (id ? watchStream(id) : undefined), [id]);
  useLiveTick(id, 0); // re-render only when the stream is reset / replaced, see below
  return id ? getLiveStore().get(id) : undefined;
}

/**
 * Calls draw on the next frame when the stream changes (coalesced; not drawn in the background).
 * Also draws once on mount and when deps change. draw receives the latest state, or undefined
 * when the stream does not exist.
 */
export function useLiveDraw(
  id: string | null,
  draw: (st: StreamState | undefined) => void,
  deps: DependencyList,
): void {
  const drawRef = useRef(draw);
  drawRef.current = draw;
  const key = useRef({}).current;

  useEffect(() => {
    const run = () => drawRef.current(id ? getLiveStore().get(id) : undefined);
    frameClock.request(key, run);
    const off = getLiveStore().onChange((changed) => {
      if (changed === id) frameClock.request(key, run);
    });
    return () => {
      off();
      frameClock.cancel(key);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, key, ...deps]);
}

/**
 * Re-renders the component when the stream changes, at most hz times per second (hz = 0: only
 * when the stream object is replaced). Returns the stream's version, usable as a useMemo dependency.
 */
export function useLiveTick(id: string | null, hz = 4): number {
  const [version, setVersion] = useState(0);
  const last = useRef(0);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const stateRef = useRef<StreamState | undefined>(undefined);

  useEffect(() => {
    if (!id) return;
    const store = getLiveStore();
    stateRef.current = store.get(id);
    const flush = () => {
      timer.current = undefined;
      last.current = Date.now();
      setVersion(store.get(id)?.version ?? 0);
    };
    const off = store.onChange((changed) => {
      if (changed !== id) return;
      const st = store.get(id);
      const replaced = st !== stateRef.current;
      stateRef.current = st;
      if (hz <= 0) {
        if (replaced) flush();
        return;
      }
      if (timer.current !== undefined) return;
      const wait = Math.max(0, 1000 / hz - (Date.now() - last.current));
      timer.current = setTimeout(flush, wait);
    });
    // The stream object may only appear when the list arrives late: re-render once then too
    const offList = store.onList(() => {
      const st = store.get(id);
      if (st !== stateRef.current) {
        stateRef.current = st;
        flush();
      }
    });
    return () => {
      off();
      offList();
      if (timer.current !== undefined) clearTimeout(timer.current);
      timer.current = undefined;
    };
  }, [id, hz]);

  return version;
}

/** Re-renders every ms milliseconds (for text such as "x s ago"). */
export function useNow(ms = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(t);
  }, [ms]);
  return now;
}

/** "3 s ago", "2 min ago". */
export function fmtAgo(thenMs: number | null, nowMs: number): string {
  if (thenMs === null) return "—";
  const s = Math.max(0, (nowMs - thenMs) / 1000);
  if (s < 1) return tr("刚刚", "just now");
  if (s < 60) return tr(`${Math.floor(s)} s 前`, `${Math.floor(s)} s ago`);
  if (s < 3600) return tr(`${Math.floor(s / 60)} min 前`, `${Math.floor(s / 60)} min ago`);
  if (s < 86400) return tr(`${Math.floor(s / 3600)} h 前`, `${Math.floor(s / 3600)} h ago`);
  return tr(`${Math.floor(s / 86400)} 天前`, `${Math.floor(s / 86400)} d ago`);
}
