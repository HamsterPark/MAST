// selectionStore: which items are selected in the data gallery.
//
// The pure logic is in lib/gallery/selection.ts; this is only the zustand binding. The selection
// set is replaced with a new Set on every change and cards subscribe to `s.ids.has(id)`, so only
// cards whose state really changed re-render. GalleryApp clears it on view change.

import { create } from "zustand";
import { pickClick, pickKey, type HasId, type SelectionState } from "@/lib/gallery/selection";

interface SelectionStore extends SelectionState {
  clear: () => void;
  setOne: (id: string, on: boolean) => void;
  click: (list: readonly HasId[], j: number, shift: boolean, checked: boolean) => void;
  key: (list: readonly HasId[], j: number, code: "KeyS" | "BracketLeft" | "BracketRight") => void;
  removeMany: (ids: readonly string[]) => void;
}

export const useSelectionStore = create<SelectionStore>()((set, get) => ({
  ids: new Set<string>(),
  lastId: null,
  startId: null,
  clear: () => set({ ids: new Set(), lastId: null, startId: null }),
  setOne: (id, on) => {
    const ids = new Set(get().ids);
    if (on) ids.add(id);
    else ids.delete(id);
    set({ ids });
  },
  click: (list, j, shift, checked) => set((st) => pickClick(st, list, j, shift, checked)),
  key: (list, j, code) => set((st) => pickKey(st, list, j, code)),
  removeMany: (drop) => {
    const ids = new Set(get().ids);
    for (const id of drop) ids.delete(id);
    set({ ids });
  },
}));
