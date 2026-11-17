// The component catalogue: what each type is called, how it is grouped in the palette,
// what a new one starts with, and which type parameters it has (section 4 of
// docs/v2/design/optical_layout.md; the `pbs` type and the beam props of section 3 of
// docs/v2/design/optical_layout_v2.md).
//
// The type ids ("mirror", "beam_splitter", ...) are identity strings: they are stored,
// compared and sent to the server. Only the labels below go through tr(), and they are
// looked up from the raw id (typeLabel), never the other way round.

import { tr } from "../i18n.ts";
import type { ComponentType, Props } from "./types.ts";
import { COMPONENT_TYPES, LIMITS, THROUGHPUT_TYPES } from "./types.ts";

export type Category = "source" | "steer" | "optic" | "gate" | "detect" | "end" | "other";

export const CATEGORY_ORDER: readonly Category[] = ["source", "steer", "optic", "gate", "detect", "end", "other"];

export function categoryLabel(cat: Category): string {
  switch (cat) {
    case "source":
      return tr("光源", "Sources");
    case "steer":
      return tr("反射与分束", "Mirrors and splitters");
    case "optic":
      return tr("透射元件", "Transmissive optics");
    case "gate":
      return tr("光阑与快门", "Irises and shutters");
    case "detect":
      return tr("样品与探测", "Sample and detection");
    case "end":
      return tr("光阱与光纤入口", "Dumps and fibre inputs");
    default:
      return tr("自定义", "Custom");
  }
}

/** What the component does to a ray, for the UI (the tracer switches on the type itself). */
export type Role = "source" | "mirror" | "splitter" | "dichroic" | "transmit" | "gate" | "terminate" | "custom";

export interface TypeInfo {
  type: ComponentType;
  category: Category;
  role: Role;
  /** prefix of generated ids and labels: "m" gives m1, M1 */
  prefix: string;
  defaultAperture: number;
  defaultAngle: number;
  defaultProps: () => Props;
}

const info = (
  type: ComponentType,
  category: Category,
  role: Role,
  prefix: string,
  defaultAperture: number,
  defaultAngle: number,
  defaultProps: () => Props = () => ({}),
): TypeInfo => ({ type, category, role, prefix, defaultAperture, defaultAngle, defaultProps });

/** Direction a fold mirror faces so that a +x beam leaves towards +y. */
export const FOLD_ANGLE = 135;
/** A receiving face that looks into a beam travelling along +x. */
const FACING_PLUS_X_BEAM = 180;

export const CATALOG: Record<ComponentType, TypeInfo> = {
  laser: info("laser", "source", "source", "las", 2, 0, () => ({ wavelength_nm: 532 })),
  fiber_out: info("fiber_out", "source", "source", "fo", 2, 0, () => ({ wavelength_nm: 532 })),
  lamp: info("lamp", "source", "source", "lamp", 5, 0),
  led: info("led", "source", "source", "led", 5, 0),

  mirror: info("mirror", "steer", "mirror", "m", 25.4, FOLD_ANGLE),
  beam_splitter: info("beam_splitter", "steer", "splitter", "bs", 25.4, FOLD_ANGLE, () => ({ reflect_fraction: 0.5 })),
  pbs: info("pbs", "steer", "splitter", "pbs", 25.4, FOLD_ANGLE),
  dichroic: info("dichroic", "steer", "dichroic", "dm", 25.4, FOLD_ANGLE, () => ({ edge_nm: 600, reflects: "short" })),
  grating: info("grating", "steer", "mirror", "gr", 25, FOLD_ANGLE),

  lens: info("lens", "optic", "transmit", "len", 25.4, 0),
  objective: info("objective", "optic", "transmit", "obj", 10, 0),
  waveplate: info("waveplate", "optic", "transmit", "wp", 12.7, 0, () => ({ retardance: "half" })),
  polarizer: info("polarizer", "optic", "transmit", "pol", 12.7, 0),
  filter: info("filter", "optic", "transmit", "flt", 25.4, 0),
  window: info("window", "optic", "transmit", "win", 25.4, 0),
  nd: info("nd", "optic", "transmit", "nd", 25.4, 0),
  aotf: info("aotf", "optic", "transmit", "aotf", 25.4, 0),
  prism: info("prism", "optic", "transmit", "pr", 25.4, 0),
  periscope: info("periscope", "optic", "transmit", "per", 25.4, 0),

  iris: info("iris", "gate", "gate", "ir", 25.4, 0),
  shutter: info("shutter", "gate", "gate", "sh", 12.7, 0, () => ({ closed: false })),

  sample: info("sample", "detect", "terminate", "smp", 10, FACING_PLUS_X_BEAM, () => ({ reflects: false })),
  detector: info("detector", "detect", "terminate", "det", 10, FACING_PLUS_X_BEAM),
  camera: info("camera", "detect", "terminate", "cam", 10, FACING_PLUS_X_BEAM),
  spectrometer: info("spectrometer", "detect", "terminate", "spec", 10, FACING_PLUS_X_BEAM),
  power_meter: info("power_meter", "detect", "terminate", "pm", 10, FACING_PLUS_X_BEAM),

  beam_dump: info("beam_dump", "end", "terminate", "bd", 25.4, FACING_PLUS_X_BEAM),
  blocker: info("blocker", "end", "terminate", "blk", 25.4, FACING_PLUS_X_BEAM),
  fiber_in: info("fiber_in", "end", "terminate", "fi", 5, FACING_PLUS_X_BEAM),

  custom: info("custom", "other", "custom", "c", 25.4, 0, () => ({ behavior: "transmit" })),
};

export function typeInfo(type: ComponentType): TypeInfo {
  return CATALOG[type];
}

/** Types of one category, in palette order. */
export function typesIn(cat: Category): ComponentType[] {
  return COMPONENT_TYPES.filter((t) => CATALOG[t].category === cat);
}

export function typeLabel(type: string): string {
  switch (type) {
    case "laser":
      return tr("激光器", "Laser");
    case "fiber_out":
      return tr("光纤输出", "Fibre output");
    case "lamp":
      return tr("灯", "Lamp");
    case "led":
      return "LED";
    case "mirror":
      return tr("反射镜", "Mirror");
    case "beam_splitter":
      return tr("分束器", "Beam splitter");
    case "pbs":
      return tr("偏振分束器", "Polarizing beam splitter");
    case "dichroic":
      return tr("二向色镜", "Dichroic mirror");
    case "grating":
      return tr("光栅", "Grating");
    case "lens":
      return tr("透镜", "Lens");
    case "objective":
      return tr("物镜", "Objective");
    case "waveplate":
      return tr("波片", "Waveplate");
    case "polarizer":
      return tr("偏振片", "Polarizer");
    case "filter":
      return tr("滤光片", "Filter");
    case "window":
      return tr("窗片", "Window");
    case "nd":
      return tr("ND 滤光片", "ND filter");
    case "aotf":
      return "AOTF";
    case "prism":
      return tr("棱镜", "Prism");
    case "periscope":
      return tr("潜望镜", "Periscope");
    case "iris":
      return tr("光阑", "Iris");
    case "shutter":
      return tr("快门", "Shutter");
    case "sample":
      return tr("样品", "Sample");
    case "detector":
      return tr("探测器", "Detector");
    case "camera":
      return tr("相机", "Camera");
    case "spectrometer":
      return tr("光谱仪", "Spectrometer");
    case "power_meter":
      return tr("功率计", "Power meter");
    case "beam_dump":
      return tr("光阱", "Beam dump");
    case "blocker":
      return tr("挡板", "Blocker");
    case "fiber_in":
      return tr("光纤耦合", "Fibre input");
    case "custom":
      return tr("自定义", "Custom");
    default:
      return type;
  }
}

/**
 * A sentence on what a type does to the beam where its name alone does not say it (palette tooltip,
 * inspector); null for the rest.
 */
export function typeNote(type: string): string | null {
  switch (type) {
    case "pbs":
      return tr(
        "透射 p（平台平面内的偏振）、反射 s（竖直的偏振）；两路各分多少由到达的偏振决定，没有反射比例可填",
        "Transmits p (polarisation in the table plane) and reflects s (vertical polarisation); how much goes each way follows from the arriving polarisation, there is no fraction to set",
      );
    case "beam_splitter":
      return tr("按反射比例分成反射、透射两路，与偏振无关", "Splits by its reflected fraction into a reflected and a transmitted beam, whatever the polarisation");
    default:
      return null;
  }
}

/** What `angle_deg` means for this type (section 2), for the hint under the angle box. */
export function angleMeaning(type: ComponentType): string {
  switch (CATALOG[type].role) {
    case "source":
      return tr("出射方向", "Emission direction");
    case "mirror":
    case "splitter":
    case "dichroic":
      return tr("反射面朝向（法线方向）；沿 +x 的光折到 +y 的 45° 折返镜是 135°", "Direction the reflecting face points to (its normal); the 45° fold mirror that turns a +x beam to +y is 135°");
    case "terminate":
      return tr("接收面朝向；迎着沿 +x 传播的光是 180°", "Direction the receiving face points to; facing a beam that travels along +x is 180°");
    case "custom":
      return tr("光轴 / 法线方向", "Axis / normal direction");
    default:
      return tr("光轴方向", "Optical axis direction");
  }
}

// ── type parameters (props) ───────────────────────────────────────────────────

export type PropKind = "number" | "numberList" | "enum" | "bool" | "componentRef";

export interface PropOption {
  value: string;
  label: string;
}

export interface PropField {
  key: string;
  kind: PropKind;
  label: string;
  unit?: string;
  min?: number;
  max?: number;
  /** `min` itself is not allowed (a wavelength of 0 nm) */
  minExclusive?: boolean;
  /** decimals the server keeps: 3 unless said otherwise (the editor rounds the same way) */
  decimals?: number;
  /** the server needs it (a dichroic mirror's edge) */
  required?: boolean;
  options?: PropOption[];
  /** for numberList: at most this many entries, no repeats */
  maxItems?: number;
  /** for componentRef: only components of this type are offered */
  refType?: ComponentType;
  /**
   * The server takes this key only when another key of the same props is given (`value` absent) or has
   * exactly `value` (v2 section 3.2: `pol_angle_deg` needs `polarization` = `linear`, `m2` needs
   * `beam_diameter_mm`); otherwise it answers 422. The form hides the field, validate.ts reports it and
   * setProp drops it when the other key changes so that the layout can still be saved.
   */
  dependsOn?: { key: string; value?: string };
  hint?: string;
  /**
   * Where the inspector shows the field: "position" next to x, y and the angle (the heights of
   * docs/v2/design/optical_layout_3d.md section 2), absent among the type's parameters.
   */
  group?: "position";
}

/** Numbers of props are rounded to this many decimals unless the field says otherwise (model.py: Prop.decimals). */
export const DEFAULT_PROP_DECIMALS = 3;

// The ranges and rounding of the server's PROPS table (MASTv2/mast/optical_layout/model.py),
// so the editor refuses and rounds exactly what the server would.
const WAVELENGTH_MAX = 1e7;

/**
 * v2 section 4.5: the axis of a waveplate or polarizer is measured from the component's local +y (in the table
 * plane, along its face) towards straight up. Light travelling along the component's axis direction sees that
 * angle as it is; from the back it sees 180° minus it, at an angle the projection.
 */
const axisHint = (): string =>
  tr(
    "0° = 平台平面内、沿器件面（局部 +y），90° = 竖直向上；沿光轴方向传播的光看到的就是这个角，从背面来或斜着来按投影换算；不填按 0° 算",
    "0° = in the table plane along the face (local +y), 90° = straight up; light travelling along the axis direction sees this angle, light from the back or at an angle sees its projection; empty counts as 0°",
  );

const wavelength = (key: string, label: string, over: Partial<PropField> = {}): PropField => ({
  key,
  kind: "number",
  label,
  unit: "nm",
  min: 0,
  minExclusive: true,
  max: WAVELENGTH_MAX,
  ...over,
});

const SOURCE_FIELDS: PropField[] = [
  wavelength("wavelength_nm", tr("波长", "Wavelength"), { hint: tr("和下面的“多个波长”二选一", "Use this or the list below, not both") }),
  {
    key: "wavelengths_nm",
    kind: "numberList",
    label: tr("多个波长", "Several wavelengths"),
    unit: "nm",
    min: 0,
    minExclusive: true,
    max: WAVELENGTH_MAX,
    maxItems: 16,
    hint: tr("用逗号分隔，一束光一个波长，最多 16 个、不重复；和上面的单个波长二选一", "Comma separated, one ray per wavelength, at most 16, no repeats; use this or the single wavelength, not both"),
  },
  {
    key: "power_mw",
    kind: "number",
    label: tr("功率", "Power"),
    unit: "mW",
    min: 0,
    max: 1e7,
    hint: tr("给了多个波长时，每个波长各发这么多", "With several wavelengths, each one carries this much"),
  },
  {
    key: "polarization",
    kind: "enum",
    label: tr("偏振", "Polarisation"),
    options: [
      { value: "unpolarized", label: tr("非偏振", "Unpolarised") },
      { value: "linear", label: tr("线偏振", "Linear") },
    ],
    hint: tr("不填按非偏振光算", "Left empty, the light counts as unpolarised"),
  },
  {
    key: "pol_angle_deg",
    kind: "number",
    label: tr("偏振方向", "Polarisation angle"),
    unit: "°",
    min: -360,
    max: 360,
    decimals: 2,
    dependsOn: { key: "polarization", value: "linear" },
    hint: tr(
      "从平台平面内、光前进方向的左侧（0°，p）转向竖直向上（90°，s）；缺省 0°",
      "From in the table plane, to the left of the direction of travel (0°, p), towards straight up (90°, s); default 0°",
    ),
  },
  {
    key: "beam_diameter_mm",
    kind: "number",
    label: tr("光束直径", "Beam diameter"),
    unit: "mm",
    min: 0,
    minExclusive: true,
    max: 1000,
    decimals: 6,
    hint: tr("出口处的 1/e² 直径，腰在出口；不填就不算光斑", "1/e² diameter at the exit, waist at the exit; empty: no beam size is computed"),
  },
  {
    key: "m2",
    kind: "number",
    label: "M²",
    min: 1,
    max: 1000,
    decimals: 3,
    dependsOn: { key: "beam_diameter_mm" },
    hint: tr("光束质量因子，缺省 1", "Beam quality factor, default 1"),
  },
];

/** `throughput` (v2 section 3.2): the share of the power that goes on after the component acts. */
function throughputField(type: ComponentType): PropField {
  const base = { key: "throughput", kind: "number" as const, min: 0, max: 1, decimals: 6 };
  const tail = tr("，0 到 1，缺省 1", ", 0 to 1, default 1");
  switch (type) {
    case "mirror":
      return { ...base, label: tr("反射率", "Reflectance"), hint: tr("反射后剩下的功率比例", "Share of the power that is reflected") + tail };
    case "sample":
      return { ...base, label: tr("反射率", "Reflectance"), hint: tr("勾选“反射光”时才用到", "Used when it reflects light") + tail };
    case "grating":
      return { ...base, label: tr("零级效率", "Zero-order efficiency"), hint: tr("按零级反射时剩下的功率比例", "Share of the power in the zero order") + tail };
    case "polarizer":
      return { ...base, label: tr("沿轴透过率", "Transmission along the axis"), hint: tr("偏振沿透光轴的光透过的比例", "Share of the light polarised along the axis that is transmitted") + tail };
    case "filter":
      return { ...base, label: tr("通带内透过率", "In-band transmission"), hint: tr("通带内的光透过的比例", "Share of the light in the pass band that is transmitted") + tail };
    case "custom":
      return { ...base, label: tr("透过 / 反射率", "Throughput"), hint: tr("透射或反射之后剩下的功率比例", "Share of the power that goes on after it transmits or reflects") + tail };
    default:
      return { ...base, label: tr("透过率", "Transmission"), hint: tr("透过之后剩下的功率比例", "Share of the power that is transmitted") + tail };
  }
}

const withThroughput = (type: ComponentType, fields: PropField[]): PropField[] =>
  (THROUGHPUT_TYPES as readonly string[]).includes(type) ? [...fields, throughputField(type)] : fields;

/** A height above the table top (docs/v2/design/optical_layout_3d.md section 2): ±10000 mm at 0.01 mm, like x and y. */
const height = (key: string, label: string, hint: string): PropField => ({
  key,
  kind: "number",
  label,
  unit: "mm",
  min: -LIMITS.zAbs,
  max: LIMITS.zAbs,
  decimals: 2,
  hint,
  group: "position",
});

/** The props every component takes whatever its type (section 2 of the 3D contract; model.py COMMON_PROPS). */
function commonFields(): PropField[] {
  return [
    height(
      "z_mm",
      tr("高度 z", "Height z"),
      tr("光轴中心（光源：出口）离台面的高度；不填 = 台面光束高度", "Height of the optical axis (a source: its exit) above the table top; empty = the table's beam height"),
    ),
  ];
}

/**
 * The parameters each type takes: the type's own ones (section 4; v2 section 3.2 adds the beam ones; the 3D
 * contract a periscope's exit height) and the ones of every component (the height z). Unknown keys are rejected by
 * the server.
 */
export function propFields(type: ComponentType): PropField[] {
  return [...withThroughput(type, ownFields(type)), ...commonFields()];
}

function ownFields(type: ComponentType): PropField[] {
  switch (type) {
    case "laser":
    case "fiber_out":
      return SOURCE_FIELDS;
    case "lamp":
    case "led":
      return [wavelength("wavelength_nm", tr("波长（可不填）", "Wavelength (optional)"))];
    case "beam_splitter":
      return [{ key: "reflect_fraction", kind: "number", label: tr("反射比例", "Reflected fraction"), min: 0, max: 1, decimals: 6, hint: tr("0 到 1，缺省 0.5", "0 to 1, default 0.5") }];
    case "dichroic":
      return [
        wavelength("edge_nm", tr("截止波长", "Edge wavelength"), { required: true }),
        {
          key: "reflects",
          kind: "enum",
          label: tr("反射哪一侧", "Reflects"),
          options: [
            { value: "short", label: tr("短波（低于截止波长）", "Short wavelengths (below the edge)") },
            { value: "long", label: tr("长波（高于截止波长）", "Long wavelengths (above the edge)") },
          ],
          hint: tr("缺省反射短波；正好等于截止波长时透射", "Short by default; a wavelength exactly at the edge is transmitted"),
        },
      ];
    case "grating":
      return [{ key: "lines_per_mm", kind: "number", label: tr("刻线密度", "Groove density"), unit: tr("线/mm", "lines/mm"), min: 0, minExclusive: true, max: 1e5, hint: tr("v1 按零级反射处理", "v1 treats it as a zero-order mirror") }];
    case "lens":
    case "objective":
      return [{ key: "focal_mm", kind: "number", label: tr("焦距", "Focal length"), unit: "mm", min: -1e5, max: 1e5, hint: tr("追迹不偏折中心光线；光斑按它聚焦或发散", "The trace does not bend the central ray; the beam size focuses or spreads with it") }];
    case "waveplate":
      return [
        {
          key: "retardance",
          kind: "enum",
          label: tr("相位延迟", "Retardance"),
          options: [
            { value: "half", label: "λ/2" },
            { value: "quarter", label: "λ/4" },
          ],
          hint: tr("不填则对偏振不起作用", "Left empty, it does nothing to the polarisation"),
        },
        { key: "fast_axis_deg", kind: "number", label: tr("快轴角", "Fast-axis angle"), unit: "°", min: -360, max: 360, decimals: 2, hint: axisHint() },
      ];
    case "polarizer":
      return [{ key: "axis_deg", kind: "number", label: tr("透光轴角", "Transmission axis"), unit: "°", min: -360, max: 360, decimals: 2, hint: axisHint() }];
    case "filter":
      return [
        wavelength("pass_min_nm", tr("通带下限", "Pass band from")),
        wavelength("pass_max_nm", tr("通带上限", "Pass band to"), { hint: tr("波长在通带外的光被挡掉；下限不能大于上限", "Light outside the band is blocked; the lower end cannot exceed the upper") }),
      ];
    case "nd":
      return [{ key: "od", kind: "number", label: "OD", min: 0, max: 20, hint: tr("功率乘 10^(−OD)", "The power is multiplied by 10^(−OD)") }];
    case "periscope":
      return [
        height(
          "z_out_mm",
          tr("出口高度", "Exit height"),
          tr(
            "不填 = 入口高度；光沿器件朝向（+x）穿过时从高度 z 升或降到这里，反向穿过时反过来",
            "Empty = the entrance height; light travelling along the component's direction (+x) goes from the height z to this one, light the other way round from this one to z",
          ),
        ),
      ];
    case "iris":
      return [{ key: "open_mm", kind: "number", label: tr("开口直径", "Opening"), unit: "mm", min: 0, minExclusive: true, max: 500, hint: tr("光线偏离中心（平面内或高度方向）超过开口一半就被挡掉", "A ray further than half the opening from the centre (sideways or in height) is blocked") }];
    case "shutter":
      return [{ key: "closed", kind: "bool", label: tr("关闭（挡光）", "Closed (blocks the beam)") }];
    case "sample":
      return [{ key: "reflects", kind: "bool", label: tr("反射光", "Reflects light"), hint: tr("勾选后像反射镜；否则光在此终止", "Ticked it acts like a mirror; otherwise the beam ends here") }];
    case "fiber_in":
      return [
        {
          key: "continues_at",
          kind: "componentRef",
          refType: "fiber_out",
          label: tr("续接到", "Continues at"),
          hint: tr(
            "选另一端的光纤输出；它只在被照到时发光，波长和功率随到达的光（乘耦合效率），偏振和光束直径用它自己的设置",
            "The fibre output at the other end; it emits only when lit, with the wavelengths and the power that arrive (times the coupling), and with its own polarisation and beam diameter",
          ),
        },
        { key: "coupling", kind: "number", label: tr("耦合效率", "Coupling efficiency"), min: 0, max: 1, decimals: 6, hint: tr("续接出去的功率乘它，0 到 1，缺省 1", "The power that continues is multiplied by it, 0 to 1, default 1") },
      ];
    case "custom":
      return [
        {
          key: "behavior",
          kind: "enum",
          label: tr("对光线的作用", "Effect on a ray"),
          options: [
            { value: "transmit", label: tr("透射", "Transmit") },
            { value: "reflect", label: tr("反射", "Reflect") },
            { value: "stop", label: tr("终止", "Stop") },
          ],
          hint: tr("缺省透射", "Transmit by default"),
        },
      ];
    default:
      return [];
  }
}

/** Whether the condition a field depends on (PropField.dependsOn) holds for these props (`null` is "not given"). */
export function dependencyMet(f: PropField, props: Props): boolean {
  const dep = f.dependsOn;
  if (!dep) return true;
  const v = props[dep.key];
  if (v === undefined || v === null) return false;
  return dep.value === undefined || v === dep.value;
}

/**
 * The fields the inspector shows among a component's parameters for these props: a field whose dependency does
 * not hold is hidden (the angle of unpolarised light, the M² of a beam without a diameter), unless it has a value,
 * so that a value the server would refuse can be seen and cleared. The heights (group "position") are not among
 * them: the inspector shows them with the position.
 */
export function visiblePropFields(type: ComponentType, props: Props): PropField[] {
  return propFields(type).filter((f) => f.group !== "position" && (dependencyMet(f, props) || (props[f.key] !== undefined && props[f.key] !== null)));
}

/** The field of a type's parameters by key. */
export function propField(type: ComponentType, key: string): PropField | undefined {
  return propFields(type).find((f) => f.key === key);
}

/** Keys allowed in `props` for a type. */
export function allowedPropKeys(type: ComponentType): string[] {
  return propFields(type).map((f) => f.key);
}

/** `props` with every key the type does not have removed (the server rejects unknown keys). */
export function cleanProps(type: ComponentType, props: Props): Props {
  const keys = new Set(allowedPropKeys(type));
  const out: Props = {};
  for (const [k, v] of Object.entries(props)) if (keys.has(k)) out[k] = v;
  return out;
}

// ── ids and labels ────────────────────────────────────────────────────────────

/** The lowest unused id `<prefix><n>` (n from 1). */
export function nextId(prefix: string, taken: ReadonlySet<string>): string {
  for (let n = 1; ; n++) {
    const id = `${prefix}${n}`;
    if (!taken.has(id)) return id;
  }
}

/** A label for a generated id: "m3" gives "M3". */
export function labelFromId(id: string): string {
  return id.toUpperCase();
}
