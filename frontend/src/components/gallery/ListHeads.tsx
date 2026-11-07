// List-view headers: a directory (with the "reviewed" flag and directory note), all items of a kind, or the latest batch.

import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import type { GalleryItem, Kind } from "@/lib/gallery/types";
import { KLAB, dirLabel, dirnameOf, isDateSeg, lastSeg } from "@/lib/gallery/format";
import { viewSearch } from "@/lib/gallery/route";
import { useMarksStore } from "./marksStore";
import { tr } from "@/lib/i18n";

const EN_KIND: Record<Kind, string> = { f: "frames", s: "spectra", g: "grid spectra" };

export function H1({ children }: { children: React.ReactNode }) {
  return <h1 className="mb-0.5 mt-1 text-[1.35rem] font-semibold text-mast-text">{children}</h1>;
}

export function Sub({ children }: { children: React.ReactNode }) {
  return <div className="mb-2.5 text-[13px] text-mast-muted">{children}</div>;
}

export function BackToDirs() {
  return (
    <Link to={{ search: `?${viewSearch({ v: "dirs" })}` }} className="text-sm text-mast-accent hover:underline">
      {tr("← 目录", "← Directories")}
    </Link>
  );
}

/** Directory header: counts, source directory, "reviewed" flag and directory note (debounced 700 ms, saved on leave). */
export function DirHead({ d, base }: { d: string; base: GalleryItem[] }) {
  const dv = useMarksStore((s) => s.doc.days[d]);
  const setDay = useMarksStore((s) => s.setDay);
  const [note, setNote] = useState(dv?.note ?? "");
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const noteRef = useRef(note);
  noteRef.current = note;

  // When the server document changed (edited in another tab) and the user is not typing, sync the note.
  const focused = useRef(false);
  useEffect(() => {
    if (!focused.current && !timer.current) setNote(dv?.note ?? "");
  }, [dv?.note]);

  const saveNote = () => {
    if (timer.current) {
      clearTimeout(timer.current);
      timer.current = null;
    }
    const cur = useMarksStore.getState().doc.days[d];
    if ((cur?.note ?? "") === noteRef.current) return;
    useMarksStore.getState().setDay(d, { done: cur?.done ?? false, note: noteRef.current });
  };
  useEffect(() => () => {
    if (timer.current) saveNote();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const c = { f: 0, s: 0, g: 0 };
  let nd = 0;
  for (const it of base) {
    c[it.k]++;
    if (it.dup) nd++;
  }
  const sample = base[0];
  const isDay = isDateSeg(lastSeg(d));

  return (
    <div>
      <BackToDirs />
      <H1>{dirLabel(d)}</H1>
      <Sub>
        {tr(`${c.f} 帧`, `${c.f} frames`)}
        {nd ? tr(`（其中重复保存 ${nd}）`, ` (${nd} duplicate saves)`) : ""} · {tr(`${c.s} 条谱`, `${c.s} spectra`)} ·{" "}
        {tr(`${c.g} 个网格谱`, `${c.g} grid spectra`)} · {tr("原始目录", "source directory")}{" "}
        <span className="font-mono">{sample ? dirnameOf(sample.p) : d}</span> ·{" "}
        {tr("点图放大后可用键盘连续标记", "open an image to mark continuously with the keyboard")}
      </Sub>
      <div className="my-1.5 flex items-start gap-2.5">
        <label className="flex shrink-0 items-center gap-1 whitespace-nowrap text-[13px] text-mast-text">
          <input
            type="checkbox"
            checked={!!dv?.done}
            onChange={(e) => setDay(d, { done: e.target.checked, note: noteRef.current })}
          />
          {isDay ? tr("本日已过完", "Day reviewed") : tr("本目录已过完", "Directory reviewed")}
        </label>
        <textarea
          value={note}
          placeholder={
            isDay
              ? tr("这一天的备注：针尖状态、做了什么、看到了什么…", "Notes for this day: tip state, what was done, what was seen…")
              : tr("这个目录的备注：针尖状态、做了什么、看到了什么…", "Notes for this directory: tip state, what was done, what was seen…")
          }
          onFocus={() => (focused.current = true)}
          onBlur={() => {
            focused.current = false;
            saveNote();
          }}
          onChange={(e) => {
            setNote(e.target.value);
            if (timer.current) clearTimeout(timer.current);
            timer.current = setTimeout(saveNote, 700);
          }}
          className="h-9 min-h-9 flex-1 resize-y rounded-[3px] border border-mast-border bg-mast-panel px-[5px] py-0.5 text-[13px] text-mast-text"
        />
      </div>
    </div>
  );
}

export function AllHead({ k }: { k: Kind }) {
  return (
    <div>
      <H1>{tr(`全部${KLAB[k]}`, `All ${EN_KIND[k]}`)}</H1>
      <Sub>{tr("跨目录，按目录与时间排；方括号里是所在目录。", "Across directories, ordered by directory and time; the directory is shown in square brackets.")}</Sub>
    </div>
  );
}

export function NewHead({ lastBatch }: { lastBatch: string }) {
  return (
    <div>
      <BackToDirs />
      <H1>{tr(`最新一批（${lastBatch} 加入）`, `Latest batch (added ${lastBatch})`)}</H1>
      <Sub>{tr("这次更新新到的全部文件。", "All files that arrived in this update.")}</Sub>
    </div>
  );
}
