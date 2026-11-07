// The state of the layout editor as a pure reducer: the undo history, what the server
// last had, and what a refused save (409) leaves behind. The page wires it to
// useReducer; everything that can go wrong with saving is decided here, in code the unit
// tests can reach (test/opticalLayoutEditorState.test.ts).

import type { OpticalLayout } from "./types.ts";
import type { History } from "./history.ts";
import { canRedo, canUndo, commit, createHistory, redo, replacePresent, undo } from "./history.ts";

export interface Conflict {
  /** the revision the server has now, when it said so */
  currentRevision: number | null;
  /** who saved it and when, when the server said so */
  updatedBy?: string;
  updatedAt?: string;
  message: string;
}

export interface EditorState {
  history: History<OpticalLayout>;
  /** the layout as the server last had it (loaded or saved) */
  baseline: OpticalLayout;
  /** the revision our next save builds on: sent as `base_revision` */
  baseRevision: number;
  conflict: Conflict | null;
}

export type EditorAction =
  | { type: "load"; layout: OpticalLayout }
  | { type: "edit"; layout: OpticalLayout; key?: string; now?: number }
  | { type: "undo" }
  | { type: "redo" }
  /** the server accepted `sent` and answered `saved` */
  | { type: "saved"; sent: OpticalLayout; saved: OpticalLayout }
  | { type: "conflict"; conflict: Conflict }
  /** take the server's newest copy as the present; the local edits stay one undo away */
  | { type: "adopt-latest"; latest: OpticalLayout }
  | { type: "dismiss-conflict" };

export function initialEditorState(layout: OpticalLayout): EditorState {
  return { history: createHistory(layout), baseline: layout, baseRevision: layout.revision, conflict: null };
}

/** Keys that do not belong to the content: the server writes them. */
const SERVER_KEYS = new Set(["revision", "updated_at", "updated_by"]);

/** JSON with sorted keys, so two copies that differ only in key order compare equal. */
export function stableStringify(v: unknown): string {
  if (Array.isArray(v)) return `[${v.map(stableStringify).join(",")}]`;
  if (v !== null && typeof v === "object") {
    const o = v as Record<string, unknown>;
    return `{${Object.keys(o)
      .sort()
      .map((k) => `${JSON.stringify(k)}:${stableStringify(o[k])}`)
      .join(",")}}`;
  }
  return JSON.stringify(v) ?? "null";
}

/** The two layouts say the same thing (revision, time and author aside). */
export function sameContent(a: OpticalLayout, b: OpticalLayout): boolean {
  if (a === b) return true;
  const strip = (l: OpticalLayout) => {
    const o: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(l)) if (!SERVER_KEYS.has(k)) o[k] = v;
    return stableStringify(o);
  };
  return strip(a) === strip(b);
}

export function isDirty(state: EditorState): boolean {
  return !sameContent(state.history.present, state.baseline);
}

export function editorReducer(state: EditorState, action: EditorAction): EditorState {
  switch (action.type) {
    case "load":
      return initialEditorState(action.layout);
    case "edit": {
      const history = commit(state.history, action.layout, { key: action.key, now: action.now });
      return history === state.history ? state : { ...state, history };
    }
    case "undo": {
      const history = undo(state.history);
      return history === state.history ? state : { ...state, history };
    }
    case "redo": {
      const history = redo(state.history);
      return history === state.history ? state : { ...state, history };
    }
    case "saved": {
      // Edits made while the request was in flight stay: only when the present is still
      // what was sent does the server's copy replace it.
      const untouched = Object.is(state.history.present, action.sent);
      const present = untouched
        ? action.saved
        : {
            ...state.history.present,
            revision: action.saved.revision,
            updated_at: action.saved.updated_at,
            updated_by: action.saved.updated_by,
          };
      return {
        history: replacePresent(state.history, present),
        baseline: action.saved,
        baseRevision: action.saved.revision,
        conflict: null,
      };
    }
    case "conflict":
      return { ...state, conflict: action.conflict };
    case "adopt-latest": {
      const history = commit(state.history, action.latest, { key: undefined });
      return { history, baseline: action.latest, baseRevision: action.latest.revision, conflict: null };
    }
    case "dismiss-conflict":
      return state.conflict ? { ...state, conflict: null } : state;
  }
}

export { canRedo, canUndo };
