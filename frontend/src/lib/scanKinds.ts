// Scan file-type filter — turning a chip into the `ext` the server understands.
//
// This lives apart from the component because the failure mode is silent. Get it
// wrong and the Data tab shows a plausible-looking screen of the wrong files, or
// an empty one while the disk is full. A wrong list looks exactly like a right one.
//
// The rule the server holds up its end of: `ext` is applied BEFORE the newest-n
// slice, and `counts_by_ext` describes the whole discovery result rather than
// the returned page. Everything here assumes both.

import { tr } from "./i18n.ts";

/** Extensions that get their own chip, in the order an operator looks for them. */
export const KIND_EXTS = [".sxm", ".dat", ".3ds", ".sm4"] as const;

/** An `ext` value that matches nothing. Used when "Other" has no members. */
export const EXT_MATCH_NONE = ".__none__";

export function isKnownKind(ext: string): boolean {
  return (KIND_EXTS as readonly string[]).includes(ext);
}

/**
 * Chip id → the `ext` query value, or undefined for "no filter".
 *
 * `other` expands to whatever the server reported that is NOT one of the named
 * kinds, rather than a hardcoded complement — a second copy of the backend's
 * `_SCAN_EXTS` would drift the first time someone adds a recognised extension,
 * and the symptom would be files quietly missing from the "Other" chip.
 */
export function extParam(kind: string, countKeys: readonly string[]): string | undefined {
  if (kind === "all") return undefined;
  if (kind !== "other") return kind;
  const others = countKeys.filter((e) => !isKnownKind(e));
  // Asking for a filter that matches nothing is deliberate: dropping the filter
  // would show EVERY file under the "Other" chip, which would answer a question nobody
  // asked.
  return others.length ? others.join(",") : EXT_MATCH_NONE;
}

/** Files under "Other", summed from the server's full-result counts. */
export function otherCount(counts: Record<string, number>): number {
  return Object.entries(counts)
    .filter(([e]) => !isKnownKind(e))
    .reduce((a, [, n]) => a + n, 0);
}

// ── grouping (current-monitor CSVs must not crowd the .sxm scans off the screen) ──
//
// The exclusion list in scan_preview._NON_SCAN_DIR_NAMES is the fix at the
// source, but it is a BLOCKLIST: a new telemetry writer can appear in a directory
// nobody has listed yet (e.g. `env/`, then `env/signals/`). A blocklist only ever
// knows about the writers already encountered.
//
// Grouping is the part that does not need updating: a mtime-desc list lets any
// permanently-fresh file family win the whole screen; grouped by KIND it cannot —
// the scans keep their own heading no matter how many CSVs are newer. So the
// two fixes are doing different jobs, and the grouping is the one that holds
// when the next writer shows up.

export type ScanGroupId = "image" | "spectrum" | "other";

export interface ScanGroup {
  id: ScanGroupId;
  label: string;
  /** Why this group exists, for the heading's tooltip. */
  hint: string;
}

/** Render order: what the operator opens the Data tab to look at comes first. */
export const SCAN_GROUPS: readonly ScanGroup[] = [
  { id: "image", label: tr("扫描图像", "Scan images"), hint: tr("形貌图 —— .sxm / .sm4", "Topography images — .sxm / .sm4") },
  { id: "spectrum", label: tr("谱与曲线", "Spectra and curves"), hint: tr("点谱与谱图 —— .dat / .3ds", "Point spectra and spectrograms — .dat / .3ds") },
  {
    id: "other",
    label: tr("其他数据", "Other data"),
    hint: tr("监控与导出的数值文本 —— .csv / .txt / .asc / .tsv", "Monitoring and exported numeric text — .csv / .txt / .asc / .tsv"),
  },
];

const IMAGE_EXTS = new Set([".sxm", ".sm4"]);
const SPECTRUM_EXTS = new Set([".dat", ".3ds"]);

export function groupOf(ext: string): ScanGroupId {
  const e = String(ext || "").toLowerCase();
  if (IMAGE_EXTS.has(e)) return "image";
  if (SPECTRUM_EXTS.has(e)) return "spectrum";
  return "other";
}

/**
 * Split a page of files into groups, preserving the server's mtime order
 * inside each one.
 *
 * A REORDERING, never a filter: every input file appears in exactly one output
 * group. Dropping one here would give a list that looks right while a
 * file the operator can see on disk is missing from it.
 */
export function groupScans<T extends { ext: string }>(
  files: readonly T[],
): { group: ScanGroup; files: T[] }[] {
  const byId = new Map<ScanGroupId, T[]>();
  for (const f of files) {
    const id = groupOf(f.ext);
    const bucket = byId.get(id);
    if (bucket) bucket.push(f);
    else byId.set(id, [f]);
  }
  return SCAN_GROUPS.filter((g) => byId.get(g.id)?.length).map((g) => ({
    group: g,
    files: byId.get(g.id) as T[],
  }));
}
