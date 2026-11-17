// Assemblies (docs/v2/design/optical_layout_v2.md section 8.6): groups of components that are always set
// up together, inserted in one go: a two-mirror fold, beam expanders, a 4f relay, a λ/2 + PBS attenuator, a
// fibre coupler and a fibre collimator.
//
// A template is data: its parts are placed for a beam that arrives along +x at the anchor (0, 0) (for the
// fibre collimator, the beam leaves the fibre there along +x). Inserting one is a pure function: the parts
// get ids that nothing in the layout has (the catalogue's prefixes, the lowest free numbers), the whole
// assembly can be turned by an angle about the anchor, and it arrives as ONE new layout, so the editor
// records ONE undo step and selects the new ids, which the keyboard then turns as a group ([ and ]).
// Pure, so it is tested without a browser (test/opticalLayoutTemplates.test.ts), including that every
// assembly does to a beam what its name says (the expanders expand, the attenuator splits, ...).

import { tr } from "../i18n.ts";
import type { ComponentType, LayoutComponent, OpticalLayout, Props, Vec } from "./types.ts";
import { LIMITS, isSourceType } from "./types.ts";
import { CATALOG, nextId } from "./catalog.ts";
import { newComponent, takenIds } from "./edit.ts";

export interface TemplatePart {
  type: ComponentType;
  /** where the part sits for a beam arriving along +x at the anchor (0, 0), mm */
  dx: number;
  dy: number;
  /** its angle_deg for that beam */
  angle: number;
  props?: Props;
  aperture?: number;
  /** what the part does in the assembly, for its annotation ("first lens", "dump on the reflected port") */
  role: () => string;
}

export interface Template {
  /** an identity string: never translated */
  id: string;
  name: () => string;
  hint: () => string;
  parts: readonly TemplatePart[];
}

export const TEMPLATES: readonly Template[] = [
  {
    id: "fold_pair",
    name: () => tr("两镜折返（潜望镜对）", "Mirror pair (beam walk)"),
    hint: () => tr("两面 45° 镜：光沿 +x 进来，平移 50 mm 后沿 +x 出去；两面镜子一起调光的位置和方向。", "Two 45° mirrors: the beam comes in along +x and leaves along +x, 50 mm to the side; together they set its position and direction."),
    parts: [
      { type: "mirror", dx: 0, dy: 0, angle: 135, role: () => tr("两镜折返：第一面", "mirror pair: first") },
      { type: "mirror", dx: 0, dy: 50, angle: 315, role: () => tr("两镜折返：第二面", "mirror pair: second") },
    ],
  },
  {
    id: "kepler_3x",
    name: () => tr("开普勒扩束 3×", "Keplerian expander 3×"),
    hint: () => tr("f 50 与 f 150 相距 200 mm（f1 + f2），中间有实焦点，光斑放大 3 倍。", "f 50 and f 150, 200 mm apart (f1 + f2), with a real focus between them: the beam comes out 3 times wider."),
    parts: [
      { type: "lens", dx: 0, dy: 0, angle: 0, props: { focal_mm: 50 }, role: () => tr("开普勒扩束 3×：f 50", "Keplerian 3×: f 50") },
      { type: "lens", dx: 200, dy: 0, angle: 0, props: { focal_mm: 150 }, role: () => tr("开普勒扩束 3×：f 150", "Keplerian 3×: f 150") },
    ],
  },
  {
    id: "galilean_2x",
    name: () => tr("伽利略扩束 2×", "Galilean expander 2×"),
    hint: () => tr("f −50 与 f 100 相距 50 mm，没有实焦点，光斑放大 2 倍。", "f −50 and f 100, 50 mm apart, no real focus: the beam comes out twice as wide."),
    parts: [
      { type: "lens", dx: 0, dy: 0, angle: 0, props: { focal_mm: -50 }, role: () => tr("伽利略扩束 2×：f −50", "Galilean 2×: f −50") },
      { type: "lens", dx: 50, dy: 0, angle: 0, props: { focal_mm: 100 }, role: () => tr("伽利略扩束 2×：f 100", "Galilean 2×: f 100") },
    ],
  },
  {
    id: "relay_4f",
    name: () => tr("4f 中继（f 100）", "4f relay (f 100)"),
    hint: () => tr("两片 f 100 相距 200 mm：把第一片前 100 mm 的像以 1:1 倒立送到第二片后 100 mm。", "Two f 100 lenses 200 mm apart: relay the plane 100 mm before the first to 100 mm after the second, 1:1 and inverted."),
    parts: [
      { type: "lens", dx: 0, dy: 0, angle: 0, props: { focal_mm: 100 }, role: () => tr("4f 中继：第一片", "4f relay: first lens") },
      { type: "lens", dx: 200, dy: 0, angle: 0, props: { focal_mm: 100 }, role: () => tr("4f 中继：第二片", "4f relay: second lens") },
    ],
  },
  {
    id: "hwp_pbs_attenuator",
    name: () => tr("λ/2 + PBS 可调衰减", "λ/2 + PBS attenuator"),
    hint: () => tr("转 λ/2 调透过 PBS 的功率；反射口的光进光阱。快轴 22.5° 时 p 光一半透过。", "Turn the λ/2 to set how much passes the PBS; the reflected port goes into a beam dump. At 22.5° half of p light passes."),
    parts: [
      { type: "waveplate", dx: 0, dy: 0, angle: 0, props: { retardance: "half", fast_axis_deg: 22.5 }, role: () => tr("可调衰减：λ/2", "attenuator: λ/2") },
      { type: "pbs", dx: 50, dy: 0, angle: 135, role: () => tr("可调衰减：PBS", "attenuator: PBS") },
      { type: "beam_dump", dx: 50, dy: 50, angle: 270, role: () => tr("可调衰减：反射口光阱", "attenuator: dump on the reflected port") },
    ],
  },
  {
    id: "fibre_coupler",
    name: () => tr("光纤耦合（f 11 + 光纤入口）", "Fibre coupler (f 11 + fibre input)"),
    hint: () => tr("f 11 透镜把准直光聚到 11 mm 后的光纤端面上。", "An f 11 lens focuses the collimated beam on the fibre face 11 mm behind it."),
    parts: [
      { type: "lens", dx: 0, dy: 0, angle: 0, props: { focal_mm: 11 }, aperture: 5.5, role: () => tr("光纤耦合：f 11 透镜", "fibre coupler: f 11 lens") },
      { type: "fiber_in", dx: 11, dy: 0, angle: 180, role: () => tr("光纤耦合：光纤入口", "fibre coupler: fibre input") },
    ],
  },
  {
    id: "fibre_collimator",
    name: () => tr("光纤准直（光纤输出 + f 11）", "Fibre collimator (fibre output + f 11)"),
    hint: () => tr("光纤输出沿 +x 发光，f 11 透镜在 11 mm 处把它准直。", "The fibre output emits along +x; an f 11 lens 11 mm away collimates it."),
    parts: [
      { type: "fiber_out", dx: 0, dy: 0, angle: 0, role: () => tr("光纤准直：光纤输出", "fibre collimator: fibre output") },
      { type: "lens", dx: 11, dy: 0, angle: 0, props: { focal_mm: 11 }, aperture: 5.5, role: () => tr("光纤准直：f 11 透镜", "fibre collimator: f 11 lens") },
    ],
  },
];

export function templateById(id: string): Template | undefined {
  return TEMPLATES.find((t) => t.id === id);
}

export type TemplateRefusal = "components" | "sources" | "type";

export interface TemplateInsert {
  layout: OpticalLayout;
  /** the ids of the new components, in the template's order (empty when refused) */
  ids: string[];
  refusal: TemplateRefusal | null;
}

const canon = (deg: number) => {
  const a = ((deg % 360) + 360) % 360;
  return Math.round(a * 100) / 100 === 360 ? 0 : Math.round(a * 100) / 100;
};

/** Where the parts of `template` land with its anchor at `at`, turned by `turnDeg` about it. */
function partPositions(template: Template, at: Vec, turnDeg: number): Vec[] {
  const t = turnDeg * (Math.PI / 180);
  const cos = Math.cos(t);
  const sin = Math.sin(t);
  return template.parts.map((part) => ({ x: at.x + part.dx * cos - part.dy * sin, y: at.y + part.dx * sin + part.dy * cos }));
}

/** Tries at moving an assembly off the components already there (as many as for a single component). */
export const FREE_ANCHOR_TRIES = 24;

/**
 * Where the anchor goes: `at`, or, when a part would land on a component already there (within 1 mm, e.g. the same
 * assembly added twice at the middle of the view: the copy would hide exactly under the first), one hole further
 * right and one down, and so on, as the editor places a single component.
 */
export function freeAnchor(layout: OpticalLayout, template: Template, at: Vec, turnDeg = 0): Vec {
  const step = layout.table.hole_pitch_mm > 0 ? layout.table.hole_pitch_mm : 25;
  const taken = (p: Vec) => layout.components.some((c) => Math.hypot(c.x_mm - p.x, c.y_mm - p.y) < 1);
  let a = at;
  for (let i = 0; i < FREE_ANCHOR_TRIES && partPositions(template, a, turnDeg).some(taken); i++) a = { x: a.x + step, y: a.y - step };
  return a;
}

/**
 * The layout with `template` added, its anchor at `at` (moved on by freeAnchor when a part would land on a
 * component already there) and the whole assembly turned by `turnDeg` about it (counter-clockwise). Refused,
 * without any change, when the layout would pass its limits (400 components, 32 light sources) or this build's
 * catalogue does not know one of the types.
 */
export function insertTemplate(layout: OpticalLayout, template: Template, at: Vec, turnDeg = 0): TemplateInsert {
  if (template.parts.some((p) => !(p.type in CATALOG))) return { layout, ids: [], refusal: "type" };
  if (layout.components.length + template.parts.length > LIMITS.components) return { layout, ids: [], refusal: "components" };
  const sources = layout.components.filter((c) => isSourceType(c.type)).length + template.parts.filter((p) => isSourceType(p.type)).length;
  if (sources > LIMITS.sources) return { layout, ids: [], refusal: "sources" };
  const taken = takenIds(layout);
  const positions = partPositions(template, freeAnchor(layout, template, at, turnDeg), turnDeg);
  const added: LayoutComponent[] = [];
  for (const [i, part] of template.parts.entries()) {
    const id = nextId(CATALOG[part.type].prefix, taken);
    taken.add(id);
    const pos = positions[i]!;
    const c = newComponent(part.type, id, pos, {
      angle_deg: canon(part.angle + turnDeg),
      ...(part.aperture !== undefined ? { aperture_mm: part.aperture } : {}),
      ...(part.props ? { props: { ...CATALOG[part.type].defaultProps(), ...part.props } } : {}),
    });
    added.push({ ...c, annotations: { ...c.annotations, function: part.role() } });
  }
  return { layout: { ...layout, components: [...layout.components, ...added] }, ids: added.map((c) => c.id), refusal: null };
}
