// Checks of a layout against the rules of the server's model (section 3 and section 11.1 of
// docs/v2/design/optical_layout.md; MASTv2/mast/optical_layout/model.py), done early so the
// editor can say what is wrong before the server answers 422. The server stays the
// authority: these checks reject only what the model rejects, never something it accepts
// (test/opticalLayoutFixtures.test.ts runs them over every fixture layout, which the
// server accepts, and expects no error).

import { tr } from "../i18n.ts";
import type { LayoutComponent, OpticalLayout } from "./types.ts";
import { GRID_KINDS, LIMITS, isComponentType, isSourceType } from "./types.ts";
import { propFields } from "./catalog.ts";
import { charCount } from "./text.ts";

export type IssueLevel = "error" | "warn";

export interface Issue {
  level: IssueLevel;
  /** where: "title", "table.hole_pitch_mm", "components[m1].props.edge_nm" ... */
  path: string;
  message: string;
  componentId?: string;
}

const issue = (level: IssueLevel, path: string, message: string, componentId?: string): Issue => {
  const i: Issue = { level, path, message };
  if (componentId !== undefined) i.componentId = componentId;
  return i;
};

const finite = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

/** Size of the layout as the server counts it: the UTF-8 bytes of its compact JSON. */
export function jsonBytes(layout: OpticalLayout): number {
  return new TextEncoder().encode(JSON.stringify(layout)).length;
}

/** A layout id is a file name: the format, and no name Windows reserves. */
export function layoutIdFormatProblem(id: string): "format" | "reserved" | null {
  if (!LIMITS.idPattern.test(id)) return "format";
  if (LIMITS.reservedIds.includes(id.toLowerCase())) return "reserved";
  return null;
}

const tooLong = (what: string, max: number) => tr(`${what}最多 ${max} 个字符。`, `${what} is at most ${max} characters.`);

export function validateComponent(c: LayoutComponent, layout?: OpticalLayout): Issue[] {
  const out: Issue[] = [];
  const at = (suffix: string) => `components[${c.id}]${suffix}`;
  if (!LIMITS.idPattern.test(c.id)) {
    out.push(issue("error", at(".id"), tr("编号只能含字母、数字、下划线和连字符，1 到 40 个字符。", "An id may contain letters, digits, underscore and hyphen, 1 to 40 characters."), c.id));
  }
  if (!isComponentType(c.type)) {
    out.push(issue("error", at(".type"), tr(`未知的器件类型“${String(c.type)}”。`, `Unknown component type "${String(c.type)}".`), c.id));
    return out;
  }
  if (charCount(c.label) > LIMITS.labelMax) out.push(issue("error", at(".label"), tooLong(tr("名称", "The label"), LIMITS.labelMax), c.id));
  for (const key of ["x_mm", "y_mm"] as const) {
    const v = c[key];
    if (!finite(v)) out.push(issue("error", at(`.${key}`), tr("坐标必须是数。", "A coordinate must be a number."), c.id));
    else if (Math.abs(v) > LIMITS.coordAbs) out.push(issue("error", at(`.${key}`), tr(`坐标绝对值不能超过 ${LIMITS.coordAbs} mm。`, `A coordinate cannot exceed ${LIMITS.coordAbs} mm.`), c.id));
  }
  if (!finite(c.angle_deg)) out.push(issue("error", at(".angle_deg"), tr("角度必须是数。", "The angle must be a number."), c.id));
  else if (Math.abs(c.angle_deg) > LIMITS.angleAbs) out.push(issue("error", at(".angle_deg"), tr("角度的绝对值太大。", "The angle is too large."), c.id));
  if (!finite(c.aperture_mm) || c.aperture_mm < LIMITS.apertureMin || c.aperture_mm > LIMITS.apertureMax) {
    out.push(issue("error", at(".aperture_mm"), tr(`口径要在 ${LIMITS.apertureMin} 到 ${LIMITS.apertureMax} mm 之间。`, `The aperture must be between ${LIMITS.apertureMin} and ${LIMITS.apertureMax} mm.`), c.id));
  }

  // type parameters
  const fields = propFields(c.type);
  const known = new Set(fields.map((f) => f.key));
  for (const key of Object.keys(c.props)) {
    if (!known.has(key)) out.push(issue("error", at(`.props.${key}`), tr(`“${key}”不是 ${c.type} 的参数。`, `"${key}" is not a parameter of ${c.type}.`), c.id));
  }
  for (const f of fields) {
    const v = c.props[f.key];
    const p = at(`.props.${f.key}`);
    if (v === undefined || v === null) continue;
    const inRange = (n: number) => !((f.min !== undefined && (f.minExclusive ? n <= f.min : n < f.min)) || (f.max !== undefined && n > f.max));
    switch (f.kind) {
      case "number":
        if (!finite(v)) out.push(issue("error", p, tr(`${f.label}必须是数。`, `${f.label} must be a number.`), c.id));
        else if (!inRange(v)) out.push(issue("error", p, tr(`${f.label}超出范围。`, `${f.label} is out of range.`), c.id));
        break;
      case "numberList":
        if (!Array.isArray(v) || v.length === 0 || !v.every(finite)) out.push(issue("error", p, tr(`${f.label}必须是一列数，不能为空。`, `${f.label} must be a non-empty list of numbers.`), c.id));
        else if (f.maxItems !== undefined && v.length > f.maxItems) out.push(issue("error", p, tr(`${f.label}最多 ${f.maxItems} 项。`, `${f.label} takes at most ${f.maxItems} entries.`), c.id));
        else if (!(v as number[]).every(inRange)) out.push(issue("error", p, tr(`${f.label}里有超出范围的值。`, `${f.label} has a value out of range.`), c.id));
        else if (new Set(v as number[]).size !== v.length) out.push(issue("error", p, tr(`${f.label}里有重复的值。`, `${f.label} has repeated values.`), c.id));
        break;
      case "enum":
        if (!f.options?.some((o) => o.value === v)) out.push(issue("error", p, tr(`${f.label}的取值不对。`, `${f.label} has an unknown value.`), c.id));
        break;
      case "bool":
        if (typeof v !== "boolean") out.push(issue("error", p, tr(`${f.label}必须是开关。`, `${f.label} must be on or off.`), c.id));
        break;
      case "componentRef":
        if (typeof v !== "string") out.push(issue("error", p, tr(`${f.label}必须是器件编号。`, `${f.label} must be a component id.`), c.id));
        else if (v.trim() !== "" && !LIMITS.idPattern.test(v.trim())) out.push(issue("error", p, tr(`${f.label}不是合法的编号。`, `${f.label} is not a valid id.`), c.id));
        else if (v.trim() !== "" && layout && !layout.components.some((o) => o.id === v.trim() && o.type === f.refType)) {
          // the server accepts it; the description of the layout reports it
          out.push(issue("warn", p, tr(`没有编号为 ${v} 的${f.refType ?? "器件"}。`, `There is no ${f.refType ?? "component"} with the id ${v}.`), c.id));
        }
        break;
    }
  }
  for (const f of fields) {
    if (f.required && (c.props[f.key] === undefined || c.props[f.key] === null)) {
      out.push(issue("error", at(`.props.${f.key}`), tr(`${f.label}必须填写。`, `${f.label} is required.`), c.id));
    }
  }
  if (c.type === "laser" || c.type === "fiber_out") {
    // exactly one of the two forms; for a fibre output that another fibre input continues at, the
    // trace ignores it (it emits what arrives), but the server still wants it
    const hasOne = c.props["wavelength_nm"] !== undefined && c.props["wavelength_nm"] !== null;
    const hasList = c.props["wavelengths_nm"] !== undefined && c.props["wavelengths_nm"] !== null;
    if (hasOne === hasList) {
      out.push(issue("error", at(".props.wavelength_nm"), hasOne ? tr("单个波长和多个波长只能给一种。", "Give either the single wavelength or the list, not both.") : tr("必须给出波长：单个波长或一列波长。", "A wavelength is required: the single one or a list."), c.id));
    }
  }
  if (c.type === "filter") {
    const lo = c.props["pass_min_nm"];
    const hi = c.props["pass_max_nm"];
    if (finite(lo) && finite(hi) && lo > hi) out.push(issue("error", at(".props.pass_min_nm"), tr("通带下限不能大于上限。", "The lower end of the pass band cannot exceed the upper end."), c.id));
  }

  // annotations
  const a = c.annotations;
  const str = (key: string, v: unknown, max: number) => {
    if (typeof v !== "string") out.push(issue("error", at(`.annotations.${key}`), tr("必须是文字。", "Must be text."), c.id));
    else if (charCount(v) > max) out.push(issue("error", at(`.annotations.${key}`), tr(`最多 ${max} 个字符（现在 ${charCount(v)}）。`, `At most ${max} characters (now ${charCount(v)}).`), c.id));
  };
  str("model", a.model, LIMITS.stringMax);
  str("manufacturer", a.manufacturer, LIMITS.stringMax);
  str("function", a.function, LIMITS.stringMax);
  str("actuator", a.actuator, LIMITS.stringMax);
  str("connection", a.connection, LIMITS.stringMax);
  str("datasheet_url", a.datasheet_url, LIMITS.stringMax);
  str("notes", a.notes, LIMITS.notesMax);
  if (a.datasheet_url !== "" && !/^https?:\/\/\S+$/i.test(a.datasheet_url)) {
    out.push(issue("error", at(".annotations.datasheet_url"), tr("数据手册要写成 http:// 或 https:// 开头的网址（或留空）。", "The datasheet must be an http:// or https:// address (or empty)."), c.id));
  }
  if (a.extra.length > LIMITS.extraPairs) {
    out.push(issue("error", at(".annotations.extra"), tr(`补充项最多 ${LIMITS.extraPairs} 条。`, `At most ${LIMITS.extraPairs} extra entries.`), c.id));
  }
  const seen = new Set<string>();
  a.extra.forEach((e, i) => {
    const p = at(`.annotations.extra[${i}]`);
    if (e.key.trim() === "") out.push(issue("error", p, tr("补充项的名称不能为空。", "An extra entry needs a name."), c.id));
    else if (charCount(e.key) > LIMITS.extraKeyMax) out.push(issue("error", p, tr(`名称最多 ${LIMITS.extraKeyMax} 个字符。`, `The name is at most ${LIMITS.extraKeyMax} characters.`), c.id));
    if (charCount(e.value) > LIMITS.extraValueMax) out.push(issue("error", p, tr(`内容最多 ${LIMITS.extraValueMax} 个字符。`, `The value is at most ${LIMITS.extraValueMax} characters.`), c.id));
    if (seen.has(e.key)) out.push(issue("warn", p, tr(`名称“${e.key}”重复。`, `The name "${e.key}" is repeated.`), c.id));
    seen.add(e.key);
  });

  // links: references only
  for (const key of ["device_id", "slot"] as const) {
    const v = c.links[key];
    if (v !== null && !LIMITS.refPattern.test(v)) {
      out.push(issue("error", at(`.links.${key}`), tr("只能含字母、数字、下划线、点、冒号和连字符，最多 64 个字符。", "Letters, digits, underscore, dot, colon and hyphen only, at most 64 characters."), c.id));
    }
  }
  return out;
}

export function validateLayout(layout: OpticalLayout): Issue[] {
  const out: Issue[] = [];
  const idProblem = layoutIdFormatProblem(layout.id);
  if (idProblem === "format") {
    out.push(issue("error", "id", tr("光路图编号只能含字母、数字、下划线和连字符，1 到 40 个字符。", "A layout id may contain letters, digits, underscore and hyphen, 1 to 40 characters.")));
  } else if (idProblem === "reserved") {
    out.push(issue("error", "id", tr("这个编号是 Windows 的保留名，不能做文件名。", "This id is a reserved Windows name and cannot be a file name.")));
  }
  if (layout.title.trim() === "") out.push(issue("warn", "title", tr("还没有标题。", "The layout has no title yet.")));
  if (charCount(layout.title) > LIMITS.titleMax) out.push(issue("error", "title", tooLong(tr("标题", "The title"), LIMITS.titleMax)));
  if (charCount(layout.notes) > LIMITS.notesMax) out.push(issue("error", "notes", tooLong(tr("备注", "The notes"), LIMITS.notesMax)));

  const t = layout.table;
  for (const key of ["width_mm", "height_mm"] as const) {
    if (!finite(t[key]) || t[key] < LIMITS.tableSizeMin || t[key] > LIMITS.tableSizeMax) {
      out.push(issue("error", `table.${key}`, tr(`平台尺寸要在 ${LIMITS.tableSizeMin} 到 ${LIMITS.tableSizeMax} mm 之间。`, `The table size must be between ${LIMITS.tableSizeMin} and ${LIMITS.tableSizeMax} mm.`)));
    }
  }
  if (!finite(t.hole_pitch_mm) || t.hole_pitch_mm < LIMITS.pitchMin || t.hole_pitch_mm > LIMITS.pitchMax) {
    out.push(issue("error", "table.hole_pitch_mm", tr(`孔距要在 ${LIMITS.pitchMin} 到 ${LIMITS.pitchMax} mm 之间。`, `The hole pitch must be between ${LIMITS.pitchMin} and ${LIMITS.pitchMax} mm.`)));
  }
  if (!finite(t.hole_offset_mm) || t.hole_offset_mm < 0 || t.hole_offset_mm > LIMITS.holeOffsetMax) {
    out.push(issue("error", "table.hole_offset_mm", tr(`首孔偏移要在 0 到 ${LIMITS.holeOffsetMax} mm 之间。`, `The first hole's offset must be between 0 and ${LIMITS.holeOffsetMax} mm.`)));
  }
  if (!(GRID_KINDS as readonly string[]).includes(t.grid)) out.push(issue("error", "table.grid", tr("孔位样式只能是 metric、imperial 或 none。", "The grid must be metric, imperial or none.")));

  if (layout.components.length > LIMITS.components) {
    out.push(issue("error", "components", tr(`器件最多 ${LIMITS.components} 个。`, `At most ${LIMITS.components} components.`)));
  }
  if (layout.components.filter((c) => isSourceType(c.type)).length > LIMITS.sources) {
    out.push(issue("error", "components", tr(`光源最多 ${LIMITS.sources} 个。`, `At most ${LIMITS.sources} light sources.`)));
  }
  if (layout.texts.length > LIMITS.texts) out.push(issue("error", "texts", tr(`文字标注最多 ${LIMITS.texts} 条。`, `At most ${LIMITS.texts} text labels.`)));

  // an id is unique over components and text labels together
  const ids = new Set<string>();
  for (const c of layout.components) {
    if (ids.has(c.id)) out.push(issue("error", `components[${c.id}].id`, tr(`编号 ${c.id} 重复。`, `The id ${c.id} is used twice.`), c.id));
    ids.add(c.id);
    out.push(...validateComponent(c, layout));
  }
  for (const x of layout.texts) {
    const p = `texts[${x.id}]`;
    if (!LIMITS.idPattern.test(x.id)) out.push(issue("error", `${p}.id`, tr("文字标注的编号格式不对。", "A text label has a malformed id.")));
    if (ids.has(x.id)) out.push(issue("error", `${p}.id`, tr(`编号 ${x.id} 重复。`, `The id ${x.id} is used twice.`)));
    ids.add(x.id);
    if (!finite(x.x_mm) || !finite(x.y_mm) || Math.abs(x.x_mm) > LIMITS.coordAbs || Math.abs(x.y_mm) > LIMITS.coordAbs) {
      out.push(issue("error", p, tr("文字标注的坐标必须是不超过 100000 mm 的数。", "A text label needs numeric coordinates within 100000 mm.")));
    }
    if (charCount(x.text) > LIMITS.stringMax) out.push(issue("error", `${p}.text`, tooLong(tr("文字", "The text"), LIMITS.stringMax)));
    if (!finite(x.size) || x.size < LIMITS.textSizeMin || x.size > LIMITS.textSizeMax) {
      out.push(issue("error", `${p}.size`, tr(`大小要在 ${LIMITS.textSizeMin} 到 ${LIMITS.textSizeMax} 之间。`, `The size must be between ${LIMITS.textSizeMin} and ${LIMITS.textSizeMax}.`)));
    }
  }
  if (jsonBytes(layout) > LIMITS.jsonBytes) out.push(issue("error", "layout", tr("整张图太大了，超过 1 MB。", "The layout is larger than 1 MB.")));
  return out;
}

export function errorsOf(issues: readonly Issue[]): Issue[] {
  return issues.filter((i) => i.level === "error");
}

/** Messages of a component's own issues, for the field they belong to. */
export function issuesFor(issues: readonly Issue[], componentId: string, suffix: string): Issue[] {
  const prefix = `components[${componentId}]${suffix}`;
  return issues.filter((i) => i.path === prefix || i.path.startsWith(`${prefix}.`) || i.path.startsWith(`${prefix}[`));
}
