// The table exports of the optical layout (docs/v2/design/optical_layout_exports.md): a bill of materials
// and a table of the beams, as CSV files for a spreadsheet.
//
//   * bomCsv(): one row per component (what it is, its model and maker, how it is wired, where it stands),
//     then, after an empty row, the components counted by model and manufacturer;
//   * beamTableCsv(): one row per segment of every beam, with the power, polarisation and spot size the beam
//     inspector shows for it. Read off the editor's own trace (trace.ts) with the functions of beam.ts that the
//     inspector uses, so a number here is the number on the screen.
//
// The files are for Excel as much as for a program:
//   * UTF-8 with a byte order mark, without which Excel reads Chinese as mojibake;
//   * CRLF line ends, fields quoted when they hold a comma, a quote, a line break or leading / trailing blanks
//     (RFC 4180), a quote inside written twice;
//   * a text cell a spreadsheet would take for a formula (it starts with = + - @, a tab or a line break, or their
//     full-width forms, which some versions also read) gets a ' in front (CSV injection). Only text cells: a number
//     is written as a number, so a negative coordinate or Stokes component stays a number.
// Pure, so the tests run without a browser (test/opticalLayoutExportCsv.test.ts).

import { tr } from "../i18n.ts";
import { typeLabel } from "./catalog.ts";
import { pyRound } from "./geometry.ts";
import { polarizationText, powerText, propagateQ, spotDiameterMm, waistDiameterMm, waistInSegment } from "./beam.ts";
import type { LayoutComponent, OpticalLayout } from "./types.ts";
import type { DetailedBranch, DetailedTrace } from "./trace.ts";

// ── the CSV writer ────────────────────────────────────────────────────────────

/** The byte order mark that tells Excel the file is UTF-8. */
export const CSV_BOM = "﻿";
/** Line end of the file (RFC 4180, and what Excel writes). */
export const CSV_EOL = "\r\n";
/** What goes in front of a text cell a spreadsheet would read as a formula. */
export const FORMULA_GUARD = "'";

/**
 * First characters with which a spreadsheet takes a cell for a formula, or strips them and then does: = + - @, a
 * tab, a carriage return, a line feed, and the full-width = + - @.
 */
const FORMULA_LEADS: ReadonlySet<string> = new Set(["=", "+", "-", "@", "\t", "\r", "\n", "＝", "＋", "－", "＠"]);

/** A cell: text, a number, or nothing (an empty cell). */
export type Cell = string | number | null | undefined;

/** `text` with a ' in front when a spreadsheet would read it as a formula. */
export function guardFormula(text: string): string {
  return text.length > 0 && FORMULA_LEADS.has(text[0]!) ? FORMULA_GUARD + text : text;
}

/** A number as a cell: the shortest decimal that reads back as the same double; no "-0"; empty when not finite. */
export function numberCell(x: number): string {
  return Number.isFinite(x) ? String(x + 0) : "";
}

/** A field that must be quoted: it holds a quote, a comma or a line break, or starts or ends with a blank. */
const NEEDS_QUOTES = /[",\r\n]|^\s|\s$/;

/** One cell as it is written in the file. */
export function csvField(v: Cell): string {
  if (v === null || v === undefined) return "";
  if (typeof v === "number") return numberCell(v);
  const t = guardFormula(v);
  return NEEDS_QUOTES.test(t) ? `"${t.replace(/"/g, '""')}"` : t;
}

/** One row of cells. */
export function csvRow(cells: readonly Cell[]): string {
  return cells.map(csvField).join(",");
}

/** A whole file: the byte order mark, then every row, each ended with CRLF. An empty row is an empty line. */
export function csvText(rows: ReadonlyArray<readonly Cell[]>): string {
  return CSV_BOM + rows.map((r) => csvRow(r) + CSV_EOL).join("");
}

// ── file names ────────────────────────────────────────────────────────────────

export type TableKind = "bom" | "beams";

/** `<id>-r<revision>[-unsaved]-<kind>.csv`, with nothing in it a file system dislikes. */
export function tableFileName(layout: Pick<OpticalLayout, "id" | "revision">, kind: TableKind, unsaved = false): string {
  const id = layout.id.replace(/[^A-Za-z0-9_-]/g, "_") || "layout";
  return `${id}-r${Math.max(0, Math.floor(layout.revision))}${unsaved ? "-unsaved" : ""}-${kind}.csv`;
}

// ── bill of materials ─────────────────────────────────────────────────────────

/** Coordinates and angles at the 0.001 the layout stores them at. */
const r3 = (x: number): number => pyRound(x, 3) + 0;

export function bomHeader(): string[] {
  return [
    "id",
    tr("类型", "Type"),
    tr("标签", "Label"),
    tr("型号", "Model"),
    tr("厂家", "Manufacturer"),
    tr("用途", "Function"),
    tr("电动", "Motorized"),
    tr("执行器", "Actuator"),
    tr("连接", "Connection"),
    tr("数据手册", "Datasheet"),
    tr("备注", "Notes"),
    "device_id",
    "slot",
    "x (mm)",
    "y (mm)",
    tr("角度 (°)", "Angle (°)"),
    tr("附加字段", "Extra fields"),
  ];
}

/** The extra key / value pairs of a component in one cell: "key=value; key=value". */
function extraText(c: LayoutComponent): string {
  return c.annotations.extra.map((p) => `${p.key}=${p.value}`).join("; ");
}

export function bomRow(c: LayoutComponent): Cell[] {
  const a = c.annotations;
  return [
    c.id,
    typeLabel(c.type),
    c.label,
    a.model,
    a.manufacturer,
    a.function,
    a.motorized ? tr("是", "yes") : tr("否", "no"),
    a.actuator,
    a.connection,
    a.datasheet_url,
    a.notes,
    c.links.device_id ?? "",
    c.links.slot ?? "",
    r3(c.x_mm),
    r3(c.y_mm),
    r3(c.angle_deg),
    extraText(c),
  ];
}

/** One line of the totals: the components of one model by one manufacturer. */
export interface BomGroup {
  /** as first written in the layout (blanks at the ends removed) */
  model: string;
  manufacturer: string;
  count: number;
  /** the types among them, by name, in the order they first come */
  types: string[];
  ids: string[];
}

/** What two spellings of one model or maker have in common: no blanks at the ends, single blanks inside, any case. */
const groupKey = (s: string): string => s.trim().replace(/\s+/g, " ").toLowerCase();

/**
 * The components counted by model and manufacturer. "Thorlabs" and "thorlabs " are one maker (the key ignores case
 * and blanks; the first spelling is the one shown). Groups with a manufacturer come first, by manufacturer then model
 * (by code point, the same on every machine); then those with a model and no manufacturer; the components with
 * neither come last, as one group.
 */
export function bomGroups(components: readonly LayoutComponent[]): BomGroup[] {
  const groups = new Map<string, BomGroup & { mk: string; mo: string }>();
  for (const c of components) {
    const mo = groupKey(c.annotations.model);
    const mk = groupKey(c.annotations.manufacturer);
    const key = `${mk}\u0000${mo}`;
    let g = groups.get(key);
    if (!g) {
      g = { model: c.annotations.model.trim(), manufacturer: c.annotations.manufacturer.trim(), count: 0, types: [], ids: [], mk, mo };
      groups.set(key, g);
    }
    g.count += 1;
    g.ids.push(c.id);
    const t = typeLabel(c.type);
    if (!g.types.includes(t)) g.types.push(t);
  }
  const rank = (s: string): number => (s === "" ? 1 : 0);
  const cmp = (a: string, b: string): number => (a < b ? -1 : a > b ? 1 : 0);
  return [...groups.values()]
    .sort((a, b) => rank(a.mk) - rank(b.mk) || cmp(a.mk, b.mk) || rank(a.mo) - rank(b.mo) || cmp(a.mo, b.mo))
    .map(({ model, manufacturer, count, types, ids }) => ({ model, manufacturer, count, types, ids }));
}

export function bomGroupHeader(): string[] {
  return [tr("型号", "Model"), tr("厂家", "Manufacturer"), tr("数量", "Count"), tr("类型", "Types"), tr("器件", "Components")];
}

/** The rows of the bill of materials: the components, an empty row, a title, the totals by model and manufacturer. */
export function bomRows(layout: OpticalLayout): Cell[][] {
  const rows: Cell[][] = [bomHeader(), ...layout.components.map(bomRow)];
  rows.push([], [tr("按型号与厂家汇总", "Totals by model and manufacturer")], bomGroupHeader());
  for (const g of bomGroups(layout.components)) rows.push([g.model, g.manufacturer, g.count, g.types.join(tr("、", ", ")), g.ids.join(" ")]);
  return rows;
}

export function bomCsv(layout: OpticalLayout): string {
  return csvText(bomRows(layout));
}

// ── the beams ─────────────────────────────────────────────────────────────────

/** What a component does to the beam at the end of a segment, as the beam inspector says it. */
export function actionText(action: string): string {
  switch (action) {
    case "reflect":
      return tr("反射", "reflects");
    case "transmit":
      return tr("透射", "passes");
    case "split":
      return tr("分束", "splits");
    case "stop":
      return tr("终止", "stops");
    case "blocked":
      return tr("挡住", "blocks");
    case "back_hit":
      return tr("打在背面", "hits the back");
    default:
      return tr("作用", "acts on it");
  }
}

/** Why a branch ends where no component is (the last segment of a beam that leaves the table, ...), as the inspector says it. */
export function endText(end: string): string {
  switch (end) {
    case "left_table":
      return tr("离开台面", "leaves the table");
    case "max_events":
      return tr("作用次数到上限", "64 interactions reached");
    case "budget":
      return tr("追迹在此截断", "the trace stops here");
    case "blocked":
      return tr("被挡住，光到此为止", "blocked: the light ends here");
    case "back_hit":
      return tr("打在器件背面，光到此为止", "hits the back of a component: the light ends here");
    case "stop":
      return tr("被器件接收", "received by a component");
    case "split":
      return tr("在这里分束", "splits here");
    default:
      return tr("光到此为止", "the light ends here");
  }
}

export function beamHeader(): string[] {
  return [
    tr("光源", "Source"),
    tr("光源 id", "Source id"),
    tr("波长 (nm)", "Wavelength (nm)"),
    tr("分支", "Branch"),
    tr("段", "Segment"),
    tr("起点", "From"),
    tr("起点 id", "From id"),
    tr("终点", "To"),
    tr("终点 id", "To id"),
    tr("终点处", "At the end"),
    tr("长度 (mm)", "Length (mm)"),
    tr("光程起点 (mm)", "Path at start (mm)"),
    tr("相对功率 (%)", "Relative power (%)"),
    tr("功率 (mW)", "Power (mW)"),
    tr("功率", "Power"),
    tr("偏振", "Polarisation"),
    "s1 = S1/S0",
    "s2 = S2/S0",
    "s3 = S3/S0",
    tr("段首直径 1/e² (mm)", "Diameter at start, 1/e² (mm)"),
    tr("段尾直径 1/e² (mm)", "Diameter at end, 1/e² (mm)"),
    tr("束腰离段首 (mm)", "Waist from start (mm)"),
    tr("束腰直径 1/e² (mm)", "Waist diameter, 1/e² (mm)"),
  ];
}

/**
 * The numbers at the precision of the contract trace (v2 section 4.8 and 12.2): lengths at 0.001 mm, the relative
 * power at 1e-9 (so the percentage at 1e-7), mW at 9 decimals, Stokes at 1e-9, diameters at 1e-9 mm, the waist
 * position at 1e-6 mm (a focus 4.000002 mm behind an objective keeps its last digit).
 */
const r6 = (x: number): number => pyRound(x, 6) + 0;
const r7 = (x: number): number => pyRound(x, 7) + 0;
const r9 = (x: number): number => pyRound(x, 9) + 0;
const r9n = (x: number | null): number | null => (x === null ? null : r9(x));

/**
 * One row per segment of every beam: trees in the order of the trace, branches in theirs, segments in turn. The
 * component a segment starts from is the one the beam inspector names: the event before it, or for the first
 * segment the source (the root) or the splitter the branch came out of. Every beam number is read with the
 * functions the inspector uses (powerText, polarizationText, spotDiameterMm, waistInSegment, ...); a segment of a
 * trace that has no beam data (an older server's) has those cells empty.
 */
export function beamRows(layout: OpticalLayout, trace: DetailedTrace): Cell[][] {
  const labels = new Map(layout.components.map((c) => [c.id, c.label || c.id]));
  const label = (id: string): string => labels.get(id) ?? id;
  const header = beamHeader();
  const rows: Cell[][] = [header];
  for (const tree of trace) {
    const wl = tree.wavelength_nm;
    const m2 = tree.beam?.m2 ?? null;
    const sourceMw = tree.beam?.source_mw ?? null;
    const byId = new Map<string, DetailedBranch>(tree.branches.map((b) => [b.branch, b]));
    for (const br of tree.branches) {
      const parent = br.parent === null ? undefined : byId.get(br.parent);
      br.segments.forEach((seg, i) => {
        const fromId = i > 0 ? br.events[i - 1]!.component : br.parent === null ? tree.source : (parent?.events.at(-1)?.component ?? tree.source);
        const ev = br.events[i];
        const L = Math.hypot(seg.x1 - seg.x0, seg.y1 - seg.y0);
        const startPath = i > 0 ? br.events[i - 1]!.path_mm : br.startPath;
        const b = seg.beam;
        const row: Cell[] = [
          label(tree.source),
          tree.source,
          wl,
          br.branch,
          i + 1,
          label(fromId),
          fromId,
          ev ? label(ev.component) : "",
          ev ? ev.component : "",
          ev ? actionText(ev.action) : endText(br.end),
          r3(L),
          r3(startPath),
        ];
        if (!b) {
          // every row as wide as the header: the beam cells empty
          rows.push([...row, ...new Array<Cell>(header.length - row.length).fill(null)]);
          return;
        }
        const mw = sourceMw === null ? null : sourceMw * b.p;
        const d0 = spotDiameterMm(b.q, wl, m2);
        const d1 = b.q === null ? null : spotDiameterMm(propagateQ(b.q, L), wl, m2);
        const w = waistInSegment(b.q, L);
        // as the inspector says it: a waist up to 0.001 mm past the end (section 12.3.3) is at the end
        const waistAt = w === null ? null : Math.min(L, Math.max(0, w));
        const waistD = w === null || b.q === null || wl === null || m2 === null ? null : waistDiameterMm(b.q, wl, m2);
        row.push(
          r7(b.p * 100),
          r9n(mw),
          powerText(b.p, mw),
          polarizationText(b.s),
          b.s === null ? null : r9(b.s[0]),
          b.s === null ? null : r9(b.s[1]),
          b.s === null ? null : r9(b.s[2]),
          r9n(d0),
          r9n(d1),
          waistAt === null ? null : r6(waistAt),
          r9n(waistD),
        );
        rows.push(row);
      });
    }
  }
  return rows;
}

export function beamTableCsv(layout: OpticalLayout, trace: DetailedTrace): string {
  return csvText(beamRows(layout, trace));
}
