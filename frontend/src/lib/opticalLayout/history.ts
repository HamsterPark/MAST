// Undo / redo over immutable snapshots (section 5: at least 100 steps).
//
// Generic in the snapshot type so it can be tested with plain numbers. Layout edits are
// immutable and share the components they do not touch, so keeping 200 snapshots costs
// far less than 200 full copies.
//
// Consecutive edits of the same kind (holding an arrow key, nudging a component with
// [ and ]) pass the same `key` and fold into one step while they follow each other
// within COALESCE_WINDOW_MS; otherwise a held key would use up the whole undo stack
// in two seconds.

export const DEFAULT_HISTORY_LIMIT = 200;
/** What the spec asks for; the limit may never be set below it. */
export const MIN_UNDO_STEPS = 100;
export const COALESCE_WINDOW_MS = 1000;

export interface History<T> {
  past: T[];
  present: T;
  future: T[];
  limit: number;
  /** key of the last commit, which the next commit may merge into */
  lastKey: string | null;
  lastAt: number;
}

export function createHistory<T>(present: T, limit: number = DEFAULT_HISTORY_LIMIT): History<T> {
  return { past: [], present, future: [], limit: Math.max(MIN_UNDO_STEPS, Math.floor(limit)), lastKey: null, lastAt: 0 };
}

export interface CommitOptions {
  /** edits with the same key that follow each other closely become one undo step */
  key?: string;
  now?: number;
}

/** Makes `next` the present. The same snapshot again is not a step. A new edit drops the redo stack. */
export function commit<T>(h: History<T>, next: T, opts: CommitOptions = {}): History<T> {
  if (Object.is(next, h.present)) return h;
  const now = opts.now ?? Date.now();
  const key = opts.key ?? null;
  if (key !== null && key === h.lastKey && now - h.lastAt < COALESCE_WINDOW_MS && h.past.length > 0) {
    return { ...h, present: next, future: [], lastAt: now };
  }
  const past = [...h.past, h.present];
  if (past.length > h.limit) past.splice(0, past.length - h.limit);
  return { ...h, past, present: next, future: [], lastKey: key, lastAt: now };
}

export function canUndo<T>(h: History<T>): boolean {
  return h.past.length > 0;
}

export function canRedo<T>(h: History<T>): boolean {
  return h.future.length > 0;
}

export function undo<T>(h: History<T>): History<T> {
  const prev = h.past[h.past.length - 1];
  if (h.past.length === 0 || prev === undefined) return h;
  return { ...h, past: h.past.slice(0, -1), present: prev, future: [h.present, ...h.future], lastKey: null };
}

export function redo<T>(h: History<T>): History<T> {
  const next = h.future[0];
  if (h.future.length === 0 || next === undefined) return h;
  return { ...h, past: [...h.past, h.present], present: next, future: h.future.slice(1), lastKey: null };
}

/** Swaps the present without making a step (for example the server's copy right after a save). */
export function replacePresent<T>(h: History<T>, next: T): History<T> {
  return Object.is(next, h.present) ? h : { ...h, present: next, lastKey: null };
}
