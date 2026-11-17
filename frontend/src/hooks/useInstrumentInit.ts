import { useQuery } from "@tanstack/react-query";
import { api } from "@/api/client";

/** New-instrument initialisation checklist. The banner (AppLayout) and the page (SetupPage)
 *  share one query, so completing an item updates both and the banner never keeps nagging after
 *  the page says it is done.
 *
 *  The backend never returns 500: an unreadable checklist degrades to degraded=true. Callers must
 *  not present an unreadable checklist as "nothing left to fill in".
 *
 *  `enabled: false` (the banner on a station without the STM safety parameters) asks for
 *  nothing: the checklist is made of STM quantities. */
export const INSTRUMENT_INIT_KEY = ["instrument-init"] as const;

export function useInstrumentInit({ enabled = true }: { enabled?: boolean } = {}) {
  return useQuery({
    queryKey: INSTRUMENT_INIT_KEY,
    enabled,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/instrument-init");
      if (error) throw error;
      return data;
    },
    // The checklist only changes when someone saves; no polling, but refetch on returning to the
    // page so the banner does not show a stale count after the profile was edited in Settings.
    staleTime: 15_000,
    refetchOnWindowFocus: true,
  });
}
