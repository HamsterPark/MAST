// Lightbox with keyboard marking, including the spectrum/neighbour-frame comparison and anchoring.
//
// Rendered into document.body (portal): MAST's scroll container is AppLayout's <main>, and a
// fixed overlay left inside <main> would let wheel events bubble up and scroll the page below.
//
// The keyboard listener is registered once and reads "which item is current" from the `latest`
// ref. When paging, rating and anchoring are pressed in quick succession within one event loop,
// a stale item in a closure would apply the rating to the previous card.

import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { createPortal } from "react-dom";
import clsx from "clsx";
import type { GalleryItem } from "@/lib/gallery/types";
import { KLAB, autoTags, fmtT, fmtTs, fullPath, meta, num } from "@/lib/gallery/format";
import { anchorOf, initialNeighbours, tBeg, tEnd } from "@/lib/gallery/context";
import { HOT_LABELS, keyAction } from "@/lib/gallery/keys";
import { ratingToggle } from "@/lib/gallery/marks";
import { seriesOf, shortFn } from "@/lib/gallery/series";
import { viewSearch } from "@/lib/gallery/route";
import { Link } from "react-router-dom";
import type { GalleryModel } from "./useGalleryData";
import { useMarksStore } from "./marksStore";
import { useSelectionStore } from "./selectionStore";
import { ContextStage, type Side } from "./ContextStage";
import { Chip, DARK_BTN, Kbd, arText, chipClass, darkRatingOnClass } from "./bits";
import { CopyButton } from "./clipboard";
import { LightboxFigureButton } from "./figures/LightboxFigureButton";
import { tr } from "@/lib/i18n";

const AUTO_KEY = "mast.gallery.auto";

function readAuto(): boolean {
  try {
    return localStorage.getItem(AUTO_KEY) === "true";
  } catch {
    return false;
  }
}

const fix2 = (v?: number | null) => (Number(v) || 0).toFixed(2);

export function Lightbox({
  list,
  index,
  model,
  onIndex,
  onClose,
}: {
  list: GalleryItem[];
  index: number;
  model: GalleryModel;
  onIndex: (j: number) => void;
  onClose: () => void;
}) {
  const j = Math.max(0, Math.min(list.length - 1, index));
  const it = list[j];
  const spectrumLike = !!it && ((it.k === "s" && !it.ex) || it.k === "g");

  const m = useMarksStore((s) => (it ? s.doc.items[it.id] : undefined));
  const tagTable = useMarksStore((s) => s.doc.tags);
  const series = useMarksStore((s) => s.doc.series);
  const toggleTag = useMarksStore((s) => s.toggleTag);
  const setTags = useMarksStore((s) => s.setTags);
  const selIds = useSelectionStore((s) => s.ids);
  const startId = useSelectionStore((s) => s.startId);

  const [auto, setAuto] = useState(readAuto);
  const [interactive, setInteractive] = useState(false);
  const [newTag, setNewTag] = useState("");

  // Which two frames sit on the sides: on an item change, recompute as (last saved before start / first started after end / the anchored frame).
  const [sides, setSides] = useState<{ id: string; prev: number; next: number } | null>(null);
  let curSides = sides;
  if (it && spectrumLike && (!sides || sides.id !== it.id)) {
    const anchorId = useMarksStore.getState().doc.items[it.id]?.anchor?.id;
    curSides = { id: it.id, ...initialNeighbours(model.frames, tBeg(it), tEnd(it), anchorId) };
    setSides(curSides);
  }

  // Note: local draft, saved with a 700 ms debounce and on blur; flushed before switching items.
  const [note, setNote] = useState("");
  const noteRef = useRef<HTMLTextAreaElement>(null);
  const noteFor = useRef<string | null>(null);
  const noteTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    if (!it) return;
    if (noteFor.current !== it.id || document.activeElement !== noteRef.current) {
      setNote(m?.note ?? "");
      noteFor.current = it.id;
    }
  }, [it, m?.note]);

  const latest = useRef({ it, j, list, auto, sides: curSides, spectrumLike, note, onIndex, onClose });
  latest.current = { it, j, list, auto, sides: curSides, spectrumLike, note, onIndex, onClose };

  const actions = useRef({
    flushNote: () => {},
    go: (_to: number) => {},
    rate: (_r: number, _fromKey: boolean) => {},
    anchorTo: (_side: Side) => {},
    close: () => {},
  });
  actions.current.flushNote = () => {
    if (noteTimer.current) {
      clearTimeout(noteTimer.current);
      noteTimer.current = null;
    }
    const cur = latest.current;
    if (!cur.it || noteFor.current !== cur.it.id) return;
    const stored = useMarksStore.getState().doc.items[cur.it.id]?.note ?? "";
    if (stored !== cur.note) useMarksStore.getState().updateMark(cur.it, { note: cur.note });
  };
  actions.current.go = (to: number) => {
    const cur = latest.current;
    actions.current.flushNote();
    if (!cur.list.length) return;
    cur.onIndex(Math.max(0, Math.min(cur.list.length - 1, to)));
  };
  actions.current.rate = (r: number, fromKey: boolean) => {
    const cur = latest.current;
    if (!cur.it) return;
    const old = useMarksStore.getState().doc.items[cur.it.id];
    useMarksStore.getState().updateMark(cur.it, { r: fromKey ? r : ratingToggle(old?.r, r) });
    if (fromKey && cur.auto && r !== 0 && cur.j < cur.list.length - 1) actions.current.go(cur.j + 1);
  };
  actions.current.anchorTo = (side: Side) => {
    const cur = latest.current;
    if (!cur.it || !cur.sides) return;
    const f = model.frames[cur.sides[side]];
    if (!f) return;
    const old = useMarksStore.getState().doc.items[cur.it.id];
    if (old?.anchor?.id === f.id) useMarksStore.getState().updateMark(cur.it, { anchor: null });
    else useMarksStore.getState().updateMark(cur.it, { anchor: anchorOf(cur.it, f) });
  };
  actions.current.close = () => {
    actions.current.flushNote();
    latest.current.onClose();
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const cur = latest.current;
      if (!cur.it) return;
      const tg = e.target as HTMLElement | null;
      const tag = tg?.tagName;
      const inField =
        !!tg &&
        (tag === "TEXTAREA" ||
          tag === "SELECT" ||
          (tag === "INPUT" && (tg as HTMLInputElement).type !== "checkbox"));
      const a = keyAction({
        code: e.code,
        ctrl: e.ctrlKey,
        meta: e.metaKey,
        alt: e.altKey,
        inField,
        spectrumLike: cur.spectrumLike,
      });
      if (!a) return;
      e.preventDefault();
      const act = actions.current;
      switch (a.t) {
        case "blur":
          tg?.blur();
          break;
        case "next":
          act.go(cur.j + 1);
          break;
        case "prev":
          act.go(cur.j - 1);
          break;
        case "first":
          act.go(0);
          break;
        case "last":
          act.go(cur.list.length - 1);
          break;
        case "rate":
          act.rate(a.r, true);
          break;
        case "note":
          noteRef.current?.focus();
          break;
        case "pick":
          useSelectionStore.getState().key(cur.list, cur.j, a.code);
          break;
        case "anchor":
          act.anchorTo(a.side);
          break;
        case "tag": {
          const t = useMarksStore.getState().doc.tags[a.index];
          if (t) useMarksStore.getState().toggleTag(cur.it, t);
          break;
        }
        case "close":
          act.close();
          break;
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  // Close itself when the list becomes empty (the last item was filtered out elsewhere).
  useEffect(() => {
    if (!list.length) actions.current.close();
  }, [list.length]);

  // Save any unsaved note on leave (also covers unmounting on route change).
  useEffect(() => () => actions.current.flushNote(), []);

  // Preload the next and previous images.
  useEffect(() => {
    for (const n of [list[j + 1], list[j - 1]]) {
      if (!n) continue;
      new Image().src = n.th;
      if (n.li) new Image().src = n.li;
    }
  }, [list, j]);

  if (!it) return null;

  const mark = m ?? { r: 0, tags: [], note: "" };
  const sids = seriesOf(series, it.id);
  const startItem = startId ? model.byId.get(startId) : undefined;
  const extraTags = (mark.tags || []).filter((t) => !tagTable.includes(t));

  const metaLines: ReactNode[] = [tr(`${KLAB[it.k]} · 目录 ${it.d}`, `${KLAB[it.k]} · directory ${it.d}`), meta(it)];
  if (it.k === "f") {
    metaLines.push(
      tr(
        `中心 (${fix2(it.cx)}, ${fix2(it.cy)}) nm · 转角 ${it.ang || 0}° · 行 ${it.rows}/${it.rall} · ` +
          `保存 ${fmtTs(it.mt)}${it.li ? " · 右图为 lock-in dI/dV 通道" : ""}`,
        `center (${fix2(it.cx)}, ${fix2(it.cy)}) nm · angle ${it.ang || 0}° · rows ${it.rows}/${it.rall} · ` +
          `saved ${fmtTs(it.mt)}${it.li ? " · right image is the lock-in dI/dV channel" : ""}`,
      ),
    );
    if (it.ar) metaLines.push(<span className="text-[#fbbf24]">{tr("原子判据判不了：", "Atomic criterion could not decide: ")}{arText(it.ar)}</span>);
  }
  if (it.k === "s") {
    metaLines.push(
      tr(
        `${it.sw || 1} sweep${
          it.lic ? " · 谱图下半为 lock-in dI/dV" : it.dn ? " · 没有 lock-in，谱图下半为 I(V) 数值求导的 dI/dV" : ""
        } · 开始 ${fmtTs(it.t)} · 保存 ${fmtTs(it.mt)}`,
        `${it.sw || 1} sweep${
          it.lic
            ? " · lower half of the plot is lock-in dI/dV"
            : it.dn
              ? " · no lock-in; lower half is dI/dV from numerical differentiation of I(V)"
              : ""
        } · started ${fmtTs(it.t)} · saved ${fmtTs(it.mt)}`,
      ),
    );
  }
  if (it.k === "g") {
    metaLines.push(
      tr(
        `中心 (${fix2(it.cx)}, ${fix2(it.cy)}) nm · ${it.sw ?? "?"} sweep · 完成 ${it.have}/${(it.gx ?? 0) * (it.gy ?? 0)} · ` +
          `结束 ${fmtT(it.t1)} · 图中第 0 行在下沿（按点坐标核对过）`,
        `center (${fix2(it.cx)}, ${fix2(it.cy)}) nm · ${it.sw ?? "?"} sweep · done ${it.have}/${(it.gx ?? 0) * (it.gy ?? 0)} · ` +
          `ended ${fmtT(it.t1)} · row 0 is at the bottom edge of the plot (checked against point coordinates)`,
      ),
    );
  }

  const node = (
    <div className="fixed inset-0 z-[60] flex bg-[rgba(10,10,12,.93)] text-[#eee] max-[760px]:flex-col">
      <div
        className={clsx(
          "flex min-w-0 flex-1 justify-center gap-2.5 overflow-auto overscroll-contain p-3.5",
          spectrumLike ? "items-start" : "items-center",
        )}
        onClick={(e) => {
          if (e.target === e.currentTarget) actions.current.close();
        }}
      >
        {spectrumLike && curSides ? (
          <ContextStage
            it={it}
            frames={model.frames}
            sides={curSides}
            anchoredId={mark.anchor?.id}
            interactive={interactive}
            onStep={(side, step) =>
              setSides((s) =>
                s
                  ? { ...s, [side]: Math.max(0, Math.min(model.frames.length - 1, s[side] + step)) }
                  : s,
              )
            }
            onAnchor={(side) => actions.current.anchorTo(side)}
          />
        ) : (
          <>
            <img
              src={it.th}
              alt=""
              className={clsx(
                "max-h-[calc(100vh-28px)] bg-white object-contain",
                it.li ? "max-w-[calc(50%-5px)]" : "max-w-full",
              )}
            />
            {it.li && (
              <img src={it.li} alt="" className="max-h-[calc(100vh-28px)] max-w-[calc(50%-5px)] bg-white object-contain" />
            )}
          </>
        )}
      </div>

      <button
        type="button"
        title={tr("关闭（Esc）", "Close (Esc)")}
        onClick={() => actions.current.close()}
        className="absolute right-[352px] top-2 text-xl text-[#ccc] hover:text-white max-[760px]:right-2.5"
      >
        ✕
      </button>

      <aside className="w-[340px] flex-none overflow-auto overscroll-contain border-l border-[#333] bg-[#1c1f23] px-3.5 py-3 text-[13px] max-[760px]:max-h-[45vh] max-[760px]:w-auto max-[760px]:border-l-0 max-[760px]:border-t">
        <h3 className="mb-1 break-all font-mono text-[15px]">{it.fn}</h3>
        <div className="break-all font-mono text-xs leading-normal text-[#b9c0c8]">
          {metaLines.map((line, i) => (
            <div key={i}>{line}</div>
          ))}
          <div>
            {autoTags(it, model.lastBatch, model.numOf).map((t) => (
              <Chip key={t.kind} kind={t.kind} title={t.title}>
                {t.text}
              </Chip>
            ))}
            {sids.map((sid) => (
              <Link key={sid} to={{ search: `?${viewSearch({ v: "series", s: sid })}` }} className={chipClass("ser")}>
                ▤ {series[sid]?.name || sid}
              </Link>
            ))}
            {mark.anchor && (
              <Chip kind="anc" title={mark.anchor.desc}>
                ⌖ {shortFn(mark.anchor.fn)}
              </Chip>
            )}
          </div>
          <div>
            <span className="text-[#8a939c]">{fullPath(it)}</span>{" "}
            <CopyButton
              text={fullPath(it)}
              label={tr("复制路径", "Copy path")}
              className="!border-[#444] !bg-[#2a2e33] !text-[#eee]"
            />
          </div>
          {(it.cpl?.length ?? 0) > 1 && (
            <div className="mt-1 text-[#8a939c]">
              {tr(`字节相同的副本 ${it.cpl!.length} 处：`, `Byte-identical copies (${it.cpl!.length}):`)}
              {it.cpl!.map((p) => (
                <div key={p}>· {p}</div>
              ))}
            </div>
          )}
          <div className="text-[#c4b5fd]">
            {selIds.has(it.id) ? tr("☑ 已选中", "☑ Selected") : tr("☐ 未选中", "☐ Not selected")} ·{" "}
            {tr(`共选 ${selIds.size} 个`, `${selIds.size} selected`)}
            {startItem ? tr(` · 起点 ${num(startItem)}`, ` · start ${num(startItem)}`) : ""} ·{" "}
            {tr("S 选/取消 · [ 起点 · ] 选到这里", "S toggle · [ start · ] select to here")}
          </div>
        </div>

        <div className="mb-1 mt-2.5 flex flex-wrap items-center gap-[5px]">
          {([
            [1, tr("✓ 可用", "✓ Usable"), "1"],
            [2, tr("★ 重点", "★ Key"), "2"],
            [-1, tr("✗ 排除", "✗ Exclude"), "3"],
            [0, tr("清除", "Clear"), "0"],
          ] as const).map(([r, label, key]) => (
            <button
              key={r}
              type="button"
              onClick={() => actions.current.rate(r, false)}
              className={clsx(DARK_BTN, r !== 0 && mark.r === r && darkRatingOnClass(r))}
            >
              {label}
              <Kbd dark>{key}</Kbd>
            </button>
          ))}
        </div>

        <div className="mb-1 mt-2.5 flex flex-wrap items-center gap-[5px]">
          {tagTable.map((t, i) => (
            <button
              key={t}
              type="button"
              onClick={() => toggleTag(it, t)}
              className={clsx(
                DARK_BTN,
                "!px-[7px] !py-px text-xs",
                (mark.tags || []).includes(t) && "!border-[#a897ee] !bg-[#5b46b0]",
              )}
            >
              {t}
              {i < 9 && <Kbd dark>{HOT_LABELS[i]}</Kbd>}
            </button>
          ))}
          {extraTags.map((t) => (
            <button
              key={t}
              type="button"
              onClick={() => toggleTag(it, t)}
              className={clsx(DARK_BTN, "!border-[#a897ee] !bg-[#5b46b0] !px-[7px] !py-px text-xs")}
            >
              {t}
            </button>
          ))}
        </div>
        <div className="mb-1 mt-2.5">
          <input
            value={newTag}
            placeholder={tr("新标签，回车添加", "New tag, Enter to add")}
            onChange={(e) => setNewTag(e.target.value)}
            onKeyDown={(e) => {
              if (e.key !== "Enter") return;
              const t = newTag.trim();
              if (!t) return;
              if (!useMarksStore.getState().doc.tags.includes(t)) setTags([...useMarksStore.getState().doc.tags, t]);
              toggleTag(it, t, true);
              setNewTag("");
            }}
            className="w-[140px] rounded-[3px] border border-[#444] bg-[#111316] px-[5px] py-0.5 text-[13px] text-[#eee]"
          />
        </div>
        <textarea
          ref={noteRef}
          value={note}
          placeholder={tr("备注（N 键聚焦，Esc 退出输入）", "Note (N to focus, Esc to leave)")}
          onChange={(e) => {
            setNote(e.target.value);
            noteFor.current = it.id;
            if (noteTimer.current) clearTimeout(noteTimer.current);
            noteTimer.current = setTimeout(() => actions.current.flushNote(), 700);
          }}
          onBlur={() => actions.current.flushNote()}
          className="h-[110px] w-full rounded-[3px] border border-[#444] bg-[#111316] px-[5px] py-0.5 text-[13px] text-[#eee]"
        />

        <div className="mt-1.5 flex items-center justify-between">
          <button type="button" className={DARK_BTN} onClick={() => actions.current.go(j - 1)}>
            {tr("← 上一个", "← Previous")}
          </button>
          <span className="font-mono">
            {j + 1} / {list.length}
          </span>
          <button type="button" className={DARK_BTN} onClick={() => actions.current.go(j + 1)}>
            {tr("下一个 →", "Next →")}
          </button>
        </div>
        <label className="mt-2 block text-[12.5px] text-[#c9d0d7]">
          <input
            type="checkbox"
            checked={auto}
            onChange={(e) => {
              setAuto(e.target.checked);
              try {
                localStorage.setItem(AUTO_KEY, String(e.target.checked));
              } catch {
                /* works without remembering */
              }
            }}
          />{" "}
          {tr("按 1/2/3 打分后自动跳到下一个", "Advance automatically after rating with 1/2/3")}
        </label>
        {it.k === "s" && !it.ex && (
          <label className="mt-1 block text-[12.5px] text-[#c9d0d7]" title={tr("改用 MAST 数据页的交互谱图（可缩放、看全部通道）", "Use the interactive spectrum plot from the MAST data page (zoomable, all channels)")}>
            <input type="checkbox" checked={interactive} onChange={(e) => setInteractive(e.target.checked)} />{" "}
            {tr("谱图改用交互曲线", "Interactive spectrum plot")}
          </label>
        )}
        <LightboxFigureButton it={it} />
        <div className="mt-3 text-[11.5px] leading-relaxed text-[#8a939c]">
          {tr("←/→ 翻页 · 1 可用 · 2 重点 · 3 排除 · 0 清除", "←/→ page · 1 usable · 2 key · 3 exclude · 0 clear")}
          <br />
          {tr("Q W E R T Y U I O 开关对应标签 · N 写备注 · Esc 关闭", "Q W E R T Y U I O toggle the matching tag · N write note · Esc close")}
          <br />
          {tr(
            "S 选中/取消 · [ 定起点 · ] 选到这里（一段）→ 底部存为系列",
            "S select/deselect · [ set start · ] select up to here (a range) → save as series from the bottom bar",
          )}
          <br />
          {tr("谱/网格：A 系于前一张帧 · D 系于后一张帧", "Spectrum/grid: A anchor to previous frame · D anchor to next frame")}
          <br />
          <span className="text-[#6d7681]">{tr("改动自动存盘（顶栏显示保存状态）", "Changes are saved automatically (save state shown in the header)")}</span>
        </div>
      </aside>
    </div>
  );

  return createPortal(node, document.body);
}
