// An explicit `.ts` suffix is the convention in this directory: `node --test` runs
// the TS sources directly and the ESM resolver does not add the suffix for you
// (same for envHistory.ts / monitoring.ts / initFilter.ts).
import { LEGACY_REDIRECTS } from "./nav.ts";

// ════════════════════════════════════════════════════════════════════════════
// "Is the user standing on the setup page right now" — the banner uses this to
// decide whether to stay quiet.
//
// Why a separate pure function: the check depends on a **premise that is
// written in another file**.
//
// The setup page moved from `/setup` to `/settings/setup` and the old address
// became a redirect. So "checking `/settings/setup` is enough" holds, but only
// because **`/setup` exists solely as a redirect** — and that fact lives in
// router.tsx, not in the banner.
//
// Two things follow silently:
//
//   1. `<Navigate>` redirects in an effect, not during render. So arriving via
//      an old bookmark leaves **one frame** where the pathname is still `/setup`
//      while AppLayout (and the banner) is already mounted — the banner flashes,
//      pointing at the place the user is about to land.
//   2. More importantly: if `/setup` ever becomes a real route again, or the
//      redirect moves server-side, the banner would sit squarely **on top of the
//      setup page itself**, and whoever makes that change has no reason to look
//      at this file.
//
// So the aliases are not hard-coded; they are **derived** from
// `LEGACY_REDIRECTS` — that table is the source of truth for "which old
// addresses point here", and it only ever grows. Add another old address that
// points at the setup page and this follows automatically.
// ════════════════════════════════════════════════════════════════════════════

/** Canonical address of the setup page. It shares its source with the `SECTION_ELEMENTS`
 *  key in router.tsx and the section name in nav.ts; agreement between the three is pinned
 *  by `tests/v2/unit/api/test_instrument_init_frontend_parity.py`. */
export const SETUP_PATH = "/settings/setup";

/**
 * Every address that lands on the setup page: the canonical address plus all old
 * addresses pointing at it.
 *
 * Exported for tests — a list that should have two entries but has only one
 * behaves exactly like a correct one until someone actually uses the old
 * bookmark.
 */
export const SETUP_ALIASES: string[] = [
  SETUP_PATH,
  ...Object.entries(LEGACY_REDIRECTS)
    .filter(([, to]) => to === SETUP_PATH)
    .map(([from]) => from),
];

/**
 * True when this pathname is the setup page (or one of its old addresses).
 *
 * Prefix match rather than equality: if the setup page later grows sub-segments
 * (`/settings/setup/xxx`), the banner should still stay quiet. Something like
 * `/settings/setupfoo` does **not** count — the boundary must fall on a path
 * separator, otherwise a new page whose name merely shares the prefix would make
 * the banner mysteriously vanish there.
 */
export function isSetupPath(pathname: string): boolean {
  const p = (pathname || "").replace(/\/+$/, "") || "/";
  return SETUP_ALIASES.some(
    (a) => p === a || p.startsWith(a.endsWith("/") ? a : `${a}/`),
  );
}
