// react-query hooks over the optical layout client (api/opticalLayout.ts) and the two
// existing lists the link pickers use: the optical-bench devices (GET /api/optics/devices)
// and the manual optical-state slots (GET /api/operator/optical-state).

import { useMemo } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import { useOpticalState } from "./operator";
import { createOpticalLayoutApi, type ArchiveBody, type CommentBody, type PutBody, type ResolveBody } from "./opticalLayout";
import type { DeviceOption } from "@/components/opticallayout/LinkFields";

export const layoutApi = createOpticalLayoutApi();

export const LAYOUTS_KEY = ["optical-layouts"] as const;
const LIST_POLL_MS = 20_000;
const COMMENTS_POLL_MS = 10_000;

/** The layouts on the server, with their open-comment counts (agents comment at any time, so it is polled). */
export function useLayoutList() {
  return useQuery({
    queryKey: LAYOUTS_KEY,
    queryFn: ({ signal }) => layoutApi.list(signal),
    refetchInterval: (q) => (q.state.error ? 60_000 : LIST_POLL_MS),
    retry: false,
    staleTime: 5_000,
  });
}

/**
 * One layout, loaded once. The editor owns the state afterwards, so nothing refetches it
 * behind the operator's back (a refetch on focus would overwrite edits in progress);
 * "load the newest" calls `refetch` on purpose.
 */
export function useLayoutDocument(id: string | null) {
  return useQuery({
    queryKey: [...LAYOUTS_KEY, "doc", id] as const,
    queryFn: ({ signal }) => layoutApi.get(id as string, signal),
    enabled: id !== null,
    staleTime: Infinity,
    gcTime: 0,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    retry: false,
  });
}

export function useSaveLayout() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: PutBody }) => layoutApi.put(id, body),
    onSuccess: (_doc, { id }) => {
      void qc.invalidateQueries({ queryKey: LAYOUTS_KEY, exact: true });
      void qc.invalidateQueries({ queryKey: [...LAYOUTS_KEY, "history", id] });
    },
  });
}

export function useArchiveLayout() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: ArchiveBody }) => layoutApi.archive(id, body),
    onSuccess: () => void qc.invalidateQueries({ queryKey: LAYOUTS_KEY }),
  });
}

export function useLayoutHistory(id: string | null, enabled: boolean) {
  return useQuery({
    queryKey: [...LAYOUTS_KEY, "history", id] as const,
    queryFn: ({ signal }) => layoutApi.history(id as string, 100, signal),
    enabled: enabled && id !== null,
    staleTime: 5_000,
    retry: false,
  });
}

export function useRevisionLayout(id: string | null, revision: number | null) {
  return useQuery({
    queryKey: [...LAYOUTS_KEY, "revision", id, revision] as const,
    queryFn: ({ signal }) => layoutApi.revision(id as string, revision as number, signal),
    enabled: id !== null && revision !== null,
    staleTime: Infinity,
    retry: false,
  });
}

export function useLayoutComments(id: string | null, enabled = true) {
  return useQuery({
    queryKey: [...LAYOUTS_KEY, "comments", id] as const,
    queryFn: ({ signal }) => layoutApi.comments(id as string, signal),
    enabled: enabled && id !== null,
    refetchInterval: (q) => (q.state.error ? 60_000 : COMMENTS_POLL_MS),
    retry: false,
    staleTime: 3_000,
  });
}

export function useAddComment(id: string | null) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: CommentBody) => layoutApi.addComment(id as string, body),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: [...LAYOUTS_KEY, "comments", id] });
      void qc.invalidateQueries({ queryKey: LAYOUTS_KEY, exact: true });
    },
  });
}

export function useResolveComment(id: string | null) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ commentId, body }: { commentId: string; body: ResolveBody }) => layoutApi.resolveComment(id as string, commentId, body),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: [...LAYOUTS_KEY, "comments", id] });
      void qc.invalidateQueries({ queryKey: LAYOUTS_KEY, exact: true });
    },
  });
}

/** The optical-bench devices MAST knows, for the device picker. Reads the config; opens nothing. */
export function useDeviceOptions(enabled: boolean) {
  const q = useQuery({
    queryKey: ["optics", "devices"] as const,
    queryFn: async (): Promise<DeviceOption[]> => {
      const { data, error } = await api.GET("/api/optics/devices");
      if (error) throw error;
      return (data?.devices ?? []).map((d) => ({
        id: d.id,
        name: d.name,
        kind: d.kind,
        enabled: d.enabled,
        connected: d.connected,
      }));
    },
    enabled,
    staleTime: 30_000,
    retry: false,
  });
  return q;
}

/** The manual optical-state slots and their values, for the slot picker. */
export function useSlotOptions(enabled: boolean) {
  const q = useOpticalState(enabled);
  return useMemo(
    () => ({
      slots: q.data?.slots ?? null,
      values: q.data?.values ?? null,
      error: q.isError ? String((q.error as Error | null)?.message ?? q.error) : null,
    }),
    [q.data, q.isError, q.error],
  );
}
