// Keyboard shortcuts of the layout editor, section 5 of
// docs/v2/design/optical_layout.md:
//
//   arrow keys   move the selection 1 mm   (Shift 0.1 mm, Alt 10 mm)
//   [  and  ]    rotate 1 degree           (Shift 0.1, Alt 15)
//   Delete       delete the selection
//   Ctrl+Z / Ctrl+Y   undo / redo
//
// "Up" means up on the screen, which is +y on the table. `[` turns counter-clockwise
// (the positive angle direction, as on the screen), `]` clockwise. Shift wins over Alt
// when both are held, so a doubtful chord gives the smaller step.
//
// Pure: a key event goes in, a command comes out; the page applies it. That keeps the
// step sizes testable without a browser (test/opticalLayoutKeys.test.ts).

import { MOVE_STEP_MM, ROTATE_STEP_DEG, stepSize } from "./geometry.ts";

export interface KeyLike {
  key: string;
  code?: string;
  ctrlKey?: boolean;
  metaKey?: boolean;
  shiftKey?: boolean;
  altKey?: boolean;
}

export type KeyCommand =
  | { kind: "move"; dx: number; dy: number }
  | { kind: "rotate"; deg: number }
  | { kind: "delete" }
  | { kind: "undo" }
  | { kind: "redo" }
  | { kind: "save" }
  | { kind: "select-all" }
  | { kind: "escape" };

export function interpretKey(e: KeyLike): KeyCommand | null {
  const mod = Boolean(e.ctrlKey || e.metaKey);
  const key = e.key;

  if (mod) {
    if (e.altKey) return null;
    const k = key.toLowerCase();
    if (k === "z") return e.shiftKey ? { kind: "redo" } : { kind: "undo" };
    if (k === "y") return { kind: "redo" };
    if (k === "s") return { kind: "save" };
    if (k === "a") return { kind: "select-all" };
    return null;
  }

  const size = stepSize({ shift: e.shiftKey, alt: e.altKey });
  const step = MOVE_STEP_MM[size];
  switch (key) {
    case "ArrowLeft":
      return { kind: "move", dx: -step, dy: 0 };
    case "ArrowRight":
      return { kind: "move", dx: step, dy: 0 };
    case "ArrowUp":
      return { kind: "move", dx: 0, dy: step };
    case "ArrowDown":
      return { kind: "move", dx: 0, dy: -step };
    case "Delete":
    case "Backspace":
      return { kind: "delete" };
    case "Escape":
      return { kind: "escape" };
  }

  // Shift turns [ into { on most layouts; the physical key is the reliable answer.
  if (key === "[" || key === "{" || e.code === "BracketLeft") return { kind: "rotate", deg: ROTATE_STEP_DEG[size] };
  if (key === "]" || key === "}" || e.code === "BracketRight") return { kind: "rotate", deg: -ROTATE_STEP_DEG[size] };
  return null;
}

/** The shape of a DOM element the check needs, so tests do not need a DOM. */
export interface TargetLike {
  tagName?: string;
  isContentEditable?: boolean;
}

/** Typing in a box must never move a component. */
export function isEditableTarget(t: TargetLike | null | undefined): boolean {
  if (!t) return false;
  if (t.isContentEditable) return true;
  const tag = (t.tagName ?? "").toUpperCase();
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
}

/** Editing keys the editor itself uses on a command, for the on-screen help. */
export function shortcutHelp(): Array<{ keys: string; zh: string; en: string }> {
  return [
    { keys: "← ↑ → ↓", zh: "移动 1 mm（Shift 0.1 mm，Alt 10 mm）", en: "Move 1 mm (Shift 0.1 mm, Alt 10 mm)" },
    { keys: "[  ]", zh: "旋转 1°，[ 逆时针（Shift 0.1°，Alt 15°）", en: "Rotate 1°, [ counter-clockwise (Shift 0.1°, Alt 15°)" },
    { keys: "Delete", zh: "删除所选器件", en: "Delete the selection" },
    { keys: "Ctrl+Z / Ctrl+Y", zh: "撤销 / 重做", en: "Undo / redo" },
    { keys: "Ctrl+S", zh: "保存", en: "Save" },
    { keys: "Esc", zh: "取消当前工具 / 取消选择", en: "Cancel the tool / clear the selection" },
  ];
}
