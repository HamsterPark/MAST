// Stream list: every stream declared in the LiveHub gets the default renderer for
// its kind automatically.
//
// Every new data acquisition must be plottable live on the page. This list is the
// guarantee: nobody needs to remember to write a page for a new acquisition; as
// soon as it declares a stream, it appears here.
//
// Running streams are expanded by default (expanded = subscribed to its frames);
// ended streams, or ones already drawn elsewhere on the page, are collapsed.
// Collapsed streams are not subscribed, so dozens of unwatched streams cannot
// saturate the Tailscale tunnel.

import { useMemo, useState } from "react";
import clsx from "clsx";
import { Card, EmptyNote } from "@/components/ui";
import { useLiveStreams } from "@/hooks/useLive";
import { byLiveness } from "@/lib/live/pick";
import type { StreamState } from "@/lib/live/store";
import { LiveStreamBody, LiveStreamHeader, kindLabel } from "./LiveStreamView";
import { LiveConnectionBadge } from "./LiveConnectionBadge";
import { ToggleChip } from "./controls";
import { tr } from "@/lib/i18n";

/** Maximum number expanded by default (the rest are collapsed; one click opens them). */
const AUTO_EXPAND = 6;

export function LiveStreamList({
  /** Streams already drawn elsewhere on the page: still listed, but collapsed by default. */
  shownElsewhere = [],
  /** List only streams whose id satisfies the predicate (the optics page shows only its own). */
  filter,
  emptyLabel = tr("LiveHub 里还没有任何流。任何采集一开始声明流，它就会出现在这里。", "No streams in the LiveHub yet. Any acquisition appears here as soon as it declares a stream."),
}: {
  shownElsewhere?: string[];
  filter?: (id: string, st: StreamState) => boolean;
  emptyLabel?: string;
}) {
  const all = useLiveStreams();
  const [q, setQ] = useState("");
  const [activeOnly, setActiveOnly] = useState(false);
  const [open, setOpen] = useState<Record<string, boolean>>({});

  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return [...all].sort(byLiveness).filter((s) => {
      if (filter && !filter(s.meta.id, s)) return false;
      if (activeOnly && (s.meta.ended || s.gone)) return false;
      if (!needle) return true;
      return [s.meta.id, s.meta.title, s.meta.source, s.meta.kind].some((v) => v.toLowerCase().includes(needle));
    });
  }, [all, q, activeOnly, filter]);

  const elsewhere = new Set(shownElsewhere);
  let auto = 0;
  const isOpen = (s: StreamState): boolean => {
    const explicit = open[s.meta.id];
    if (explicit !== undefined) return explicit;
    if (s.meta.ended || s.gone || elsewhere.has(s.meta.id)) return false;
    auto += 1;
    return auto <= AUTO_EXPAND;
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder={tr("按 id / 标题 / 来源筛选", "Filter by id / title / source")}
          className="w-64 rounded-mast-ctl border border-mast-border-strong bg-mast-panel px-2.5 py-1.5 text-sm text-mast-text outline-none focus:border-mast-accent"
        />
        <ToggleChip on={activeOnly} onClick={() => setActiveOnly((v) => !v)}>
          {tr("只看进行中", "Running only")}
        </ToggleChip>
        <span className="text-xs text-mast-faint">{tr(`${rows.length} 个流`, `${rows.length} stream(s)`)}</span>
        <span className="ml-auto">
          <LiveConnectionBadge />
        </span>
      </div>
      {!rows.length ? (
        <EmptyNote label={all.length ? tr("没有符合筛选的流", "No streams match the filter") : emptyLabel} />
      ) : (
        rows.map((s) => {
          const expanded = isOpen(s);
          return (
            <Card key={s.meta.id} className={clsx(!expanded && "py-3")}>
              <LiveStreamHeader
                st={s}
                right={
                  <button
                    type="button"
                    onClick={() => setOpen((o) => ({ ...o, [s.meta.id]: !expanded }))}
                    className="shrink-0 rounded-mast-ctl border border-mast-border px-2.5 py-1 text-xs text-mast-muted hover:text-mast-text"
                    aria-expanded={expanded}
                  >
                    {expanded ? tr("收起", "Collapse") : tr(`展开${kindLabel(s.meta.kind)}`, `Expand ${kindLabel(s.meta.kind)}`)}
                  </button>
                }
              />
              {expanded && (
                <div className="mt-3">
                  <LiveStreamBody st={s} compact />
                </div>
              )}
            </Card>
          );
        })
      )}
    </div>
  );
}
