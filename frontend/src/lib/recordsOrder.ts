// Display order of experiment records. Kept pure so node --test can pin it.

function when(item: { timestamp?: string | null }): number {
  const t = item.timestamp ? Date.parse(item.timestamp) : NaN;
  return Number.isNaN(t) ? -Infinity : t;
}

/**
 * Latest record first; entries with no readable time go last. Same-time entries keep
 * "later in the input = newer" (the backend returns them oldest first, so a burst of
 * actions within one timestamp stays in reverse execution order). Input is not modified.
 */
export function newestFirst<T extends { timestamp?: string | null }>(items: readonly T[]): T[] {
  return [...items].reverse().sort((a, b) => {
    const [ta, tb] = [when(a), when(b)];
    return ta === tb ? 0 : tb > ta ? 1 : -1;
  });
}
