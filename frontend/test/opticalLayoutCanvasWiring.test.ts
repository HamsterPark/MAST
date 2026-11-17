// ════════════════════════════════════════════════════════════════════════════
// Optical layout v2 canvas — wiring (docs/v2/design/optical_layout_v2.md section 8).
//
//     cd frontend && npm run test:unit
//
// The components are .tsx (Node cannot run JSX), so the places where correct logic is easy to leave
// unconnected are pinned from source, in the style of opticalLayoutWiring.test.ts:
//
//   · the layers are read over their defaults, so a layer added later starts on;
//   · focus mode is a key (F) and a button, Esc leaves it after the tool and before the selection, and it
//     covers the page's content area only: the top bar with the E-STOP stays visible and reachable;
//   · the figure export is the canvas component in its paper variant, with print colours, nothing of
//     editing in it, and a PNG made in the browser;
//   · leaving focus mode puts the view back (the canvas remembers the sizes it had).
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it } from "node:test";

const HERE = fileURLToPath(new URL(".", import.meta.url));
const SRC = join(HERE, "..", "src");
const read = (...p: string[]) => readFileSync(join(SRC, ...p), "utf8");

const EDITOR = read("components", "opticallayout", "LayoutEditor.tsx");
const CANVAS = read("components", "opticallayout", "LayoutCanvas.tsx");
const SVG = read("components", "opticallayout", "LayoutSvg.tsx");
const TOOLBAR = read("components", "opticallayout", "Toolbar.tsx");

describe("the layers", () => {
  it("are read over their defaults (a stored v1 choice gets the new layers on)", () => {
    assert.match(EDITOR, /useState<DisplayOptions>\(\(\) => withLayerDefaults\(readRaw\(DISPLAY_KEY\)\)\)/);
    assert.ok(!/isDisplay/.test(EDITOR), "the all-or-nothing v1 validator is gone");
  });

  it("the menu is built from the lib's groups, and the beam layers say they need the beams", () => {
    assert.match(TOOLBAR, /LAYER_GROUPS\.map/);
    assert.match(TOOLBAR, /BEAM_LAYERS\.includes\(id\) && !p\.display\.beams/);
  });

  it("the picture honours them: texts, alignment through layerShown", () => {
    assert.match(SVG, /display\.texts && layout\.texts\.length > 0/);
    assert.match(SVG, /layerShown\(display, "alignment"\)/);
  });
});

describe("focus mode", () => {
  it("F toggles it, the toolbar has a button for it", () => {
    assert.match(EDITOR, /case "focus":\s*handled\(\);\s*setFocusMode\(\(f\) => !f\);/);
    assert.match(TOOLBAR, /<ToolButton on=\{p\.focus\} onClick=\{\(\) => p\.onFocus\(!p\.focus\)\}/);
  });

  it("Esc cancels the tool first, then leaves focus mode, then clears the selection", () => {
    const esc = EDITOR.slice(EDITOR.indexOf('case "escape":'), EDITOR.indexOf('case "focus":'));
    const tool = esc.indexOf("setToolChecked");
    const focus = esc.indexOf("setFocusMode(false)");
    const sel = esc.indexOf("setSelection([])");
    assert.ok(tool > 0 && focus > tool && sel > focus, esc);
  });

  it("covers the page's content area (<main>), never the top bar with the E-STOP", () => {
    assert.match(EDITOR, /document\.querySelector\("main"\)/);
    assert.match(EDITOR, /style=\{focusMode \? \(focusRect \? \{ top: focusRect\.top, left: focusRect\.left, width: focusRect\.width, height: focusRect\.height \}/);
    assert.ok(!/requestFullscreen/.test(EDITOR + CANVAS), "no browser full screen: it would hide the E-STOP");
  });

  it("folds the palette, the inspector and the name row away, and the Add menu stands in for the palette", () => {
    assert.match(EDITOR, /<aside className=\{focusMode \? "hidden" : "hidden min-h-0 xl:block"\}/);
    assert.match(EDITOR, /focusMode && "hidden"\)\} aria-label=\{tr\("检查面板", "Inspector"\)\}/);
    assert.match(TOOLBAR, /className=\{p\.focus \? undefined : "xl:hidden"\}/);
  });

  it("the canvas keeps what was in view through the resize, and leaving puts the view back", () => {
    assert.match(CANVAS, /const r = viewAfterResize\(resizeMemory\.current, v, prev, now\);/);
    assert.match(CANVAS, /followResize\(prev, size\);/);
    assert.match(CANVAS, /publish\(node\.clientWidth, node\.clientHeight\)/, "first measurement in the observer's box");
    assert.ok(!/useEffect\(\(\) => \(\) => observer\.current\?\.disconnect\(\), \[\]\)/.test(CANVAS), "StrictMode would leave the canvas deaf to resizes");
  });

  it("no view change under a drag: a resize during one is followed when it ends", () => {
    assert.match(CANVAS, /if \(drag\.current\) \{\s*if \(!resizedInDrag\.current\) resizedInDrag\.current = prev;\s*return;\s*\}/);
    assert.match(CANVAS, /const before = resizedInDrag\.current;\s*resizedInDrag\.current = null;\s*if \(before && lastSize\.current\) followResize\(before, lastSize\.current\);/);
  });

  it("in focus mode the notices float over the canvas, so they never change its height", () => {
    assert.match(EDITOR, /\{!focusMode && banners\}/, "in the flow only outside focus mode");
    assert.match(EDITOR, /\{focusMode && banners && \(\s*<div className="pointer-events-none absolute [^"]*" data-focus-banners="">/);
    assert.match(EDITOR, /<div className=\{focusMode \? "relative h-full min-h-0 min-w-0"/, "the canvas cell holds them");
  });

  it("Esc in an open menu closes the menu and nothing else (the editor skips a key already spent)", () => {
    assert.match(TOOLBAR, /if \(e\.key !== "Escape"\) return;\s*e\.preventDefault\(\);\s*setOpen\(false\);/);
    assert.match(EDITOR, /isEditableTarget\(e\.target as HTMLElement \| null\) \|\| e\.defaultPrevented\) return;/);
  });
});

describe("the beams (sections 8.2, 8.3)", () => {
  const DRAW = read("lib", "opticalLayout", "beamDraw.ts");
  const INSPECTOR = read("components", "opticallayout", "BeamInspector.tsx");

  it("the picture draws the scene of beamDraw, with the envelope and polarisation layers", () => {
    assert.match(SVG, /beamScene\(trace, view, theme, \{ envelope, polarization, clip:/);
    assert.match(SVG, /envelope=\{layerShown\(display, "envelope"\)\}/);
    assert.match(SVG, /polarization=\{layerShown\(display, "polarization"\)\}/);
    assert.match(SVG, /if \(!showPower\) return \[\];\s*return powerGroupsOf\(trace, theme\);/, "power tags only with their layer");
    assert.match(SVG, /for \(const t of powerTags\(trace, theme\)\)/, "grouped per component");
  });

  it("no physics is written in the canvas: sizes and polarisation come from beam.ts", () => {
    assert.match(DRAW, /from "\.\/beam\.ts"/);
    for (const [name, text] of [
      ["beamDraw.ts", DRAW],
      ["BeamInspector.tsx", INSPECTOR],
    ] as const) {
      assert.ok(!/Math\.sqrt\(|Math\.asin\(|lambdaMm|1e-6 \*/.test(text), `${name} computes beam physics itself`);
    }
  });
});

describe("the beam inspector (section 8.4)", () => {
  const INSPECTOR_TEXT = read("components", "opticallayout", "BeamInspector.tsx");
  it("is a tab of the inspector, fed the pick the canvas made", () => {
    assert.match(EDITOR, /\["beam", tr\("光束", "Beam"\), null\]/);
    assert.match(EDITOR, /<BeamInspector layout=\{shown\} trace=\{trace\} pick=\{beamRef\}/);
    assert.match(EDITOR, /onBackgroundClick=\{pickBeamAt\}/);
    assert.match(EDITOR, /cur\.display\.beams \? nearestSegment\(cur\.trace, world, BEAM_PICK_PX \/ k\) : null/, "only beams that are drawn are picked");
    assert.match(CANVAS, /if \(!\(k && cur\.onBackgroundClick\?\.\(click\.world, k\)\)\) cur\.onSelect\(\[\]\);/);
    assert.match(CANVAS, /beamPick=\{props\.beamPick\}/);
  });

  it("a pick is kept by name across re-traces and Esc lets it go after the selection", () => {
    assert.match(EDITOR, /refOf\(trace, beamPick\)/);
    assert.match(EDITOR, /\} else if \(cur\.beamPick\) \{\s*handled\(\);\s*setBeamPick\(null\);/);
  });

  it("the tooltip of the beam under the pointer is the canvas's own, and quiet while dragging, over a component or with the beams hidden", () => {
    assert.match(CANVAS, /nearestSegment\(trace, cursor, BEAM_PICK_PX \/ view\.k\)/);
    assert.match(CANVAS, /cursor && view && beamsShown && tool === "select" && !busy && hover === null/);
    assert.match(CANVAS, /const beamsShown = display\.beams;/);
    assert.match(CANVAS, /beamAt\(tree, seg, hit\.along\)/);
  });

  it("the check panel gets the layout, so it lists the beam checks; they count in the tab's number", () => {
    // not the layout of a drag in progress: its rows would be worked out on every frame (and the alignment rows
    // are not worked out then): the panel says it waits for the drop
    assert.match(EDITOR, /<CheckPanel diagnostics=\{checks\} issues=\{issues\} trace=\{trace\} layout=\{preview \? undefined : shown\} pending=\{preview !== null\}/);
    assert.match(EDITOR, /\+ beamWarnings;/);
    const PANEL = read("components", "opticallayout", "CheckPanel.tsx");
    assert.match(PANEL, /\) : pending \? \(\s*<p [^>]*data-check-pending="">/, "pending before the no-problems note");
  });

  it("a component picked on the canvas while the beam tab is open goes to Properties", () => {
    assert.match(EDITOR, /setSelection\(ids\);\s*if \(ids\.length > 0\) setTab\(\(t\) => \(t === "beam" \? "inspector" : t\)\);/);
    assert.match(EDITOR, /onSelect=\{selectIds\}/);
  });

  it("every end of a path is said in words, none as its identifier", () => {
    for (const end of ["stop", "left_table", "max_events", "blocked", "back_hit", "split", "budget"]) {
      assert.match(INSPECTOR_TEXT, new RegExp(`case "${end}":\\s*return tr\\(`), end);
    }
    assert.ok(!/default:\s*return (end|action);/.test(INSPECTOR_TEXT), "no identifier falls through");
  });
});

describe("assemblies (section 8.6)", () => {
  const PALETTE = read("components", "opticallayout", "Palette.tsx");

  it("the palette lists them, a click adds one at the centre, a drag drops it where it lands", () => {
    assert.match(PALETTE, /TEMPLATES\.filter\(/);
    assert.match(PALETTE, /onClick=\{\(\) => onAddTemplate\(t\.id\)\}/);
    assert.match(PALETTE, /e\.dataTransfer\.setData\(TEMPLATE_MIME, t\.id\)/);
    assert.match(CANVAS, /props\.onDropTemplate\(template, w\)/);
    assert.match(EDITOR, /onDropTemplate=\{placeTemplate\}/);
    assert.match(TOOLBAR, /TEMPLATES\.map\(\(t\) => \(\s*<MenuItem key=\{t\.id\} disabled=\{p\.readOnly\} onClick=\{\(\) => p\.onAddTemplate\(t\.id\)\}/, "and the Add menu, for focus mode and narrow windows");
  });

  it("an assembly is ONE edit (one undo step) and arrives selected, so the keys turn and move it as a group", () => {
    const fn = EDITOR.slice(EDITOR.indexOf("const placeTemplate = useCallback("), EDITOR.indexOf("const onAddTemplate = useCallback("));
    assert.match(fn, /const r = insertTemplate\(latest\.current\.present, template, at\);/);
    assert.equal((fn.match(/edit\(/g) ?? []).length, 1, fn);
    assert.match(fn, /edit\(\(\) => r\.layout\);\s*setSelection\(r\.ids\);/);
  });
});

describe("the figure export", () => {
  it("is the canvas component in its paper variant, in print colours, made standalone", () => {
    const fn = EDITOR.slice(EDITOR.indexOf("const paperFigure = "), EDITOR.indexOf("const exportPaper = async"));
    assert.match(fn, /renderToStaticMarkup\(/);
    assert.match(fn, /variant="paper"/);
    assert.match(fn, /theme="light"/);
    assert.match(fn, /resolveCssVars\(markup, \(n\) => paperColor\(n\)\)/);
    assert.match(fn, /standaloneSvg\(/);
    // the PNG: the browser draws it three times the figure's pixels, and the file says its size on paper (figurePng.ts)
    const exp = EDITOR.slice(EDITOR.indexOf("const exportPaper = async"), EDITOR.indexOf("const exportPng = async"));
    assert.match(exp, /figurePng\(fig\.text, fig, \{ kind: "scale", scale: 3 \}\)/);
    const FIGURE_PNG = read("components", "opticallayout", "figurePng.ts");
    assert.match(FIGURE_PNG, /rasterizeSvg\(svgText, figure\.width, figure\.height, plan\.scale\)/);
    assert.match(FIGURE_PNG, /withPhys\(drawn, ppm\)/);
    assert.match(fn, /grid: false, rulers: false, alignment: false, comments: false/);
    assert.match(fn, /holes: paperHoles/);
  });

  it("the paper variant leaves out what belongs to editing and draws a scale bar", () => {
    assert.match(SVG, /!paper && display\.rulers && <Rulers/);
    assert.match(SVG, /!paper && display\.comments && commentCounts/);
    assert.match(SVG, /const readouts = !paper && /);
    assert.match(SVG, /paper && <ScaleBarMark/);
    assert.match(SVG, /level=\{paper \? "ok" : /);
  });

  it("frames the names, powers and notes as the figure writes them, and draws glyph details at a readable size", () => {
    const fn = EDITOR.slice(EDITOR.indexOf("const paperFigure = "), EDITOR.indexOf("const exportPaper = async"));
    assert.match(fn, /screenBoxes: \(view\) => paperTextBoxes\(shown, trace, view, layers, "light"\)/);
    assert.match(fn, /componentBox: \(c, k\) => rotatedBox\(c, shapeBounds\(k === null \? glyphShapes\(c\) : paperShapes\(glyphShapes\(c\), k\)\)\)/);
    assert.ok(fn.indexOf("const layers") < fn.indexOf("paperFrame("), "framed with the layers it is drawn with");
    const GLYPH = read("components", "opticallayout", "GlyphShapes.tsx");
    assert.match(GLYPH, /const shapes = useMemo\(\(\) => \(paper \? paperShapes\(given, k\) : given\), \[paper, given, k\]\);/);
  });

  it("the figure's halo is a stroke and nothing else: fill none, so a PDF holds each name once more, not twice", () => {
    // MuPDF writes a text with a fill and a stroke as two text objects: a filled halo would make every name three
    // times selectable and findable in the PDF export. The text on top covers the inside of the letters anyway.
    const halo = SVG.slice(SVG.indexOf("function HaloText("), SVG.indexOf("function paperBeamDashes("));
    const layers = [...halo.matchAll(/<text \{\.\.\.q\}([^>]*)>/g)].map((m) => m[1]!);
    assert.equal(layers.length, 2, "the halo and the text");
    assert.match(layers[0]!, /^ fill="none" stroke="var\(--mast-panel\)" strokeWidth=\{3\} strokeLinejoin="round" aria-hidden="true"$/);
    assert.equal(layers[1], "", "the text itself keeps the fill it was given");
  });

  it("uses nothing other programs get wrong: no paint-order, no vector-effect, no <polygon>, no dash array on a beam, a font on every text", () => {
    const GLYPH = read("components", "opticallayout", "GlyphShapes.tsx");
    // strokes in the glyph's millimetre frame: vector-effect on the screen only, px / k in the figure
    assert.match(GLYPH, /return paper \? \{ strokeWidth: px \/ k \} : \{ strokeWidth: px, vectorEffect: "non-scaling-stroke" \};/);
    const code = (text: string) => text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
    const glyphCode = code(GLYPH);
    assert.equal((glyphCode.match(/"non-scaling-stroke"/g) ?? []).length, 2, "only in strokeIn (its type and its value)");
    assert.ok(!/<polygon/.test(glyphCode + code(SVG)), "MuPDF leaves a <polygon> open");
    assert.match(GLYPH, /fontFamily=\{PAPER_FONT\}/);
    // halos: paint-order on the screen only; in the figure a stroked copy under the text
    assert.equal((SVG.match(/paintOrder/g) ?? []).length, 1, "the screen's HALO only");
    assert.match(SVG, /if \(!paper\) \{\s*return \(\s*<text \{\.\.\.p\} \{\.\.\.HALO\}>/);
    assert.match(SVG, /<text \{\.\.\.q\} fill="none" stroke="var\(--mast-panel\)"/);
    assert.equal((SVG.match(/\{\.\.\.HALO\}/g) ?? []).length, 1, "every halo goes through HaloText");
    // bands: in a group that carries the view, their edge px / k in the figure
    assert.match(SVG, /strokeWidth=\{paper \? 0\.7 \/ view\.k : 0\.7\}\s*vectorEffect=\{paper \? undefined : "non-scaling-stroke"\}/);
    // beam lines: the dashes as segments in the figure
    assert.match(SVG, /const dashes = paper && c\.look\.dash \? paperBeamDashes\(c\.d, c\.look\.dash\) : null;/);
    assert.match(SVG, /strokeDasharray=\{dashes === null \? \(c\.look\.dash \?\? undefined\) : undefined\}/);
  });
});
