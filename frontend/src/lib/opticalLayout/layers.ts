// What the canvas draws: the layers of the "Layers" menu (docs/v2/design/optical_layout_v2.md section 8.5).
//
// The choice is kept in localStorage. A stored choice outlives the code that wrote it: a layer added later
// (v2 added texts, envelope, polarisation and power to v1's seven) would arrive as `undefined` and read as
// OFF, so an operator who used the editor once would never see the new layer and would have no way to know
// it existed. So the stored value is read key by key over the defaults (the same rule as the scan map's
// `withLayerDefaults`, docs/v2/design/scan_map_plan_display.md section 3.5): a missing or malformed key
// is the default, a stored boolean wins. Pure, so the merge is tested (test/opticalLayoutLayers.test.ts).

import { tr } from "../i18n.ts";

export const LAYER_IDS = ["holes", "grid", "rulers", "labels", "texts", "beams", "envelope", "polarization", "power", "alignment", "comments"] as const;

export type LayerId = (typeof LAYER_IDS)[number];
export type Layers = Record<LayerId, boolean>;

/** Everything on: a new layer is seen first and switched off by whoever does not want it. */
export const DEFAULT_LAYERS: Readonly<Layers> = Object.freeze({
  holes: true,
  grid: true,
  rulers: true,
  labels: true,
  texts: true,
  beams: true,
  envelope: true,
  polarization: true,
  power: true,
  alignment: true,
  comments: true,
});

/** The stored choice over the defaults, key by key; anything that is not a boolean is the default. */
export function withLayerDefaults(stored: unknown): Layers {
  const out: Layers = { ...DEFAULT_LAYERS };
  if (typeof stored !== "object" || stored === null || Array.isArray(stored)) return out;
  const s = stored as Record<string, unknown>;
  for (const id of LAYER_IDS) {
    const v = s[id];
    if (typeof v === "boolean") out[id] = v;
  }
  return out;
}

/** The layers that draw something about the beams, and so need the beams on. */
export const BEAM_LAYERS: readonly LayerId[] = ["envelope", "polarization", "power", "alignment"];

/** Whether a layer is actually drawn: a beam layer also needs the beams. */
export function layerShown(layers: Layers, id: LayerId): boolean {
  if (!layers[id]) return false;
  return BEAM_LAYERS.includes(id) ? layers.beams : true;
}

export function layerLabel(id: LayerId): string {
  switch (id) {
    case "holes":
      return tr("孔位", "Holes");
    case "grid":
      return tr("网格", "Grid");
    case "rulers":
      return tr("标尺", "Rulers");
    case "labels":
      return tr("器件名称", "Component names");
    case "texts":
      return tr("文字标注", "Text notes");
    case "beams":
      return tr("光束", "Beams");
    case "envelope":
      return tr("光斑包络（1/e²）", "Beam envelope (1/e²)");
    case "polarization":
      return tr("偏振记号", "Polarisation marks");
    case "power":
      return tr("功率标注", "Power labels");
    case "alignment":
      return tr("对准指示（偏离 / 入射角）", "Alignment (offset / incidence)");
    case "comments":
      return tr("评论标记", "Comment pins");
  }
}

export interface LayerGroup {
  key: string;
  label: () => string;
  layers: readonly LayerId[];
}

/** The menu, in groups: the table, the components, the light. */
export const LAYER_GROUPS: readonly LayerGroup[] = [
  { key: "table", label: () => tr("台面", "Table"), layers: ["holes", "grid", "rulers"] },
  { key: "parts", label: () => tr("器件与文字", "Components and text"), layers: ["labels", "texts", "comments"] },
  { key: "light", label: () => tr("光", "Light"), layers: ["beams", "envelope", "polarization", "power", "alignment"] },
];
