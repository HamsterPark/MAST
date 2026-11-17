// Colours of the 3D view, per theme. The 2D canvas takes its colours from the design tokens (index.css); the
// 3D view needs plain colours for its materials, so the scene's own ones are here, chosen to sit with the
// tokens (the table is the panel colour, the background the page colour; the view reads those two from the
// page itself when it can, see View3D.tsx). Beams keep the colours of colors.ts.
//
// Hardware is drawn as it looks on an optical table, toned for the screen: anodised mounts darker than the
// steel posts, glass a pale blue that stays see-through, absorbers near black, mirror coatings bright.

import type { ThemeName } from "../colors.ts";
import { wavelengthColor } from "../colors.ts";
import type { Tint } from "./parts.ts";

export interface Palette3D {
  background: string;
  tableTop: string;
  tableSide: string;
  holes: string;
  grid: string;
  /** what an outline of a selected component is drawn in (the accent of the theme) */
  selection: string;
  hover: string;
  locked: string;
  /** the opacity of see-through parts */
  glassOpacity: number;
  tints: Record<Exclude<Tint, "wl" | "nd">, string>;
}

export const PALETTES: Record<ThemeName, Palette3D> = {
  dark: {
    background: "#0a0e13",
    tableTop: "#1f2630",
    tableSide: "#151b23",
    holes: "#0b0f14",
    grid: "#3a4555",
    selection: "#28d0e6",
    hover: "#9aa4b4",
    locked: "#fbbf24",
    glassOpacity: 0.32,
    tints: {
      base: "#4a5361",
      post: "#a7b0bc",
      mount: "#3b4350",
      body: "#56606f",
      metal: "#8b95a3",
      mirror: "#dfe6ee",
      grating: "#c7b27a",
      glass: "#8cc4ff",
      window: "#b9dcff",
      polarizer: "#7f9bb8",
      coating: "#e6edf5",
      pbs: "#f2c46d",
      dark: "#15191f",
      sensor: "#22303f",
      ink: "#0d1117",
      port: "#0d1117",
      sample: "#9aa6b5",
      lamp: "#ffe7a3",
      housing: "#7d8897",
    },
  },
  light: {
    background: "#f4f6f9",
    tableTop: "#dde2ea",
    tableSide: "#b9c1cd",
    holes: "#8f98a6",
    grid: "#aab3c2",
    selection: "#0b6f83",
    hover: "#52596b",
    locked: "#b45309",
    glassOpacity: 0.38,
    tints: {
      base: "#5b6472",
      post: "#9aa3ae",
      mount: "#3f4752",
      body: "#68717f",
      metal: "#8a939f",
      mirror: "#f3f6fa",
      grating: "#b39a55",
      glass: "#5aa3e6",
      window: "#9ccbf2",
      polarizer: "#5c7c9c",
      coating: "#e9eef4",
      pbs: "#e0a434",
      dark: "#1d2128",
      sensor: "#2c3a4a",
      ink: "#161a21",
      port: "#161a21",
      sample: "#8c98a8",
      lamp: "#ffd36b",
      housing: "#8c96a3",
    },
  },
};

/** "#rrggbb" → [r, g, b] in 0..1 (sRGB). */
export function hexRgb(hex: string): [number, number, number] {
  const m = /^#([0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) return [0.5, 0.5, 0.5];
  const n = parseInt(m[1]!, 16);
  return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255];
}

export function rgbHex([r, g, b]: [number, number, number]): string {
  const h = (v: number) =>
    Math.round(Math.min(1, Math.max(0, v)) * 255)
      .toString(16)
      .padStart(2, "0");
  return `#${h(r)}${h(g)}${h(b)}`;
}

/** a + (b - a) · t, per channel, in sRGB (good enough for a tint). */
export function mixHex(a: string, b: string, t: number): string {
  const x = hexRgb(a);
  const y = hexRgb(b);
  return rgbHex([x[0] + (y[0] - x[0]) * t, x[1] + (y[1] - x[1]) * t, x[2] + (y[2] - x[2]) * t]);
}

/** The colour of a part. */
export function tintColor(p: { tint: Tint; wl?: number; density?: number }, theme: ThemeName): string {
  const pal = PALETTES[theme];
  if (p.tint === "wl") return p.wl !== undefined ? wavelengthColor(p.wl, theme) : pal.tints.glass;
  // an ND filter is greyer the denser it is (the same density as its 2D drawing)
  if (p.tint === "nd") return mixHex(pal.tints.window, pal.tints.dark, Math.min(1, Math.max(0, p.density ?? 0.22)));
  return pal.tints[p.tint];
}
