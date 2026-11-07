// ════════════════════════════════════════════════════════════════════════════
// Optical layout page — wiring. What the unit tests of the pure logic cannot see.
//
//     cd frontend && npm run test:unit
//
// The components are .tsx (Node cannot run JSX), so the places where correct logic is easy
// to leave unconnected are pinned from source, in the style of operatorWiring.test.ts:
//
//   · the page is reachable (nav + router), on both station profiles, and not legacy;
//   · every call goes to /api/optical-layouts, never to /api/ext (an agent comments there,
//     the operator edits here), and every write carries the operator's name;
//   · dragging never touches the undo history per pointer event: positions go through the
//     frame throttle and ONE step is committed on release, so the trace runs once per frame;
//   · the keyboard map is the lib's, typing in a box never moves a component;
//   · saving handles 409 ("changed elsewhere": load the newest / save as new) and a missing
//     PNG renderer without an error;
//   · nothing is drawn with an external icon set or the gwoptics library (licence);
//   · the dev mock cannot reach a production bundle.
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it } from "node:test";

import { TABS, findTab, sectionIds, sectionPath, visibleTabs } from "../src/lib/nav.ts";

const HERE = fileURLToPath(new URL(".", import.meta.url));
const SRC = join(HERE, "..", "src");
const read = (...p: string[]) => readFileSync(join(SRC, ...p), "utf8");

function files(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) files(p, out);
    else if (/\.tsx?$/.test(name)) out.push(p);
  }
  return out;
}

const COMPONENT_FILES = files(join(SRC, "components", "opticallayout"));
const LIB_FILES = files(join(SRC, "lib", "opticalLayout"));
const PAGE = join(SRC, "pages", "OpticalLayoutPage.tsx");
const API_FILES = [join(SRC, "api", "opticalLayout.ts"), join(SRC, "api", "opticalLayoutHooks.ts")];
/** Everything this feature owns. */
const OWN = [...COMPONENT_FILES, ...LIB_FILES, PAGE, ...API_FILES];

const CANVAS = read("components", "opticallayout", "LayoutCanvas.tsx");
const EDITOR = read("components", "opticallayout", "LayoutEditor.tsx");
const CLIENT = read("api", "opticalLayout.ts");

/** String literals of a source file (what could become a URL), comments excluded. */
function literals(text: string): string[] {
  return [...text.matchAll(/(["'`])((?:\\.|(?!\1)[^\\])*)\1/g)].map((m) => m[2]!);
}

describe("the page is reachable, on both station profiles", () => {
  it("it is a section of Experimental, next to the optics bench", () => {
    const tab = findTab("/experimental");
    assert.ok(tab, "the Experimental tab exists");
    const ids = sectionIds(tab);
    assert.ok(ids.includes("layout"));
    assert.equal(sectionPath(tab, "layout"), "/experimental/layout");
    assert.ok(ids.indexOf("layout") > ids.indexOf("optics"), "after the optics bench");
  });

  it("it is not legacy and has no status marker: visible whatever the settings", () => {
    const section = findTab("/experimental")!.sections!.find((s) => s.seg === "layout")!;
    assert.equal(section.legacy, undefined);
    assert.equal(section.status, undefined);
    for (const legacy of [true, false]) {
      const shown = visibleTabs(legacy).find((t) => t.to === "/experimental")!;
      assert.ok(sectionIds(shown).includes("layout"), `legacy features ${legacy ? "on" : "off"}`);
    }
    assert.ok(TABS.length <= 12, "and the top bar did not grow");
  });

  it("the router maps it to the page", () => {
    assert.match(read("router.tsx"), /"\/experimental\/layout":\s*<OpticalLayoutPage \/>/);
    assert.match(read("router.tsx"), /import OpticalLayoutPage from "@\/pages\/OpticalLayoutPage";/);
  });

  it("nothing in the feature depends on the station profile", () => {
    for (const f of OWN) {
      if (f.endsWith("devMock.ts")) continue; // it fakes /api/health for the page, nothing more
      const text = readFileSync(f, "utf8");
      assert.ok(!/useStationProfile|useIsOpticalStation|isOpticalProfile|station_profile/.test(text.replace(/\/\/.*$/gm, "")), `${f} looks at the station profile`);
    }
  });
});

describe("the calls of the feature", () => {
  it("every URL goes to /api/optical-layouts (or to the two existing lists the link pickers read)", () => {
    const allowed = ["/api/optical-layouts", "/api/optics/devices", "/api/operator/optical-state"];
    for (const f of OWN) {
      if (f.endsWith("devMock.ts")) continue;
      for (const l of literals(readFileSync(f, "utf8"))) {
        if (!l.startsWith("/api/")) continue;
        assert.ok(allowed.some((a) => l.startsWith(a)), `${f}: ${l}`);
      }
    }
  });

  it("nothing builds an external-agent URL", () => {
    for (const f of OWN) {
      assert.deepEqual(literals(readFileSync(f, "utf8")).filter((l) => l.includes("/api/ext")), [], f);
    }
  });

  it("writes are PUT / POST with a JSON body, and the client sends the Content-Type header", () => {
    assert.match(CLIENT, /"Content-Type"\] = "application\/json"/);
    assert.match(CLIENT, /init\.body = JSON\.stringify\(body\)/);
    const writes = [...CLIENT.matchAll(/send\("(PUT|POST)",/g)];
    assert.equal(writes.length, 4, "save (PUT), archive, comment, resolve; going back to a revision is a save");
    // a write names `body` as its last argument (checked per line: the paths contain parentheses)
    for (const line of CLIENT.split("\n").filter((l) => /send\("(?:PUT|POST)",/.test(l))) assert.ok(/,\s*body\)/.test(line), `a write without a body: ${line.trim()}`);
  });

  it("every write is made with the operator's name", () => {
    const pageAndEditor = EDITOR + readFileSync(PAGE, "utf8") + read("components", "opticallayout", "CommentsPanel.tsx");
    assert.match(pageAndEditor, /useOperatorName\(\)/);
    const withOperatorCalls = [...pageAndEditor.matchAll(/withOperator\(/g)].length;
    // save, save as, restore, archive, comment, resolve
    assert.ok(withOperatorCalls >= 6, `${withOperatorCalls} writes use withOperator`);
    // and the places that call the mutations / api writes take their body from it
    assert.match(EDITOR, /save\.mutateAsync\(\{ id: layout\.id, body: body\.value \}\)/);
    assert.match(EDITOR, /layoutApi\.put\(id, body\.value\)/);
  });

  it("the document is read once: nothing refetches it behind the operator's back", () => {
    const hooks = read("api", "opticalLayoutHooks.ts");
    const doc = hooks.slice(hooks.indexOf("export function useLayoutDocument"), hooks.indexOf("export function useSaveLayout"));
    assert.match(doc, /staleTime: Infinity/);
    assert.match(doc, /refetchOnWindowFocus: false/);
    assert.match(doc, /refetchOnReconnect: false/);
  });
});

describe("dragging", () => {
  it("positions reach the page through the frame throttle, not on every pointer event", () => {
    assert.match(CANVAS, /createFrameThrottle<ReadonlyMap<string, Vec>>\(\(m\) => latest\.current\.onPreview\(m\)/);
    assert.match(CANVAS, /requestAnimationFrame\(cb\)/);
    assert.match(CANVAS, /moveThrottle\.push\(positions\)/);
    // onPreview is called with a map only from the throttle; the only other call clears it
    const calls = [...CANVAS.matchAll(/onPreview\(([^)]*)\)/g)].map((m) => m[1]!.trim());
    assert.deepEqual(calls.sort(), ["m", "null"].sort(), calls.join(" | "));
  });

  it("the release commits ONE step with the last position, after cancelling the pending frame", () => {
    const end = CANVAS.slice(CANVAS.indexOf("const endDrag"), CANVAS.indexOf("// Escape while dragging"));
    assert.match(end, /moveThrottle\.cancel\(\);[\s\S]*onCommitMove\(last\)[\s\S]*onPreview\(null\)/);
    assert.match(EDITOR, /onCommitMove=\{\(positions\) => edit\(\(l\) => setPositions\(l, positions\), undefined\)\}/);
  });

  it("panning and the cursor read-out are throttled too", () => {
    assert.match(CANVAS, /panThrottle/);
    assert.match(CANVAS, /cursorThrottle/);
  });

  it("the trace is recomputed from what is shown (the preview while dragging), memoised on it", () => {
    assert.match(EDITOR, /const trace = useMemo\(\(\) => traceLayoutDetailed\(shown\), \[shown\]\);/);
    assert.match(EDITOR, /setPositions\(present, preview\)/);
  });

  it("the cursor read-out and the tool previews live above the layout, so a mouse move does not repaint it", () => {
    assert.match(CANVAS, /className="pointer-events-none absolute left-0 top-0"/);
    assert.match(CANVAS, /renderOverlay\?\.\(\{ view, cursor \}\)/);
    assert.match(read("components", "opticallayout", "LayoutSvg.tsx"), /export const LayoutSvg = memo\(/);
  });

  it("a locked component is not dragged, and says why", () => {
    assert.match(CANVAS, /if \(comp\.locked\)/);
    assert.match(CANVAS, /onLockedDrag/);
  });

  it("Escape during a drag puts everything back", () => {
    assert.match(CANVAS, /e\.key === "Escape" && drag\.current/);
  });
});

describe("the keyboard", () => {
  it("is the lib's map, and a box that is being typed in is left alone", () => {
    assert.match(EDITOR, /isEditableTarget\(e\.target as HTMLElement \| null\)/);
    assert.match(EDITOR, /interpretKey\(e\)/);
  });

  it("Ctrl+S saves through a ref, so the handler never holds a stale operator name", () => {
    assert.match(EDITOR, /saveRef\.current\(\)/);
    assert.match(EDITOR, /saveRef\.current = doSave/);
  });

  it("the wheel zooms through a native, non-passive listener (React's are passive)", () => {
    assert.match(CANVAS, /addEventListener\("wheel", onWheel, \{ passive: false \}\)/);
  });
});

describe("saving", () => {
  it("a 409 raises the banner with both ways out, and nothing of the operator's is dropped", () => {
    assert.match(EDITOR, /dispatch\(\{ type: "conflict"/);
    assert.match(EDITOR, /<ConflictBanner/);
    assert.match(EDITOR, /onLoadLatest=/);
    assert.match(EDITOR, /onSaveAs=/);
    assert.match(EDITOR, /dispatch\(\{ type: "adopt-latest"/);
  });

  it("errors in the layout are shown before the request is made", () => {
    assert.match(EDITOR, /errorsOf\(validateLayout\(layout\)\)/);
  });

  it("leaving with unsaved edits asks first: reload, in-app navigation, switching layouts", () => {
    assert.match(EDITOR, /addEventListener\("beforeunload"/);
    assert.match(readFileSync(PAGE, "utf8"), /useBlocker\(/);
    assert.match(readFileSync(PAGE, "utf8"), /window\.confirm\(CONFIRM_LEAVE\(\)\)/);
  });

  it("PNG export without a server renderer is a message, not an error", () => {
    assert.match(EDITOR, /e instanceof PngUnavailableError/);
    assert.match(CLIENT, /class PngUnavailableError/);
  });

  it("SVG export is the canvas component rendered to a string, with the theme's variables filled in", () => {
    assert.match(EDITOR, /renderToStaticMarkup\(/);
    assert.match(EDITOR, /resolveCssVars\(markup,/);
    assert.match(EDITOR, /standaloneSvg\(/);
  });
});

describe("licence and look", () => {
  it("no external icon set, no gwoptics, no image files", () => {
    const forbidden = [/gwoptics/i, /ComponentLibrary/i, /lucide/i, /react-icons/i, /@heroicons/i, /fontawesome/i, /from "[^"]*\.(?:svg|png|jpe?g)"/, /<img\b/];
    for (const f of OWN) {
      const text = readFileSync(f, "utf8");
      // the file that explains the rule may name what it forbids
      const body = f.endsWith("glyphs.ts") ? text.replace(/^\/\/.*$/gm, "") : text;
      for (const re of forbidden) assert.ok(!re.test(body), `${f} matches ${re}`);
    }
  });

  it("every component type has its own drawing, made in the lib from simple shapes", () => {
    const glyphs = read("lib", "opticalLayout", "glyphs.ts");
    for (const t of ["mirror", "beam_splitter", "dichroic", "grating", "lens", "objective", "waveplate", "polarizer", "filter", "window", "nd", "aotf", "prism", "periscope", "iris", "shutter", "sample", "detector", "camera", "spectrometer", "power_meter", "beam_dump", "blocker", "fiber_in", "fiber_out", "laser", "lamp", "led", "custom"]) {
      assert.match(glyphs, new RegExp(`case "${t}"`), t);
    }
  });

  it("colours come from the design tokens (the token test covers the classes; this covers the SVG attributes)", () => {
    for (const f of COMPONENT_FILES) {
      const text = readFileSync(f, "utf8").replace(/\/\/.*$/gm, "");
      const hexes = text.match(/["'`]#[0-9a-fA-F]{3,8}["'`]/g) ?? [];
      assert.deepEqual(hexes, [], `${f} hard-codes colours`);
    }
  });
});

describe("identity strings are never translated", () => {
  it("no comparison involves a tr() call", () => {
    const bad: string[] = [];
    for (const f of OWN) {
      const text = readFileSync(f, "utf8");
      for (const m of text.matchAll(/(?:[=!]==?\s*tr\(|\btr\([^()]*\)\s*[=!]==?)/g)) bad.push(`${f}: ${m[0]}`);
    }
    assert.deepEqual(bad, []);
  });

  it("type, action and end are looked up from the raw value", () => {
    assert.match(read("lib", "opticalLayout", "catalog.ts"), /switch \(type\) \{\s*case "laser":/);
    assert.match(read("lib", "opticalLayout", "indicators.ts"), /action === "back_hit"/);
    assert.match(read("lib", "opticalLayout", "indicators.ts"), /action === "blocked"/);
  });
});

describe("the dev mock stays out of production", () => {
  it("only main.tsx imports it, and only behind import.meta.env.DEV", () => {
    const main = read("main.tsx");
    const dev = main.slice(main.indexOf("if (import.meta.env.DEV)"));
    assert.match(dev, /await import\("\.\/lib\/opticalLayout\/devMock"\)/);
    for (const f of files(SRC)) {
      if (f.endsWith("main.tsx") || f.endsWith("devMock.ts")) continue;
      assert.ok(!/opticalLayout\/devMock|["']\.\/devMock/.test(readFileSync(f, "utf8")), `${f} imports the dev mock`);
    }
    assert.ok(existsSync(join(SRC, "lib", "opticalLayout", "devMock.ts")));
  });
});

describe("a narrow window can still add components and find them", () => {
  const TOOLBAR = read("components", "opticallayout", "Toolbar.tsx");
  const FORM = read("components", "opticallayout", "LayoutForm.tsx");

  it("the palette collapses below the xl breakpoint, so the toolbar has an Add menu in its place", () => {
    assert.match(EDITOR, /xl:grid-cols-\[/, "the editor grid is a single column on narrow windows");
    assert.match(TOOLBAR, /xl:hidden/, "the Add menu shows only where the palette does not");
    assert.match(TOOLBAR, /onAdd\(/);
    assert.match(EDITOR, /onAdd=\{onAdd\}/);
    // the menu offers every category the palette does
    assert.match(TOOLBAR, /CATEGORY_ORDER/);
    assert.match(TOOLBAR, /typesIn\(/);
  });

  it("the inspector, with nothing selected, lists the components: selecting one from the list moves the view to it", () => {
    assert.match(FORM, /data-component-list/);
    assert.match(FORM, /onSelect\(c\.id\)/);
    assert.match(EDITOR, /levels=\{levels\}/);
    // `actions.select` focuses the first of the ids; the list goes through it, not through the bare setter
    assert.match(EDITOR, /onSelect=\{\(id\) => actions\.select\(\[id\]\)\}/);
    assert.match(EDITOR, /select: \(ids: string\[\]\) => \{\s*setSelection\(ids\);\s*if \(ids\[0\]\) focusOn\(ids\[0\]\);/);
  });

  it("a light source is never reported as 'not reached by a beam'", () => {
    assert.match(FORM, /isSourceType\(c\.type\)/);
  });
});

describe("the picker's list follows what was written", () => {
  it("save-as and 'load the newest' ask the list for a refresh; archiving leaves the layout after the list no longer has it", () => {
    assert.match(EDITOR, /refreshList\(\);\s*onSwitchTo\(id\);/);
    assert.match(EDITOR, /setViewingRev\(null\);\s*refreshList\(\);/);
    const page = readFileSync(PAGE, "utf8");
    assert.match(page, /list\.refetch\(\)\.then\(\(\) => setParams\(\{\}, \{ replace: true \}\)\)/);
  });
});

describe("a fibre output follows what arrives at its fibre input", () => {
  const FORM = read("components", "opticallayout", "ComponentForm.tsx");

  it("linking a fibre input goes through linkFibre with what the trace says arrives, and the person is told", () => {
    assert.match(FORM, /c\.type === "fiber_in" && field\.key === "continues_at"/);
    assert.match(FORM, /linkFibre\(layout, c\.id, outId, arrivingWavelengths\(ctx\.trace, c\.id\)\)/);
    assert.match(FORM, /ctx\.actions\.notify\(/);
    assert.match(EDITOR, /notify: \(text: string\) => say\("info", text\)/);
    assert.match(EDITOR, /layout: shown,\s*trace,/, "the inspector gets the trace the canvas draws");
  });

  it("an output whose wavelength differs from what arrives offers the arriving one, as an explicit action", () => {
    assert.match(FORM, /setSourceWavelengths\(l, c\.id, arriving\)/);
    assert.match(FORM, /mismatch &&/);
  });
});

describe("a cut trace is said, not hidden", () => {
  const SVG = read("components", "opticallayout", "LayoutSvg.tsx");
  const FORM = read("components", "opticallayout", "ComponentForm.tsx");
  const LIST = read("components", "opticallayout", "LayoutForm.tsx");
  const TRACE = read("lib", "opticalLayout", "trace.ts");

  it("the editor shows a banner made from truncation(trace), worked out again only when the trace changes", () => {
    assert.match(EDITOR, /const cut = useMemo\(\(\) => truncation\(trace\), \[trace\]\);/);
    assert.match(EDITOR, /\{cut && \(\s*<div role="status" data-trace-cut=""/, "shown whenever the trace is cut, as a status strip");
    assert.match(EDITOR, /traceCutMessage\(cut\)/);
  });

  it("the near misses come from shownNearMisses, which skips them while a drag is previewed", () => {
    assert.match(EDITOR, /const misses = useMemo\(\(\) => shownNearMisses\(shown, trace, preview !== null\), \[shown, trace, preview\]\);/);
    assert.ok(!/\bnearMisses\(shown/.test(EDITOR), "the editor does not call nearMisses itself any more");
  });

  it("the banner has a button that moves the view to where the trace stopped, through the canvas's focus", () => {
    assert.match(EDITOR, /data-trace-cut-locate/);
    assert.match(EDITOR, /onClick=\{showCut\}/);
    assert.match(EDITOR, /const p = cutPoint\(latest\.current\.trace\);/);
    assert.match(EDITOR, /setFocus\(\(f\) => \(\{ point: p, nonce: \(f\?\.nonce \?\? 0\) \+ 1, center: true, minZoom: CUT_ZOOM \}\)\)/);
    assert.match(CANVAS, /if \(focus\.center\) \{\s*setView\(centerOn\(view, focus\.point, size\.w, size\.h, focus\.minZoom \?\? 0\)\);/);
  });

  it("the check panel gets the problem from the same trace (diagnostics puts the cut first)", () => {
    assert.match(EDITOR, /diagnostics\(shown, trace,/);
  });

  it("the canvas marks the point where the trace stopped, with a square big enough to find in a dense field", () => {
    assert.match(SVG, /branch\.end === "budget"/);
    assert.match(SVG, /<rect key="end" x=\{last\.x - 8\} y=\{last\.y - 8\} width=\{16\} height=\{16\} fill="var\(--mast-danger\)" stroke="var\(--mast-panel\)" strokeWidth=\{2\.5\} \/>/);
  });

  it("nothing says that no beam reaches a component when the trace was cut", () => {
    assert.match(FORM, /<HitList cut=\{truncation\(ctx\.trace\) !== null\}/, "the inspector's hit list is told");
    assert.match(FORM, /\{cut \? tr\("追迹被截断，不知道有没有光打到它。"/, "and says so instead of 'no beam reaches it'");
    assert.match(LIST, /isSourceType\(c\.type\) \? undefined : traceCut \? tr\(/, "the dot in the component list");
    assert.match(EDITOR, /traceCut=\{cut !== null\}/);
    assert.match(EDITOR, /aimErrorText\(r\.error, truncation\(cur\.trace\) !== null\)/, "the aim tool's 'no beam' answer");
  });

  it("the tracer counts events in one shared budget that every walk sees, and the options can make it smaller", () => {
    assert.match(TRACE, /maxTraceEvents: 5000/);
    assert.match(TRACE, /function walk\(\s*w: World,\s*budget: Budget,/);
    assert.match(TRACE, /if \(budget\.events >= budget\.limit\)/);
    assert.match(TRACE, /if \(budget\.cut\) return;/, "the transmitted sibling is not made after a cut");
    assert.match(TRACE, /if \(budget\.cut\) break;/, "no later tree, no fibre continuation");
  });
});
