// Card grid with incremental rendering.
//
// Cards render in batches of 150; another batch is added whenever the sentinel is within
// 1500 px of the viewport bottom. IntersectionObserver only fires when the intersection
// state *changes*: after a batch is added the sentinel may still be within 1500 px, so the
// observer is re-created on every change of the shown count to report the current state
// again. Otherwise a very tall window would stall after the first batch.
//
// The scroll container is AppLayout's <main>, not window; using the viewport as the observer
// root is enough (intersection is computed against the viewport, and the sentinel moves
// through it as <main> scrolls).

import { useEffect, useRef, useState } from "react";
import type { GalleryItem } from "@/lib/gallery/types";
import { GalleryCard } from "./GalleryCard";
import { tr } from "@/lib/i18n";

const BATCH = 150;

export function CardGrid({
  list,
  cw,
  showDir,
  currentId,
  minShown,
  lastBatch,
  numOf,
  onOpen,
  onPick,
}: {
  list: GalleryItem[];
  cw: number;
  showDir: boolean;
  currentId: string | null;
  /** Render at least this many cards (when the lightbox has paged ahead and the grid must scroll to a card on close). */
  minShown: number;
  lastBatch: string;
  numOf: (id: string) => string | null;
  onOpen: (j: number) => void;
  onPick: (j: number, shift: boolean, checked: boolean) => void;
}) {
  // Restart from the first batch when the list changes (reset during render to avoid flashing the old count).
  const [st, setSt] = useState({ list, shown: BATCH });
  if (st.list !== list) setSt({ list, shown: BATCH });
  const shown = st.list === list ? st.shown : BATCH;
  const need = Math.min(list.length, Math.max(shown, minShown));

  const sentinel = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = sentinel.current;
    if (!el || need >= list.length) return;
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          setSt((s) => (s.list === list ? { list, shown: Math.min(list.length, s.shown + BATCH) } : s));
        }
      },
      { rootMargin: "0px 0px 1500px 0px" },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [list, need]);

  return (
    <>
      <div
        className="mt-3 grid gap-2.5"
        style={{ gridTemplateColumns: `repeat(auto-fill, minmax(${cw}px, 1fr))` }}
      >
        {list.slice(0, need).map((it, j) => (
          <GalleryCard
            key={it.id}
            item={it}
            j={j}
            showDir={showDir}
            current={it.id === currentId}
            lastBatch={lastBatch}
            numOf={numOf}
            onOpen={onOpen}
            onPick={onPick}
          />
        ))}
      </div>
      <div ref={sentinel} className="mx-auto my-[18px] text-center text-sm text-mast-muted">
        {need < list.length
          ? tr(`已显示 ${need} / ${list.length}，往下滚继续`, `Showing ${need} / ${list.length}, scroll down for more`)
          : list.length
            ? ""
            : tr("没有符合条件的文件", "No matching files")}
      </div>
    </>
  );
}
