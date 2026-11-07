// The component catalogue: what each type is called, how it is grouped in the palette,
// what a new one starts with, and which type parameters it has (section 4 of
// docs/v2/design/optical_layout.md).
//
// The type ids ("mirror", "beam_splitter", ...) are identity strings: they are stored,
// compared and sent to the server. Only the labels below go through tr(), and they are
// looked up from the raw id (typeLabel), never the other way round.

import { tr } from "../i18n.ts";
import type { ComponentType, Props } from "./types.ts";
import { COMPONENT_TYPES } from "./types.ts";

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
  hint?: string;
}

/** Numbers of props are rounded to this many decimals unless the field says otherwise (model.py: Prop.decimals). */
export const DEFAULT_PROP_DECIMALS = 3;

// The ranges and rounding of the server's PROPS table (MASTv2/mast/optical_layout/model.py),
// so the editor refuses and rounds exactly what the server would.
const WAVELENGTH_MAX = 1e7;

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
  { key: "power_mw", kind: "number", label: tr("功率", "Power"), unit: "mW", min: 0, max: 1e7 },
];

/** The type parameters section 4 lists for each type. Unknown keys are rejected by the server. */
export function propFields(type: ComponentType): PropField[] {
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
      return [{ key: "focal_mm", kind: "number", label: tr("焦距", "Focal length"), unit: "mm", min: -1e5, max: 1e5, hint: tr("仅作注释，追迹不偏折", "Annotation only; the trace does not bend rays") }];
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
        },
        { key: "fast_axis_deg", kind: "number", label: tr("快轴角", "Fast-axis angle"), unit: "°", min: -360, max: 360, decimals: 2 },
      ];
    case "polarizer":
      return [{ key: "axis_deg", kind: "number", label: tr("透光轴角", "Transmission axis"), unit: "°", min: -360, max: 360, decimals: 2 }];
    case "filter":
      return [
        wavelength("pass_min_nm", tr("通带下限", "Pass band from")),
        wavelength("pass_max_nm", tr("通带上限", "Pass band to"), { hint: tr("波长在通带外的光被挡掉；下限不能大于上限", "Light outside the band is blocked; the lower end cannot exceed the upper") }),
      ];
    case "nd":
      return [{ key: "od", kind: "number", label: "OD", min: 0, max: 20 }];
    case "iris":
      return [{ key: "open_mm", kind: "number", label: tr("开口直径", "Opening"), unit: "mm", min: 0, minExclusive: true, max: 500, hint: tr("光线偏离中心超过开口一半就被挡掉", "A ray further than half the opening from the centre is blocked") }];
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
          hint: tr("选另一端的光纤输出；它只在被照到时发光，波长随到达的光，不看它自己的波长设置", "The fibre output at the other end; it emits only when lit, with the wavelengths that arrive, and ignores its own wavelength setting"),
        },
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
