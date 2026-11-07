// filtersStore: filter-bar state of the data gallery.
//
// One copy is shared by all views (switching from a directory to "All frames" keeps the bias,
// tag and other filters), persisted in localStorage["mast.gallery.filters"]; the search text is
// not persisted. Validation on read is in lib/gallery/filters.ts.

import { create } from "zustand";
import {
  FILTERS_KEY,
  parseStoredFilters,
  serializeFilters,
  type Filters,
} from "@/lib/gallery/filters";

function readInitial(): Filters {
  try {
    return parseStoredFilters(localStorage.getItem(FILTERS_KEY));
  } catch {
    return parseStoredFilters(null);
  }
}

interface FiltersStore {
  F: Filters;
  setF: (patch: Partial<Filters>) => void;
}

export const useFiltersStore = create<FiltersStore>()((set, get) => ({
  F: readInitial(),
  setF: (patch) => {
    const F = { ...get().F, ...patch };
    set({ F });
    try {
      localStorage.setItem(FILTERS_KEY, serializeFilters(F));
    } catch {
      /* Filters must work even when they cannot be remembered */
    }
  },
}));
