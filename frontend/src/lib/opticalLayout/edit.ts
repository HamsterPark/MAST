// Edit operations on an optical layout. Every function is pure: it returns a new layout
// and leaves the argument alone, and a component an operation does not touch keeps its
// object identity, so React can skip it and an undo step shares almost all of its memory
// with the one before (test/opticalLayoutEdit.test.ts).
//
// Values are normalised on the way in the way the server saves them (section 3): x and y
// to 0.01 mm, the angle to [0, 360) at 0.01 degree, the aperture to 0.1 mm and within
// 0.1 to 500 mm. Locked components are skipped by the operations an accidental gesture
// can trigger (drag, key nudge, rotate, delete); typing a number into the inspector goes
// through the setters, which do not check the lock.

import type {
  Annotations,
  ComponentType,
  LayoutComponent,
  LayoutText,
  Links,
  OpticalLayout,
  Props,
  TableSpec,
  Vec,
} from "./types.ts";
import { LIMITS, SCHEMA_VERSION, isSourceType } from "./types.ts";
import { CATALOG, DEFAULT_PROP_DECIMALS, cleanProps, labelFromId, nextId, propField, propFields } from "./catalog.ts";
import { canonicalAngle, pyRound, round2, rotateAbout } from "./geometry.ts";
import { cleanLine, cleanText } from "./text.ts";

export const DEFAULT_TABLE: TableSpec = {
  width_mm: 1200,
  height_mm: 900,
  hole_pitch_mm: 25,
  hole_offset_mm: 12.5,
  grid: "metric",
};

export function emptyAnnotations(): Annotations {
  return {
    model: "",
    manufacturer: "",
    function: "",
    motorized: false,
    actuator: "",
    connection: "",
    datasheet_url: "",
    notes: "",
    extra: [],
  };
}

/** The aperture as the server keeps it: 0.1 to 500 mm at 0.01 mm. */
export function clampAperture(mm: number): number {
  if (!Number.isFinite(mm)) return LIMITS.apertureMin;
  return Math.min(LIMITS.apertureMax, Math.max(LIMITS.apertureMin, round2(mm)));
}

/** Position, angle and aperture the way the server stores them. */
export function normalizeGeometry<T extends Pick<LayoutComponent, "x_mm" | "y_mm" | "angle_deg" | "aperture_mm">>(c: T): T {
  const x = round2(c.x_mm);
  const y = round2(c.y_mm);
  const a = canonicalAngle(c.angle_deg);
  const ap = clampAperture(c.aperture_mm);
  if (x === c.x_mm && y === c.y_mm && a === c.angle_deg && ap === c.aperture_mm) return c;
  return { ...c, x_mm: x, y_mm: y, angle_deg: a, aperture_mm: ap };
}

export function emptyLayout(id: string, title: string, table: TableSpec = DEFAULT_TABLE): OpticalLayout {
  return {
    schema: SCHEMA_VERSION,
    id,
    title: cleanLine(title),
    revision: 0,
    updated_at: "",
    updated_by: "",
    table: { ...table },
    components: [],
    texts: [],
    notes: "",
  };
}

export interface NewComponentOptions {
  id?: string;
  label?: string;
  angle_deg?: number;
  aperture_mm?: number;
  props?: Props;
}

/** A component of the given type with the catalogue defaults. */
export function newComponent(type: ComponentType, id: string, pos: Vec, opts: NewComponentOptions = {}): LayoutComponent {
  const info = CATALOG[type];
  return normalizeGeometry({
    id,
    type,
    label: opts.label ?? labelFromId(id),
    x_mm: pos.x,
    y_mm: pos.y,
    angle_deg: opts.angle_deg ?? info.defaultAngle,
    aperture_mm: opts.aperture_mm ?? info.defaultAperture,
    props: opts.props ?? info.defaultProps(),
    annotations: emptyAnnotations(),
    links: { device_id: null, slot: null },
    locked: false,
  });
}

export function takenIds(layout: OpticalLayout): Set<string> {
  return new Set([...layout.components.map((c) => c.id), ...layout.texts.map((t) => t.id)]);
}

export function getComponent(layout: OpticalLayout, id: string): LayoutComponent | undefined {
  return layout.components.find((c) => c.id === id);
}

export type AddRefusal = "components" | "sources" | null;

/** Why another component of this type cannot be added (section 3 limits), or null. */
export function addRefusal(layout: OpticalLayout, type: ComponentType): AddRefusal {
  if (layout.components.length >= LIMITS.components) return "components";
  if (isSourceType(type) && layout.components.filter((c) => isSourceType(c.type)).length >= LIMITS.sources) return "sources";
  return null;
}

/** Adds a component and returns the new layout and the new id. A refused add returns the layout as it was. */
export function addComponent(
  layout: OpticalLayout,
  type: ComponentType,
  pos: Vec,
  opts: NewComponentOptions = {},
): { layout: OpticalLayout; id: string | null } {
  if (addRefusal(layout, type) !== null) return { layout, id: null };
  const id = opts.id ?? nextId(CATALOG[type].prefix, takenIds(layout));
  const c = newComponent(type, id, pos, opts);
  return { layout: { ...layout, components: [...layout.components, c] }, id };
}

function mapSelected(
  layout: OpticalLayout,
  ids: ReadonlySet<string>,
  fn: (c: LayoutComponent) => LayoutComponent,
): OpticalLayout {
  let changed = false;
  const components = layout.components.map((c) => {
    if (!ids.has(c.id)) return c;
    const next = fn(c);
    if (next !== c) changed = true;
    return next;
  });
  return changed ? { ...layout, components } : layout;
}

/** Ids among `ids` whose component is locked. */
export function lockedAmong(layout: OpticalLayout, ids: readonly string[]): string[] {
  const set = new Set(ids);
  return layout.components.filter((c) => set.has(c.id) && c.locked).map((c) => c.id);
}

/** Removes components. A fibre input that continued at a removed fibre output loses that link. Locked ones stay. */
export function removeComponents(layout: OpticalLayout, ids: readonly string[]): OpticalLayout {
  const gone = new Set(layout.components.filter((c) => ids.includes(c.id) && !c.locked).map((c) => c.id));
  if (gone.size === 0) return layout;
  const components = layout.components
    .filter((c) => !gone.has(c.id))
    .map((c) => {
      const to = c.props["continues_at"];
      if (typeof to === "string" && gone.has(to)) {
        const { continues_at: _drop, ...rest } = c.props;
        void _drop;
        return { ...c, props: rest };
      }
      return c;
    });
  return { ...layout, components };
}

/** Moves the unlocked components among `ids` by a vector (mm). */
export function moveBy(layout: OpticalLayout, ids: readonly string[], dx: number, dy: number): OpticalLayout {
  if (dx === 0 && dy === 0) return layout;
  return mapSelected(layout, new Set(ids), (c) =>
    c.locked ? c : normalizeGeometry({ ...c, x_mm: c.x_mm + dx, y_mm: c.y_mm + dy }),
  );
}

/** Puts one component at a position (the inspector's x / y boxes and snapping). Ignores the lock. */
export function setPosition(layout: OpticalLayout, id: string, x: number, y: number): OpticalLayout {
  return mapSelected(layout, new Set([id]), (c) => {
    const next = normalizeGeometry({ ...c, x_mm: x, y_mm: y });
    return next.x_mm === c.x_mm && next.y_mm === c.y_mm ? c : next;
  });
}

/** Puts several components at exact positions in one step (a drag of a group). Locked ones stay. */
export function setPositions(layout: OpticalLayout, pos: ReadonlyMap<string, Vec>): OpticalLayout {
  return mapSelected(layout, new Set(pos.keys()), (c) => {
    const p = pos.get(c.id);
    if (!p || c.locked) return c;
    const next = normalizeGeometry({ ...c, x_mm: p.x, y_mm: p.y });
    return next.x_mm === c.x_mm && next.y_mm === c.y_mm ? c : next;
  });
}

/** Rotates the unlocked components among `ids`, each about its own centre. */
export function rotateBy(layout: OpticalLayout, ids: readonly string[], dDeg: number): OpticalLayout {
  if (dDeg === 0) return layout;
  return mapSelected(layout, new Set(ids), (c) =>
    c.locked ? c : normalizeGeometry({ ...c, angle_deg: c.angle_deg + dDeg }),
  );
}

/** Rotates a group as a whole about a pivot: positions swing round it and every angle turns with it. */
export function rotateGroup(layout: OpticalLayout, ids: readonly string[], dDeg: number, pivot: Vec): OpticalLayout {
  if (dDeg === 0) return layout;
  return mapSelected(layout, new Set(ids), (c) => {
    if (c.locked) return c;
    const p = rotateAbout({ x: c.x_mm, y: c.y_mm }, pivot, dDeg);
    return normalizeGeometry({ ...c, x_mm: p.x, y_mm: p.y, angle_deg: c.angle_deg + dDeg });
  });
}

/** Sets the angle of one component (typed into the inspector, or by the aim tool). Ignores the lock. */
export function setAngle(layout: OpticalLayout, id: string, deg: number): OpticalLayout {
  return mapSelected(layout, new Set([id]), (c) => {
    const next = normalizeGeometry({ ...c, angle_deg: deg });
    return next.angle_deg === c.angle_deg ? c : next;
  });
}

export function setAperture(layout: OpticalLayout, id: string, mm: number): OpticalLayout {
  return mapSelected(layout, new Set([id]), (c) => {
    const next = normalizeGeometry({ ...c, aperture_mm: mm });
    return next.aperture_mm === c.aperture_mm ? c : next;
  });
}

export function setPose(layout: OpticalLayout, id: string, pose: { x: number; y: number; angle: number }): OpticalLayout {
  return mapSelected(layout, new Set([id]), (c) => {
    const next = normalizeGeometry({ ...c, x_mm: pose.x, y_mm: pose.y, angle_deg: pose.angle });
    return next.x_mm === c.x_mm && next.y_mm === c.y_mm && next.angle_deg === c.angle_deg ? c : next;
  });
}

export function setLabel(layout: OpticalLayout, id: string, label: string): OpticalLayout {
  const clean = cleanLine(label);
  return mapSelected(layout, new Set([id]), (c) => (c.label === clean ? c : { ...c, label: clean }));
}

export function setLocked(layout: OpticalLayout, ids: readonly string[], locked: boolean): OpticalLayout {
  return mapSelected(layout, new Set(ids), (c) => (c.locked === locked ? c : { ...c, locked }));
}

/** Why a new id is not acceptable, or null. */
export function idProblem(layout: OpticalLayout, newId: string, currentId: string | null): "format" | "taken" | null {
  if (!LIMITS.idPattern.test(newId)) return "format";
  if (newId !== currentId && takenIds(layout).has(newId)) return "taken";
  return null;
}

/** Renames a component id. A fibre input that continues at it follows. An unusable id changes nothing. */
export function renameComponent(layout: OpticalLayout, oldId: string, newId: string): OpticalLayout {
  if (oldId === newId || idProblem(layout, newId, oldId) !== null) return layout;
  if (!getComponent(layout, oldId)) return layout;
  return {
    ...layout,
    components: layout.components.map((c) => {
      if (c.id === oldId) return { ...c, id: newId };
      if (c.props["continues_at"] === oldId) return { ...c, props: { ...c.props, continues_at: newId } };
      return c;
    }),
  };
}

/** A parameter value as the server keeps it: numbers at the field's decimals, a list without repeats, an id trimmed. */
export function normalizePropValue(type: ComponentType, key: string, value: unknown): unknown {
  const f = propField(type, key);
  if (!f) return value;
  const decimals = f.decimals ?? DEFAULT_PROP_DECIMALS;
  if (f.kind === "number" && typeof value === "number") return pyRound(value, decimals) + 0;
  if (f.kind === "numberList" && Array.isArray(value)) {
    const out: number[] = [];
    for (const v of value) {
      if (typeof v !== "number") continue;
      const r = pyRound(v, decimals) + 0;
      if (!out.includes(r)) out.push(r);
    }
    return out;
  }
  if (f.kind === "componentRef" && typeof value === "string") return value.trim();
  return value;
}

/**
 * Sets or (undefined / null / "" / an empty list) clears one type parameter. A laser or a
 * fibre output has its wavelength either as one number or as a list, never both, so setting
 * one clears the other.
 */
export function setProp(layout: OpticalLayout, id: string, key: string, value: unknown): OpticalLayout {
  return mapSelected(layout, new Set([id]), (c) => {
    const normalized = normalizePropValue(c.type, key, value);
    const clear = normalized === undefined || normalized === null || normalized === "" || (Array.isArray(normalized) && normalized.length === 0);
    const props: Props = { ...c.props };
    if (clear) {
      if (!(key in props)) return c;
      delete props[key];
      return { ...c, props };
    }
    if (Object.is(c.props[key], normalized) || (Array.isArray(normalized) && JSON.stringify(c.props[key]) === JSON.stringify(normalized))) return c;
    props[key] = normalized;
    if (c.type === "laser" || c.type === "fiber_out") {
      if (key === "wavelength_nm") delete props["wavelengths_nm"];
      if (key === "wavelengths_nm") delete props["wavelength_nm"];
    }
    return { ...c, props };
  });
}

/** One value goes in `wavelength_nm`, several in `wavelengths_nm` (the server wants exactly one of the two). */
export function setSourceWavelengths(layout: OpticalLayout, id: string, list: readonly number[]): OpticalLayout {
  const waves = list.slice(0, LIMITS.maxWavelengths);
  if (waves.length === 0) return layout;
  return waves.length === 1 ? setProp(layout, id, "wavelength_nm", waves[0]) : setProp(layout, id, "wavelengths_nm", [...waves]);
}

export interface FibreLink {
  layout: OpticalLayout;
  /** the wavelengths written into the fibre output, or null when it was left as it was */
  wavelengths: number[] | null;
}

/**
 * Connects a fibre input to a fibre output (`continues_at`; an undefined output removes the
 * link). When light already reaches the input, the output's wavelength is made to match it,
 * but only while the output still has the wavelength a new one is created with: once it is
 * fed the tracer ignores that number (the output emits what arrives), so it is only what
 * people read in the inspector and what the server insists on, and a value somebody typed
 * is theirs and stays.
 */
export function linkFibre(layout: OpticalLayout, fiberInId: string, fiberOutId: string | undefined, arriving: readonly number[]): FibreLink {
  const linked = setProp(layout, fiberInId, "continues_at", fiberOutId);
  if (!fiberOutId || arriving.length === 0) return { layout: linked, wavelengths: null };
  const out = linked.components.find((c) => c.id === fiberOutId);
  if (!out || out.type !== "fiber_out") return { layout: linked, wavelengths: null };
  const fresh = CATALOG.fiber_out.defaultProps();
  const untouched = out.props["wavelengths_nm"] === undefined && out.props["wavelength_nm"] === fresh["wavelength_nm"];
  if (!untouched) return { layout: linked, wavelengths: null };
  const waves = arriving.slice(0, LIMITS.maxWavelengths);
  const next = setSourceWavelengths(linked, fiberOutId, waves);
  return next === linked ? { layout: linked, wavelengths: null } : { layout: next, wavelengths: [...waves] };
}

const LINE_NOTES = new Set<keyof Annotations>(["notes"]);

/** An annotation patch cleaned the way the server keeps text (control characters out, one-line fields on one line). */
function cleanAnnotationPatch(patch: Partial<Annotations>): Partial<Annotations> {
  const out: Partial<Annotations> = {};
  for (const [k, v] of Object.entries(patch) as [keyof Annotations, Annotations[keyof Annotations]][]) {
    if (k === "extra" && Array.isArray(v)) out.extra = (v as Annotations["extra"]).map((e) => ({ key: cleanLine(e.key), value: cleanLine(e.value) }));
    else if (typeof v === "string") (out as Record<string, unknown>)[k] = LINE_NOTES.has(k) ? cleanText(v) : cleanLine(v);
    else (out as Record<string, unknown>)[k] = v;
  }
  return out;
}

export function setAnnotation(layout: OpticalLayout, id: string, rawPatch: Partial<Annotations>): OpticalLayout {
  const patch = cleanAnnotationPatch(rawPatch);
  return mapSelected(layout, new Set([id]), (c) => {
    const differs = (Object.keys(patch) as (keyof Annotations)[]).some((k) =>
      k === "extra" ? JSON.stringify(c.annotations.extra) !== JSON.stringify(patch.extra) : !Object.is(c.annotations[k], patch[k]),
    );
    return differs ? { ...c, annotations: { ...c.annotations, ...patch } } : c;
  });
}

export function setLinks(layout: OpticalLayout, id: string, patch: Partial<Links>): OpticalLayout {
  return mapSelected(layout, new Set([id]), (c) => {
    const differs = (Object.keys(patch) as (keyof Links)[]).some((k) => c.links[k] !== patch[k]);
    return differs ? { ...c, links: { ...c.links, ...patch } } : c;
  });
}

/**
 * Changes the type of a component. Parameters the new type does not have are dropped
 * (the server rejects unknown keys) and the ones it needs get their defaults. Position,
 * annotations and links stay; the aperture follows the new type's default only if it
 * still had the old type's default.
 */
export function changeType(layout: OpticalLayout, id: string, type: ComponentType): OpticalLayout {
  return mapSelected(layout, new Set([id]), (c) => {
    if (c.type === type) return c;
    const oldInfo = CATALOG[c.type];
    const info = CATALOG[type];
    const kept = cleanProps(type, c.props);
    const defaults = info.defaultProps();
    const fields = propFields(type);
    const props: Props = { ...kept };
    for (const f of fields) if (f.required && props[f.key] === undefined && defaults[f.key] !== undefined) props[f.key] = defaults[f.key];
    // a laser or fibre output needs a wavelength, as one number or as a list
    if ((type === "laser" || type === "fiber_out") && props["wavelength_nm"] === undefined && props["wavelengths_nm"] === undefined) {
      props["wavelength_nm"] = defaults["wavelength_nm"];
    }
    return normalizeGeometry({
      ...c,
      type,
      props,
      aperture_mm: c.aperture_mm === oldInfo.defaultAperture ? info.defaultAperture : c.aperture_mm,
    });
  });
}

/** Copies components with new ids, shifted by `offset`; returns the new ids. */
export function duplicateComponents(
  layout: OpticalLayout,
  ids: readonly string[],
  offset: Vec = { x: 25, y: -25 },
): { layout: OpticalLayout; ids: string[] } {
  const taken = takenIds(layout);
  const wanted = new Set(ids);
  const copies: LayoutComponent[] = [];
  const newIds: string[] = [];
  let room = LIMITS.components - layout.components.length;
  let sourceRoom = LIMITS.sources - layout.components.filter((c) => isSourceType(c.type)).length;
  for (const c of layout.components) {
    if (!wanted.has(c.id) || room <= 0) continue;
    if (isSourceType(c.type)) {
      if (sourceRoom <= 0) continue;
      sourceRoom -= 1;
    }
    const id = nextId(CATALOG[c.type].prefix, taken);
    taken.add(id);
    room -= 1;
    newIds.push(id);
    copies.push(
      normalizeGeometry({
        ...c,
        id,
        label: c.label === labelFromId(c.id) ? labelFromId(id) : c.label,
        x_mm: c.x_mm + offset.x,
        y_mm: c.y_mm + offset.y,
        props: { ...c.props },
        annotations: { ...c.annotations, extra: c.annotations.extra.map((e) => ({ ...e })) },
        links: { ...c.links },
        locked: false,
      }),
    );
  }
  if (copies.length === 0) return { layout, ids: [] };
  return { layout: { ...layout, components: [...layout.components, ...copies] }, ids: newIds };
}

// ── texts, table, metadata ────────────────────────────────────────────────────

export function addText(layout: OpticalLayout, pos: Vec, text: string): { layout: OpticalLayout; id: string | null } {
  if (layout.texts.length >= LIMITS.texts) return { layout, id: null };
  const id = nextId("t", takenIds(layout));
  const t: LayoutText = { id, x_mm: round2(pos.x), y_mm: round2(pos.y), text: cleanLine(text), size: 1 };
  return { layout: { ...layout, texts: [...layout.texts, t] }, id };
}

export function updateText(layout: OpticalLayout, id: string, patch: Partial<Omit<LayoutText, "id">>): OpticalLayout {
  let changed = false;
  const texts = layout.texts.map((t) => {
    if (t.id !== id) return t;
    const next = { ...t, ...patch };
    if (patch.text !== undefined) next.text = cleanLine(patch.text);
    if (patch.size !== undefined) next.size = round2(patch.size);
    if (patch.x_mm !== undefined) next.x_mm = round2(patch.x_mm);
    if (patch.y_mm !== undefined) next.y_mm = round2(patch.y_mm);
    if (next.text === t.text && next.x_mm === t.x_mm && next.y_mm === t.y_mm && next.size === t.size) return t;
    changed = true;
    return next;
  });
  return changed ? { ...layout, texts } : layout;
}

export function removeText(layout: OpticalLayout, id: string): OpticalLayout {
  return layout.texts.some((t) => t.id === id) ? { ...layout, texts: layout.texts.filter((t) => t.id !== id) } : layout;
}

export function setTable(layout: OpticalLayout, patch: Partial<TableSpec>): OpticalLayout {
  const differs = (Object.keys(patch) as (keyof TableSpec)[]).some((k) => layout.table[k] !== patch[k]);
  return differs ? { ...layout, table: { ...layout.table, ...patch } } : layout;
}

export function setTitle(layout: OpticalLayout, title: string): OpticalLayout {
  const clean = cleanLine(title);
  return layout.title === clean ? layout : { ...layout, title: clean };
}

export function setNotes(layout: OpticalLayout, notes: string): OpticalLayout {
  const clean = cleanText(notes);
  return layout.notes === clean ? layout : { ...layout, notes: clean };
}
