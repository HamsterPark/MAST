// A canvas cannot read CSS variables: read the computed values of the design
// tokens and hand them to uPlot / canvas. Callers re-read on theme change (put the
// useUiStore theme in the dependencies).
//
// Token names are always --mast-*; looking up tokens that do not exist (such as
// --accent / --border) would return the hard-coded fallback colour in both themes.

export function cssToken(name: string, fallback: string): string {
  if (typeof window === "undefined") return fallback;
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

export interface ChartTheme {
  /** Axis text. */
  axis: string;
  /** Grid lines (hairline, solid). */
  grid: string;
  tick: string;
  text: string;
  faint: string;
  accent: string;
  /** Chart background (used for empty canvas pixels and marker outlines). */
  surface: string;
  panel2: string;
  /** Fixed-order palette for multiple channels (--mast-series-1..6). */
  series: string[];
}

const SERIES_FALLBACK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#9085e9"];

export function readChartTheme(): ChartTheme {
  return {
    axis: cssToken("--mast-muted", "#9aa4b4"),
    grid: cssToken("--mast-border", "#2c3440"),
    tick: cssToken("--mast-border-strong", "#3a4555"),
    text: cssToken("--mast-text", "#e8ecf3"),
    faint: cssToken("--mast-faint", "#6b7588"),
    accent: cssToken("--mast-accent", "#28d0e6"),
    surface: cssToken("--mast-bg", "#0a0e13"),
    panel2: cssToken("--mast-panel-2", "#1b222c"),
    series: SERIES_FALLBACK.map((fb, i) => cssToken(`--mast-series-${i + 1}`, fb)),
  };
}

/**
 * Colour of channel i. Channels beyond the sixth do not get new hues (the 7th and
 * 1st cannot be told apart with colour-blindness) and are drawn light grey; the
 * legend still lists their names, so they are identified by name and value.
 */
export function seriesColor(theme: ChartTheme, i: number): string {
  return i < theme.series.length ? theme.series[i]! : theme.faint;
}

/** #rrggbb → [r, g, b] (for drawing ImageData). Falls back to the dark panel colour when unparsable. */
export function hexRgb(c: string): [number, number, number] {
  const m = /^#([0-9a-f]{6})$/i.exec(c.trim());
  if (!m) return [27, 34, 44];
  const n = Number.parseInt(m[1]!, 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

/** Make a colour semi-transparent (for history overlay lines). */
export function withAlpha(color: string, alpha: number): string {
  return `color-mix(in srgb, ${color} ${Math.round(alpha * 100)}%, transparent)`;
}
