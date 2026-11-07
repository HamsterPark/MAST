// ════════════════════════════════════════════════════════════════════════════
// marksStore: marks of the data gallery.
//
// The server-side marks.json is the single source of truth; this is its browser mirror plus the
// save driver:
//   * every change is applied to the local document first (the UI updates at once) and also
//     enters the pending queue;
//   * a batch is sent after a 350 ms debounce, with **one request in flight at a time**, so the
//     server receives them in order;
//   * a failed batch is put back in the queue (without overwriting newer edits made meanwhile)
//     and retried after 5 s;
//   * pending is also written to localStorage["mast.gallery.pending"], so after closing the
//     page, a server restart or a network drop, the next visit applies it and resends;
//   * when the page becomes visible again with nothing pending, `rev` tells whether the server
//     copy changed.
//
// Cards subscribe by id (`useMarksStore(s => s.doc.items[id])`): every change yields a new
// document object, but an unchanged entry keeps its object reference, so editing one entry
// re-renders one card.
//
// `epoch` is incremented only when the **whole document is replaced by the server version**
// (load, changed by another tab, after an import). List views recompute their filters from it;
// per-item edits made on this page do not touch it, so the list is not re-sorted while paging
// and rating (otherwise pressing ✗ under "Hide ✗" would remove the card at once and the next
// one would jump under the pointer).
// ════════════════════════════════════════════════════════════════════════════

import { create } from "zustand";
import { api } from "@/api/client";
import type { components } from "@/api/schema";
import type { DirMark, GalleryItem, Mark, MarksDoc, Patch, Series } from "@/lib/gallery/types";
import {
  PENDING_KEY,
  applyPatchLocal,
  composeMark,
  emptyPatch,
  mergePatch,
  normaliseDoc,
  parsePending,
  patchEmpty,
  requeue,
  toggledTags,
  tsClock,
  withDir,
  withMark,
  withSeries,
} from "@/lib/gallery/marks";
import { nowStr } from "@/lib/gallery/format";
import { tr } from "@/lib/i18n";

/**
 * loading: server document not yet received · idle: connected, nothing saved yet · pending: saving ·
 * ok: saved · bad: save failed (changes are kept in the browser and will be resent) ·
 * offline: marks service unavailable
 */
export type SaveState = "loading" | "idle" | "pending" | "ok" | "bad" | "offline";

interface MarksState {
  loaded: boolean;
  online: boolean;
  doc: MarksDoc;
  pending: Patch;
  saveState: SaveState;
  savedAt: string;
  busy: boolean;
  again: boolean;
  epoch: number;
  init: () => Promise<void>;
  setMark: (id: string, m: Mark | null) => void;
  updateMark: (it: GalleryItem, patch: Partial<Mark>) => void;
  toggleTag: (it: GalleryItem, t: string, on?: boolean) => void;
  setSeries: (sid: string, s: Series | null) => void;
  setDay: (d: string, v: DirMark) => void;
  setTags: (tags: string[]) => void;
  flush: () => Promise<void>;
  refresh: () => Promise<boolean>;
  reload: () => Promise<void>;
}

const nextTs = tsClock();
let timer: ReturnType<typeof setTimeout> | null = null;
let initPromise: Promise<void> | null = null;
/** Retry timer for when the service is not up; keep only one so repeated component mounts do not stack several. */
let retryTimer: ReturnType<typeof setTimeout> | null = null;

function lsGet(k: string): string | null {
  try {
    return localStorage.getItem(k);
  } catch {
    return null;
  }
}
function lsSet(k: string, v: string): void {
  try {
    localStorage.setItem(k, v);
  } catch {
    /* Ignore failures to store: the page must still work in private mode */
  }
}
function lsDel(k: string): void {
  try {
    localStorage.removeItem(k);
  } catch {
    /* Same as above */
  }
}

/** Merge a batch of changes into the unsaved changes kept in localStorage. */
function persistPending(p: Patch): void {
  if (patchEmpty(p)) return;
  lsSet(PENDING_KEY, JSON.stringify(mergePatch(parsePending(lsGet(PENDING_KEY)) ?? emptyPatch(), p)));
}

async function fetchDoc(): Promise<MarksDoc | null> {
  try {
    const { data, error } = await api.GET("/api/gallery/marks");
    if (error || !data || (data as { degraded?: boolean }).degraded) return null;
    return normaliseDoc(data);
  } catch {
    return null;
  }
}

/** The request body: when tags is null the key is omitted (meaning "tag table unchanged"). */
function requestBody(p: Patch): components["schemas"]["GalleryMarksPatch"] {
  const body: Record<string, unknown> = { items: p.items, series: p.series, days: p.days };
  if (p.tags) body.tags = p.tags;
  // The generated GalleryMark marks fields with defaults as required; delete entries {del, ts}
  // and marks with only some keys are both valid on the wire (the server uses exclude_unset).
  // See the top of lib/gallery/types.ts.
  return body as unknown as components["schemas"]["GalleryMarksPatch"];
}

export const useMarksStore = create<MarksState>()((set, get) => {
  const schedule = () => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => void get().flush(), 350);
    persistPending(get().pending);
    set({ saveState: "pending" });
  };

  return {
    loaded: false,
    online: false,
    doc: normaliseDoc(null),
    pending: emptyPatch(),
    saveState: "loading",
    savedAt: "",
    busy: false,
    again: false,
    epoch: 0,

    init: () => {
      if (initPromise) return initPromise;
      if (get().online) return Promise.resolve();
      initPromise = (async () => {
        const fetched = await fetchDoc();
        if (!fetched) {
          set({ loaded: true, online: false, saveState: "offline" });
          initPromise = null;
          // Service not up: retry in 5 s. Changes made meanwhile still enter the queue and localStorage.
          if (!retryTimer) {
            retryTimer = setTimeout(() => {
              retryTimer = null;
              void get().init();
            }, 5000);
          }
          return;
        }
        let doc = fetched;
        let pending = get().pending;
        const stored = parsePending(lsGet(PENDING_KEY));
        if (stored) {
          // Changes that were not saved last time: apply to the UI first, then merge into the queue and resend.
          doc = applyPatchLocal(doc, stored);
          pending = mergePatch(stored, pending);
        }
        if (!patchEmpty(get().pending)) doc = applyPatchLocal(doc, get().pending);
        set((s) => ({
          doc,
          pending,
          loaded: true,
          online: true,
          epoch: s.epoch + 1,
          savedAt: fetched.updated ?? "",
          saveState: patchEmpty(pending) ? (fetched.updated ? "ok" : "idle") : "pending",
        }));
        if (!patchEmpty(pending)) void get().flush();
      })();
      return initPromise;
    },

    setMark: (id, m) => {
      const entry = m ?? { del: true as const, ts: nextTs() };
      set((s) => ({
        doc: withMark(s.doc, id, m),
        pending: { ...s.pending, items: { ...s.pending.items, [id]: entry } },
      }));
      schedule();
    },

    updateMark: (it, patch) => {
      const m = composeMark(get().doc.items[it.id], patch, it, nowStr(), nextTs());
      get().setMark(it.id, m);
    },

    toggleTag: (it, t, on) => {
      const tags = toggledTags(get().doc.items[it.id]?.tags ?? [], t, on);
      if (tags) get().updateMark(it, { tags });
    },

    setSeries: (sid, s) => {
      const stamped = s ? { ...s, t: nowStr(), ts: nextTs() } : null;
      const entry = stamped && stamped.ids.length ? stamped : { del: true as const, ts: nextTs() };
      set((st) => ({
        doc: withSeries(st.doc, sid, stamped),
        pending: { ...st.pending, series: { ...st.pending.series, [sid]: entry } },
      }));
      schedule();
    },

    setDay: (d, v) => {
      const vv: DirMark = { ...v, ts: nextTs() };
      set((st) => ({
        doc: withDir(st.doc, d, vv),
        pending: { ...st.pending, days: { ...st.pending.days, [d]: vv } },
      }));
      schedule();
    },

    setTags: (tags) => {
      set((st) => ({ doc: { ...st.doc, tags }, pending: { ...st.pending, tags } }));
      schedule();
    },

    flush: async () => {
      if (timer) {
        clearTimeout(timer);
        timer = null;
      }
      if (get().busy) {
        set({ again: true });
        return;
      }
      const body = get().pending;
      if (patchEmpty(body)) return;
      set({ pending: emptyPatch(), busy: true, saveState: "pending" });
      persistPending(body);
      try {
        const { data, error } = await api.POST("/api/gallery/marks/patch", { body: requestBody(body) });
        if (error || !data || !data.ok) throw new Error(data?.detail || "patch failed");
        set((s) => ({
          doc: { ...s.doc, rev: data.rev, updated: data.updated },
          saveState: "ok",
          savedAt: data.updated,
          online: true,
        }));
        if (patchEmpty(get().pending)) lsDel(PENDING_KEY);
      } catch {
        set((s) => ({ pending: requeue(s.pending, body), saveState: "bad" }));
        if (timer) clearTimeout(timer);
        timer = setTimeout(() => void get().flush(), 5000);
      } finally {
        set({ busy: false });
        if (get().again) {
          set({ again: false });
          void get().flush();
        }
      }
    },

    refresh: async () => {
      const st = get();
      if (!st.online || st.busy || !patchEmpty(st.pending)) return false;
      const d = await fetchDoc();
      if (!d || d.rev === get().doc.rev) return false;
      if (!patchEmpty(get().pending) || get().busy) return false; // Edited while fetching: do not overwrite
      set((s) => ({ doc: d, savedAt: d.updated ?? s.savedAt, epoch: s.epoch + 1 }));
      return true;
    },

    reload: async () => {
      const d = await fetchDoc();
      if (!d) return;
      set((s) => ({
        doc: applyPatchLocal(d, s.pending),
        savedAt: d.updated ?? s.savedAt,
        online: true,
        epoch: s.epoch + 1,
      }));
    },
  };
});

/** Save-state text for the header. */
export function saveText(state: SaveState, savedAt: string): string {
  switch (state) {
    case "ok":
      return tr(`已存盘 ${(savedAt || "").slice(11)} → marks.json`, `Saved ${(savedAt || "").slice(11)} → marks.json`);
    case "pending":
      return tr("保存中…", "Saving…");
    case "bad":
      return tr("⚠ 没存上，改动先记在浏览器，服务恢复后自动补存", "⚠ Not saved; changes are kept in the browser and saved automatically once the service recovers");
    case "offline":
      return tr("⚠ 标记服务不可用：改动先记在浏览器，连上后自动补存", "⚠ Marks service unavailable; changes are kept in the browser and saved automatically once connected");
    case "idle":
      return tr("已连上存盘服务 → marks.json", "Connected to the save service → marks.json");
    default:
      return tr("读取标记…", "Reading marks…");
  }
}
