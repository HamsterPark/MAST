// Bottom action bar.
//
// Appears once cards are selected: rate together, add/remove a tag together, "Save as series…"
// (series name, rating, tags, note; or merge into an existing series), "Remove from series" on
// a series page, and clear selection. Not shown on the directory overview, Marked page,
// Figures page or data-roots page (the figures page has no cards to select).
//
// When every selected item is a spectrum, a "stitch figure" control is added (design section 10:
// single-spectrum wide-range stitching).

import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import clsx from "clsx";
import type { GalleryView } from "@/lib/gallery/route";
import { viewSearch } from "@/lib/gallery/route";
import { autoName, kindOf, mergedMemberIds, newSeriesId, rangeText } from "@/lib/gallery/series";
import type { GalleryModel } from "./useGalleryData";
import { useMarksStore } from "./marksStore";
import { useSelectionStore } from "./selectionStore";
import { toast } from "./toast";
import { SMALL_BTN, SMALL_SELECT } from "./bits";
import { StitchSelected } from "./figures/StitchControls";
import { tr } from "@/lib/i18n";

const ACC = "!border-mast-accent !bg-mast-accent !text-mast-accent-ink";

export function SelectionBar({ view, model }: { view: GalleryView; model: GalleryModel }) {
  const ids = useSelectionStore((s) => s.ids);
  const clear = useSelectionStore((s) => s.clear);
  const tagTable = useMarksStore((s) => s.doc.tags);
  const series = useMarksStore((s) => s.doc.series);
  const nav = useNavigate();

  const its = useMemo(() => model.items.filter((it) => ids.has(it.id)), [ids, model.items]);

  const [tag, setTag] = useState("");
  const [formOpen, setFormOpen] = useState(false);
  const [target, setTarget] = useState("");
  const [name, setName] = useState("");
  const [note, setNote] = useState("");
  const [form, setForm] = useState<{ r: number; tags: string[] }>({ r: 0, tags: [] });

  const hidden =
    !its.length || view.v === "dirs" || view.v === "marked" || view.v === "figures" || view.v === "setup";
  useEffect(() => {
    if (hidden) setFormOpen(false);
  }, [hidden]);
  if (hidden) return null;

  const fillForm = (sid: string) => {
    const s = sid ? series[sid] : undefined;
    setName(s ? s.name : autoName(its));
    setNote(s ? s.note || "" : "");
    setForm({ r: s ? s.r || 0 : 0, tags: s ? [...(s.tags || [])] : [] });
  };
  const curTag = tag && tagTable.includes(tag) ? tag : (tagTable[0] ?? "");
  const allSpectra = its.every((it) => it.k === "s");

  const saveSeries = () => {
    if (!its.length) return;
    const old = target ? series[target] : undefined;
    const memberIds = mergedMemberIds(old?.ids ?? [], its, model.items);
    const members = memberIds.map((id) => model.byId.get(id)).filter((x): x is NonNullable<typeof x> => !!x);
    const sid = target || newSeriesId(Date.now());
    const finalName = name.trim() || autoName(members);
    useMarksStore.getState().setSeries(sid, {
      ...(old ?? {}),
      name: finalName,
      k: kindOf(members),
      ids: memberIds,
      r: form.r,
      tags: [...form.tags],
      note,
    });
    setFormOpen(false);
    toast(
      <>
        {old ? tr("已并入系列", "Merged into series") : tr("已存为系列", "Saved as series")}{" "}
        <Link to={{ search: `?${viewSearch({ v: "series", s: sid })}` }}>{finalName}</Link>
        {tr(`（${memberIds.length} 个）`, ` (${memberIds.length} items)`)}
      </>,
    );
    clear();
  };

  const removeFromSeries = () => {
    if (view.v !== "series") return;
    const s = useMarksStore.getState().doc.series[view.s];
    if (!s) return;
    const left = s.ids.filter((id) => !ids.has(id));
    useMarksStore.getState().setSeries(view.s, left.length ? { ...s, ids: left } : null);
    clear();
    if (!left.length) nav({ search: `?${viewSearch({ v: "marked" })}` });
  };

  return (
    <div className="fixed inset-x-0 bottom-0 z-40 border-t-2 border-mast-accent bg-mast-panel px-[18px] py-2 text-[13px] text-mast-text shadow-[0_-4px_16px_rgba(0,0,0,.14)]">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
        <b>{tr(`已选 ${its.length} 个`, `${its.length} selected`)}</b>
        <span className="text-mast-muted">{rangeText(its)}</span>
        <span className="inline-flex items-center gap-[3px]">
          {tr("一起打分", "Rate together")}
          {([
            [1, "✓"],
            [2, "★"],
            [-1, "✗"],
            [0, tr("清评级", "Clear rating")],
          ] as const).map(([r, lab]) => (
            <button
              key={r}
              type="button"
              className={SMALL_BTN}
              onClick={() => {
                for (const it of its) useMarksStore.getState().updateMark(it, { r });
                toast(tr(`已给 ${its.length} 个打分`, `Rated ${its.length} items`));
              }}
            >
              {lab}
            </button>
          ))}
        </span>
        <span className="inline-flex items-center gap-[3px]">
          <select className={SMALL_SELECT} value={curTag} onChange={(e) => setTag(e.target.value)}>
            {tagTable.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
          {([
            [true, tr("一起加标签", "Add tag to all")],
            [false, tr("去掉", "Remove")],
          ] as const).map(([add, lab]) => (
            <button
              key={String(add)}
              type="button"
              className={SMALL_BTN}
              disabled={!curTag}
              onClick={() => {
                for (const it of its) useMarksStore.getState().toggleTag(it, curTag, add);
                toast(
                  add
                    ? tr(`已加标签「${curTag}」×${its.length}`, `Added tag "${curTag}" ×${its.length}`)
                    : tr(`已去掉标签「${curTag}」×${its.length}`, `Removed tag "${curTag}" ×${its.length}`),
                );
              }}
            >
              {lab}
            </button>
          ))}
        </span>
        <button
          type="button"
          className={clsx(SMALL_BTN, ACC)}
          onClick={() => {
            if (!formOpen) fillForm(target);
            setFormOpen((o) => !o);
          }}
        >
          {tr("存为系列…", "Save as series…")}
        </button>
        {allSpectra && <StitchSelected ids={its.map((it) => it.id)} />}
        {view.v === "series" && (
          <button type="button" className={SMALL_BTN} onClick={removeFromSeries}>
            {tr("从本系列移除", "Remove from this series")}
          </button>
        )}
        <button type="button" className={SMALL_BTN} onClick={clear}>
          {tr("取消选择", "Clear selection")}
        </button>
      </div>

      {formOpen && (
        <div className="mt-[7px] flex flex-wrap items-center gap-x-3 gap-y-1.5 border-t border-mast-border pt-[7px]">
          <select
            className={SMALL_SELECT}
            value={target}
            onChange={(e) => {
              setTarget(e.target.value);
              fillForm(e.target.value);
            }}
          >
            <option value="">{tr("新建系列", "New series")}</option>
            {Object.entries(series).map(([sid, s]) => (
              <option key={sid} value={sid}>
                {tr("并入：", "Merge into: ")}{s.name}
              </option>
            ))}
          </select>
          <input
            className={`${SMALL_SELECT} w-[26rem] max-w-full`}
            value={name}
            placeholder={tr("系列名", "Series name")}
            autoFocus
            onChange={(e) => setName(e.target.value)}
          />
          <span className="inline-flex gap-[3px]">
            {([
              [1, tr("✓ 可用", "✓ Usable")],
              [2, tr("★ 重点", "★ Key")],
              [-1, tr("✗ 排除", "✗ Exclude")],
            ] as const).map(([r, lab]) => (
              <button
                key={r}
                type="button"
                className={clsx(SMALL_BTN, form.r === r && ACC)}
                onClick={() => setForm((f) => ({ ...f, r: f.r === r ? 0 : r }))}
              >
                {lab}
              </button>
            ))}
          </span>
          <span className="inline-flex flex-wrap gap-[3px]">
            {tagTable.map((t) => (
              <button
                key={t}
                type="button"
                className={clsx(SMALL_BTN, "text-xs", form.tags.includes(t) && ACC)}
                onClick={() =>
                  setForm((f) => ({
                    ...f,
                    tags: f.tags.includes(t) ? f.tags.filter((x) => x !== t) : [...f.tags, t],
                  }))
                }
              >
                {t}
              </button>
            ))}
          </span>
          <input
            className={`${SMALL_SELECT} w-72 max-w-full`}
            value={note}
            placeholder={tr("系列备注（这组数据是什么、为什么有用）", "Series note (what this data is and why it is useful)")}
            onChange={(e) => setNote(e.target.value)}
          />
          <button type="button" className={clsx(SMALL_BTN, ACC)} onClick={saveSeries}>
            {tr("保存系列", "Save series")}
          </button>
          <button type="button" className={SMALL_BTN} onClick={() => setFormOpen(false)}>
            {tr("收起", "Collapse")}
          </button>
        </div>
      )}
    </div>
  );
}
