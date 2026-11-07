// Server data of the data gallery: index / build status / config (react-query) and the model
// derived from the index.
//
// The whole index is fetched at once (a few thousand entries, a few hundred KB gzipped); after
// that, filtering, directory summaries and neighbour-frame lookup all run in the browser, so
// changing a filter needs no network round trip. The index is invalidated and re-fetched only
// when a build completes (GalleryApp watches the running -> not-running edge of status);
// otherwise staleTime is infinite.

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/api/client";
import type { GalleryItem, GalleryRoot } from "@/lib/gallery/types";
import { num } from "@/lib/gallery/format";
import { framesTimeline } from "@/lib/gallery/context";

export const GALLERY_KEYS = {
  index: ["gallery", "index"] as const,
  status: ["gallery", "status"] as const,
  config: ["gallery", "config"] as const,
};

export function useGalleryIndex() {
  return useQuery({
    queryKey: GALLERY_KEYS.index,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/gallery/index");
      if (error) throw error;
      return data;
    },
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  });
}

export function useGalleryStatus() {
  return useQuery({
    queryKey: GALLERY_KEYS.status,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/gallery/status");
      if (error) throw error;
      return data;
    },
    // Poll every 1.5 s while a build runs, otherwise every 15 s (another tab or the CLI may be building).
    refetchInterval: (q) => (q.state.data?.running ? 1500 : 15000),
  });
}

export function useGalleryConfig() {
  return useQuery({
    queryKey: GALLERY_KEYS.config,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/gallery/config");
      if (error) throw error;
      return data;
    },
  });
}

export interface GalleryModel {
  /** All items ordered by (directory, start time, filename). */
  items: GalleryItem[];
  byId: Map<string, GalleryItem>;
  /** Frames used for neighbour lookup (have a save time, not duplicate saves), ascending by save time. */
  frames: GalleryItem[];
  lastBatch: string;
  generated: string;
  built: boolean;
  roots: GalleryRoot[];
  counts: { f: number; s: number; g: number; dup: number };
  numOf: (id: string) => string | null;
}

export function buildModel(data: {
  items?: GalleryItem[];
  last_batch?: string;
  generated?: string;
  built?: boolean;
  roots?: GalleryRoot[];
}): GalleryModel {
  const items = [...(data.items ?? [])].sort(
    (a, b) =>
      (a.d < b.d ? -1 : a.d > b.d ? 1 : 0) ||
      (a.t || a.mt || 0) - (b.t || b.mt || 0) ||
      (a.fn < b.fn ? -1 : a.fn > b.fn ? 1 : 0),
  );
  const byId = new Map(items.map((it) => [it.id, it]));
  const counts = { f: 0, s: 0, g: 0, dup: 0 };
  for (const it of items) {
    counts[it.k]++;
    if (it.dup) counts.dup++;
  }
  return {
    items,
    byId,
    frames: framesTimeline(items),
    lastBatch: data.last_batch ?? "",
    generated: data.generated ?? "",
    built: !!data.built,
    roots: data.roots ?? [],
    counts,
    numOf: (id) => {
      const it = byId.get(id);
      return it ? num(it) : null;
    },
  };
}

/** Index query + derived model (the model is recomputed only when the index data changes). */
export function useGalleryModel() {
  const q = useGalleryIndex();
  const model = useMemo(() => (q.data ? buildModel(q.data) : null), [q.data]);
  return { q, model };
}
