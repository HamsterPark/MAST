// ════════════════════════════════════════════════════════════════════════════
// Section 11 of docs/v2/design/optical_layout.md ("contract supplement") as the editor
// sees it: what the server keeps, what it refuses, and the shapes of its answers.
//
//     cd frontend && npm run test:unit
//
// The backend's model (MASTv2/mast/optical_layout/model.py) is strict: unknown keys at every
// level are a 422, numbers are rounded at fixed decimals, text is cleaned and refused (never
// cut) when too long. The editor mirrors those rules so that what it shows is what the
// server will store, and what it refuses is what the server would refuse. The fixtures
// (opticalLayoutFixtures.test.ts) check the other direction: nothing the server accepts
// is refused here.
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { cleanLine, cleanText, charCount } from "../src/lib/opticalLayout/text.ts";
import { addComponent, emptyLayout, normalizePropValue, setAnnotation, setLabel, setNotes, setProp, setTitle, updateText, addText, changeType } from "../src/lib/opticalLayout/edit.ts";
import { errorsOf, layoutIdFormatProblem, validateComponent, validateLayout } from "../src/lib/opticalLayout/validate.ts";
import { nearMisses } from "../src/lib/opticalLayout/indicators.ts";
import { traceLayoutDetailed } from "../src/lib/opticalLayout/trace.ts";
import { parseComment, parseDocument, parseLinksState, parseRevisionLayout, parseSummaries } from "../src/lib/opticalLayout/parse.ts";
import { classifySaveError, conflictAuthor, layoutIdProblem, saveFailureText } from "../src/lib/opticalLayout/saveFlow.ts";
import { pickDefaultLayout, layoutOptionLabel } from "../src/lib/opticalLayout/listing.ts";
import { GRID_KINDS, LIMITS } from "../src/lib/opticalLayout/types.ts";
import { comp, foldLayout, layoutOf } from "./helpers/opticalLayout.ts";

const paths = (l: Parameters<typeof validateLayout>[0]) => errorsOf(validateLayout(l)).map((i) => i.path);

describe("text the way the server keeps it", () => {
  it("one-line fields: control characters out, every run of whitespace one space, trimmed", () => {
    assert.equal(cleanLine("  Thorlabs \t BB1-E02\n"), "Thorlabs BB1-E02");
    assert.equal(cleanLine("a\u0000b\u0007c"), "abc");
    assert.equal(cleanLine("a  b"), "a b", "a no-break space is whitespace too");
    assert.equal(cleanLine(""), "");
  });

  it("free text: line breaks kept (as \\n), controls out, ends trimmed", () => {
    assert.equal(cleanText("\r\n first\r\nsecond\rthird \n"), "first\nsecond\nthird");
    assert.equal(cleanText("a\u0001b"), "ab");
    assert.equal(cleanText("a\tb"), "a\tb", "a tab is kept in free text");
  });

  it("characters are counted as code points, the way Python counts", () => {
    assert.equal(charCount("abc"), 3);
    assert.equal(charCount("光路图"), 3);
    assert.equal(charCount("a😀b"), 3, "an emoji is one character, not two UTF-16 units");
  });

  it("the setters clean what they store", () => {
    const l = layoutOf([comp("mirror", "m1", 0, 0, 0)]);
    assert.equal(setLabel(l, "m1", "  Fold   mirror ").components[0]!.label, "Fold mirror");
    assert.equal(setAnnotation(l, "m1", { model: " BB1 \n E02 ", notes: "\r\nline1\r\nline2\n" }).components[0]!.annotations.model, "BB1 E02");
    assert.equal(setAnnotation(l, "m1", { notes: "\r\nline1\r\nline2\n" }).components[0]!.annotations.notes, "line1\nline2");
    assert.deepEqual(setAnnotation(l, "m1", { extra: [{ key: " mount ", value: "KM  100" }] }).components[0]!.annotations.extra, [{ key: "mount", value: "KM 100" }]);
    assert.equal(setTitle(l, "  SHG   path ").title, "SHG path");
    assert.equal(setNotes(l, " a\r\nb ").notes, "a\nb");
    const t = addText(emptyLayout("a", "A"), { x: 0, y: 0 }, "x").layout;
    assert.equal(emptyLayout("a", "  New   layout ").title, "New layout");
    assert.equal(addText(emptyLayout("a", "A"), { x: 0, y: 0 }, "  enclosure   door ").layout.texts[0]!.text, "enclosure door");
    assert.equal(updateText(t, "t1", { text: " enclosure   door " }).texts[0]!.text, "enclosure door");
    assert.equal(updateText(t, "t1", { size: 1.234 }).texts[0]!.size, 1.23);
  });
});

describe("type parameters are rounded and exclusive the way the server wants", () => {
  it("numbers at 3 decimals, a splitter's fraction at 6, an axis angle at 2", () => {
    assert.equal(normalizePropValue("laser", "wavelength_nm", 532.12345), 532.123);
    assert.equal(normalizePropValue("beam_splitter", "reflect_fraction", 0.1234567), 0.123457);
    assert.equal(normalizePropValue("waveplate", "fast_axis_deg", 12.345), 12.35);
    assert.equal(normalizePropValue("polarizer", "axis_deg", -0.001), 0, "no -0");
    assert.ok(!Object.is(normalizePropValue("polarizer", "axis_deg", -0.001), -0));
    assert.equal(normalizePropValue("mirror", "anything", 1.23456), 1.23456, "a key the type does not have is left alone (validation says so)");
  });

  it("a list of wavelengths loses repeats and is rounded entry by entry", () => {
    assert.deepEqual(normalizePropValue("laser", "wavelengths_nm", [532.0004, 532.0001, 1064]), [532, 1064]);
  });

  it("an id is trimmed", () => {
    assert.equal(normalizePropValue("fiber_in", "continues_at", " fo1 "), "fo1");
  });

  it("setting the single wavelength clears the list and the other way round (the server wants exactly one)", () => {
    let l = layoutOf([comp("laser", "las1", 0, 0, 0, { props: { wavelength_nm: 532 } })]);
    l = setProp(l, "las1", "wavelengths_nm", [532, 1064]);
    assert.deepEqual(l.components[0]!.props, { wavelengths_nm: [532, 1064] });
    l = setProp(l, "las1", "wavelength_nm", 633);
    assert.deepEqual(l.components[0]!.props, { wavelength_nm: 633 });
    // other props are untouched by it
    l = setProp(setProp(l, "las1", "power_mw", 20), "las1", "wavelengths_nm", [1, 2]);
    assert.deepEqual(l.components[0]!.props, { power_mw: 20, wavelengths_nm: [1, 2] });
  });

  it("an empty list or an emptied box removes the key", () => {
    const l = layoutOf([comp("laser", "las1", 0, 0, 0, { props: { wavelengths_nm: [532], power_mw: 5 } })]);
    assert.deepEqual(setProp(l, "las1", "wavelengths_nm", []).components[0]!.props, { power_mw: 5 });
    assert.deepEqual(setProp(l, "las1", "power_mw", null).components[0]!.props, { wavelengths_nm: [532] });
  });

  it("changing a lamp into a laser gives it the wavelength the server demands", () => {
    const l = layoutOf([comp("lamp", "x", 0, 0, 0, { props: {} })]);
    assert.deepEqual(changeType(l, "x", "laser").components[0]!.props, { wavelength_nm: 532 });
    const keep = layoutOf([comp("lamp", "x", 0, 0, 0, { props: { wavelength_nm: 800 } })]);
    assert.deepEqual(changeType(keep, "x", "laser").components[0]!.props, { wavelength_nm: 800 });
  });
});

describe("early validation says what the server would say", () => {
  const withProps = (type: Parameters<typeof comp>[0], props: Record<string, unknown>) => layoutOf([comp(type, "c1", 100, 100, 0, { props })]);

  it("a laser or fibre output needs exactly one of the two wavelength forms", () => {
    assert.deepEqual(paths(withProps("laser", {})), ["components[c1].props.wavelength_nm"]);
    assert.deepEqual(paths(withProps("laser", { wavelength_nm: 532, wavelengths_nm: [532] })), ["components[c1].props.wavelength_nm"]);
    assert.deepEqual(paths(withProps("laser", { wavelength_nm: 532 })), []);
    assert.deepEqual(paths(withProps("laser", { wavelengths_nm: [532, 1064] })), []);
    assert.deepEqual(paths(withProps("fiber_out", {})), ["components[c1].props.wavelength_nm"], "even one that another fibre continues at: the server still wants it");
    assert.deepEqual(paths(withProps("lamp", {})), [], "a lamp may have no wavelength");
  });

  it("the dichroic mirror's edge is required, its side is not (it defaults to short)", () => {
    assert.deepEqual(paths(withProps("dichroic", {})), ["components[c1].props.edge_nm"]);
    assert.deepEqual(paths(withProps("dichroic", { edge_nm: 550 })), []);
    assert.deepEqual(paths(withProps("dichroic", { edge_nm: 550, reflects: "medium" })), ["components[c1].props.reflects"]);
  });

  it("wavelengths are above 0 and up to 1e7 nm; lists hold at most 16 different values", () => {
    assert.deepEqual(paths(withProps("laser", { wavelength_nm: 0 })), ["components[c1].props.wavelength_nm"]);
    assert.deepEqual(paths(withProps("laser", { wavelength_nm: 1e7 })), []);
    assert.deepEqual(paths(withProps("laser", { wavelength_nm: 1e7 + 1 })), ["components[c1].props.wavelength_nm"]);
    assert.deepEqual(paths(withProps("laser", { wavelengths_nm: Array.from({ length: 17 }, (_, i) => 400 + i) })), ["components[c1].props.wavelengths_nm"]);
    assert.deepEqual(paths(withProps("laser", { wavelengths_nm: [532, 532] })), ["components[c1].props.wavelengths_nm"]);
    assert.deepEqual(paths(withProps("laser", { wavelengths_nm: [] })).includes("components[c1].props.wavelengths_nm"), true, "an empty list is not a list");
  });

  it("each parameter has the server's range", () => {
    const bad = (type: Parameters<typeof comp>[0], key: string, v: unknown, ok = false) => {
      const base: Record<string, unknown> = type === "dichroic" ? { edge_nm: 550 } : {};
      const p = paths(withProps(type, { ...base, [key]: v }));
      assert.equal(p.length === 0, ok, `${type}.${key} = ${String(v)} -> ${p.join(",")}`);
    };
    bad("beam_splitter", "reflect_fraction", 1.01);
    bad("beam_splitter", "reflect_fraction", 1, true);
    bad("nd", "od", 20.5);
    bad("nd", "od", 20, true);
    bad("iris", "open_mm", 0);
    bad("iris", "open_mm", 500, true);
    bad("iris", "open_mm", 500.5);
    bad("waveplate", "fast_axis_deg", 361);
    bad("waveplate", "fast_axis_deg", -360, true);
    bad("lens", "focal_mm", -1e5, true);
    bad("lens", "focal_mm", 1e5 + 1);
    bad("grating", "lines_per_mm", 0);
    bad("grating", "lines_per_mm", 1200, true);
    bad("laser", "power_mw", -1);
    bad("shutter", "closed", "yes");
    bad("custom", "behavior", "explode");
  });

  it("a filter's pass band must not be upside down", () => {
    assert.deepEqual(paths(withProps("filter", { pass_min_nm: 700, pass_max_nm: 500 })), ["components[c1].props.pass_min_nm"]);
    assert.deepEqual(paths(withProps("filter", { pass_min_nm: 500, pass_max_nm: 700 })), []);
    assert.deepEqual(paths(withProps("filter", { pass_min_nm: 700 })), []);
  });

  it("a fibre input may point at nothing (the server accepts it): only a warning", () => {
    const l = withProps("fiber_in", { continues_at: "ghost" });
    assert.deepEqual(errorsOf(validateLayout(l)), []);
    assert.equal(validateLayout(l).filter((i) => i.level === "warn").length, 1);
    assert.deepEqual(paths(withProps("fiber_in", { continues_at: "has space" })), ["components[c1].props.continues_at"]);
  });

  it("a label is at most 80 characters, a title 200, a text 200", () => {
    const l = layoutOf([{ ...comp("mirror", "m1", 0, 0, 0), label: "x".repeat(81) }]);
    assert.deepEqual(paths(l), ["components[m1].label"]);
    assert.deepEqual(paths(layoutOf([{ ...comp("mirror", "m1", 0, 0, 0), label: "x".repeat(80) }])), []);
    assert.deepEqual(paths({ ...foldLayout(), title: "t".repeat(201) }), ["title"]);
    assert.deepEqual(paths({ ...foldLayout(), notes: "n".repeat(2001) }), ["notes"]);
    const withText = addText(foldLayout(), { x: 0, y: 0 }, "x").layout;
    assert.deepEqual(paths({ ...withText, texts: [{ ...withText.texts[0]!, text: "y".repeat(201) }] }), ["texts[t1].text"]);
    assert.deepEqual(paths({ ...withText, texts: [{ ...withText.texts[0]!, size: 25 }] }), ["texts[t1].size"]);
    assert.deepEqual(paths({ ...withText, texts: [{ ...withText.texts[0]!, size: 0.05 }] }), ["texts[t1].size"]);
  });

  it("the datasheet is an http(s) address or empty", () => {
    const at = (url: string) => paths(setAnnotation(layoutOf([comp("mirror", "m1", 0, 0, 0)]), "m1", { datasheet_url: url }));
    assert.deepEqual(at(""), []);
    assert.deepEqual(at("https://www.thorlabs.com/BB1-E02"), []);
    assert.deepEqual(at("HTTP://example.com/a"), []);
    assert.deepEqual(at("ftp://example.com/a"), ["components[m1].annotations.datasheet_url"]);
    assert.deepEqual(at("www.example.com"), ["components[m1].annotations.datasheet_url"]);
    assert.deepEqual(at("https://example.com/a b"), ["components[m1].annotations.datasheet_url"]);
  });

  it("links are references of up to 64 characters from a limited alphabet", () => {
    const base = layoutOf([comp("mirror", "m1", 0, 0, 0)]);
    const links = (device_id: string | null, slot: string | null) => paths({ ...base, components: [{ ...base.components[0]!, links: { device_id, slot } }] });
    assert.deepEqual(links("witec", "excitation_waveplate"), []);
    assert.deepEqual(links("sat:rig1.hwp-rot", null), []);
    assert.deepEqual(links("has space", null), ["components[m1].links.device_id"]);
    assert.deepEqual(links(null, "x".repeat(65)), ["components[m1].links.slot"]);
    assert.deepEqual(links("", null), ["components[m1].links.device_id"], "an empty string is not a reference: the editor stores null");
  });

  it("the table: size 10 to 20000, pitch 1 to 500, offset 0 to 500, grid metric / imperial / none", () => {
    const t = (over: Partial<ReturnType<typeof foldLayout>["table"]>) => paths({ ...foldLayout(), table: { ...foldLayout().table, ...over } });
    assert.deepEqual(t({}), []);
    assert.deepEqual(t({ width_mm: 9 }), ["table.width_mm"]);
    assert.deepEqual(t({ height_mm: 20001 }), ["table.height_mm"]);
    assert.deepEqual(t({ hole_pitch_mm: 0.5 }), ["table.hole_pitch_mm"]);
    assert.deepEqual(t({ hole_pitch_mm: 501 }), ["table.hole_pitch_mm"]);
    assert.deepEqual(t({ hole_offset_mm: -1 }), ["table.hole_offset_mm"]);
    assert.deepEqual(t({ hole_offset_mm: 501 }), ["table.hole_offset_mm"]);
    for (const g of GRID_KINDS) assert.deepEqual(t({ grid: g }), []);
    assert.deepEqual(t({ grid: "polar" }), ["table.grid"]);
  });

  it("an id is unique over components and text labels together; a layout id is not a Windows device name", () => {
    const l = addText(layoutOf([comp("mirror", "t1", 0, 0, 0)]), { x: 0, y: 0 }, "a").layout;
    // addText avoids t1, so build the clash by hand
    const clash = { ...l, texts: [{ ...l.texts[0]!, id: "t1" }] };
    assert.ok(errorsOf(validateLayout(clash)).some((i) => i.message.includes("t1")));
    for (const bad of ["con", "NUL", "Com1", "lpt9", "AUX", "prn"]) assert.equal(layoutIdFormatProblem(bad), "reserved", bad);
    assert.equal(layoutIdFormatProblem("console"), null);
    assert.equal(layoutIdFormatProblem("com10"), null, "only com1 to com9");
    assert.equal(layoutIdFormatProblem("has space"), "format");
    assert.ok(layoutIdProblem("con", new Set()));
    assert.ok(paths({ ...foldLayout(), id: "nul" }).includes("id"));
  });

  it("ranges and limits of a component", () => {
    const base = comp("mirror", "m1", 0, 0, 0);
    const v = (over: Partial<typeof base>) => paths(layoutOf([{ ...base, ...over }]));
    assert.deepEqual(v({ x_mm: 100_000 }), []);
    assert.deepEqual(v({ x_mm: 100_000.01 }), ["components[m1].x_mm"]);
    assert.deepEqual(v({ angle_deg: 1e6 }), []);
    assert.deepEqual(v({ angle_deg: 1e6 + 1 }), ["components[m1].angle_deg"]);
    assert.deepEqual(v({ aperture_mm: 0.09 }), ["components[m1].aperture_mm"]);
    assert.deepEqual(v({ aperture_mm: 500 }), []);
    assert.deepEqual(errorsOf(validateComponent({ ...base, props: { od: 1 } })).map((i) => i.path), ["components[m1].props.od"], "a mirror has no parameters");
    const added = addComponent(emptyLayout("main", "Main"), "mirror", { x: 5, y: 5 }).layout;
    assert.deepEqual(paths(added), []);
  });

  it("the fixtures' limits are the numbers in LIMITS", () => {
    assert.equal(LIMITS.labelMax, 80);
    assert.equal(LIMITS.stringMax, 200);
    assert.equal(LIMITS.notesMax, 2000);
    assert.equal(LIMITS.maxWavelengths, 16);
    assert.equal(LIMITS.refPattern.test("a".repeat(64)), true);
    assert.equal(LIMITS.refPattern.test("a".repeat(65)), false);
  });
});

describe("near misses are the backend's definition", () => {
  const miss = (laserY: number) => {
    const l = foldLayout({ laserY });
    return nearMisses(l, traceLayoutDetailed(l));
  };

  it("outside the aperture but within one aperture of the centre", () => {
    // the mirror's aperture is 25.4: |offset| between 12.7 and 25.4. A beam dy above the centre
    // crosses the surface line |offset| = dy * sqrt(2) from it.
    assert.equal(miss(62.5 + 10).length, 1, "14.1 mm: a near miss");
    assert.equal(miss(62.5 + 17).length, 1, "24.0 mm: still within one aperture");
    assert.equal(miss(62.5 + 19).length, 0, "26.9 mm: more than one aperture from the centre");
    assert.equal(miss(62.5 + 5).length, 0, "7.1 mm: a hit, not a miss");
  });

  it("the old, wider window (up to one aperture beyond the edge) is gone", () => {
    assert.equal(miss(62.5 + 25).length, 0);
  });
});

describe("the server's answers, as the contract writes them", () => {
  it("a document is nested: {layout, trace, links_state}", () => {
    const l = foldLayout();
    const d = parseDocument({ layout: { ...l, id: "main", revision: 4 }, trace: [], links_state: { m1: { device: { id: "witec", known: false, name: "witec", kind: "", enabled: null, connected: null } } } });
    assert.equal(d.layout.id, "main");
    assert.equal(d.layout.revision, 4);
    assert.equal(d.trace?.length, 0);
    assert.equal(d.linksState["m1"]?.device?.known, false);
  });

  it("a flat document is still read", () => {
    assert.equal(parseDocument({ ...foldLayout(), id: "flat" }).layout.id, "flat");
  });

  it("a revision is {revision, updated_at, updated_by, layout, trace}; the layout carries the revision it was saved as", () => {
    const r = parseRevisionLayout({ revision: 2, updated_at: "t", updated_by: "ana", layout: { ...foldLayout(), id: "main", revision: 7 }, trace: [] });
    assert.equal(r.id, "main");
    assert.equal(r.revision, 2);
  });

  it("a comment says who wrote it in `author`; `by` is the name", () => {
    const c = parseComment({ id: "c12", author: "agent", by: "ext:claude-1", text: "t", component_id: "m1", created_at: "t", resolved: false, resolved_by: null, resolved_at: null })!;
    assert.deepEqual([c.kind, c.by, c.id], ["agent", "ext:claude-1", "c12"]);
    assert.equal(parseComment({ id: "c1", author: "operator", by: "ana", text: "t" })!.kind, "operator");
    assert.equal(parseComment({ id: "c1", by: "author-named-agent", text: "t" })!.by, "author-named-agent");
  });

  it("the list may carry an unreadable file with its error", () => {
    const list = parseSummaries({ layouts: [{ id: "a", title: "A", revision: 3, updated_at: "2026-11-03T10:00:00+00:00", updated_by: "x", components: 2, open_comments: 0 }, { id: "broken", error: "not valid JSON" }] });
    assert.equal(list[1]!.error, "not valid JSON");
    assert.equal(list[0]!.error, undefined);
    assert.equal(pickDefaultLayout(list), "a", "an unreadable file is never opened first");
    assert.equal(pickDefaultLayout([list[1]!]), null);
    assert.match(layoutOptionLabel(list[1]!), /broken/);
  });

  it("links_state: only components with links, device and slot with their `known`", () => {
    const s = parseLinksState({
      hwp1: {
        device: { id: "hwp_rot", known: true, name: "K10CR1", type: "kinesis", kind: "rotation", enabled: true, connected: false, status: { pos: 3 } },
        slot: { id: "excitation_waveplate", known: true, label: "Excitation waveplate", kind: "choice", value: "hwp_532", valid: true, unit: "", set_by: "ana", set_at: "t", valid_until: null },
      },
      m2: { slot: { id: "gone", known: null, error: "unreadable" } },
    });
    assert.equal(s["hwp1"]?.device?.connected, false);
    assert.equal(s["hwp1"]?.slot?.value, "hwp_532");
    assert.equal(s["m2"]?.device, null);
    assert.equal(s["m2"]?.slot?.known, null);
    assert.equal(s["m2"]?.slot?.error, "unreadable");
  });

  it("a 409 says who saved and when", () => {
    const body = { detail: "base_revision 3 is behind 5", current_revision: 5, updated_by: "ben", updated_at: "2026-11-03T09:00:00+00:00" };
    assert.deepEqual(conflictAuthor(body), { updatedBy: "ben", updatedAt: "2026-11-03T09:00:00+00:00" });
    const f = classifySaveError({ status: 409, body });
    assert.ok(f.kind === "conflict" && f.updatedBy === "ben" && f.currentRevision === 5);
    assert.deepEqual(conflictAuthor(null), { updatedBy: "", updatedAt: "" });
  });

  it("503 means the disk could not be written and nothing changed; 429 is a limit; 415 is the write guard", () => {
    assert.equal(classifySaveError({ status: 503, body: { detail: "disk full" } }).kind, "unavailable");
    assert.match(saveFailureText(classifySaveError({ status: 503, body: { detail: "disk full" } })), /disk full/);
    assert.equal(classifySaveError({ status: 429, body: { detail: "1000 open comments" } }).kind, "limit");
    assert.equal(classifySaveError({ status: 415 }).kind, "forbidden");
    assert.equal(classifySaveError({ status: 403 }).kind, "forbidden");
  });
});
