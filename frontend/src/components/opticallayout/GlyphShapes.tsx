import { memo, useMemo } from "react";
import { paperShapes, type Fill, type Shape, type Tone, type Weight } from "@/lib/opticalLayout/glyphs";
import { wavelengthColor, type ThemeName } from "@/lib/opticalLayout/colors";
import { dashedPath, parseDash } from "@/lib/opticalLayout/dash";
import { PAPER_FONT } from "@/lib/opticalLayout/exportStyle";

// Draws a glyph (lib/opticalLayout/glyphs.ts) as SVG elements. Colours are the design
// tokens, so both themes work; a caller can recolour the line art (selected, warning,
// error) with `ink`. Strokes use vector-effect="non-scaling-stroke": the glyph is scaled
// from millimetres to pixels by its parent group, and a line must stay as thin on screen
// when zoomed in as when zoomed out.
//
// `halo` draws the same outline again, wide and translucent, under the glyph: the glow of a
// selected or hovered component, shaped like the component itself.
//
// Texts and dials are counter-rotated (the parent group turns by -angle and flips y), so they
// read upright whatever the component's angle; they scale with the zoom like the rest.
//
// The figure (`paper`) is read by programs that do not know vector-effect (PDF converters, slide
// and drawing programs, which would draw a 1 px line k times thicker): there every stroke width
// and dash is written in millimetres, px / k, and the texts and dials are the figure's
// (glyphs.ts paperShapes: all of them, at a readable size). The figure also does without what MuPDF (and
// so PDFs made with it) gets wrong: a dash pattern is drawn as separate dashes, a text names its font itself
// and is placed on its baseline. A polygon is a closed path everywhere (MuPDF leaves a <polygon> open).

const TONE: Record<Exclude<Tone, "wl">, string> = {
  ink: "var(--mast-text)",
  muted: "var(--mast-muted)",
  faint: "var(--mast-faint)",
  glass: "var(--mast-info)",
  none: "none",
};

const FILL: Record<Exclude<Fill, "wl" | "grey">, string> = {
  none: "none",
  body: "var(--mast-panel)",
  metal: "var(--mast-border)",
  glass: "var(--mast-info)",
  dark: "var(--mast-text)",
};

const FILL_OPACITY: Record<Fill, number> = { none: 1, body: 1, metal: 1, glass: 0.15, dark: 0.86, grey: 0.4, wl: 0.5 };

const WEIGHT: Record<Weight, number> = { hair: 0.75, fine: 1, normal: 1.35, thick: 2.3 };

interface Props {
  shapes: readonly Shape[];
  theme: ThemeName;
  /** pixels per millimetre the glyph is drawn at: details below their zoom are left out */
  k: number;
  /** the component's angle, to keep texts and dials upright */
  angle: number;
  /** replaces the colour of the line art (ink, glass and muted strokes, the face) */
  ink?: string;
  /** draw the glow under the glyph instead of the glyph */
  halo?: { color: string; opacity: number };
  /** the figure export: glass is outlined in ink like everything else (black line art) */
  paper?: boolean;
}

function strokeOf(s: Shape, theme: ThemeName, ink: string | undefined, paper: boolean): string {
  const tone = s.tone ?? "ink";
  if (tone === "wl") return s.wl !== undefined ? wavelengthColor(s.wl, theme) : TONE.glass;
  if (ink && tone !== "faint" && tone !== "none") return ink;
  if (paper && tone === "glass") return TONE.ink;
  return TONE[tone];
}

function fillOf(s: Shape, theme: ThemeName): { fill: string; opacity: number } {
  const f = s.fill ?? "none";
  const opacity = s.fo ?? FILL_OPACITY[f];
  if (f === "wl") return { fill: s.wl !== undefined ? wavelengthColor(s.wl, theme) : FILL.glass, opacity };
  // an absorber is black on either theme (a black anodised part on a dark table still looks black; its outline
  // shows it); an ND plate is a grey that gets more solid with its density
  if (f === "dark") return theme === "dark" ? { fill: "var(--mast-bg)", opacity: 1 } : { fill: FILL.dark, opacity };
  if (f === "grey") return { fill: theme === "dark" ? "var(--mast-muted)" : "var(--mast-text)", opacity };
  return { fill: FILL[f], opacity };
}

/**
 * How a stroke of `px` screen pixels is written in the glyph's millimetre frame: as is with vector-effect on the
 * screen, as px / k in the figure.
 */
function strokeIn(px: number, k: number, paper: boolean): { strokeWidth: number; vectorEffect?: "non-scaling-stroke" } {
  return paper ? { strokeWidth: px / k } : { strokeWidth: px, vectorEffect: "non-scaling-stroke" };
}

/** A dash pattern of screen pixels, in the glyph's frame (see strokeIn). */
function dashIn(dash: string | undefined, k: number, paper: boolean): string | undefined {
  if (!dash || !paper) return dash;
  return dash
    .trim()
    .split(/[\s,]+/)
    .map((v) => String(Math.round((Number(v) / k) * 10000) / 10000))
    .join(" ");
}

/** A closed polygon as path data (a <polygon> is left open by some viewers). */
const polyD = (pts: ReadonlyArray<readonly [number, number]>) => `M${pts.map(([x, y]) => `${x} ${y}`).join("L")}Z`;

/**
 * A dashed outline in the figure: the dashes as segments (px pattern, written in mm), or null when the shape has no
 * dash, or too many dashes (then stroke-dasharray stays).
 */
function paperDashes(pts: ReadonlyArray<{ x: number; y: number }>, dash: string | undefined, k: number): string | null {
  const pattern = parseDash(dash);
  return pattern ? dashedPath(pts, pattern, 1 / k, 4) : null;
}

/** The baseline of a text centred on its point (what dominant-baseline="central" does), in ems. */
const CENTRAL_BASELINE_EM = 0.35;

/** A dial's marks in its own upright frame (y down on the screen, so "up" is -y). */
function DialMarks({ r, deg, mark, stroke, width, k, paper }: { r: number; deg: number; mark: "axis" | "grid"; stroke: string; width: number; k: number; paper: boolean }) {
  const t = deg * (Math.PI / 180);
  const ux = Math.cos(t);
  const uy = -Math.sin(t);
  const common = { stroke, ...strokeIn(width, k, paper), strokeLinecap: "round" as const };
  if (mark === "axis") {
    const L = r * 0.92;
    return <line x1={-ux * L} y1={-uy * L} x2={ux * L} y2={uy * L} {...common} {...strokeIn(width * 1.5, k, paper)} />;
  }
  // parallel chords along the axis, like the slats of a picket fence
  const nx = -uy;
  const ny = ux;
  return (
    <>
      {[-0.5, 0, 0.5].map((o) => {
        const half = Math.sqrt(Math.max(0, 1 - o * o)) * r * 0.88;
        const cx = nx * o * r;
        const cy = ny * o * r;
        return <line key={o} x1={cx - ux * half} y1={cy - uy * half} x2={cx + ux * half} y2={cy + uy * half} {...common} />;
      })}
    </>
  );
}

export const GlyphShapes = memo(function GlyphShapes({ shapes: given, theme, k, angle, ink, halo, paper = false }: Props) {
  const upright = `scale(1 -1) rotate(${angle})`;
  const shapes = useMemo(() => (paper ? paperShapes(given, k) : given), [paper, given, k]);
  return (
    <>
      {shapes.map((s, i) => {
        if (s.min !== undefined && k < s.min) return null;
        if (halo) {
          if (s.k === "text" || s.k === "dial") return null;
          const width = WEIGHT[s.weight ?? "normal"] + 5;
          const glow = { fill: "none", stroke: halo.color, strokeOpacity: halo.opacity, ...strokeIn(width, k, paper), strokeLinejoin: "round" as const, strokeLinecap: "round" as const };
          switch (s.k) {
            case "line":
              return <line key={i} x1={s.x1} y1={s.y1} x2={s.x2} y2={s.y2} {...glow} />;
            case "rect":
              return <rect key={i} x={s.x} y={s.y} width={s.w} height={s.h} rx={s.rx} {...glow} />;
            case "poly":
              return <path key={i} d={polyD(s.pts)} {...glow} />;
            case "path":
              return <path key={i} d={s.d} {...glow} />;
            case "circle":
              return <circle key={i} cx={s.cx} cy={s.cy} r={s.r} {...glow} />;
          }
          return null;
        }
        const stroke = strokeOf(s, theme, ink, paper);
        const width = WEIGHT[s.weight ?? "normal"];
        const common = {
          stroke,
          ...strokeIn(width, k, paper),
          strokeOpacity: s.so,
          strokeDasharray: dashIn(s.dash, k, paper),
          strokeLinejoin: "round" as const,
          strokeLinecap: "round" as const,
        };
        switch (s.k) {
          case "line": {
            const dashes = paper ? paperDashes([{ x: s.x1, y: s.y1 }, { x: s.x2, y: s.y2 }], s.dash, k) : null;
            if (dashes !== null) return <path key={i} d={dashes} fill="none" {...common} strokeDasharray={undefined} />;
            return <line key={i} x1={s.x1} y1={s.y1} x2={s.x2} y2={s.y2} {...common} />;
          }
          case "rect": {
            const f = fillOf(s, theme);
            const dashes = paper
              ? paperDashes(
                  [
                    { x: s.x, y: s.y },
                    { x: s.x + s.w, y: s.y },
                    { x: s.x + s.w, y: s.y + s.h },
                    { x: s.x, y: s.y + s.h },
                    { x: s.x, y: s.y },
                  ],
                  s.dash,
                  k,
                )
              : null;
            if (dashes !== null) {
              return (
                <g key={i}>
                  <rect x={s.x} y={s.y} width={s.w} height={s.h} rx={s.rx} fill={f.fill} fillOpacity={f.opacity} stroke="none" />
                  <path d={dashes} fill="none" {...common} strokeDasharray={undefined} />
                </g>
              );
            }
            return <rect key={i} x={s.x} y={s.y} width={s.w} height={s.h} rx={s.rx} fill={f.fill} fillOpacity={f.opacity} {...common} />;
          }
          case "poly": {
            const f = fillOf(s, theme);
            return <path key={i} d={polyD(s.pts)} fill={f.fill} fillOpacity={f.opacity} {...common} />;
          }
          case "path": {
            const f = fillOf(s, theme);
            return <path key={i} d={s.d} fill={f.fill} fillOpacity={f.opacity} {...common} />;
          }
          case "circle": {
            const f = fillOf(s, theme);
            return <circle key={i} cx={s.cx} cy={s.cy} r={s.r} fill={f.fill} fillOpacity={f.opacity} {...common} />;
          }
          case "text":
            return paper ? (
              <text
                key={i}
                transform={`translate(${s.x} ${s.y}) ${upright}`}
                y={CENTRAL_BASELINE_EM * s.size}
                fontSize={s.size}
                textAnchor="middle"
                fill={stroke}
                fillOpacity={s.so}
                fontWeight={600}
                fontFamily={PAPER_FONT}
              >
                {s.text}
              </text>
            ) : (
              <text
                key={i}
                transform={`translate(${s.x} ${s.y}) ${upright}`}
                fontSize={s.size}
                textAnchor="middle"
                dominantBaseline="central"
                fill={stroke}
                fillOpacity={s.so}
                fontWeight={600}
                style={{ fontFamily: "inherit" }}
              >
                {s.text}
              </text>
            );
          case "dial":
            return (
              <g key={i} transform={`translate(${s.cx} ${s.cy}) ${upright}`}>
                <circle r={s.r} fill="var(--mast-panel)" stroke={stroke} {...strokeIn(WEIGHT.hair, k, paper)} />
                {paper ? (
                  <path d={paperDashes([{ x: -s.r, y: 0 }, { x: s.r, y: 0 }], "1.5 1.5", k) ?? `M${-s.r} 0H${s.r}`} fill="none" stroke="var(--mast-faint)" {...strokeIn(WEIGHT.hair, k, paper)} />
                ) : (
                  <line x1={-s.r} y1={0} x2={s.r} y2={0} stroke="var(--mast-faint)" {...strokeIn(WEIGHT.hair, k, paper)} strokeDasharray="1.5 1.5" />
                )}
                <DialMarks r={s.r} deg={s.deg} mark={s.mark} stroke={stroke} width={width} k={k} paper={paper} />
              </g>
            );
        }
      })}
    </>
  );
});
