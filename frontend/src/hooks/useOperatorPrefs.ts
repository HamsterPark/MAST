import { useSyncExternalStore } from "react";
import { OPERATOR_NAME_KEY, OPERATOR_NAME_MAX, readOperatorName, writeOperatorName } from "@/lib/operatorPrompts";

// Two small per-browser settings shared by every part of the operator UI (the alert host
// in the app shell, the prompt board, the optical-state panel): who is answering, and
// whether a new prompt should beep. A module-level store + useSyncExternalStore keeps all
// of them showing the same value, and the `storage` event keeps two tabs in step.

interface Store<T> {
  get: () => T;
  set: (v: T) => void;
  subscribe: (cb: () => void) => () => void;
}

function createStore<T>(key: string, read: () => T, write: (v: T) => void): Store<T> {
  let value: T | undefined;
  const subs = new Set<() => void>();
  const emit = () => subs.forEach((f) => f());
  if (typeof window !== "undefined") {
    window.addEventListener("storage", (e) => {
      if (e.key === key) {
        value = read();
        emit();
      }
    });
  }
  return {
    get: () => (value === undefined ? (value = read()) : value),
    set: (v) => {
      value = v;
      write(v);
      emit();
    },
    subscribe: (cb) => {
      subs.add(cb);
      return () => void subs.delete(cb);
    },
  };
}

// The box holds what is typed (a trailing space is part of typing "Ana Ruiz"); what is
// stored and sent is the tidied name (cleanOperatorName).
const nameStore = createStore<string>(
  OPERATOR_NAME_KEY,
  () => readOperatorName(),
  (v) => void writeOperatorName(v),
);

/** The operator's name for this browser, remembered between visits. */
export function useOperatorName(): [string, (name: string) => void] {
  const name = useSyncExternalStore(nameStore.subscribe, nameStore.get, () => "");
  return [name, (n) => nameStore.set(n.slice(0, OPERATOR_NAME_MAX))];
}

export const OPERATOR_SOUND_KEY = "mast.operatorSound";

const soundStore = createStore<boolean>(
  OPERATOR_SOUND_KEY,
  () => {
    try {
      return window.localStorage.getItem(OPERATOR_SOUND_KEY) !== "0";
    } catch {
      return true;
    }
  },
  (v) => {
    try {
      window.localStorage.setItem(OPERATOR_SOUND_KEY, v ? "1" : "0");
    } catch {
      /* storage off: the choice holds for this page load */
    }
  },
);

/** Whether a new prompt beeps (on by default). */
export function useOperatorSound(): [boolean, (on: boolean) => void] {
  const on = useSyncExternalStore(soundStore.subscribe, soundStore.get, () => true);
  return [on, (v) => soundStore.set(v)];
}
