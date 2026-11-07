import { useQuery } from "@tanstack/react-query";
import { api } from "@/api/client";
import { tr } from "../../lib/i18n";

// Shared query for GET /api/scans/preview.
//
// One definition rather than one per component: the grid card, the preview panel
// and the lightbox all ask the same endpoint at different sizes, and separate
// copies of a query key are how one of them ends up not sharing the cache.

/** Background-subtraction mode. `auto` measures the frame and is slow — grids must not send it. */
export type FlattenMode = "raw" | "plane" | "line" | "auto";

/** Modes offered in the grid. `auto` is deliberately absent: see useScanPreview. */
export const GRID_FLATTEN_MODES: FlattenMode[] = ["raw", "plane", "line"];
export const ALL_FLATTEN_MODES: FlattenMode[] = ["raw", "plane", "line", "auto"];

export const FLATTEN_LABEL: Record<string, string> = {
  raw: tr("原始", "Raw"),
  plane: tr("扣平面", "Plane"),
  line: tr("逐行平场", "Line-by-line"),
  auto: tr("自动", "Auto"),
  // What `auto` can resolve to — shown after the fact, so these need names too.
  poly2: tr("扣二阶曲面", "2nd-order surface"),
  masked_line: tr("主台面逐行", "Main-terrace line-by-line"),
};

export function flattenLabel(mode: string | null | undefined): string {
  if (!mode) return "";
  return FLATTEN_LABEL[mode] ?? mode;
}

/**
 * One file's rendered preview.
 *
 * `auto` costs ~3 s the first time a file is measured (the server caches the
 * verdict afterwards), so a grid of thumbnails must never request it: thirty
 * cards would queue thirty measurements. The grid passes one of
 * GRID_FLATTEN_MODES; only the single-file panel offers `auto`.
 */
export function useScanPreview(
  path: string | null,
  opts: { size?: number; flatten?: FlattenMode; channel?: string | null } = {},
) {
  const { size = 256, flatten, channel } = opts;
  return useQuery({
    enabled: !!path,
    queryKey: ["scans", "preview", path, size, flatten ?? "", channel ?? ""],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/scans/preview", {
        params: {
          query: {
            path: path as string,
            size,
            ...(flatten ? { flatten } : {}),
            ...(channel ? { channel } : {}),
          },
        },
      });
      if (error) throw error;
      return data;
    },
    // A rendered scan file does not change: the server keys its cache on the
    // file's mtime, so re-asking for the same picture is pure cost. Keeping it
    // fresh for five minutes is what stops a grid re-fetching every thumbnail
    // each time the operator switches tabs and back.
    staleTime: 5 * 60_000,
    // Keep the previous picture on screen while a new flatten mode renders,
    // instead of blanking every card mid-toggle.
    placeholderData: (prev) => prev,
  });
}
