// Reading what the server sends. The shapes are the contract of section 3 and 6.1 of
// docs/v2/design/optical_layout.md, but a reply is data from outside: every reader here
// takes `unknown`, fills what an older or newer server may leave out with the defaults
// of the model, and refuses (LayoutParseError) only what would make the editor draw or
// save garbage. A layout is never silently "repaired" in a way that changes what is
// stored: an unknown component type is an error, not a "custom".
//
// Pure, so the replies of a fake server can be fed through it in tests
// (test/opticalLayoutApi.test.ts).

import { tr } from "../i18n.ts";
import type {
  Annotations,
  CommentAuthorKind,
  LayoutComment,
  LayoutComponent,
  LayoutDocument,
  LayoutSummary,
  LayoutText,
  LinkState,
  LinksState,
  OpticalLayout,
  RevisionInfo,
  TableSpec,
  Trace,
  TraceBranch,
  TraceEvent,
  SourceTrace,
  BranchEnd,
} from "./types.ts";
import { SCHEMA_VERSION, isComponentType } from "./types.ts";
import { DEFAULT_TABLE, emptyAnnotations } from "./edit.ts";

export class LayoutParseError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "LayoutParseError";
  }
}

const isObj = (v: unknown): v is Record<string, unknown> => v !== null && typeof v === "object" && !Array.isArray(v);
const str = (v: unknown, dflt = ""): string => (typeof v === "string" ? v : v === null || v === undefined ? dflt : String(v));
const optStr = (v: unknown): string | null => (typeof v === "string" && v !== "" ? v : null);
const num = (v: unknown, dflt: number): number => {
  const n = typeof v === "number" ? v : typeof v === "string" && v.trim() !== "" ? Number(v) : Number.NaN;
  return Number.isFinite(n) ? n : dflt;
};
const nullableNum = (v: unknown): number | null => {
  if (v === null || v === undefined || v === "") return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
};

function parseAnnotations(raw: unknown): Annotations {
  const a = isObj(raw) ? raw : {};
  const extra = Array.isArray(a["extra"])
    ? a["extra"].filter(isObj).map((e) => ({ key: str(e["key"]), value: str(e["value"]) }))
    : [];
  return {
    ...emptyAnnotations(),
    model: str(a["model"]),
    manufacturer: str(a["manufacturer"]),
    function: str(a["function"]),
    motorized: a["motorized"] === true,
    actuator: str(a["actuator"]),
    connection: str(a["connection"]),
    datasheet_url: str(a["datasheet_url"]),
    notes: str(a["notes"]),
    extra,
  };
}

function parseComponent(raw: unknown, index: number): LayoutComponent {
  if (!isObj(raw)) throw new LayoutParseError(tr(`第 ${index + 1} 个器件不是对象。`, `Component ${index + 1} is not an object.`));
  const id = str(raw["id"]);
  const type = raw["type"];
  if (!isComponentType(type)) {
    throw new LayoutParseError(
      tr(
        `器件 ${id || index + 1} 的类型“${str(type)}”这个版本的编辑器不认识。`,
        `Component ${id || index + 1} has the type "${str(type)}", which this version of the editor does not know.`,
      ),
    );
  }
  const links = isObj(raw["links"]) ? raw["links"] : {};
  return {
    id,
    type,
    label: str(raw["label"], id),
    x_mm: num(raw["x_mm"], 0),
    y_mm: num(raw["y_mm"], 0),
    angle_deg: num(raw["angle_deg"], 0),
    aperture_mm: num(raw["aperture_mm"], 25.4),
    props: isObj(raw["props"]) ? { ...raw["props"] } : {},
    annotations: parseAnnotations(raw["annotations"]),
    links: { device_id: optStr(links["device_id"]), slot: optStr(links["slot"]) },
    locked: raw["locked"] === true,
  };
}

function parseTable(raw: unknown): TableSpec {
  const t = isObj(raw) ? raw : {};
  return {
    width_mm: num(t["width_mm"], DEFAULT_TABLE.width_mm),
    height_mm: num(t["height_mm"], DEFAULT_TABLE.height_mm),
    hole_pitch_mm: num(t["hole_pitch_mm"], DEFAULT_TABLE.hole_pitch_mm),
    hole_offset_mm: num(t["hole_offset_mm"], DEFAULT_TABLE.hole_offset_mm),
    grid: str(t["grid"], DEFAULT_TABLE.grid) || DEFAULT_TABLE.grid,
  };
}

function parseText(raw: unknown): LayoutText | null {
  if (!isObj(raw)) return null;
  return { id: str(raw["id"]), x_mm: num(raw["x_mm"], 0), y_mm: num(raw["y_mm"], 0), text: str(raw["text"]), size: num(raw["size"], 1) };
}

/** A layout as stored (section 3). Throws LayoutParseError for something that is not one. */
export function parseLayout(raw: unknown): OpticalLayout {
  if (!isObj(raw)) throw new LayoutParseError(tr("服务器返回的不是光路图。", "The server did not return a layout."));
  const id = str(raw["id"]);
  if (id === "") throw new LayoutParseError(tr("光路图缺少编号。", "The layout has no id."));
  const rawComponents = raw["components"];
  if (rawComponents !== undefined && !Array.isArray(rawComponents)) {
    throw new LayoutParseError(tr("光路图的器件列表格式不对。", "The component list of the layout is malformed."));
  }
  const components: unknown[] = Array.isArray(rawComponents) ? rawComponents : [];
  const texts = Array.isArray(raw["texts"]) ? raw["texts"].map(parseText).filter((t): t is LayoutText => t !== null) : [];
  return {
    schema: num(raw["schema"], SCHEMA_VERSION),
    id,
    title: str(raw["title"]),
    revision: Math.max(0, Math.floor(num(raw["revision"], 0))),
    updated_at: str(raw["updated_at"]),
    updated_by: str(raw["updated_by"]),
    table: parseTable(raw["table"]),
    components: components.map(parseComponent),
    texts,
    notes: str(raw["notes"]),
  };
}

export function parseSummary(raw: unknown): LayoutSummary | null {
  if (!isObj(raw)) return null;
  const id = str(raw["id"]);
  if (!id) return null;
  const s: LayoutSummary = {
    id,
    title: str(raw["title"]),
    revision: Math.max(0, Math.floor(num(raw["revision"], 0))),
    updated_at: str(raw["updated_at"]),
    updated_by: str(raw["updated_by"]),
    components: Math.max(0, Math.floor(num(raw["components"], 0))),
    open_comments: Math.max(0, Math.floor(num(raw["open_comments"], 0))),
  };
  const error = optStr(raw["error"]);
  if (error !== null) s.error = error;
  return s;
}

/** `GET /api/optical-layouts` -> `{layouts: [...]}` (a bare list is accepted too). */
export function parseSummaries(raw: unknown): LayoutSummary[] {
  const list = Array.isArray(raw) ? raw : isObj(raw) && Array.isArray(raw["layouts"]) ? raw["layouts"] : [];
  return list.map(parseSummary).filter((s): s is LayoutSummary => s !== null);
}

export function parseRevisionInfo(raw: unknown): RevisionInfo | null {
  if (!isObj(raw)) return null;
  const revision = nullableNum(raw["revision"]);
  if (revision === null) return null;
  return {
    revision,
    updated_at: str(raw["updated_at"]),
    updated_by: str(raw["updated_by"]),
    components: nullableNum(raw["components"]),
    title: str(raw["title"]),
  };
}

/** `GET .../history` -> `{revisions: [...]}`; `{history: [...]}` and a bare list are accepted. Newest first. */
export function parseRevisions(raw: unknown): RevisionInfo[] {
  const pick = (k: string) => (isObj(raw) && Array.isArray(raw[k]) ? (raw[k] as unknown[]) : null);
  const list = Array.isArray(raw) ? raw : (pick("revisions") ?? pick("history") ?? pick("entries") ?? []);
  return list.map(parseRevisionInfo).filter((r): r is RevisionInfo => r !== null).sort((a, b) => b.revision - a.revision);
}

/** Who wrote a comment: the server's `author` is "operator" or "agent"; other spellings are read as well. */
export function authorKind(raw: Record<string, unknown>): CommentAuthorKind {
  const k = str(raw["author"] ?? raw["kind"] ?? raw["author_kind"] ?? raw["by_kind"] ?? raw["role"]).toLowerCase();
  if (k === "agent" || k === "operator") return k;
  const source = str(raw["source"]).toLowerCase();
  if (["agent", "ext", "mcp", "api", "external"].includes(source)) return "agent";
  if (raw["agent"] === true || raw["is_agent"] === true) return "agent";
  return "operator";
}

export function parseComment(raw: unknown): LayoutComment | null {
  if (!isObj(raw)) return null;
  const id = str(raw["id"] ?? raw["cid"]);
  if (!id) return null;
  const resolved = raw["resolved"] === true || raw["status"] === "resolved";
  return {
    id,
    by: str(raw["by"]),
    kind: authorKind(raw),
    text: str(raw["text"]),
    component_id: optStr(raw["component_id"]),
    created_at: str(raw["created_at"] ?? raw["at"]),
    resolved,
    resolved_by: optStr(raw["resolved_by"]),
    resolved_at: optStr(raw["resolved_at"]),
  };
}

/** `GET .../comments` -> `{comments: [...], open_count}` or a bare list, oldest first. */
export function parseComments(raw: unknown): LayoutComment[] {
  const list = Array.isArray(raw) ? raw : isObj(raw) && Array.isArray(raw["comments"]) ? raw["comments"] : [];
  return list.map(parseComment).filter((c): c is LayoutComment => c !== null);
}

export function openCommentCount(comments: readonly LayoutComment[]): number {
  return comments.filter((c) => !c.resolved).length;
}

const knownOf = (v: unknown): boolean | null => (typeof v === "boolean" ? v : null);

function parseLinkState(raw: unknown): LinkState {
  const o = isObj(raw) ? raw : {};
  const dev = isObj(o["device"]) ? o["device"] : null;
  const slot = isObj(o["slot"]) ? o["slot"] : null;
  const device: LinkState["device"] = dev
    ? {
        id: str(dev["id"]),
        known: knownOf(dev["known"]),
        name: str(dev["name"] ?? dev["id"]),
        kind: str(dev["kind"]),
        enabled: typeof dev["enabled"] === "boolean" ? dev["enabled"] : null,
        connected: typeof dev["connected"] === "boolean" ? dev["connected"] : null,
      }
    : null;
  if (device && dev && typeof dev["error"] === "string") device.error = dev["error"];
  const slotState: LinkState["slot"] = slot
    ? {
        id: str(slot["id"]),
        known: knownOf(slot["known"]),
        label: str(slot["label"] ?? slot["id"]),
        value: slot["value"] === null || slot["value"] === undefined ? "" : typeof slot["value"] === "object" ? JSON.stringify(slot["value"]) : String(slot["value"]),
        valid: typeof slot["valid"] === "boolean" ? slot["valid"] : null,
      }
    : null;
  if (slotState && slot && typeof slot["error"] === "string") slotState.error = slot["error"];
  return { device, slot: slotState };
}

export function parseLinksState(raw: unknown): LinksState {
  const out: LinksState = {};
  if (isObj(raw)) for (const [id, v] of Object.entries(raw)) out[id] = parseLinkState(v);
  return out;
}

const BRANCH_ENDS: readonly string[] = ["stop", "left_table", "max_events", "blocked", "back_hit", "split", "budget"];

/** The server's trace, read loosely: the editor draws its own, this is for comparing. Null if absent. */
export function parseTrace(raw: unknown): Trace | null {
  if (!Array.isArray(raw)) return null;
  const out: SourceTrace[] = [];
  for (const s of raw) {
    if (!isObj(s) || !Array.isArray(s["branches"])) continue;
    const branches: TraceBranch[] = [];
    for (const b of s["branches"]) {
      if (!isObj(b)) continue;
      const events: TraceEvent[] = (Array.isArray(b["events"]) ? b["events"] : []).filter(isObj).map((e) => {
        const ev: TraceEvent = {
          component: str(e["component"]),
          action: str(e["action"]),
          x: num(e["x"], 0),
          y: num(e["y"], 0),
          offset_mm: num(e["offset_mm"], 0),
          incidence_deg: num(e["incidence_deg"], 0),
          path_mm: num(e["path_mm"], 0),
        };
        return ev;
      });
      const segments = (Array.isArray(b["segments"]) ? b["segments"] : []).filter(isObj).map((g) => ({
        x0: num(g["x0"], 0),
        y0: num(g["y0"], 0),
        x1: num(g["x1"], 0),
        y1: num(g["y1"], 0),
      }));
      const end = str(b["end"]);
      branches.push({
        branch: str(b["branch"]),
        fraction: num(b["fraction"], 1),
        segments,
        events,
        end: (BRANCH_ENDS.includes(end) ? end : "stop") as BranchEnd,
      });
    }
    out.push({ source: str(s["source"]), wavelength_nm: nullableNum(s["wavelength_nm"]), branches });
  }
  return out;
}

/**
 * `GET /api/optical-layouts/{id}` and the answer of a PUT: `{layout, trace, links_state}`
 * (section 11.3). A flat reply with the layout's own fields at the top is read as well.
 */
export function parseDocument(raw: unknown): LayoutDocument {
  const o = isObj(raw) ? raw : {};
  const inner = isObj(o["layout"]) ? o["layout"] : raw;
  return { layout: parseLayout(inner), trace: parseTrace(o["trace"]), linksState: parseLinksState(o["links_state"]) };
}

/** `GET .../revisions/{rev}`: `{revision, updated_at, updated_by, layout, trace}`. The layout, with the revision it was saved as. */
export function parseRevisionLayout(raw: unknown): OpticalLayout {
  const o = isObj(raw) ? raw : {};
  const layout = parseLayout(isObj(o["layout"]) ? o["layout"] : raw);
  const rev = nullableNum(o["revision"]);
  return rev !== null ? { ...layout, revision: Math.max(0, Math.floor(rev)) } : layout;
}

/** The body of a layout for PUT: the fields of section 3, nothing else (no trace, no links_state). */
export function layoutForWrite(layout: OpticalLayout): OpticalLayout {
  return {
    schema: SCHEMA_VERSION,
    id: layout.id,
    title: layout.title,
    revision: layout.revision,
    updated_at: layout.updated_at,
    updated_by: layout.updated_by,
    table: { ...layout.table },
    components: layout.components,
    texts: layout.texts,
    notes: layout.notes,
  };
}
