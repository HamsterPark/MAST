// Series page.
//
// The header lets you rename the series and edit its rating, tags and note, or delete it
// (two clicks to confirm; the members' own item marks are untouched). When the series holds
// spectra, the positions of all members are drawn, numbered by time, on the frames before and
// after the series; the whole series can be anchored to a frame, and ticking "also write to
// every spectrum" anchors each spectrum individually. Below is the member list (same filter bar
// and lightbox).

import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import clsx from "clsx";
import type { GalleryItem, Series } from "@/lib/gallery/types";
import { RLAB, dirLabel, fmtT, fmtTs, num } from "@/lib/gallery/format";
import { anchorOf, initialNeighbours, inside, itemGeometry, seriesAnchorOf, spanOf } from "@/lib/gallery/context";
import { seriesMembers, shortFn } from "@/lib/gallery/series";
import { viewSearch } from "@/lib/gallery/route";
import type { GalleryModel } from "./useGalleryData";
import { useMarksStore } from "./marksStore";
import { FramePanel, type Side } from "./ContextStage";
import { ListView } from "./ListView";
import { H1, Sub } from "./ListHeads";
import { SMALL_BTN } from "./bits";
import { SeriesFigureActions } from "./figures/SeriesFigureActions";
import { tr } from "@/lib/i18n";

function SeriesHead({ sid, s, mem }: { sid: string; s: Series; mem: GalleryItem[] }) {
  const setSeries = useMarksStore((st) => st.setSeries);
  const tagTable = useMarksStore((st) => st.doc.tags);
  const nav = useNavigate();
  const [name, setName] = useState(s.name);
  const [note, setNote] = useState(s.note || "");
  const [armed, setArmed] = useState(false);
  const noteTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const noteRef = useRef(note);
  noteRef.current = note;

  const cur = () => useMarksStore.getState().doc.series[sid];
  const save = (patch: Partial<Series>) => {
    const c = cur();
    if (c) setSeries(sid, { ...c, ...patch });
  };
  const saveNote = () => {
    if (noteTimer.current) {
      clearTimeout(noteTimer.current);
      noteTimer.current = null;
    }
    const c = cur();
    if (c && (c.note || "") !== noteRef.current) save({ note: noteRef.current });
  };
  useEffect(() => () => {
    if (noteTimer.current) saveNote();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => {
    if (!armed) return;
    const t = setTimeout(() => setArmed(false), 4000);
    return () => clearTimeout(t);
  }, [armed]);

  const c = { f: 0, s: 0, g: 0 };
  for (const it of mem) c[it.k]++;
  const a = mem[0];
  const b = mem[mem.length - 1];
  const missing = (s.ids || []).length - mem.length;

  return (
    <div>
      <Link to={{ search: `?${viewSearch({ v: "marked" })}` }} className="text-sm text-mast-accent hover:underline">
        {tr("← 已标记", "← Marked")}
      </Link>
      {a && (
        <>
          {" · "}
          <Link to={{ search: `?${viewSearch({ v: "dir", d: a.d })}` }} className="text-sm text-mast-accent hover:underline">
            {dirLabel(a.d)}
          </Link>
        </>
      )}
      <H1>▤ {s.name}</H1>
      <Sub>
        {tr(`${mem.length} 个`, `${mem.length} items`)} (
        {c.f ? tr(`帧 ${c.f} `, `frames ${c.f} `) : ""}
        {c.s ? tr(`谱 ${c.s} `, `spectra ${c.s} `) : ""}
        {c.g ? tr(`网格 ${c.g}`, `grids ${c.g}`) : ""})
        {a && b ? ` · ${num(a)} → ${num(b)} · ${fmtT(a.t)} → ${fmtT(b.mt || b.t1 || b.t)}` : ""} · {tr("系列号", "series id")} {sid}
        {missing > 0 && (
          <span className="text-mast-danger">
            {" "}
            · {tr(`${missing} 个成员不在图库里`, `${missing} members are not in the gallery`)}
          </span>
        )}
      </Sub>
      <div className="my-1.5 mb-2.5 flex flex-wrap items-center gap-x-2.5 gap-y-2">
        <input
          value={name}
          size={44}
          title={tr("系列名，改完回车或点别处保存", "Series name; press Enter or click elsewhere to save")}
          onChange={(e) => setName(e.target.value)}
          onBlur={() => save({ name: name.trim() || s.name })}
          onKeyDown={(e) => {
            if (e.key === "Enter") (e.target as HTMLInputElement).blur();
          }}
          className="rounded-[3px] border border-mast-border bg-mast-panel px-[5px] py-0.5 text-[13px] text-mast-text"
        />
        <span className="inline-flex gap-1">
          {[1, 2, -1].map((r) => (
            <button
              key={r}
              type="button"
              onClick={() => save({ r: s.r === r ? 0 : r })}
              className={clsx(SMALL_BTN, s.r === r && "!border-mast-accent !bg-mast-accent !text-mast-accent-ink")}
            >
              {RLAB[String(r)]}
            </button>
          ))}
        </span>
        <span className="inline-flex flex-wrap gap-[3px]">
          {tagTable.map((t) => {
            const on = (s.tags || []).includes(t);
            return (
              <button
                key={t}
                type="button"
                onClick={() => save({ tags: on ? s.tags.filter((x) => x !== t) : [...(s.tags || []), t] })}
                className={clsx(SMALL_BTN, "text-xs", on && "!border-mast-accent !bg-mast-accent !text-mast-accent-ink")}
              >
                {t}
              </button>
            );
          })}
        </span>
        <textarea
          value={note}
          placeholder={tr("系列备注", "Series note")}
          onChange={(e) => {
            setNote(e.target.value);
            if (noteTimer.current) clearTimeout(noteTimer.current);
            noteTimer.current = setTimeout(saveNote, 700);
          }}
          onBlur={saveNote}
          className="h-9 min-w-[240px] flex-1 resize-y rounded-[3px] border border-mast-border bg-mast-panel px-[5px] py-0.5 text-[13px] text-mast-text"
        />
        <button
          type="button"
          className={clsx(SMALL_BTN, armed && "!border-mast-danger !text-mast-danger")}
          onClick={() => {
            if (!armed) {
              setArmed(true);
              return;
            }
            setSeries(sid, null);
            nav({ search: `?${viewSearch({ v: "marked" })}` });
          }}
        >
          {armed
            ? tr("再点一次确认删除（成员的单条标记不动）", "Click again to confirm deletion (the members' own marks are kept)")
            : tr("删除系列", "Delete series")}
        </button>
      </div>
      {s.anchor && (
        <Sub>
          ⌖ {tr("位置系于", "Position anchored to")} {shortFn(s.anchor.fn)} ({s.anchor.desc})
        </Sub>
      )}
    </div>
  );
}

function SeriesContext({ sid, s, mem, model }: { sid: string; s: Series; mem: GalleryItem[]; model: GalleryModel }) {
  const setSeries = useMarksStore((st) => st.setSeries);
  const updateMark = useMarksStore((st) => st.updateMark);
  const sp = useMemo(() => mem.filter((it) => it.k === "s" || it.k === "g"), [mem]);
  const { t0, t1 } = spanOf(sp);
  const [sides, setSides] = useState(() => initialNeighbours(model.frames, t0, t1, s.anchor?.id));
  const [each, setEach] = useState(false);
  if (!sp.length) return null;

  const onStep = (side: Side, step: number) =>
    setSides((x) => ({ ...x, [side]: Math.max(0, Math.min(model.frames.length - 1, x[side] + step)) }));
  const onAnchor = (side: Side) => {
    const f = model.frames[sides[side]];
    const cur = useMarksStore.getState().doc.series[sid];
    if (!f || !cur) return;
    if (cur.anchor?.id === f.id) {
      setSeries(sid, { ...cur, anchor: null });
      return;
    }
    setSeries(sid, { ...cur, anchor: seriesAnchorOf(f, t0, t1) });
    if (each) for (const it of sp) updateMark(it, { anchor: anchorOf(it, f) });
  };

  const panel = (side: Side) => {
    const f = model.frames[sides[side]];
    const pts = f
      ? sp.map((it, i) => {
          const p = itemGeometry(it, f).pts[0] ?? { u: 0.5, v: 0.5 };
          return { ...p, lab: String(i + 1) };
        })
      : [];
    return (
      <FramePanel
        side={side}
        frame={f}
        t0={t0}
        t1={t1}
        noun={tr("系列", "series")}
        pts={pts}
        poly={null}
        anchoredId={s.anchor?.id}
        onStep={onStep}
        onAnchor={onAnchor}
        posText={() => tr(` · ${pts.filter(inside).length}/${pts.length} 条在视野内`, ` · ${pts.filter(inside).length}/${pts.length} in view`)}
      />
    );
  };

  return (
    <div className="mb-2.5 mt-1.5 rounded-[3px] border border-mast-border bg-mast-panel px-3 py-2.5 text-mast-text">
      <div className="text-[12.5px]">
        <b className="font-semibold">{tr(`系列里 ${sp.length} 条谱的位置`, `Positions of the ${sp.length} spectra in the series`)}</b>{" "}
        {tr(`（按时间编号 1…${sp.length}；${fmtTs(t0)}–${fmtTs(t1)}）`, `(numbered 1…${sp.length} by time; ${fmtTs(t0)}–${fmtTs(t1)})`)}{" "}
        <label className="text-mast-muted">
          <input type="checkbox" checked={each} onChange={(e) => setEach(e.target.checked)} />{" "}
          {tr("系定时同时写到每一条谱", "Also write the anchor to every spectrum")}
        </label>
      </div>
      <div className="mt-2 grid max-w-[1000px] grid-cols-2 gap-3.5 max-[760px]:grid-cols-1">
        {panel("prev")}
        {panel("next")}
      </div>
    </div>
  );
}

export function SeriesView({ sid, model }: { sid: string; model: GalleryModel }) {
  const s = useMarksStore((st) => st.doc.series[sid]);
  // Memoised on the *content* of the member ids: renaming the series or editing its note or
  // rating yields a new series object; if the member list changed reference with it, the card
  // grid below would drop back to the first 150 cards and lose its scroll position.
  const idsKey = s ? s.ids.join("\n") : "";
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const mem = useMemo(() => seriesMembers(s, model.items), [idsKey, model.items]);

  if (!s) {
    return (
      <div>
        <Link to={{ search: `?${viewSearch({ v: "marked" })}` }} className="text-sm text-mast-accent hover:underline">
          {tr("← 已标记", "← Marked")}
        </Link>
        <H1>{tr("这个系列不存在或已删除", "This series does not exist or was deleted")}</H1>
      </div>
    );
  }
  return (
    <ListView
      view="series"
      model={model}
      base={mem}
      head={
        <>
          <SeriesHead key={sid} sid={sid} s={s} mem={mem} />
          <SeriesFigureActions key={`fig:${sid}`} sid={sid} mem={mem} model={model} />
        </>
      }
      below={<SeriesContext key={sid} sid={sid} s={s} mem={mem} model={model} />}
    />
  );
}
