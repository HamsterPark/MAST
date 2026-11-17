// Optical layout data model.
//
// Hand-written from docs/v2/design/optical_layout.md section 3 (the layout JSON) and
// section 4 (the trace output), with the additions of docs/v2/design/optical_layout_v2.md
// (the `pbs` type and the beam props of section 3, the `beam` keys of the trace in section 4.7)
// and of docs/v2/design/optical_layout_3d.md (heights: `table.beam_height_mm`, `props.z_mm`,
// a periscope's `props.z_out_mm`; `z_mm` and `passes` of a segment, `z_mm` and `dz_mm` of an event).
// The backend (MASTv2/mast/optical_layout/) validates the same shapes with pydantic; these
// types could be replaced by generated ones if the generated schema carried them.
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
  /** heights (docs/v2/design/optical_layout_3d.md section 2): props.z_mm and props.z_out_mm within ±zAbs mm */
  zAbs: 10_000,
  /** table.beam_height_mm: above 0, at most this */
  beamHeightMax: 10_000,
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
  "pbs",
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

/**
 * The values of `props.polarization` of a laser or a fibre output (v2 section 3.2). Not given is
 * "unpolarized": the trace treats light of unknown polarisation as unpolarised and says so.
 */
export const POLARIZATIONS = ["unpolarized", "linear"] as const;
export type Polarization = (typeof POLARIZATIONS)[number];

/**
 * Component types whose `props.throughput` (0 to 1, default 1) scales the power that goes on after
 * the component acts (v2 section 3.2: the reflectance of a mirror, the transmittance of a lens, the
 * transmission of a polarizer along its axis, a filter's in-band transmission, a grating's zero-order
 * efficiency).
 */
export const THROUGHPUT_TYPES = [
  "mirror",
  "grating",
  "lens",
  "objective",
  "waveplate",
  "polarizer",
  "filter",
  "window",
  "aotf",
  "prism",
  "periscope",
  "sample",
  "custom",
] as const;

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
  /**
   * The height of the beam above the table top, mm (docs/v2/design/optical_layout_3d.md section 2): every
   * component and source without `props.z_mm` sits at it. Absent = 75 mm (heights.ts DEFAULT_BEAM_HEIGHT_MM);
   * an absent key is left out of the PUT body, as the server leaves it out of what it stores.
   */
  beam_height_mm?: number;
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

// The beam state (v2 sections 4.1 to 4.7). Polarisation angles are measured in the ray's own frame:
// e_h = z × d (in the table plane, to the LEFT of the direction of travel) towards e_v = z (straight
// up), so 0° is in the table plane (p for any in-plane reflection) and 90° is vertical (s).

/**
 * The normalised Stokes vector (S1/S0, S2/S0, S3/S0): S1 = I_h - I_v, S2 = I_+45 - I_-45; the sign of
 * S3 is a convention only (no handedness is named, the text says χ = ±45°). null when S0 = 0 (no light).
 */
export type StokesNorm = [number, number, number];

/**
 * The Gaussian beam parameter q = z + i·zR as [z, zR] in mm: z is the signed distance from the waist
 * (> 0 past it and diverging, < 0 before it and converging), zR > 0 the Rayleigh range.
 */
export type QParam = [number, number];

/** `beam` of a tree: what is the same for the whole tree. */
export interface TreeBeam {
  /** power of the source in mW (per wavelength); null when the source has no `power_mw` (lamps, LEDs: always null) */
  source_mw: number | null;
  /** beam quality factor M² of the tree; null when the tree has no Gaussian beam */
  m2: number | null;
}

/** `beam` of a segment: the state at the START of the segment (after the previous action). */
export interface SegmentBeam {
  /** power relative to the source of the tree (S0; 1 at the source) */
  p: number;
  s: StokesNorm | null;
  /** at the start of the segment; null when the tree has no Gaussian beam */
  q: QParam | null;
}

/** `beam` of an event: the state ARRIVING at the component, before it acts. */
export interface EventBeam {
  /** power relative to the source of the tree */
  p: number;
  /** source_mw × p; null when the tree's source_mw is null */
  mw: number | null;
  /** 1/e² diameter where the ray arrives, mm; null without a Gaussian beam */
  d_mm: number | null;
  s: StokesNorm | null;
}

/**
 * A component a segment passes over or under (docs/v2/design/optical_layout_3d.md section 3.5): in the table
 * plane the ray meets its working surface inside the aperture, but at a height more than half the aperture from
 * its axis, so it goes on untouched. `dz_mm` = the height of the beam minus that of the component's axis (a
 * periscope: of its entrance for this direction); above 0 the beam passes over it.
 */
export interface TracePass {
  component: string;
  x: number;
  y: number;
  dz_mm: number;
}

export interface Segment {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
  /**
   * 3D: the height of the beam along the segment, mm above the table top. Optional: a trace of an older server
   * has none; read it as `segment.z_mm ?? beamHeightOf(table)` (heights.ts).
   */
  z_mm?: number;
  /** v2: the beam at the start of the segment. Optional: a trace of an older server has none. */
  beam?: SegmentBeam;
  /** 3D: the components the segment passes over or under, in the order along it. Optional as z_mm. */
  passes?: TracePass[];
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
  /** 3D: the height of the beam arriving here (that of the segment that leads here). Optional: older servers. */
  z_mm?: number;
  /** Signed distance from the component centre along its active surface (local +y positive). */
  offset_mm: number;
  /**
   * 3D: the height of the beam minus that of the component's axis (a periscope: of its entrance for the direction
   * the light goes), mm; above 0 the beam is above the axis. Optional: older servers.
   */
  dz_mm?: number;
  /** Angle between the ray and the surface normal, 0 to 90. */
  incidence_deg: number;
  /** Cumulative geometric path from the source to this hit (3D: with the vertical runs inside periscopes). */
  path_mm: number;
  /** v2: the beam arriving here, before the component acts. Optional: a trace of an older server has none. */
  beam?: EventBeam;
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
  /** v2: power and beam quality of the tree. Optional: a trace of an older server has none. */
  beam?: TreeBeam;
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
