// ════════════════════════════════════════════════════════════════════════════
// Tests for src/lib/opticalLayout/exportCsv.ts: the bill of materials and the beam table
// (docs/v2/design/optical_layout_exports.md).
//
//     cd frontend && npm run test:unit
//
// What is pinned:
//   · the file is one Excel opens right: a UTF-8 byte order mark, CRLF, RFC 4180 quoting; read back with
//     an independent parser (below), every cell is what went in, Chinese, commas, quotes, line breaks;
//   · a text cell a spreadsheet would run as a formula gets a ' in front, a number never does (a negative
//     coordinate or Stokes component stays a number);
//   · the bill of materials: one row per component with the 16 columns asked for, then the totals by model
//     and manufacturer ("Thorlabs" and "thorlabs " are one maker), the unfilled ones last;
//   · the beam table: one row per segment, its numbers anchored in physics (a PBS splits +45° light in two
//     halves, H and V; the focus of an objective, case H of v2 section 7) and its words those of beam.ts,
//     which the beam inspector shows.
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it } from "node:test";

import {
  CSV_BOM,
  FORMULA_GUARD,
  actionText,
  beamHeader,
  beamRows,
  beamTableCsv,
  bomCsv,
  bomGroups,
  bomHeader,
  bomRows,
  csvField,
  csvText,
  endText,
  guardFormula,
  numberCell,
  tableFileName,
} from "../src/lib/opticalLayout/exportCsv.ts";
import { traceLayoutDetailed, type DetailedTrace } from "../src/lib/opticalLayout/trace.ts";
import { polarizationText, powerText } from "../src/lib/opticalLayout/beam.ts";
import type { Annotations, LayoutComponent } from "../src/lib/opticalLayout/types.ts";
import { comp, layoutOf } from "./helpers/opticalLayout.ts";

const HERE = fileURLToPath(new URL(".", import.meta.url));
const COMPONENTS = join(HERE, "..", "src", "components", "opticallayout");

/**
 * RFC 4180 read back, strictly: the file starts with the BOM, every record ends with CRLF, a quote only opens a
 * field or is doubled inside a quoted one, no bare CR or LF outside quotes. Independent of the writer under test.
 */
function parseCsv(text: string): string[][] {
  assert.ok(text.startsWith(CSV_BOM), "starts with the byte order mark");
  const s = text.slice(CSV_BOM.length);
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let quoted = false;
  let wasQuoted = false;
  for (let i = 0; i < s.length; ) {
    const ch = s[i]!;
    if (quoted) {
      if (ch === '"') {
        if (s[i + 1] === '"') {
          field += '"';
          i += 2;
          continue;
        }
        quoted = false;
        i += 1;
        assert.ok(s[i] === "," || s[i] === "\r" || i >= s.length, `a closing quote must end the field (at ${i})`);
        continue;
      }
      field += ch;
      i += 1;
      continue;
    }
    if (ch === '"') {
      assert.equal(field, "", `a quote inside an unquoted field (at ${i})`);
      assert.ok(!wasQuoted);
      quoted = true;
      wasQuoted = true;
      i += 1;
      continue;
    }
    if (ch === ",") {
      row.push(field);
      field = "";
      wasQuoted = false;
      i += 1;
      continue;
    }
    if (ch === "\r") {
      assert.equal(s[i + 1], "\n", `a bare CR outside quotes (at ${i})`);
      row.push(field);
      rows.push(row);
      row = [];
      field = "";
      wasQuoted = false;
      i += 2;
      continue;
    }
    assert.notEqual(ch, "\n", `a bare LF outside quotes (at ${i})`);
    field += ch;
    i += 1;
  }
  assert.ok(!quoted, "a quoted field is closed");
  assert.equal(field, "", "the file ends with CRLF");
  assert.equal(row.length, 0, "the file ends with CRLF");
  return rows;
}

const ANN = (over: Partial<Annotations>): Annotations => ({
  model: "",
  manufacturer: "",
  function: "",
  motorized: false,
  actuator: "",
  connection: "",
  datasheet_url: "",
  notes: "",
  extra: [],
  ...over,
});

const withAnn = (c: LayoutComponent, over: Partial<Annotations>, links: Partial<LayoutComponent["links"]> = {}): LayoutComponent => ({
  ...c,
  annotations: ANN(over),
  links: { ...c.links, ...links },
});

describe("the CSV writer", () => {
  it("a text cell a spreadsheet would read as a formula gets a ' in front", () => {
    for (const lead of ["=", "+", "-", "@", "\t", "\r", "\n", "＝", "＋", "－", "＠"]) {
      assert.equal(guardFormula(`${lead}1+1`), `${FORMULA_GUARD}${lead}1+1`, JSON.stringify(lead));
    }
    for (const fine of ["", "a=1", " =1", "1+1", "'=1", "M1", "反射镜", "Ø1 mm"]) assert.equal(guardFormula(fine), fine, JSON.stringify(fine));
  });

  it("a number is written as a number and never guarded: a negative one stays negative", () => {
    assert.equal(csvField(-12.5), "-12.5");
    assert.equal(csvField(-1), "-1");
    assert.equal(csvField(-0), "0");
    assert.equal(csvField(0.1 + 0.2), "0.30000000000000004", "the double as it is (callers round)");
    assert.equal(csvField(1e-7), "1e-7");
    assert.equal(numberCell(Number.NaN), "");
    assert.equal(numberCell(Number.POSITIVE_INFINITY), "");
    assert.equal(csvField(null), "");
    assert.equal(csvField(undefined), "");
    assert.equal(csvField("-1"), "'-1", "the same characters as text are guarded");
  });

  it("quotes a field with a comma, a quote, a line break or blanks at its ends, and doubles the quote", () => {
    assert.equal(csvField("a,b"), '"a,b"');
    assert.equal(csvField('say "hi"'), '"say ""hi"""');
    assert.equal(csvField("two\nlines"), '"two\nlines"');
    assert.equal(csvField("cr\rhere"), '"cr\rhere"');
    assert.equal(csvField(" lead"), '" lead"');
    assert.equal(csvField("trail "), '"trail "');
    assert.equal(csvField("plain"), "plain");
    assert.equal(csvField("中文，全角逗号"), "中文，全角逗号", "a full-width comma is not a separator");
    // guarded first, then quoted: the ' sits inside the quotes
    assert.equal(csvField('=HYPERLINK("http://x","y")'), `"'=HYPERLINK(""http://x"",""y"")"`);
    assert.equal(csvField("\tcmd"), "'\tcmd");
    assert.equal(csvField("\nx"), "\"'\nx\"");
  });

  it("a file: the byte order mark, CRLF after every row, an empty row an empty line; UTF-8 bytes EF BB BF", () => {
    const text = csvText([["a", 1], [], ["中文", -2]]);
    assert.equal(text, "﻿a,1\r\n\r\n中文,-2\r\n");
    const bytes = Buffer.from(text, "utf8");
    assert.deepEqual([...bytes.subarray(0, 3)], [0xef, 0xbb, 0xbf]);
    assert.equal(bytes.subarray(3).toString("utf8"), "a,1\r\n\r\n中文,-2\r\n");
    assert.deepEqual(parseCsv(text), [["a", "1"], [""], ["中文", "-2"]]);
  });

  it("whatever goes in comes back out of a strict reader: Chinese, commas, quotes, line breaks, empty cells", () => {
    const nasty = ["反射镜，M1", 'He said "go"', "line1\r\nline2", "", " ", "a,b,c", '"', "\n", "µm Ø χ ψ ±45°", "𝛌 surrogate"];
    const rows = parseCsv(csvText([nasty, nasty.map((x) => x + "!")]));
    assert.deepEqual(rows[0], nasty.map((x) => guardFormula(x)));
    assert.deepEqual(rows[1], nasty.map((x) => guardFormula(x + "!")));
  });

  it("names the files after the layout and its revision", () => {
    assert.equal(tableFileName({ id: "shg-main", revision: 3 }, "bom"), "shg-main-r3-bom.csv");
    assert.equal(tableFileName({ id: "shg-main", revision: 3 }, "beams", true), "shg-main-r3-unsaved-beams.csv");
    assert.equal(tableFileName({ id: "a/b:c", revision: 0 }, "bom"), "a_b_c-r0-bom.csv");
  });
});

describe("the bill of materials", () => {
  const m1 = withAnn(
    comp("mirror", "m1", 112.5, 62.5, 135, { label: "M1 转向" }),
    {
      model: "BB1-E02",
      manufacturer: "Thorlabs",
      function: "转向, steering",
      motorized: true,
      actuator: "Z825B",
      connection: "KDC101 COM15",
      datasheet_url: "https://www.thorlabs.com/x?a=1&b=2",
      notes: '第一行\n第二行, "引号"',
      extra: [{ key: "serial", value: "S/N 42" }, { key: "mount", value: "KM100" }],
    },
    { device_id: "pico1", slot: "excitation_waveplate" },
  );
  const m2 = withAnn(comp("mirror", "m2", -25.4, 300, 315), { model: "bb1-e02 ", manufacturer: " thorlabs" });
  const evil = withAnn(comp("lens", "len1", 50, 50, 0, { label: "=HYPERLINK(\"http://evil\")" }), { model: "@SUM(A1)", manufacturer: "+cmd", function: "-2+3", notes: "\tTab", connection: "\rCR" });
  const bare = comp("detector", "det1", 112.5, 200, 270);
  const layout = layoutOf([m1, bare, m2, evil]);

  it("one row per component, in the layout's order, with the columns asked for (and the extra fields last)", () => {
    const rows = parseCsv(bomCsv(layout));
    assert.deepEqual(rows[0], bomHeader());
    assert.deepEqual(rows[0], ["id", "类型", "标签", "型号", "厂家", "用途", "电动", "执行器", "连接", "数据手册", "备注", "device_id", "slot", "x (mm)", "y (mm)", "角度 (°)", "附加字段"]);
    assert.deepEqual(rows.slice(1, 5).map((r) => r[0]), ["m1", "det1", "m2", "len1"]);
    assert.deepEqual(rows[1], [
      "m1",
      "反射镜",
      "M1 转向",
      "BB1-E02",
      "Thorlabs",
      "转向, steering",
      "是",
      "Z825B",
      "KDC101 COM15",
      "https://www.thorlabs.com/x?a=1&b=2",
      '第一行\n第二行, "引号"',
      "pico1",
      "excitation_waveplate",
      "112.5",
      "62.5",
      "135",
      "serial=S/N 42; mount=KM100",
    ]);
    for (const r of rows.slice(1, 5)) assert.equal(r.length, rows[0]!.length, `${r[0]}: as wide as the header`);
  });

  it("empty fields are empty cells; no link is an empty cell, not 'null'", () => {
    const row = parseCsv(bomCsv(layout))[2]!;
    assert.equal(row[0], "det1");
    assert.equal(row[1], "探测器");
    assert.deepEqual(row.slice(3, 6), ["", "", ""]);
    assert.equal(row[6], "否");
    assert.deepEqual(row.slice(7, 13), ["", "", "", "", "", ""]);
    assert.ok(!row.includes("null") && !row.includes("undefined"));
  });

  it("a negative coordinate is a number, a formula-looking text is not run", () => {
    const rows = parseCsv(bomCsv(layout));
    const m2row = rows[3]!;
    assert.equal(m2row[13], "-25.4", "x stays a number");
    const evilRow = rows[4]!;
    assert.equal(evilRow[2], `'=HYPERLINK("http://evil")`);
    assert.equal(evilRow[3], "'@SUM(A1)");
    assert.equal(evilRow[4], "'+cmd");
    assert.equal(evilRow[5], "'-2+3");
    assert.equal(evilRow[8], "'\rCR");
    assert.equal(evilRow[10], "'\tTab");
    // and nothing in the whole file starts a cell with a formula lead except behind the guard
    for (const r of rows) for (const cell of r) if (!/^-?[\d.]+(?:e-?\d+)?$/.test(cell)) assert.ok(!/^[=+\-@\t\r\n＝＋－＠]/.test(cell), JSON.stringify(cell));
  });

  it("the totals: one line per model and maker, spelling and case aside; makers in order, the unfilled last", () => {
    const groups = bomGroups(layout.components);
    assert.deepEqual(
      groups.map((g) => [g.model, g.manufacturer, g.count, g.types, g.ids]),
      [
        ["@SUM(A1)", "+cmd", 1, ["透镜"], ["len1"]],
        ["BB1-E02", "Thorlabs", 2, ["反射镜"], ["m1", "m2"]],
        ["", "", 1, ["探测器"], ["det1"]],
      ],
    );
    const rows = parseCsv(bomCsv(layout));
    const at = rows.findIndex((r) => r[0] === "按型号与厂家汇总");
    assert.ok(at > 0 && rows[at - 1]!.join("") === "", "after an empty line");
    assert.deepEqual(rows[at + 1], ["型号", "厂家", "数量", "类型", "器件"]);
    assert.deepEqual(rows[at + 2], ["'@SUM(A1)", "'+cmd", "1", "透镜", "len1"]);
    assert.deepEqual(rows[at + 3], ["BB1-E02", "Thorlabs", "2", "反射镜", "m1 m2"]);
    assert.deepEqual(rows[at + 4], ["", "", "1", "探测器", "det1"]);
    assert.equal(rows.length, at + 5);
  });

  it("a model without a maker comes after the makers and before the unfilled; types are listed once, in order", () => {
    const l = layoutOf([
      withAnn(comp("mirror", "a", 0, 0, 0), { model: "X1" }),
      withAnn(comp("lens", "b", 0, 0, 0), { manufacturer: "Edmund", model: "L2" }),
      comp("mirror", "c", 0, 0, 0),
      withAnn(comp("lens", "d", 0, 0, 0), { model: "X1" }),
      withAnn(comp("mirror", "e", 0, 0, 0), { manufacturer: "edmund", model: "L2" }),
    ]);
    assert.deepEqual(
      bomGroups(l.components).map((g) => [g.manufacturer, g.model, g.count, g.types.join("/")]),
      [
        ["Edmund", "L2", 2, "透镜/反射镜"],
        ["", "X1", 2, "反射镜/透镜"],
        ["", "", 1, "反射镜"],
      ],
    );
  });

  it("an empty layout has the headers and nothing else", () => {
    const rows = parseCsv(bomCsv(layoutOf([])));
    assert.deepEqual(rows, [bomHeader(), [""], ["按型号与厂家汇总"], ["型号", "厂家", "数量", "类型", "器件"]]);
    assert.deepEqual(bomRows(layoutOf([])).length, 4);
  });
});

describe("the beam table", () => {
  // +45° linear, 50 mW, Ø1 mm at 532 nm into a PBS: half of it transmitted as p (0°), half reflected as s (90°)
  const pbsLayout = layoutOf([
    comp("laser", "las1", 0, 100, 0, { label: "激光器 A", props: { wavelength_nm: 532, power_mw: 50, polarization: "linear", pol_angle_deg: 45, beam_diameter_mm: 1 } }),
    comp("pbs", "pbs1", 100, 100, 135, { label: "PBS, 立方" }),
    comp("detector", "dr", 100, 300, 270, { label: "反射口" }),
    comp("detector", "dt", 300, 100, 180),
  ]);
  const col = (name: string): number => {
    const i = beamHeader().indexOf(name);
    assert.ok(i >= 0, name);
    return i;
  };

  it("one row per segment, trees and branches in the order of the trace, as wide as the header", () => {
    const trace = traceLayoutDetailed(pbsLayout);
    const rows = parseCsv(beamTableCsv(pbsLayout, trace));
    assert.deepEqual(rows[0], beamHeader());
    const expected = trace.flatMap((t) => t.branches.flatMap((b) => b.segments.map((_, i) => `${t.source}|${b.branch}|${i + 1}`)));
    assert.deepEqual(rows.slice(1).map((r) => `${r[col("光源 id")]}|${r[col("分支")]}|${r[col("段")]}`), expected);
    assert.deepEqual(expected, ["las1|0|1", "las1|0.r|1", "las1|0.t|1"]);
    for (const r of rows) assert.equal(r.length, beamHeader().length);
  });

  it("from and to by name and id: a child branch starts at the splitter it came out of", () => {
    const rows = parseCsv(beamTableCsv(pbsLayout, traceLayoutDetailed(pbsLayout)));
    const pick = (r: string[]) => [r[col("光源")], r[col("起点")], r[col("起点 id")], r[col("终点")], r[col("终点 id")], r[col("终点处")], r[col("长度 (mm)")], r[col("光程起点 (mm)")]];
    assert.deepEqual(pick(rows[1]!), ["激光器 A", "激光器 A", "las1", "PBS, 立方", "pbs1", "分束", "100", "0"]);
    assert.deepEqual(pick(rows[2]!), ["激光器 A", "PBS, 立方", "pbs1", "反射口", "dr", "终止", "200", "100"]);
    assert.deepEqual(pick(rows[3]!), ["激光器 A", "PBS, 立方", "pbs1", "DT", "dt", "终止", "200", "100"]);
  });

  it("the power and polarisation: 50 mW at +45°, then 25 mW s (V) reflected and 25 mW p (H) transmitted", () => {
    const rows = parseCsv(beamTableCsv(pbsLayout, traceLayoutDetailed(pbsLayout)));
    const pick = (r: string[]) => [r[col("相对功率 (%)")], r[col("功率 (mW)")], r[col("功率")], r[col("偏振")], r[col("s1 = S1/S0")], r[col("s2 = S2/S0")], r[col("s3 = S3/S0")]];
    assert.deepEqual(pick(rows[1]!), ["100", "50", "50 mW", "线偏 45°", "0", "1", "0"]);
    assert.deepEqual(pick(rows[2]!), ["50", "25", "25 mW", "线偏 90°（s）", "-1", "0", "0"]);
    assert.deepEqual(pick(rows[3]!), ["50", "25", "25 mW", "线偏 0°（p）", "1", "0", "0"]);
  });

  it("the words are beam.ts's, segment by segment (what the inspector shows)", () => {
    const trace = traceLayoutDetailed(pbsLayout);
    const rows = beamRows(pbsLayout, trace).slice(1);
    const segs = trace.flatMap((t) => t.branches.flatMap((b) => b.segments.map((g) => ({ g, mw: t.beam!.source_mw }))));
    segs.forEach(({ g, mw }, i) => {
      assert.equal(rows[i]![col("功率")], powerText(g.beam!.p, mw === null ? null : mw * g.beam!.p));
      assert.equal(rows[i]![col("偏振")], polarizationText(g.beam!.s));
    });
  });

  it("the spot: Ø1 mm at the source, Ø1.002291 mm 100 mm on (case G of v2 section 7); no waist past the source", () => {
    const rows = beamRows(pbsLayout, traceLayoutDetailed(pbsLayout));
    const d0 = rows[1]![col("段首直径 1/e² (mm)")] as number;
    const d1 = rows[1]![col("段尾直径 1/e² (mm)")] as number;
    assert.equal(d0, 1);
    assert.ok(Math.abs(d1 - 1.002291) < 1e-6, String(d1));
    assert.equal(rows[2]![col("段首直径 1/e² (mm)")], d1, "the child starts where the parent ended");
    for (const r of rows.slice(1)) {
      assert.equal(r[col("束腰离段首 (mm)")], null);
      assert.equal(r[col("束腰直径 1/e² (mm)")], null);
    }
  });

  it("the focus of an objective (case H): 4 mm on, at the sample in the focal plane, Ø0.805958 µm; no power known: relative only", () => {
    const l = layoutOf([
      comp("laser", "las1", 0, 100, 0, { props: { wavelength_nm: 633, beam_diameter_mm: 4 } }),
      comp("objective", "obj", 50, 100, 0, { props: { focal_mm: 4 } }),
      comp("sample", "smp", 54, 100, 180),
    ]);
    const rows = parseCsv(beamTableCsv(l, traceLayoutDetailed(l)));
    assert.equal(rows.length, 3);
    const [first, second] = [rows[1]!, rows[2]!];
    assert.equal(first[col("束腰离段首 (mm)")], "", "a waist at the source is no focus");
    assert.equal(second[col("起点 id")], "obj");
    assert.equal(second[col("束腰离段首 (mm)")], "4", "the waist 2 nm past the sample is the segment's, said at its end (section 12.3.3)");
    assert.equal(second[col("束腰直径 1/e² (mm)")], "0.000805958");
    assert.equal(second[col("功率 (mW)")], "", "no power_mw: no absolute power");
    assert.equal(second[col("功率")], "相对 100 %");
    assert.equal(second[col("偏振")], "非偏振");
    assert.deepEqual([second[col("s1 = S1/S0")], second[col("s2 = S2/S0")], second[col("s3 = S3/S0")]], ["0", "0", "0"]);
  });

  it("no light (crossed polarisers): 0 %, no Stokes, 'no light'; the geometry goes on", () => {
    const l = layoutOf([
      comp("laser", "las1", 0, 100, 0, { props: { wavelength_nm: 532, power_mw: 10, polarization: "linear" } }),
      comp("polarizer", "p1", 100, 100, 0, { props: { axis_deg: 90 } }),
      comp("detector", "d", 300, 100, 180),
    ]);
    const rows = parseCsv(beamTableCsv(l, traceLayoutDetailed(l)));
    const last = rows[rows.length - 1]!;
    assert.equal(last[col("起点 id")], "p1");
    assert.deepEqual([last[col("相对功率 (%)")], last[col("功率 (mW)")], last[col("功率")], last[col("偏振")]], ["0", "0", "0 mW", "无光"]);
    assert.deepEqual([last[col("s1 = S1/S0")], last[col("s2 = S2/S0")], last[col("s3 = S3/S0")]], ["", "", ""]);
    assert.equal(last[col("段首直径 1/e² (mm)")], "", "no beam_diameter_mm: no spot");
  });

  it("a branch that leaves the table ends in words; a trace without beam data has the beam cells empty", () => {
    const l = layoutOf([comp("laser", "las1", 50, 62.5, 0, { props: { wavelength_nm: 532 } }), comp("mirror", "m1", 112.5, 62.5, 135)]);
    const trace = traceLayoutDetailed(l);
    const rows = parseCsv(beamTableCsv(l, trace));
    const last = rows[rows.length - 1]!;
    assert.equal(last[col("终点")], "");
    assert.equal(last[col("终点 id")], "");
    assert.equal(last[col("终点处")], "离开台面");
    const stripped: DetailedTrace = trace.map((t) => ({ ...t, beam: undefined, branches: t.branches.map((b) => ({ ...b, segments: b.segments.map(({ x0, y0, x1, y1 }) => ({ x0, y0, x1, y1 })) })) }));
    const old = parseCsv(beamTableCsv(l, stripped));
    assert.equal(old.length, rows.length);
    for (const r of old.slice(1)) {
      assert.equal(r.length, beamHeader().length);
      assert.deepEqual(r.slice(col("相对功率 (%)")), new Array(beamHeader().length - col("相对功率 (%)")).fill(""));
      assert.notEqual(r[col("长度 (mm)")], "");
    }
  });

  it("the actions and ends are said in the inspector's words (BeamInspector.tsx has the same pairs)", () => {
    const inspector = readFileSync(join(COMPONENTS, "BeamInspector.tsx"), "utf8");
    const mineSrc = readFileSync(join(HERE, "..", "src", "lib", "opticalLayout", "exportCsv.ts"), "utf8");
    const body = (fn: string) => mineSrc.slice(mineSrc.indexOf(`export function ${fn}(`)).split(/\r?\n\}\r?\n/)[0]!;
    const pairs = (fn: string) => [...body(fn).matchAll(/tr\("[^"]*", "[^"]*"\)/g)].map((m) => m[0]);
    for (const fn of ["actionText", "endText"]) {
      const mine = pairs(fn);
      assert.ok(mine.length >= 6, `${fn}: ${mine.length}`);
      for (const p of mine) assert.ok(inspector.includes(p), `${fn}: ${p} is not in BeamInspector.tsx`);
    }
    assert.equal(actionText("reflect"), "反射");
    assert.equal(endText("left_table"), "离开台面");
  });
});
