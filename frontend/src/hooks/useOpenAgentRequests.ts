import { useQuery } from "@tanstack/react-query";
import { api } from "@/api/client";

/**
 * Number of unanswered agent-to-operator requests on the wishlist board.
 *
 * The board is the better of the two ask-the-operator channels (an answer is injected into the
 * agent's next turn automatically, no polling) but had no visibility: nothing told the operator a
 * request had arrived, so an agent could ask for a sample change and wait indefinitely. The
 * literature fetch board got a badge for the same reason; this is the same fix.
 *
 * Reads `pending_count` off the response rather than filtering rows client-side. A client-side
 * filter on a status string the backend never emits counts zero forever and the badge never shows.
 *
 * Fails silently to 0: a badge is an affordance, never a reason for the shell to show an error
 * or stall.
 */
const POLL_MS = 60_000;

export function useOpenAgentRequests(): number {
  const q = useQuery({
    queryKey: ["wishlist", "open-request-count"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/wishlist");
      if (error) throw error;
      return data;
    },
    refetchInterval: POLL_MS,
    refetchOnWindowFocus: true,
    retry: false,
    staleTime: POLL_MS / 2,
  });

  const d = q.data as { pending_count?: number | null; degraded?: boolean } | undefined;
  if (!d || d.degraded) return 0;
  const n = Number(d.pending_count ?? 0);
  return Number.isFinite(n) && n > 0 ? n : 0;
}
