// /api/operator — the operator side of the structured human-in-the-loop primitive.
//
// Contract: docs/v2/design/optical_station_witec.md §6.2 / D8 / D9 (backend:
// MASTv2/mast/api/routes/operator.py). These endpoints are for the web page and the
// launcher popup only; an agent can post and wait for a prompt but never answers it, so
// they are deliberately not part of /api/ext/v1 or MCP.
//
// They are not in the generated `schema.d.ts` yet, so this is a small hand-typed fetch
// client (shapes and parsing live in lib/operatorPrompts.ts / lib/opticalState.ts, both
// unit-tested). Same origin, so the browser attaches the Basic / session credentials and
// an Origin equal to the Host, which is what the service's cross-site-write check wants.
// Bodies are always JSON: a body-less write would be a one-request CSRF door.

import { useMemo } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useWsEvent } from "@/hooks/useWsEvents";
import { api } from "./client";
import {
  errorDetail,
  openPromptFeed,
  parsePrompts,
  pollDelayMs,
  sortHistory,
  type AnswerBody,
  type DismissBody,
  type OpenPromptFeed,
} from "../lib/operatorPrompts.ts";
import {
  isOpticalProfile,
  parseHistory,
  parseOpticalState,
  type HistoryEntry,
  type OpticalStateSnapshot,
  type SlotPutBody,
} from "../lib/opticalState.ts";

/** How often the open prompts are asked for (the nav badge, the toast, the sound). */
export const OPERATOR_POLL_MS = 2_500;
const PROMPTS_ALL_POLL_MS = 10_000;
const OPTICAL_POLL_MS = 5_000;

export const PROMPTS_KEY = ["operator", "prompts"] as const;
export const OPTICAL_KEY = ["operator", "optical-state"] as const;

export class OperatorApiError extends Error {
  /** HTTP status; 0 when nothing answered */
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "OperatorApiError";
    this.status = status;
  }
}

async function call<T>(method: "GET" | "POST" | "PUT", path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const init: RequestInit = { method, headers, signal, credentials: "same-origin" };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let res: Response;
  try {
    res = await fetch(path, init);
  } catch (e) {
    if ((e as { name?: string } | null)?.name === "AbortError") throw e;
    throw new OperatorApiError(String((e as Error)?.message ?? e), 0);
  }
  const text = await res.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }
  if (!res.ok) throw new OperatorApiError(errorDetail(data) || res.statusText || `HTTP ${res.status}`, res.status);
  return data as T;
}

// ── prompts ─────────────────────────────────────────────────────────────────

interface PromptsQueryOpts {
  status: "open" | "all";
  limit?: number;
  /** keep asking (default); false = read the shared cache and fetch only when it is stale */
  poll?: boolean;
  enabled?: boolean;
}

/**
 * The gate every operator query goes through. Operator prompts and the optical state exist
 * on an optical station only: on an STM station, or while the service has not said what it
 * is, nothing under /api/operator is asked for, so an STM station's request traffic stays
 * what it was before this feature.
 */
function useOperatorQueryEnabled(enabled = true): boolean {
  const optical = useIsOpticalStation();
  return optical && enabled;
}

export function useOperatorPrompts({ status, limit = 100, poll = true, enabled = true }: PromptsQueryOpts) {
  const on = useOperatorQueryEnabled(enabled);
  return useQuery({
    queryKey: [...PROMPTS_KEY, status] as const,
    queryFn: async ({ signal }) =>
      parsePrompts(await call<unknown>("GET", `/api/operator/prompts?status=${status}&limit=${limit}`, undefined, signal)),
    enabled: on,
    refetchInterval: poll
      ? (q) => pollDelayMs(q.state.error, status === "open" ? OPERATOR_POLL_MS : PROMPTS_ALL_POLL_MS)
      : false,
    // The operator is looking at the vendor software, not at this tab: keep polling anyway.
    refetchIntervalInBackground: true,
    refetchOnWindowFocus: true,
    retry: false,
    staleTime: 1_000,
  });
}

/**
 * The open prompts. Call it with `poll: true` once (AppLayout owns the polling); other
 * readers pass `poll: false` and share the cache, so there is one request stream.
 *
 * Only an optical station has operator prompts. On any other profile, or while the service
 * has not said what it is, the query is off, the socket event is not listened to, and the
 * feed is `NO_PROMPT_FEED` (empty, `enabled: false`): no request, no badge, no toast, no sound.
 * The owner keeps asking what the station is until the service answers (see
 * `useIsOpticalStation`), so a page opened while MAST was still starting finds its prompts
 * once it is up.
 *
 * The service also announces every post / answer / close on /ws/events as a pointer-only
 * `operator_prompt` event; that triggers an immediate refetch. It matters most for a tab in
 * the background, where the browser throttles the poll timer (down to about once a minute
 * after a few minutes hidden) but still delivers a socket message at once. The poll stays
 * as the floor for when the socket is down.
 */
export function useOpenPromptFeed({ poll = true }: { poll?: boolean } = {}): OpenPromptFeed {
  const qc = useQueryClient();
  const optical = useIsOpticalStation({ keepAsking: poll });
  useWsEvent(
    "operator_prompt",
    (ev) => {
      const what = (ev.data as { what?: unknown } | null)?.what;
      void qc.invalidateQueries({ queryKey: what === "optical_state" ? OPTICAL_KEY : PROMPTS_KEY });
    },
    poll && optical,
  );
  const q = useOperatorPrompts({ status: "open", poll });
  const list = q.data?.prompts;
  const persistError = q.data?.persistError ?? null;
  const errorStatus = (q.error as { status?: number } | null)?.status;
  return useMemo(
    () => openPromptFeed({ optical, list, loaded: q.isSuccess, errorStatus, persistError }),
    [optical, list, q.isSuccess, errorStatus, persistError],
  );
}

/** Closed prompts, most recently handled first. */
export function usePromptHistory(enabled: boolean) {
  const optical = useIsOpticalStation();
  const q = useOperatorPrompts({ status: "all", limit: 100, enabled });
  const list = optical ? q.data?.prompts : undefined;
  const history = useMemo(() => sortHistory(list ?? []), [list]);
  return { history, isError: q.isError, error: q.error };
}

export function useAnswerPrompt() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: AnswerBody }) =>
      call<unknown>("POST", `/api/operator/prompts/${encodeURIComponent(id)}/answer`, body),
    // an attestation sets optical-state slots, and a 409 means the list is stale: refresh both either way
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: PROMPTS_KEY });
      void qc.invalidateQueries({ queryKey: OPTICAL_KEY });
    },
  });
}

export function useDismissPrompt() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: DismissBody }) =>
      call<unknown>("POST", `/api/operator/prompts/${encodeURIComponent(id)}/dismiss`, body),
    onSettled: () => void qc.invalidateQueries({ queryKey: PROMPTS_KEY }),
  });
}

// ── optical state ───────────────────────────────────────────────────────────

export function useOpticalState(enabled: boolean) {
  const on = useOperatorQueryEnabled(enabled);
  return useQuery({
    queryKey: OPTICAL_KEY,
    queryFn: async ({ signal }): Promise<OpticalStateSnapshot> =>
      parseOpticalState(await call<unknown>("GET", "/api/operator/optical-state", undefined, signal)),
    enabled: on,
    refetchInterval: on ? (q) => pollDelayMs(q.state.error, OPTICAL_POLL_MS) : false,
    retry: false,
    staleTime: 2_000,
  });
}

export function useOpticalHistory(limit: number, enabled: boolean) {
  const on = useOperatorQueryEnabled(enabled);
  return useQuery({
    queryKey: [...OPTICAL_KEY, "history", limit] as const,
    queryFn: async ({ signal }): Promise<HistoryEntry[]> =>
      parseHistory(await call<unknown>("GET", `/api/operator/optical-state/history?limit=${limit}`, undefined, signal)),
    enabled: on,
    refetchInterval: on ? (q) => pollDelayMs(q.state.error, PROMPTS_ALL_POLL_MS) : false,
    retry: false,
    staleTime: 5_000,
  });
}

export function useSetSlot() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ slot, body }: { slot: string; body: SlotPutBody }) =>
      call<unknown>("PUT", `/api/operator/optical-state/${encodeURIComponent(slot)}`, body),
    onSettled: () => void qc.invalidateQueries({ queryKey: OPTICAL_KEY }),
  });
}

export function useClearSlot() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ slot, body }: { slot: string; body: { by: string; source: "web" } }) =>
      call<unknown>("POST", `/api/operator/optical-state/${encodeURIComponent(slot)}/clear`, body),
    onSettled: () => void qc.invalidateQueries({ queryKey: OPTICAL_KEY }),
  });
}

// ── which kind of station this is ───────────────────────────────────────────

/** How often to ask again while the service has not said what it is. */
const PROFILE_RETRY_MS = 10_000;

/**
 * Whether the service runs as an optical station. Operator prompts, the optical-state
 * panel and the prompt board exist for that profile only, so an STM station's page and
 * request traffic stay exactly as they were. Reads `GET /api/health` `station_profile` (the
 * profile in force) through the `["health"]` query the Wishlist page, the top bar and the
 * dashboard already share, so it costs no extra request (`useStationProfile()` would also
 * fetch the settings). `isOpticalProfile` is the station profile helper's `normalizeProfile`,
 * so this and `useStationProfile().isOptical` can never disagree. Not known yet (no answer
 * so far, or no `station_profile` in it) reads as STM.
 *
 * `keepAsking`: until the service has answered once, ask again every 10 s (this page may
 * have been opened while MAST was still starting; nothing else refetches a failed health
 * query). An answer stands, since the profile only changes with a restart. Only the owner
 * of the prompt polling passes it, so there is one timer rather than one per reader.
 */
export function useIsOpticalStation({ keepAsking = false }: { keepAsking?: boolean } = {}): boolean {
  const q = useQuery({
    queryKey: ["health"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/health");
      if (error) throw error;
      return data;
    },
    staleTime: 5 * 60_000,
    refetchInterval: keepAsking ? (query) => (query.state.data === undefined ? PROFILE_RETRY_MS : false) : false,
  });
  return isOpticalProfile((q.data as { station_profile?: unknown } | undefined)?.station_profile);
}
