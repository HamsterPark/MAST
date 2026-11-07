import { useQuery } from "@tanstack/react-query";
import { api } from "@/api/client";

// Data for the coarse-motion overview map. Fetched on demand and kept out of the 3 s poll: it
// only changes around a coarse move, and each fetch reads the marker table and derives the
// stations, so polling it would pay a 3 s cost for a value that changes every few minutes.
//
// Shared hook: it used to live only in VisionPage, so the other place that shows the scan map
// (chat page -> vision buffer) had no coarse map. Two copies would eventually diverge in interval
// and endpoint, and when one queryKey has two configurations only one applies, depending on
// which component mounts first.
export function useCoarseMap() {
  return useQuery({
    queryKey: ["coarse-map"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/coarse-map");
      if (error) throw error;
      return data;
    },
    refetchInterval: 30000,
  });
}
