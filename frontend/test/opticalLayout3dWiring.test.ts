// ════════════════════════════════════════════════════════════════════════════
// The 3D view's wiring (docs/v2/design/optical_layout_3d_view.md), pinned from source in the style of
// opticalLayoutCanvasWiring.test.ts: the components are .tsx, which Node does not run, so the places where
// correct logic is easy to leave unconnected are checked as text.
//
//     cd frontend && npm run test:unit
//
//   · three.js is loaded only with the 3D view (React.lazy), never by the 2D editor;
//   · the 3D view gets the same edit callbacks as the 2D canvas, so a drag in 3D is previewed through the same
//     frame throttle and lands as ONE undo step of edit.ts setPositions, and a click selects through the same
//     selection the inspector reads;
//   · V and the toolbar switch the view; the selection, the layers and focus mode are the editor's, shared;
//   · a browser without WebGL 2 gets a message and keeps the 2D view.
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it } from "node:test";

import { interpretKey, shortcutHelp } from "../src/lib/opticalLayout/keys.ts";

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

const EDITOR = read("components", "opticallayout", "LayoutEditor.tsx");
const TOOLBAR = read("components", "opticallayout", "Toolbar.tsx");
const VIEW = read("components", "opticallayout", "view3d", "View3D.tsx");
const ENGINE = read("components", "opticallayout", "view3d", "engine.ts");
const VIEW3D_DIR = join(SRC, "components", "opticallayout", "view3d");

/** The props a JSX element gets, as text: from `<Name` to the `/>` that closes it. */
function jsxProps(text: string, name: string): string {
  const start = text.indexOf(`<${name}`);
  assert.ok(start >= 0, `<${name}> is not used`);
  return text.slice(start, text.indexOf("/>", start));
}

describe("three.js comes with the 3D view only", () => {
  it("no file outside view3d/ imports three", () => {
    for (const f of files(SRC)) {
      if (f.startsWith(VIEW3D_DIR + sep)) continue;
      const imports = [...readFileSync(f, "utf8").matchAll(/from\s+"(three(?:\/[^"]*)?)"/g)].map((m) => m[1]);
      assert.deepEqual(imports, [], relative(SRC, f));
    }
  });

  it("the editor loads the view with React.lazy and imports nothing else of it", () => {
    assert.match(EDITOR, /const View3D = lazy\(\(\) => import\("\.\/view3d\/View3D"\)\);/);
    const staticImports = [...EDITOR.matchAll(/^import (?!type\b)[^;]*from "([^"]*view3d[^"]*)"/gm)].map((m) => m[1]);
    assert.deepEqual(staticImports, []);
    assert.match(EDITOR, /<Suspense fallback=/);
    assert.match(VIEW, /^export default function View3D\(/m);
  });

  it("the modules the unit tests build in Node import with relative paths", () => {
    for (const f of ["geometries.ts", "materials.ts", "LayoutScene3D.ts", "exporters.ts"]) {
      assert.ok(!/from "@\//.test(readFileSync(join(VIEW3D_DIR, f), "utf8")), f);
    }
  });
});

describe("the 3D view edits through the editor, like the 2D canvas", () => {
  const canvas = jsxProps(EDITOR, "LayoutCanvas");
  const view = jsxProps(EDITOR, "View3D");

  it("gets the same layout, trace, selection, layers, snap mode and callbacks", () => {
    for (const prop of [
      "layout={shown}",
      "trace={trace}",
      "display={display}",
      "theme={theme}",
      "selected={selectedSet}",
      "snap={snap}",
      "tool={tool}",
      "readOnly={readOnly}",
      "focus={focus}",
      "centerRef={centerRef}",
      "onSelect={selectIds}",
      "onPreview={setPreview}",
      "onCommitMove={(positions) => edit((l) => setPositions(l, positions), undefined)}",
      "onDropType={placeAt}",
      "onDropTemplate={placeTemplate}",
      "onToolPoint={onToolPoint}",
      "beamPick={beamRef}",
      "onBackgroundClick={pickBeamAt}",
    ]) {
      assert.ok(canvas.includes(prop), `the 2D canvas no longer gets ${prop}`);
      assert.ok(view.includes(prop), `the 3D view does not get ${prop}`);
    }
  });

  it("previews a drag through a frame throttle and commits ONE step with the last position on release", () => {
    assert.match(VIEW, /createFrameThrottle<ReadonlyMap<string, Vec>>\(\(m\) => latest\.current\.onPreview\(m\)/);
    const end = VIEW.slice(VIEW.indexOf("const endPress"), VIEW.indexOf("// Escape during a drag"));
    assert.match(end, /moveThrottle\.cancel\(\);\s*if \(commit && p\.moved && lastMove\.current\) latest\.current\.onCommitMove\(lastMove\.current\);/);
    assert.match(end, /latest\.current\.onPreview\(null\);/);
    assert.match(VIEW, /e\.key === "Escape" && press\.current\?\.drag/);
  });

  it("uses the lib's drag and selection rules, not its own", () => {
    assert.match(VIEW, /dragPositions\(d, \{ x: at\[0\], y: at\[1\] \}, latest\.current\.layout\.table, dragSnapMode\(latest\.current\.snap, e\)\)/);
    assert.match(VIEW, /const next = selectionOnPress\(\[\.\.\.cur\.selected\], hit\.id, additive\);\s*cur\.onSelect\(next\);/);
    assert.match(VIEW, /selectionOnClick\(\[\.\.\.cur\.selected\], p\.hit, p\.additive\)/);
    assert.match(VIEW, /startDrag\(cur\.layout, next, hit\.id,/);
    assert.ok(!/snapPoint\(cur\.layout\.table, \{ x: lead/.test(VIEW), "no second copy of the snapping rule");
  });

  it("a locked component is not dragged, and says why", () => {
    assert.match(VIEW, /latest\.current\.onLockedDrag\?\.\(\)/);
    assert.match(EDITOR, /onLockedDrag=\{\(\) => say\("info", tr\("这个器件已锁定/);
  });

  it("a press on a component never turns the view (it is caught before the orbit controls)", () => {
    assert.match(VIEW, /onPointerDownCapture=\{onPointerDownCapture\}/);
    const down = VIEW.slice(VIEW.indexOf("const onPointerDownCapture"), VIEW.indexOf("const onPointerMove"));
    assert.ok(down.indexOf("e.stopPropagation()") > down.indexOf("if (!hit) return;"), "only a press on a component is taken from the controls");
  });
});

describe("switching the view", () => {
  it("V and the toolbar", () => {
    assert.deepEqual(interpretKey({ key: "v" }), { kind: "view3d" });
    assert.deepEqual(interpretKey({ key: "V" }), { kind: "view3d" }, "Caps Lock on");
    assert.equal(interpretKey({ key: "v", ctrlKey: true }), null, "Ctrl+V pastes");
    assert.equal(interpretKey({ key: "V", shiftKey: true }), null);
    assert.equal(interpretKey({ key: "v", altKey: true }), null);
    assert.ok(shortcutHelp().some((h) => h.keys === "V" && h.en.includes("3D")));
    assert.match(EDITOR, /case "view3d":\s*handled\(\);\s*setView3d\(\(v\) => !v\);/);
    assert.match(EDITOR, /view3d=\{view3d\}\s*onView3d=\{setView3d\}/);
    assert.match(TOOLBAR, /onClick=\{\(\) => p\.onView3d\?\.\(true\)\}/);
    assert.match(TOOLBAR, /onClick=\{\(\) => p\.onView3d\?\.\(false\)\}/);
  });

  it("the choice is remembered; the selection, the layers and focus mode are the editor's for both views", () => {
    assert.match(EDITOR, /useState<boolean>\(\(\) => readStored\(VIEW3D_KEY, false, isBool\)\)/);
    assert.match(EDITOR, /writeStored\(VIEW3D_KEY, view3d\)/);
    // one selection state, one display state, one focus state: the 3D view has none of its own
    for (const own of [/useState<string\[\]>/, /withLayerDefaults/, /setFocusMode/]) assert.ok(!own.test(VIEW), `the 3D view keeps its own ${own}`);
  });

  it("without WebGL 2 the view says so and offers the 2D view back", () => {
    assert.match(VIEW, /const \[probe\] = useState\(\(\) => probeWebgl\(\(\) => document\.createElement\("canvas"\)\)\);/);
    assert.match(VIEW, /useState<Failure \| null>\(probe\.ok \? null : "no-webgl"\)/);
    assert.match(VIEW, /onExit=\{props\.onExit\}/);
    assert.match(EDITOR, /onExit=\{\(\) => setView3d\(false\)\}/);
    assert.match(ENGINE, /addEventListener\("webglcontextlost"/);
  });
});

describe("the camera", () => {
  it("is z-up and draws on demand", () => {
    assert.match(ENGINE, /this\.persp\.up\.set\(0, 0, 1\)/);
    assert.match(ENGINE, /this\.ortho\.up\.set\(0, 0, 1\)/);
    assert.match(ENGINE, /this\.controls\.addEventListener\("change", this\.onCameraChange\)/);
    const change = ENGINE.slice(ENGINE.indexOf("private onCameraChange"), ENGINE.indexOf("private onLost"));
    assert.match(change, /this\.requestRender\(\);\s*\};\s*$/, "every camera change draws a frame");
    assert.match(change, /if \(this\.options\.low\) \{\s*this\.content\.setDetail\("low"\);/, "the coarse shapes only on a software renderer");
    // no render loop that runs whether or not anything changed
    assert.equal([...ENGINE.matchAll(/requestAnimationFrame\(/g)].length, 1);
  });
});

describe("keys, layers and the graphics context", () => {
  it("the view keys are the lib's, and typing in a box is left alone", () => {
    assert.match(VIEW, /if \(e\.defaultPrevented \|\| isEditableTarget\(e\.target as HTMLElement \| null\)\) return;\s*const k = view3dKey\(e\);/);
  });

  it("in 3D the menu greys out the layers only the 2D canvas draws", () => {
    assert.match(TOOLBAR, /const only2d = p\.view3d === true && !drawnIn3d\(id\);/);
    assert.match(TOOLBAR, /disabled=\{only2d\}/);
    assert.match(TOOLBAR, /tr\("仅 2D：3D 视图不画这一层", "2D only: the 3D view does not draw this layer"\)/);
  });

  it("a lost context is shown over the view and comes back when the browser restores it; leaving the view frees the context", () => {
    assert.match(ENGINE, /addEventListener\("webglcontextrestored", this\.onRestored, false\)/);
    assert.match(VIEW, /onContextLost: \(\) => setLost\(true\),\s*onContextRestored: \(\) => setLost\(false\),/);
    const dispose = ENGINE.slice(ENGINE.indexOf("  dispose(): void {"));
    assert.ok(dispose.indexOf("this.renderer.dispose();") >= 0 && dispose.indexOf("this.renderer.forceContextLoss();") > dispose.indexOf("this.renderer.dispose();"), "dispose, then give the context back");
    assert.ok(dispose.indexOf('removeEventListener("webglcontextlost"') < dispose.indexOf("this.renderer.forceContextLoss();"), "its own loss is not reported as a failure");
    // the scene is freed through the holder, which leaves a scene of a lost context alone (sceneHolder.ts)
    assert.match(dispose, /this\.holder\.dispose\(\);/);
    assert.match(ENGINE, /this\.holder\.contextLost\(\);[\s\S]*this\.hooks\.onContextLost\(\);/);
    assert.match(ENGINE, /private onRestored = \(\) => \{\s*this\.holder\.contextRestored\(\);/);
  });
});
