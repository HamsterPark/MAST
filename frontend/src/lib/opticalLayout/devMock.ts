// ════════════════════════════════════════════════════════════════════════════
// Development-only stand-in for /api/optical-layouts — enabled by `vite dev` with
// `?layoutmock=1` in the address, never part of a production bundle (main.tsx loads it
// behind `import.meta.env.DEV`, so the branch and this module are tree-shaken out).
//
// It keeps layouts in memory and answers the way the contract says (sections 6.1 and 11.3
// of docs/v2/design/optical_layout.md): list (newest first), get as {layout, trace,
// links_state}, PUT with a revision check (409 when the base revision is behind, 422 for
// what validate.ts refuses), history, revisions, comments with `author`, archive; PNG
// answers 404 so the "not available" path can be seen. Two layouts are seeded: a small
// readable one ("shg-demo") with a deliberate near-miss and an off-centre hit, and
// ?layoutmock=big for a 400-component stress layout, ?layoutmock=cut for the
// worst case, whose trace the budget of 5000 events cuts (banner, red square, check panel),
// and next to it "heavy-demo", the heaviest trace that still fits the budget.
//
// In the console: `__layoutMock.bump("shg-demo")` pretends another tab saved, so the next
// save in this tab gets a 409.
// ════════════════════════════════════════════════════════════════════════════

import { addComponent, emptyAnnotations, emptyLayout, newComponent, setAnnotation, setLinks, setProp, addText } from "./edit.ts";
import { traceLayout } from "./trace.ts";
import { errorsOf, validateLayout } from "./validate.ts";
import { CATALOG, typeLabel } from "./catalog.ts";
import { COMPONENT_TYPES } from "./types.ts";
import type { LayoutComment, LayoutComponent, OpticalLayout } from "./types.ts";

export const LAYOUT_DEV_MOCK_MARKER = "LAYOUT_DEV_MOCK_MARKER";

type MockComment = Omit<LayoutComment, "kind"> & { author: "operator" | "agent" };

interface Stored {
  layout: OpticalLayout;
  history: OpticalLayout[];
  comments: MockComment[];
  archived: boolean;
}

const store = new Map<string, Stored>();
let nextComment = 1;

const now = () => new Date().toISOString();
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

function c(type: Parameters<typeof newComponent>[0], id: string, x: number, y: number, angle: number, props?: Record<string, unknown>, label?: string, aperture?: number): LayoutComponent {
  return newComponent(type, id, { x, y }, { angle_deg: angle, ...(props ? { props } : {}), ...(label ? { label } : {}), ...(aperture ? { aperture_mm: aperture } : {}) });
}

function demo(): OpticalLayout {
  let l = emptyLayout("shg-demo", "SHG excitation path (demo)");
  l = {
    ...l,
    revision: 3,
    updated_at: "2026-11-03T09:30:00+00:00",
    updated_by: "ana",
    notes: "Demo layout from the development mock. 532 nm pump and 1064 nm probe are combined on a dichroic mirror and focused on the sample.",
    components: [
      c("laser", "las1", 62.5, 112.5, 0, { wavelength_nm: 532, power_mw: 50 }, "Pump 532"),
      c("laser", "las2", 62.5, 412.5, 0, { wavelength_nm: 1064, power_mw: 200 }, "Probe 1064"),
      c("waveplate", "hwp1", 162.5, 112.5, 0, { retardance: "half" }, "HWP1"),
      c("beam_splitter", "bs1", 262.5, 112.5, 135, { reflect_fraction: 0.3 }, "BS1"),
      c("detector", "det1", 262.5, 262.5, 270, undefined, "Pickoff"),
      c("mirror", "m2", 462.5, 112.5, 135, undefined, "M2"),
      c("filter", "flt1", 162.5, 412.5, 0, { pass_min_nm: 1000, pass_max_nm: 1100 }, "LP1000"),
      c("dichroic", "dm1", 462.5, 412.5, 135, { edge_nm: 600, reflects: "short" }, "DM1"),
      c("objective", "obj1", 812.5, 416.5, 0, undefined, "OBJ1"),
      c("sample", "smp1", 912.5, 412.5, 180, { reflects: false }, "Sample"),
      c("fiber_out", "fo1", 612.5, 662.5, 0, { wavelength_nm: 532 }, "Fibre out"),
      c("lens", "len1", 712.5, 662.5, 0, { focal_mm: 100 }, "L1"),
      c("fiber_in", "fi1", 812.5, 662.5, 180, { continues_at: "fo2" }, "Fibre in"),
      c("fiber_out", "fo2", 612.5, 762.5, 0, { wavelength_nm: 532 }, "Fibre out 2"),
      c("camera", "cam1", 812.5, 762.5, 180, undefined, "Camera"),
      c("mirror", "m9", 1062.5, 112.5, 315, undefined, "M9 (back side)"),
    ],
  };
  l = setAnnotation(l, "las1", { model: "Samba 532", manufacturer: "Cobolt", function: "pump", connection: "COM13" });
  l = setAnnotation(l, "hwp1", { model: "PRM1Z8 + half-wave plate", manufacturer: "Thorlabs", function: "power / polarisation", motorized: true, actuator: "KDC101", connection: "USB, satellite", extra: [{ key: "mount", value: "KM100" }] });
  l = setLinks(l, "hwp1", { device_id: "hwp_rot", slot: "excitation_waveplate" });
  l = setAnnotation(l, "obj1", { model: "LMPLFLN 50x", manufacturer: "Olympus", function: "focus on the sample", notes: "Worked at the 4 mm off-centre position on purpose in this demo." });
  l = setProp(l, "obj1", "focal_mm", 3.6);
  l = addText(l, { x: 640, y: 860 }, "enclosure door").layout;
  l = addComponent(l, "power_meter", { x: 1112.5, y: 412.5 }, { id: "pm1", label: "Power" }).layout;
  return l;
}

function big(): OpticalLayout {
  let l = emptyLayout("big-demo", "400 components (stress test)", { width_mm: 3000, height_mm: 2000, hole_pitch_mm: 25, hole_offset_mm: 12.5, grid: "metric" });
  const comps: LayoutComponent[] = [];
  for (let i = 0; i < 28; i++) comps.push(c("laser", `las${i + 1}`, 62.5, 112.5 + i * 62.5, 0, { wavelengths_nm: [532, 1064] }, `L${i + 1}`));
  for (let i = 0; i < 372; i++) {
    const col = i % 31;
    const row = Math.floor(i / 31);
    comps.push(c(i % 4 === 0 ? "beam_splitter" : i % 4 === 1 ? "mirror" : i % 4 === 2 ? "lens" : "waveplate", `x${i + 1}`, 162.5 + col * 87.5, 62.5 + row * 150, 135, undefined, `X${i + 1}`, 20));
  }
  l = { ...l, components: comps };
  return l;
}

/**
 * The worst case: 361 beam splitters 3 mm apart and 32 lasers of 16 wavelengths each, within
 * every limit of the model and still more than a trace may hold (233 760 branches without the budget).
 */
export function cutDemo(): OpticalLayout {
  const l = emptyLayout("cut-demo", "Trace budget (a layout the trace cannot finish)", { width_mm: 300, height_mm: 300, hole_pitch_mm: 25, hole_offset_mm: 12.5, grid: "metric" });
  const comps: LayoutComponent[] = [];
  for (let k = 0; k < 361; k++) comps.push(c("beam_splitter", `bs${k + 1}`, 50 + Math.floor(k / 19) * 3, 50 + (k % 19) * 3, 45, { reflect_fraction: 0.5 }, undefined, 25));
  const wavelengths = Array.from({ length: 16 }, (_, n) => 400 + 10 * n);
  for (let n = 0; n < 32; n++) comps.push(c("laser", `las${n + 1}`, 1, 50 + n * 0.5, 0, { wavelengths_nm: wavelengths }, undefined, 5));
  return { ...l, components: comps };
}

/**
 * The heaviest trace that still fits the budget: the same field of beam splitters with two lasers of 16 and
 * 13 wavelengths: about 4800 events, 6300 branches and 5000 near misses, and nothing cut.
 */
export function heavyDemo(): OpticalLayout {
  const l = cutDemo();
  const wavelengths = (n: number) => Array.from({ length: n }, (_, i) => 400 + 10 * i);
  const splitters = l.components.filter((x) => x.type === "beam_splitter");
  const lasers = l.components.filter((x) => x.type === "laser").slice(0, 2).map((x, i) => ({ ...x, props: { wavelengths_nm: wavelengths(i === 0 ? 16 : 13) } }));
  return { ...l, id: "heavy-demo", title: "Heaviest trace within the budget", components: [...splitters, ...lasers] };
}

/** Every component type once, in a grid, for looking at the drawings. */
function gallery(): OpticalLayout {
  let l = emptyLayout("gallery", "All component types (demo)", { width_mm: 800, height_mm: 700, hole_pitch_mm: 25, hole_offset_mm: 12.5, grid: "metric" });
  const types = COMPONENT_TYPES;
  const comps: LayoutComponent[] = types.map((t, i) => {
    const col = i % 6;
    const row = Math.floor(i / 6);
    return newComponent(t, `${CATALOG[t].prefix}1`, { x: 87.5 + col * 125, y: 612.5 - row * 125 }, { label: typeLabel(t) });
  });
  l = { ...l, components: comps, revision: 1, updated_at: "2026-11-03T09:00:00+00:00", updated_by: "ana" };
  return l;
}

function seed(): void {
  if (store.size > 0) return;
  const g = gallery();
  store.set(g.id, { layout: g, history: [g], archived: false, comments: [] });
  const d = demo();
  const earlier = { ...d, revision: 1, updated_at: "2026-11-02T16:00:00+00:00", updated_by: "ben", components: d.components.slice(0, 8) };
  const middle = { ...d, revision: 2, updated_at: "2026-11-03T08:00:00+00:00", updated_by: "ana", components: d.components.slice(0, 12) };
  store.set(d.id, {
    layout: d,
    history: [earlier, middle, d],
    archived: false,
    comments: [
      { id: "c1", author: "agent", by: "ext:claude-1", text: "OBJ1 sits about 4 mm off the beam centre. Is that intended? The summary flags it as off-centre.", component_id: "obj1", created_at: "2026-11-03T09:40:00+00:00", resolved: false, resolved_by: null, resolved_at: null },
      { id: "c2", author: "operator", by: "ana", text: "The 1064 filter was swapped for LP1000 last week.", component_id: "flt1", created_at: "2026-11-03T09:45:00+00:00", resolved: true, resolved_by: "ana", resolved_at: "2026-11-03T09:50:00+00:00" },
    ],
  });
  if (new URLSearchParams(window.location.search).get("layoutmock") === "big") {
    const b = big();
    store.set(b.id, { layout: b, history: [b], archived: false, comments: [] });
  }
  if (new URLSearchParams(window.location.search).get("layoutmock") === "cut") {
    for (const b of [cutDemo(), heavyDemo()]) store.set(b.id, { layout: b, history: [b], archived: false, comments: [] });
  }
}

function summary(s: Stored): Record<string, unknown> {
  return {
    id: s.layout.id,
    title: s.layout.title,
    revision: s.layout.revision,
    updated_at: s.layout.updated_at,
    updated_by: s.layout.updated_by,
    components: s.layout.components.length,
    open_comments: s.comments.filter((x) => !x.resolved).length,
  };
}

/** {layout, trace, links_state}: link state only for components that have links. */
const document = (l: OpticalLayout) => {
  const links_state: Record<string, unknown> = {};
  for (const c of l.components) {
    if (!c.links.device_id && !c.links.slot) continue;
    links_state[c.id] = {
      ...(c.links.device_id ? { device: { id: c.links.device_id, known: c.links.device_id === "hwp_rot", name: c.links.device_id, type: "mock", kind: "rotation", enabled: true, connected: c.links.device_id === "hwp_rot" } } : {}),
      ...(c.links.slot ? { slot: { id: c.links.slot, known: true, label: c.links.slot, kind: "choice", value: "hwp_532", valid: true, unit: "", set_by: "ana", set_at: "2026-11-03T09:00:00+00:00", valid_until: null } } : {}),
    };
  }
  return { layout: l, trace: traceLayout(l), links_state };
};

async function handle(url: URL, method: string, body: () => Promise<unknown>): Promise<Response | null> {
  const p = url.pathname;
  if (p === "/api/health") return json({ ok: true, station_profile: new URLSearchParams(window.location.search).get("profile") === "stm" ? "stm" : "optical", station_profile_source: "default", station_restart_pending: false });
  if (p === "/api/optics/devices") {
    return json({
      degraded: false,
      devices: [
        { id: "hwp_rot", name: "Thorlabs K10CR1 half-wave rotation", type: "thorlabs_kinesis", kind: "rotation", enabled: true, connected: true },
        { id: "superk", name: "NKT SuperK", type: "superk", kind: "laser", enabled: true, connected: false },
        { id: "pico1", name: "Picomotor controller", type: "newport_8742", kind: "openloop", enabled: false, connected: false },
        { id: "witec", name: "WITec Raman", type: "witec", kind: "spectrometer", enabled: true, connected: true },
      ],
    });
  }
  if (p === "/api/operator/optical-state") {
    return json({
      slots: [
        { id: "excitation_waveplate", label: "Excitation waveplate", kind: "choice", options: [{ id: "hwp_532", label: "λ/2 @ 532 nm", meta: {} }, { id: "hwp_633", label: "λ/2 @ 633 nm", meta: {} }], description: "Which λ/2 plate is in the excitation path." },
        { id: "objective", label: "Objective", kind: "choice", options: [{ id: "50x", label: "50x LMPLFLN", meta: {} }], description: "" },
      ],
      values: { excitation_waveplate: { value: "hwp_532", set_by: "ana", set_at: "2026-11-03T09:00:00+00:00", source: "web", valid_until: null, valid: true, note: "" } },
    });
  }
  if (!p.startsWith("/api/optical-layouts")) return null;

  seed();
  const rest = p.slice("/api/optical-layouts".length).split("/").filter(Boolean).map(decodeURIComponent);
  if (rest.length === 0) {
    if (method !== "GET") return json({ detail: "Method Not Allowed" }, 405);
    const rows: Array<Record<string, unknown>> = [...store.values()].filter((s) => !s.archived).map(summary);
    if (new URLSearchParams(window.location.search).get("layoutmock") === "broken") rows.push({ id: "broken-file", error: "not valid JSON (Expecting value: line 1 column 1)" });
    return json({ layouts: rows.sort((a, b) => String(b["updated_at"] ?? "").localeCompare(String(a["updated_at"] ?? ""))) });
  }
  const id = rest[0]!;
  const s = store.get(id);
  const sub = rest[1];

  if (!sub) {
    if (method === "GET") return s && !s.archived ? json(document(s.layout)) : json({ detail: `layout ${id} not found` }, 404);
    if (method === "PUT") {
      const b = (await body()) as { layout?: OpticalLayout; base_revision?: number; by?: string };
      if (!b?.layout || typeof b.base_revision !== "number" || !b.by) return json({ detail: [{ loc: ["body"], msg: "layout, base_revision and by are required", type: "missing" }] }, 422);
      if (b.layout.id !== id) return json({ detail: "the layout id does not match the path" }, 422);
      const cur = s?.layout.revision ?? 0;
      if (b.base_revision !== cur) return json({ detail: `base_revision ${b.base_revision} is not the current revision ${cur}`, current_revision: cur, updated_by: s?.layout.updated_by ?? "", updated_at: s?.layout.updated_at ?? "" }, 409);
      const problems = errorsOf(validateLayout(b.layout));
      if (problems.length > 0) return json({ detail: problems.map((p) => ({ loc: ["body", "layout", ...p.path.split(".")], msg: p.message, type: "value_error" })) }, 422);
      const next: OpticalLayout = { ...b.layout, id, revision: cur + 1, updated_at: now(), updated_by: b.by };
      if (s) {
        s.layout = next;
        s.history.push(next);
        s.archived = false;
      } else store.set(id, { layout: next, history: [next], comments: [], archived: false });
      return json(document(next));
    }
    return json({ detail: "Method Not Allowed" }, 405);
  }
  if (!s) return json({ detail: `layout ${id} not found` }, 404);
  if (sub === "delete" && method === "POST") {
    const b = (await body()) as { base_revision?: number };
    if (b?.base_revision !== s.layout.revision) return json({ detail: "revision is behind", current_revision: s.layout.revision }, 409);
    s.archived = true;
    return json({ id, archived: true });
  }
  if (sub === "history") return json({ revisions: [...s.history].reverse().map((h) => ({ revision: h.revision, updated_at: h.updated_at, updated_by: h.updated_by, components: h.components.length, title: h.title })) });
  if (sub === "revisions") {
    const r = s.history.find((h) => String(h.revision) === rest[2]);
    return r ? json({ revision: r.revision, updated_at: r.updated_at, updated_by: r.updated_by, layout: r, trace: traceLayout(r) }) : json({ detail: "no such revision" }, 404);
  }
  if (sub === "render.png") return json({ detail: "Not Found" }, 404);
  if (sub === "comments") {
    if (rest[2] && rest[3] === "resolve" && method === "POST") {
      const cm = s.comments.find((x) => x.id === rest[2]);
      if (!cm) return json({ detail: "no such comment" }, 404);
      const b = (await body()) as { by?: string };
      cm.resolved = true;
      cm.resolved_by = b?.by ?? "?";
      cm.resolved_at = now();
      return json(cm);
    }
    if (method === "POST") {
      const b = (await body()) as { by?: string; text?: string; component_id?: string };
      if (b?.component_id && !s.layout.components.some((x) => x.id === b.component_id)) return json({ detail: `this layout has no component ${b.component_id}` }, 422);
      const cm: MockComment = { id: `c${++nextComment + 1}`, author: "operator", by: b?.by ?? "?", text: b?.text ?? "", component_id: b?.component_id ?? null, created_at: now(), resolved: false, resolved_by: null, resolved_at: null };
      s.comments.push(cm);
      return json(cm);
    }
    const all = new URLSearchParams(url.search).get("status") !== "open";
    return json({ comments: all ? s.comments : s.comments.filter((x) => !x.resolved), open_count: s.comments.filter((x) => !x.resolved).length });
  }
  return json({ detail: "Not Found" }, 404);
}

export function installLayoutDevMockIfRequested(): boolean {
  if (typeof window === "undefined") return false;
  const q = new URLSearchParams(window.location.search);
  try {
    if (q.get("layoutmock")) sessionStorage.setItem("mast.layoutmock", q.get("layoutmock") === "0" ? "0" : "1");
    if (sessionStorage.getItem("mast.layoutmock") !== "1") return false;
  } catch {
    if (!q.get("layoutmock") || q.get("layoutmock") === "0") return false;
  }
  const realFetch = window.fetch.bind(window);
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const raw = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    const url = new URL(raw, window.location.href);
    const method = (init?.method ?? (input instanceof Request ? input.method : "GET")).toUpperCase();
    const hit = await handle(url, method, async () => (init?.body ? JSON.parse(String(init.body)) : input instanceof Request ? input.clone().json() : {}));
    return hit ?? realFetch(input, init);
  };
  (window as unknown as { __layoutMock: unknown }).__layoutMock = {
    bump(id: string) {
      seed();
      const s = store.get(id);
      if (s) s.layout = { ...s.layout, revision: s.layout.revision + 1, updated_by: "other-tab", updated_at: now() };
      return s?.layout.revision;
    },
    store,
    emptyAnnotations,
  };
  // eslint-disable-next-line no-console
  console.info(`[${LAYOUT_DEV_MOCK_MARKER}] optical layout mock enabled (?layoutmock=0 turns it off)`);
  return true;
}
