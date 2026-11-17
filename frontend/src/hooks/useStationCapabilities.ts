import { useMemo, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/api/client";
import {
  capabilitiesFrom,
  readSerialAutodetect,
  type Capability,
  type SerialAutodetect,
  type StationCapabilities,
} from "@/lib/stationCapabilities";

// What this station has (lib/stationCapabilities.ts), from GET /api/health.
//
// Same ["health"] query as the top bar, the right panel, the dashboard and the station
// profile hook, so reading it here costs no request of its own.
//
// Two answers, used for two different things:
//
//   * `has(cap)`: the capability as far as is known. Until /api/health has answered it is
//     the STM station's set (the default profile), so an STM station draws exactly what it
//     always drew from the first frame on. Use it for what is drawn.
//   * `ready`: /api/health has answered, or has failed. Anything that SENDS a request for a
//     capability waits for it: `enabled: ready && has("nanonis")`. Otherwise an optical or
//     a generic station would fire its first Nanonis poll in the instant before the service
//     said what it is. A failed health check makes `ready` true with the STM set, so an STM
//     station whose health endpoint is broken still polls as before. The first failure is
//     enough: waiting out react-query's retries (about 7 s) would hold an STM station's
//     readouts back for nothing.
//
// `keepAsking`: until the service has answered once, ask again every 10 s (a page opened
// while MAST was still starting). Only one reader passes it (the operator prompt feed owner
// in AppLayout), so there is one timer rather than one per reader.

const HEALTH_KEY = ["health"] as const;
const PROFILE_RETRY_MS = 10_000;

export interface StationCapabilityState extends StationCapabilities {
  /** /api/health has answered, or has failed at least once. */
  ready: boolean;
  /** Serial auto-detection on this station (`station_serial_autodetect`); null when the
   *  service does not say (an older backend, or not answered yet). */
  serialAutodetect: SerialAutodetect | null;
}

export function useStationCapabilities({ keepAsking = false }: { keepAsking?: boolean } = {}): StationCapabilityState {
  const q = useQuery({
    queryKey: HEALTH_KEY,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/health");
      if (error) throw error;
      return data;
    },
    staleTime: 5 * 60_000,
    refetchInterval: keepAsking ? (query) => (query.state.data === undefined ? PROFILE_RETRY_MS : false) : false,
  });
  // A latch: once ready, always ready. A health query that has never succeeded goes back to
  // "pending" (error cleared, failureCount 0) each time `keepAsking` refetches it, and without
  // the latch `ready` dropped for a moment every 10 s: the readings polls and the push handler
  // switched off and on, and the poll's timer restarted before it could ever fire.
  const seen = useRef(false);
  if (q.data !== undefined || q.isError || q.failureCount > 0) seen.current = true;
  const ready = seen.current;
  return useMemo(
    () => ({ ...capabilitiesFrom(q.data), ready, serialAutodetect: readSerialAutodetect(q.data) }),
    [q.data, ready],
  );
}

/** The station has `cap` and has said so: the gate for anything that sends a request. */
export function useHasCapability(cap: Capability, opts?: { keepAsking?: boolean }): boolean {
  const s = useStationCapabilities(opts);
  return s.ready && s.has(cap);
}
