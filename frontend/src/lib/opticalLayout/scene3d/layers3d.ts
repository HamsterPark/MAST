// Which layers of the Layers menu the 3D view draws. All of them but two, which only mean something on the flat
// canvas: the rulers along its edges, and the alignment read-outs (offset / incidence written next to each hit).
// In 3D the menu shows them greyed out, "2D only" (Toolbar.tsx). Pure.

import type { LayerId } from "../layers.ts";

export const LAYERS_2D_ONLY: readonly LayerId[] = ["rulers", "alignment"];

export function drawnIn3d(id: LayerId): boolean {
  return !LAYERS_2D_ONLY.includes(id);
}
