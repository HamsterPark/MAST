// React bindings for the manual-control panel: the unlock state (the one in lib/manual/unlock.ts),
// the shared context of local readouts, whether a card is on screen, and each card's readout.
//
// Readouts (contract §13.1): satellite devices use the LiveHub stream named in the description;
// local devices are polled by the manual-control section through the batch read-only endpoint
// GET /api/manual/readouts (one request covering all on-screen local cards, not through the
// skill-execution entry and not recorded) and handed to the cards through the context. Devices
// with readout = null (Nanonis optics groups) have no position feedback.

import { createContext, useCallback, useContext, useRef, useSyncExternalStore } from "react";
import { useLiveStream, useLiveTick, useNow } from "@/hooks/useLive";
import { tr } from "@/lib/i18n";
import { seriesLatest } from "@/lib/live/views";
import { matchAxisValues, type LocalReadout, type ManualDevice } from "@/lib/manual/protocol";
import { unlockStore } from "@/lib/manual/unlock";

export function useUnlock(id: string) {
  useSyncExternalStore(
    (cb) => unlockStore.subscribe(cb),
    () => unlockStore.getVersion(),
    () => unlockStore.getVersion(),
  );
  const unlocked = unlockStore.isUnlocked(id);
  // While unlocked, re-render every second: the countdown must advance and expiry must show as locked
  useNow(unlocked ? 1000 : 3_600_000);
  return {
    unlocked: unlockStore.isUnlocked(id),
    remainingMs: unlockStore.remainingMs(id),
    unlock: () => unlockStore.unlock(id),
    lock: () => unlockStore.lock(id),
    touch: () => unlockStore.touch(id),
  };
}

export interface ManualReadoutsValue {
  readouts: Record<string, LocalReadout>;
  /** false = the backend has no /api/manual/readouts yet (404). */
  deployed: boolean;
  /** Verbatim error of a whole-batch failure (a single device failing is in its own error). */
  error: string | null;
  setVisible: (id: string, visible: boolean) => void;
}

export const ManualReadoutsContext = createContext<ManualReadoutsValue>({
  readouts: {},
  deployed: true,
  error: null,
  setVisible: () => undefined,
});

/**
 * Whether a card is on screen (200 px margin above and below): only on-screen local cards are
 * read. Returns a ref for the card's root node. Environments without IntersectionObserver count
 * every card as on screen.
 */
export function useCardVisibility(id: string, enabled: boolean) {
  const { setVisible } = useContext(ManualReadoutsContext);
  const obs = useRef<IntersectionObserver | null>(null);
  return useCallback(
    (el: HTMLElement | null) => {
      obs.current?.disconnect();
      obs.current = null;
      if (!el || !enabled) {
        setVisible(id, false);
        return;
      }
      if (typeof IntersectionObserver === "undefined") {
        setVisible(id, true);
        return;
      }
      const o = new IntersectionObserver((entries) => setVisible(id, entries.some((e) => e.isIntersecting)), {
        rootMargin: "200px 0px",
      });
      o.observe(el);
      obs.current = o;
    },
    [id, enabled, setVisible],
  );
}

export interface DeviceReadout {
  byAxis: Record<string, number | null>;
  moving: Record<string, boolean | null>;
  homed: Record<string, boolean | null>;
  /** Per-axis extras from the readout endpoint (connected, servo, speed, status bits, step odometer...). */
  extra: Record<string, Record<string, unknown>>;
  /** Readings that match no axis (still shown, not dropped). */
  rest: Record<string, number | null>;
  /** live = real-time stream; poll = batch read-only endpoint (local stage); cache = hub cache (a satellite device's stream has not arrived yet). */
  source: "live" | "poll" | "cache" | "descriptor" | null;
  /** Verbatim read error. */
  error: string | null;
}

export function useDeviceReadout(d: ManualDevice): DeviceReadout {
  const ro = d.readout;
  const liveId = ro?.kind === "live" ? ro.stream : null;
  const st = useLiveStream(liveId);
  useLiveTick(liveId, 4);
  const ctx = useContext(ManualReadoutsContext);

  const fromDesc: Record<string, number | null> = {};
  const movingDesc: Record<string, boolean | null> = {};
  const homedDesc: Record<string, boolean | null> = {};
  // Per-axis extras from the description (the optics groups' odometer_steps live here, refreshed with the device list every 5 s; the readout endpoint wins where it has a value)
  const extraDesc: Record<string, Record<string, unknown>> = {};
  for (const a of d.axes) {
    if (a.position !== null) fromDesc[a.name] = a.position;
    movingDesc[a.name] = a.moving;
    homedDesc[a.name] = a.homed;
    if (Object.keys(a.extra).length) extraDesc[a.name] = a.extra;
  }
  const base = { byAxis: fromDesc, moving: movingDesc, homed: homedDesc, extra: extraDesc, rest: {} };

  // This device's entry from the batch read-only endpoint: local stages' readings; for satellite devices it is the hub cache (fallback when the link drops, and first-paint fill)
  const lr = ctx.readouts[d.id];

  if (liveId && st && (st.buf.kind === "scalar" || st.buf.kind === "xy") && st.received > 0) {
    const m = matchAxisValues(d.axes, seriesLatest(st.buf).values);
    // The live stream is running; if the fallback readout says the link is down ("satellite X lost: no new readings"), surface that too - the values above are the last ones received
    return { ...base, byAxis: { ...fromDesc, ...m.byAxis }, rest: m.rest, source: "live", error: lr?.error ?? null };
  }
  if (lr && !lr.error) {
    const values: Record<string, number | null> = {};
    const moving: Record<string, boolean | null> = { ...movingDesc };
    const homed: Record<string, boolean | null> = { ...homedDesc };
    const extra: Record<string, Record<string, unknown>> = { ...extraDesc };
    for (const a of lr.axes) {
      values[a.name] = a.position;
      if (a.moving !== null) moving[a.name] = a.moving;
      if (a.homed !== null) homed[a.name] = a.homed;
      extra[a.name] = { ...extraDesc[a.name], ...a.extra };
    }
    const m = matchAxisValues(d.axes, values);
    return { byAxis: { ...fromDesc, ...m.byAxis }, moving, homed, extra, rest: m.rest, source: liveId ? "cache" : "poll", error: null };
  }
  if (ro?.kind === "poll") {
    const error = lr?.error ?? (!ctx.deployed ? tr("后端还没有读数端点（GET /api/manual/readouts 返回 404）", "The backend has no readout endpoint yet (GET /api/manual/readouts returned 404)") : ctx.error);
    return { ...base, source: Object.keys(fromDesc).length ? "descriptor" : null, error };
  }
  return { ...base, source: Object.keys(fromDesc).length ? "descriptor" : null, error: lr?.error ?? null };
}
