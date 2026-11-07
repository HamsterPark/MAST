// How a beam is drawn: the colour follows the wavelength, infrared and ultraviolet get a
// fixed colour and a dashed line (they cannot be seen, so they must not pose as a
// colour), and a branch that carries less than the full beam is drawn thinner and
// fainter (section 8 of docs/v2/design/optical_layout.md).
//
// The colour is the usual piecewise approximation of the visible spectrum, then moved
// into a luminance window that stays visible on the theme it is drawn on: yellow and
// cyan on a white page need to be darker than on a dark one, pure blue on near-black
// lighter.

export type ThemeName = "dark" | "light";

export type SpectralBand = "uv" | "visible" | "ir" | "unknown";

/** The visible range of the approximation (nm). */
export const VISIBLE_MIN_NM = 380;
export const VISIBLE_MAX_NM = 780;

export function spectralBand(nm: number | null | undefined): SpectralBand {
  if (typeof nm !== "number" || !Number.isFinite(nm) || nm <= 0) return "unknown";
  if (nm < VISIBLE_MIN_NM) return "uv";
  if (nm > VISIBLE_MAX_NM) return "ir";
  return "visible";
}

/** Fixed colours, one per theme, for the two invisible bands and for a ray without a wavelength. */
export const FIXED_COLORS: Record<Exclude<SpectralBand, "visible">, Record<ThemeName, string>> = {
  uv: { dark: "#a78bfa", light: "#6d28d9" },
  ir: { dark: "#f472b6", light: "#be185d" },
  unknown: { dark: "#cbd5e1", light: "#475569" },
};

/** Piecewise-linear approximation of the colour of a visible wavelength; each channel 0..1. */
export function visibleRgb(nm: number): [number, number, number] {
  let r = 0;
  let g = 0;
  let b = 0;
  if (nm < 440) {
    r = -(nm - 440) / (440 - 380);
    b = 1;
  } else if (nm < 490) {
    g = (nm - 440) / (490 - 440);
    b = 1;
  } else if (nm < 510) {
    g = 1;
    b = -(nm - 510) / (510 - 490);
  } else if (nm < 580) {
    r = (nm - 510) / (580 - 510);
    g = 1;
  } else if (nm < 645) {
    r = 1;
    g = -(nm - 645) / (645 - 580);
  } else {
    r = 1;
  }
  // the eye is less sensitive at both ends
  let f = 1;
  if (nm < 420) f = 0.3 + (0.7 * (nm - 380)) / (420 - 380);
  else if (nm > 700) f = 0.3 + (0.7 * (780 - nm)) / (780 - 700);
  const gamma = 0.8;
  const c = (v: number) => (v <= 0 ? 0 : (v * f) ** gamma);
  return [c(r), c(g), c(b)];
}

const hex = (v: number) =>
  Math.round(Math.min(1, Math.max(0, v)) * 255)
    .toString(16)
    .padStart(2, "0");

/** "#rrggbb" for a channel triple. */
export function toHex([r, g, b]: [number, number, number]): string {
  return `#${hex(r)}${hex(g)}${hex(b)}`;
}

type Rgb = [number, number, number];

const toLinear = (c: number) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
const fromLinear = (l: number) => (l <= 0.0031308 ? l * 12.92 : 1.055 * l ** (1 / 2.4) - 0.055);

/** WCAG relative luminance of an sRGB triple (0..1 per channel). */
export function relativeLuminance([r, g, b]: Rgb): number {
  return 0.2126 * toLinear(r) + 0.7152 * toLinear(g) + 0.0722 * toLinear(b);
}

/**
 * How bright a beam may be on each theme so that it stays visible against the page and
 * the table: at most this relative luminance on the light theme (yellow on white would
 * vanish), at least this on the dark one (pure blue on near-black would too). 0.27 and
 * 0.2 give a contrast of about 3:1 and 4:1 against the theme backgrounds
 * (test/opticalLayoutView.test.ts computes it against the real ones).
 */
export const LUMINANCE_LIMIT = { light: { max: 0.27 }, dark: { min: 0.2 } } as const;

/** Moves a colour into the luminance window of a theme without changing its hue. */
export function fitToTheme(rgb: Rgb, theme: ThemeName): Rgb {
  const lin = rgb.map(toLinear) as Rgb;
  const y = 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2];
  let out = lin;
  if (theme === "light" && y > LUMINANCE_LIMIT.light.max) {
    const f = LUMINANCE_LIMIT.light.max / y;
    out = [lin[0] * f, lin[1] * f, lin[2] * f];
  } else if (theme === "dark" && y < LUMINANCE_LIMIT.dark.min) {
    const t = (LUMINANCE_LIMIT.dark.min - y) / (1 - y);
    out = [lin[0] + t * (1 - lin[0]), lin[1] + t * (1 - lin[1]), lin[2] + t * (1 - lin[2])];
  }
  return [fromLinear(out[0]), fromLinear(out[1]), fromLinear(out[2])];
}

/** The colour of a wavelength on a theme. Invisible bands and unknown wavelengths use the fixed colours. */
export function wavelengthColor(nm: number | null | undefined, theme: ThemeName): string {
  const band = spectralBand(nm);
  if (band !== "visible") return FIXED_COLORS[band][theme];
  return toHex(fitToTheme(visibleRgb(nm as number), theme));
}

export interface BeamStyle {
  color: string;
  /** SVG stroke-dasharray, null for a solid line */
  dash: string | null;
  /** px */
  width: number;
  /** 0..1 */
  opacity: number;
}

/** The look of one branch: `fraction` is its share of the source's intensity (1 for the full beam). */
export function beamStyle(nm: number | null | undefined, fraction: number, theme: ThemeName): BeamStyle {
  const f = Math.min(1, Math.max(0, Number.isFinite(fraction) ? fraction : 1));
  const band = spectralBand(nm);
  return {
    color: wavelengthColor(nm, theme),
    dash: band === "uv" ? "2 4" : band === "ir" ? "9 5" : null,
    width: Math.round((1.2 + 1.8 * f) * 100) / 100,
    opacity: Math.round((0.35 + 0.65 * f) * 100) / 100,
  };
}

/** "532 nm" for a label; "—" for a ray without a wavelength. */
export function wavelengthLabel(nm: number | null | undefined): string {
  return typeof nm === "number" && Number.isFinite(nm) ? `${Number(nm.toFixed(2))} nm` : "—";
}
