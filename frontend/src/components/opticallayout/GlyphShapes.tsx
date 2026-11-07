import { memo } from "react";
import type { Fill, Shape, Tone, Weight } from "@/lib/opticalLayout/glyphs";

// Draws a glyph (lib/opticalLayout/glyphs.ts) as SVG elements. Colours are the design
// tokens, so both themes work; a caller can recolour the strong strokes (selected,
// warning, error) with `ink`. Strokes use vector-effect="non-scaling-stroke": the glyph is
// scaled from millimetres to pixels by its parent group, and a line must stay as thin on
// screen when zoomed in as when zoomed out.

const TONE: Record<Tone, string> = {
  main: "var(--mast-text)",
  muted: "var(--mast-muted)",
  accent: "var(--mast-accent)",
};

const FILL: Record<Fill, string> = {
  none: "none",
  body: "var(--mast-panel)",
  tint: "var(--mast-accent-soft)",
  ink: "var(--mast-text)",
};

const WEIGHT: Record<Weight, number> = { hair: 0.9, normal: 1.4, thick: 2.4 };

interface Props {
  shapes: readonly Shape[];
  /** replaces the colour of the "main" and "accent" strokes and of "ink" fills */
  ink?: string;
}

function paint(s: { fill?: Fill; weight?: Weight; tone?: Tone }, ink: string | undefined) {
  const tone = s.tone ?? "main";
  const stroke = ink && (tone === "main" || tone === "accent") ? ink : TONE[tone];
  const fillKey = s.fill ?? "none";
  const fill = fillKey === "ink" && ink ? ink : FILL[fillKey];
  return { stroke, fill, strokeWidth: WEIGHT[s.weight ?? "normal"] };
}

export const GlyphShapes = memo(function GlyphShapes({ shapes, ink }: Props) {
  return (
    <>
      {shapes.map((s, i) => {
        const p = paint(s, ink);
        const common = { stroke: p.stroke, strokeWidth: p.strokeWidth, vectorEffect: "non-scaling-stroke" as const, strokeLinejoin: "round" as const, strokeLinecap: "round" as const };
        switch (s.k) {
          case "line":
            return <line key={i} x1={s.x1} y1={s.y1} x2={s.x2} y2={s.y2} {...common} />;
          case "rect":
            return <rect key={i} x={s.x} y={s.y} width={s.w} height={s.h} fill={p.fill} {...common} />;
          case "poly":
            return <polygon key={i} points={s.pts.map(([x, y]) => `${x},${y}`).join(" ")} fill={p.fill} {...common} />;
          case "path":
            return <path key={i} d={s.d} fill={p.fill} {...common} />;
          case "circle":
            return <circle key={i} cx={s.cx} cy={s.cy} r={s.r} fill={p.fill} {...common} />;
        }
      })}
    </>
  );
});
