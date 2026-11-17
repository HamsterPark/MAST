// The keys the 3D view handles itself, on top of the editor's (keys.ts: V switches the view; the arrows, [ ],
// Delete and Ctrl+Z act on the selection in both views, the arrows along the table's x and y):
//
//   1 top   2 front   3 side   4 isometric   0 fit everything   5 perspective / orthographic
//
// as on Blender's keypad. Only a bare digit counts: with Ctrl, Cmd or Alt it is the browser's (switching tabs),
// and with Shift it is another character on most layouts. Pure (test/opticalLayout3dPick.test.ts).

import type { KeyLike } from "../keys.ts";
import type { ViewPreset } from "./views.ts";

export type ViewKey = { kind: "preset"; preset: ViewPreset } | { kind: "fit" } | { kind: "projection" };

const PRESETS: Record<string, ViewPreset> = { "1": "top", "2": "front", "3": "side", "4": "iso" };

export function view3dKey(e: KeyLike): ViewKey | null {
  if (e.ctrlKey || e.metaKey || e.altKey || e.shiftKey) return null;
  const preset = PRESETS[e.key];
  if (preset) return { kind: "preset", preset };
  if (e.key === "0") return { kind: "fit" };
  if (e.key === "5") return { kind: "projection" };
  return null;
}
