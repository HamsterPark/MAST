import { PAPER_FONT } from "@/lib/opticalLayout/exportStyle";
import { HEIGHT_TRIANGLE_EM, type HeightSub } from "@/lib/opticalLayout/textPlan";

// The height mark under a component's name (docs/v2/design/optical_layout_3d.md section 7): "z 120" for a component
// whose axis is not at the table's beam height, "z 75→250" for a periscope that goes up or down, with a ▲ / ▼ before it.
// The triangle is a path, not a character, so it looks the same in every font and every viewer of the exported SVG.
// The text has its own halo, the way the names have theirs: on the screen one element with paint-order; in the figure
// (paper) a stroked copy without fill under the text, and the font named on the text itself (MuPDF and slide
// programs ignore paint-order and do not take a font from the group).

const HALO_STROKE = "var(--mast-panel)";

export function HeightMarkText({ x, y, sub, paper }: { x: number; y: number; sub: HeightSub; paper: boolean }) {
  const px = sub.px;
  const tri = sub.rise !== 0 ? Math.ceil(px * HEIGHT_TRIANGLE_EM) : 0;
  const tx = x + tri;
  const font = paper ? { fontFamily: PAPER_FONT } : {};
  // a triangle as tall as a capital letter, standing on the baseline (▲) or hanging from the cap height (▼)
  const s = px * 0.62;
  const cx = x + s / 2 + px * 0.05;
  const up = `M${cx - s / 2} ${y}L${cx + s / 2} ${y}L${cx} ${y - s}Z`;
  const down = `M${cx - s / 2} ${y - s}L${cx + s / 2} ${y - s}L${cx} ${y}Z`;
  return (
    <g data-height-mark="" pointerEvents="none">
      {sub.rise !== 0 && <path d={sub.rise > 0 ? up : down} fill="var(--mast-muted)" stroke={HALO_STROKE} strokeWidth={1} strokeLinejoin="round" />}
      {paper ? (
        <>
          <text x={tx} y={y} fontSize={px} {...font} fill="none" stroke={HALO_STROKE} strokeWidth={3} strokeLinejoin="round" aria-hidden="true">
            {sub.text}
          </text>
          <text x={tx} y={y} fontSize={px} {...font} fill="var(--mast-muted)">
            {sub.text}
          </text>
        </>
      ) : (
        <text x={tx} y={y} fontSize={px} fill="var(--mast-muted)" paintOrder="stroke" stroke={HALO_STROKE} strokeWidth={3} strokeLinejoin="round">
          {sub.text}
        </text>
      )}
    </g>
  );
}
