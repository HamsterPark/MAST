// ════════════════════════════════════════════════════════════════════════════
// stickyTab — the **pure** part of "after leaving and coming back, am I still on
// the same sub-page".
//
// The cause is the route element: it is rendered by AppLayout's <Outlet/>, and
// **each return to a section is a fresh mount**, so `useState(default)` resets
// the sub-page every time. ChatPage had a hand-written localStorage read/write
// for this. It was right — and six other pages had none.
//
// This file exists not to "save a few lines" but for the **validation on read**:
// a stored id can go stale (a sub-page is renamed, merged or deleted), and a page
// rendering with an unrecognised id gets either a blank area or a tab strip with
// no sub-page highlighted. That looks not like "stale configuration" but like
// "this page is broken". Copying it six times would leave six people to remember
// the validation.
//
// No React import ⇒ runs directly under node --test
// (`frontend/test/stickyTab.test.ts`).
// ════════════════════════════════════════════════════════════════════════════

/** Common prefix of the storage keys. Changing it invalidates everyone's remembered tabs at once — which is also the only migration path. */
export const STICKY_TAB_PREFIX = "mast.subtab.";

/** `page` → the full localStorage key. */
export function stickyTabKey(page: string): string {
  return `${STICKY_TAB_PREFIX}${page}`;
}

/**
 * Whether a value read back is usable.
 *
 * **An unrecognised value falls back to the default** rather than being returned
 * as is. Stored ids go stale: sub-pages get renamed, merged into other sections,
 * or deleted outright. A page rendering with an unrecognised id gets a blank area
 * or a tab strip with no segment highlighted — and what the user sees is not "my
 * preference expired" but "this page is broken", which is also what would get
 * reported.
 *
 * A `fallback` that is not in `valid` is still returned unchanged: the default is
 * the caller's own business, and silently altering it here would turn "the
 * default is wrong" into a problem that is hard to trace.
 */
export function pickValidTab<T extends string>(
  stored: string | null | undefined,
  valid: readonly T[],
  fallback: T,
): T {
  if (typeof stored !== "string" || !stored) return fallback;
  return (valid as readonly string[]).includes(stored) ? (stored as T) : fallback;
}

/**
 * Read a remembered sub-page. When storage is unavailable (private mode,
 * disabled) it **does not throw** and falls back to the default.
 *
 * A browser that cannot remember preferences must still be able to use the
 * software.
 */
export function readStickyTab<T extends string>(
  page: string,
  valid: readonly T[],
  fallback: T,
): T {
  try {
    return pickValidTab(localStorage.getItem(stickyTabKey(page)), valid, fallback);
  } catch {
    return fallback;
  }
}

/** Remember a sub-page. Failure is ignored — the selection still works for this session. */
export function writeStickyTab(page: string, id: string): void {
  try {
    localStorage.setItem(stickyTabKey(page), id);
  } catch {
    /* private mode / storage disabled — the selection still works this session */
  }
}
