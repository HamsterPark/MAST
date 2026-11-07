// ════════════════════════════════════════════════════════════════════════════
// useStickyTab: remembers which sub-page a section was last on.
//
// The pure logic is in `lib/stickyTab.ts` (validation on read is the key part); only the React
// binding lives here.
// ════════════════════════════════════════════════════════════════════════════

import { useCallback, useState } from "react";
import { readStickyTab, writeStickyTab } from "@/lib/stickyTab";

/**
 * A stand-in for `useState` whose value survives navigation.
 *
 * @param page   Storage key (`mast.subtab.<page>`). Nested sub-pages use dotted levels, e.g.
 *               `"agents"` and `"agents.chat.mode"`, so a key looks like its place in the UI and
 *               is recognisable in devtools.
 * @param valid  Every id this section **currently** knows. It is the tab list itself; do not keep
 *               a second copy, which would drift from the tab list and show up as "some sub-page
 *               is not remembered", a symptom nobody checks storage keys for.
 * @param fallback Used when nothing is remembered or the remembered id is stale.
 *
 * The initial value uses lazy `useState(fn)`: reading localStorage is synchronous I/O, and
 * `useState(readStickyTab(...))` would read it on **every render**.
 */
export function useStickyTab<T extends string>(
  page: string,
  valid: readonly T[],
  fallback: T,
): [T, (id: T) => void] {
  const [tab, setTabState] = useState<T>(() => readStickyTab(page, valid, fallback));
  const setTab = useCallback(
    (id: T) => {
      setTabState(id);
      writeStickyTab(page, id);
    },
    [page],
  );
  return [tab, setTab];
}
