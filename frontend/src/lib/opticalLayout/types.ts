// Optical layout data model.
//
// Hand-written from docs/v2/design/optical_layout.md section 3 (the layout JSON) and
// section 4 (the trace output). The backend (MASTv2/mast/optical_layout/) validates the
// same shapes with pydantic; these types could be replaced by generated ones if the
// generated schema carried them.
//
// Units everywhere: length mm, angle degrees (counter-clockwise from +x, [0, 360)),
// wavelength nm, power mW. Platform coordinates have the origin at the lower-left
// corner with y UP; only the SVG painter flips y.
//
// Identity strings (component `type`, trace `end`, `action`, ids) are compared and sent
// over the API as they are. They are never passed through tr(); labels for them come
// from lookup functions that switch on the raw value (catalog.ts).

export const SCHEMA_VERSION = 1;

/**
 * Limits of the server's model (docs/v2/design/optical_layout.md sections 3 and 11.1,
 * MASTv2/mast/optical_layout/model.py), checked early here so the editor can say what is
 * wrong before a save is refused. The server refuses what is too long; it never cuts.
 */
export const LIMITS = {
  components: 400,
  texts: 100,
  sources: 32,
  /** the whole layout, compact JSON, UTF-8 */
  jsonBytes: 1_000_000,
  idPattern: /^[A-Za-z0-9_-]{1,40}$/,
  idMax: 40,
  /** device ids and slot ids: references only */
  refPattern: /^[A-Za-z0-9_.:-]{1,64}$/,
  /** a layout id is a file name; Windows will not take these (any case) */
  reservedIds: ["con", "prn", "aux", "nul", "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9", "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9"] as readonly string[],
  coordAbs: 100_000,
  angleAbs: 1e6,
  apertureMin: 0.1,
  apertureMax: 500,
  labelMax: 80,
  titleMax: 200,
  /** one-line fields: model, manufacturer, function, actuator, connection, datasheet_url, extra values, text labels */
  stringMax: 200,
  notesMax: 2000,
  extraPairs: 20,
  extraKeyMax: 40,
  extraValueMax: 200,
  textSizeMin: 0.1,
  textSizeMax: 20,
  tableSizeMin: 10,
  tableSizeMax: 20_000,
  pitchMin: 1,
  pitchMax: 500,
  holeOffsetMax: 500,
  maxWavelengths: 16,
} as const;

/** The values of `table.grid`. */
export const GRID_KINDS = ["metric", "imperial", "none"] as const;

/** Light sources: they emit rays and never block one. */
export const SOURCE_TYPES = ["laser", "fiber_out", "lamp", "led"] as const;

/** Every component type of section 4, in the order the palette shows them. */
export const COMPONENT_TYPES = [
  "laser",
  "fiber_out",
  "lamp",
  "led",
  "mirror",
  "beam_splitter",
  "dichroic",
  "grating",
  "lens",
  "objective",
  "waveplate",
  "polarizer",
  "filter",
  "window",
  "nd",
  "aotf",
  "prism",
  "periscope",
  "iris",
  "shutter",
  "sample",
  "detector",
  "camera",
  "spectrometer",
  "power_meter",
  "beam_dump",
  "blocker",
  "fiber_in",
  "custom",
] as const;

export type ComponentType = (typeof COMPONENT_TYPES)[number];
export type SourceType = (typeof SOURCE_TYPES)[number];

export function isComponentType(v: unknown): v is ComponentType {
  return typeof v === "string" && (COMPONENT_TYPES as readonly string[]).includes(v);
}

export function isSourceType(type: string): type is SourceType {
  return (SOURCE_TYPES as readonly string[]).includes(type);
}

export interface Vec {
  x: number;
  y: number;
}

export interface ExtraPair {
  key: string;
  value: string;
}

export interface Annotations {
  model: string;
  manufacturer: string;
  /** What the component is for in this setup ("steering", "pump filter", ...). */
  function: string;
  motorized: boolean;
  /** Motor / controller model. Free text for people and the LLM. */
  actuator: string;
  /** Where it is wired: controller, port, channel, satellite, COM port, ... */
  connection: string;
  datasheet_url: string;
  notes: string;
  extra: ExtraPair[];
}

export interface Links {
  /** Id in the MAST device registry (witec, superk, pico1, ...); only a reference. */
  device_id: string | null;
  /** Id of a manual optical-state slot (excitation_waveplate, ...); only a reference. */
  slot: string | null;
}

export type Props = Record<string, unknown>;

export interface LayoutComponent {
  id: string;
  type: ComponentType;
  label: string;
  x_mm: number;
  y_mm: number;
  /** Local +x direction, degrees counter-clockwise from +x. See section 2 for what it means per type. */
  angle_deg: number;
  /** Length of the active surface along the local y axis. */
  aperture_mm: number;
  props: Props;
  annotations: Annotations;
  links: Links;
  /** Guards against accidental dragging in the editor. */
  locked: boolean;
}

export interface TableSpec {
  width_mm: number;
  height_mm: number;
  hole_pitch_mm: number;
  hole_offset_mm: number;
  grid: string;
}

export interface LayoutText {
  id: string;
  x_mm: number;
  y_mm: number;
  text: string;
  size: number;
}

export interface OpticalLayout {
  schema: number;
  id: string;
  title: string;
  revision: number;
  updated_at: string;
  updated_by: string;
  table: TableSpec;
  components: LayoutComponent[];
  texts: LayoutText[];
  notes: string;
}

// ── trace (section 4) ─────────────────────────────────────────────────────────

export interface Segment {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

/**
 * Why a branch ended: "stop" on the front of a terminal component, "blocked" (filter, iris,
 * shutter), "back_hit", "split" (it ended at a beam splitter, its children follow),
 * "left_table", "max_events" (64 events along one lineage), "budget" (the whole trace ran out
 * of events, section 11.2 point 13: the trace stops there).
 */
export type BranchEnd = "stop" | "left_table" | "max_events" | "blocked" | "back_hit" | "split" | "budget";

export interface TraceEvent {
  component: string;
  /** "reflect", "transmit", "split", "stop", "blocked" or "back_hit". */
  action: string;
  x: number;
  y: number;
  /** Signed distance from the component centre along its active surface (local +y positive). */
  offset_mm: number;
  /** Angle between the ray and the surface normal, 0 to 90. */
  incidence_deg: number;
  /** Cumulative geometric path from the source to this hit. */
  path_mm: number;
}

export interface TraceBranch {
  branch: string;
  fraction: number;
  segments: Segment[];
  events: TraceEvent[];
  end: BranchEnd;
}

export interface SourceTrace {
  source: string;
  wavelength_nm: number | null;
  branches: TraceBranch[];
}

export type Trace = SourceTrace[];

/**
 * What a cut trace says about itself (trace.py truncation(), read from the trace alone: a branch
 * ended with "budget"): `events` events in `branches` branches of `trees` trees were traced, the
 * last tree being the one of `source` at `wavelength_nm`, cut in `branch`.
 */
export interface Truncation {
  reason: "events";
  events: number;
  branches: number;
  trees: number;
  source: string;
  wavelength_nm: number | null;
  branch: string;
}

// ── comments, history, link state (REST, section 6.1) ─────────────────────────

export type CommentAuthorKind = "operator" | "agent";

export interface LayoutComment {
  id: string;
  by: string;
  kind: CommentAuthorKind;
  text: string;
  component_id: string | null;
  created_at: string;
  resolved: boolean;
  resolved_by: string | null;
  resolved_at: string | null;
}

export interface RevisionInfo {
  revision: number;
  updated_at: string;
  updated_by: string;
  components: number | null;
  title: string;
}

export interface LayoutSummary {
  id: string;
  title: string;
  revision: number;
  updated_at: string;
  updated_by: string;
  components: number;
  open_comments: number;
  /** the file on the server could not be read: it is listed, not opened */
  error?: string;
}

/** What the server knows about one component's links, read-only (never opens a device for it). */
export interface LinkState {
  device: {
    id: string;
    /** false: the registry has no device with this id; null: the server could not tell */
    known: boolean | null;
    name: string;
    kind: string;
    enabled: boolean | null;
    connected: boolean | null;
    error?: string;
  } | null;
  slot: {
    id: string;
    known: boolean | null;
    label: string;
    value: string;
    valid: boolean | null;
    error?: string;
  } | null;
}

export type LinksState = Record<string, LinkState>;

/** GET /api/optical-layouts/{id}: the layout plus what the server computed for it. */
export interface LayoutDocument {
  layout: OpticalLayout;
  trace: Trace | null;
  linksState: LinksState;
}
