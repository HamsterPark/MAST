// The list of layouts on the server: which one opens first, how an entry reads in the
// picker. Pure (test/opticalLayoutLabels.test.ts).

import { tr } from "../i18n.ts";
import type { LayoutSummary } from "./types.ts";

/** The layout to open when the address names none: the most recently updated, the first on a tie. */
export function pickDefaultLayout(layouts: readonly LayoutSummary[]): string | null {
  let best: LayoutSummary | null = null;
  let bestAt = -Infinity;
  for (const l of layouts) {
    if (l.error) continue;
    const t = Date.parse(l.updated_at);
    const at = Number.isFinite(t) ? t : -Infinity;
    if (best === null || at > bestAt) {
      best = l;
      bestAt = at;
    }
  }
  return best?.id ?? null;
}

/** "SHG excitation path · r3 · 2 open comments" */
export function layoutOptionLabel(l: LayoutSummary): string {
  if (l.error) return tr(`${l.id}（读不出来）`, `${l.id} (cannot be read)`);
  const bits = [l.title || l.id, `r${l.revision}`];
  if (l.open_comments > 0) bits.push(tr(`${l.open_comments} 条未解决评论`, `${l.open_comments} open comments`));
  return bits.join(" · ");
}
