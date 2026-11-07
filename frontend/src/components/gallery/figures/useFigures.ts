// Server data for figure export (docs/v2/design/data_gallery.md section 10): the artifact list and the job-slot state.
//
// The status key does not start with ["gallery", "figures"]: invalidating the artifact list
// matches by prefix and must not also force the job status to refetch.

import { useQuery } from "@tanstack/react-query";
import { api } from "@/api/client";

export const FIGURE_KEYS = {
  list: ["gallery", "figures"] as const,
  status: ["gallery", "figure-status"] as const,
};

/** Figure artifact list (read-only). FigureJobChip invalidates it when a job finishes. */
export function useGalleryFigures() {
  return useQuery({
    queryKey: FIGURE_KEYS.list,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/gallery/figures");
      if (error) throw error;
      return data;
    },
    staleTime: 30_000,
  });
}

/** Figure job slot: poll every 1.5 s while running, otherwise every 15 s (a job may have been started from another tab). */
export function useFigureStatus() {
  return useQuery({
    queryKey: FIGURE_KEYS.status,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/gallery/figures/status");
      if (error) throw error;
      return data;
    },
    refetchInterval: (q) => (q.state.data?.running ? 1500 : 15000),
  });
}
