/** What an agent actually receives on each call, and how much each part takes: pure logic.
 *
 *  ## Why a separate file
 *
 *  `.tsx` cannot be unit-tested in this repo (`npm run test:unit` is `node --test`, which
 *  cannot strip JSX), so anything that can be computed wrongly lives in a `.ts`. Every
 *  calculation here has a way to go wrong: shares can sum to 99%, code-point offsets can
 *  cut one position off, and a block of unknown size can be treated as 0 so the bar is
 *  drawn shorter than reality.
 *
 *  ## Non-obvious conventions
 *
 *  * **The supervisor has two names**: `_supervisor` in the frontend, `orchestrator` in
 *    the backend (`make_chat_model("orchestrator")`, `agent="orchestrator"` in the
 *    registry). Without the mapping the SUP column is always empty, and empty looks like
 *    "the orchestrator injects nothing".
 *  * **The `ai:response` entry at the end of a snapshot is the model's output**, not its
 *    input. It is not counted in `total_chars`, and the history count and shares exclude it.
 *  * **Offsets are in code points**: Python's `len()` on the backend counts code points,
 *    JS `.length` counts UTF-16 code units. A single emoji shifts every later segment.
 *  * **`chars` is the length before truncation**, while `content` may already be cut to
 *    20k. Splitting must clamp and say so, not pretend nothing was truncated.
 */

import type { components } from "@/api/schema";
import { tr } from "./i18n.ts";

// ── Contract types (aliases of generated types; do not copy fields here) ──

export type InjectionBlock = components["schemas"]["InjectionBlock"];
export type AgentInfo = components["schemas"]["AgentInfo"];
export type InjectionMatrix = components["schemas"]["InjectionMatrixResponse"];
export type AgentInjection = components["schemas"]["AgentInjectionResponse"];
export type LatestCapture = components["schemas"]["LatestCaptureResponse"];
export type CapturedMessage = components["schemas"]["CapturedMessageModel"];
export type ToolSurface = components["schemas"]["ToolSurfaceInfo"];

/** The registry value meaning "all agents". */
export const ALL_AGENTS = "*";

/** Frontend supervisor id (`SUP_ID` in `registry.tsx`). */
export const UI_SUPERVISOR = "_supervisor";
/** Backend id of the same thing. */
export const BACKEND_SUPERVISOR = "orchestrator";

/** Frontend agent id -> backend id. */
export function toBackendAgentId(uiId: string): string {
  return uiId === UI_SUPERVISOR ? BACKEND_SUPERVISOR : uiId;
}

/** Backend agent id -> frontend id. */
export function toUiAgentId(backendId: string): string {
  return backendId === BACKEND_SUPERVISOR ? UI_SUPERVISOR : backendId;
}

// ── Vocabulary ───────────────────────────────────────────────────────────

const WHEN_LABEL: Record<string, string> = {
  always: tr("每次调用", "Every call"),
  when_set: tr("设了才有", "Only when set"),
  on_event: tr("出事才有", "Only on an event"),
  on_mode: tr("看模式", "Depends on mode"),
  build_time: tr("建图时定", "Fixed at graph build"),
};

const POSITION_LABEL: Record<string, string> = {
  system: tr("系统消息", "System message"),
  last_human: tr("最后一条用户消息", "Last user message"),
  new_human: tr("新插一条消息", "Inserted message"),
  state_messages: tr("改写消息列表", "Rewritten message list"),
  tool_result: tr("改写工具返回", "Rewritten tool result"),
  tools: tr("工具面", "Tool surface"),
};

const AVAILABILITY_LABEL: Record<string, string> = {
  static: tr("固定文本", "Fixed text"),
  live: tr("按当前状态渲染", "Rendered from current state"),
  needs_hardware: tr("需要实时硬件", "Needs live hardware"),
  needs_request: tr("只存在于一次请求内", "Exists only within one request"),
};

/** Unknown values are returned verbatim, never as "unknown": when the backend adds an enum value the page should not turn into a wall of "unknown". */
function labelOf(table: Record<string, string>, key: string): string {
  return table[key] ?? key;
}

export const whenLabel = (w: string) => labelOf(WHEN_LABEL, w);
export const positionLabel = (p: string) => labelOf(POSITION_LABEL, p);
export const availabilityLabel = (a: string) => labelOf(AVAILABILITY_LABEL, a);

/** Can the text of this block be shown directly? Never for `needs_*`: that is the honesty rule expressed as a type. */
export function canShowText(block: Pick<InjectionBlock, "availability">): boolean {
  return block.availability === "static" || block.availability === "live";
}

/** Does this block reach `agent`? */
export function appliesTo(
  block: Pick<InjectionBlock, "agents">,
  backendId: string,
): boolean {
  const list = block.agents ?? [];
  if (list.length === 0) return false;
  return list.includes(ALL_AGENTS) || list.includes(backendId);
}

// ── Message splitting ────────────────────────────────────────────────────

export type SplitMessages = {
  system: CapturedMessage[];
  history: CapturedMessage[];
  /** The model's own output (the last entry of the snapshot). Not input. */
  response: CapturedMessage | null;
};

export function splitMessages(messages: CapturedMessage[] | undefined): SplitMessages {
  const all = messages ?? [];
  let response: CapturedMessage | null = null;
  const rest: CapturedMessage[] = [];
  for (const m of all) {
    if (m.role === "ai:response") response = m;
    else rest.push(m);
  }
  return {
    system: rest.filter((m) => m.role === "system"),
    history: rest.filter((m) => m.role !== "system"),
    response,
  };
}

// ── Splitting one message by block ───────────────────────────────────────

export type Segment = {
  /** null = this segment has no owner (text outside the injection helpers). */
  id: string | null;
  start: number;
  end: number;
  text: string;
  chars: number;
  /** This segment lies beyond `content` (the original was truncated). */
  clipped: boolean;
};

export type SegmentIssue =
  | { kind: "beyond_content"; detail: string }
  | { kind: "overlap"; detail: string }
  | { kind: "chars_mismatch"; detail: string };

type BlockRef = { id: string; start: number; end: number; chars: number };

/** Split a message's text by block into segments carrying a source label.
 *
 *  Gaps are filled with unattributed segments (`id: null`) rather than dropped: dropped
 *  gaps would make the segments not add up to the full text, and "does not add up" is
 *  exactly the kind of problem this page exists to expose.
 */
export function splitByBlocks(
  content: string,
  chars: number,
  blocks: BlockRef[] | null | undefined,
): { segments: Segment[]; issues: SegmentIssue[] } {
  // Cut by code point: backend offsets come from Python len(), which counts code points.
  const cp = Array.from(content ?? "");
  const n = cp.length;
  const issues: SegmentIssue[] = [];
  const cut = (a: number, b: number) => cp.slice(a, b).join("");

  const list = [...(blocks ?? [])].sort((x, y) => x.start - y.start);
  if (list.length === 0) {
    return {
      segments: n === 0 ? [] : [{ id: null, start: 0, end: n, text: content,
                                  chars: n, clipped: false }],
      issues,
    };
  }

  const segments: Segment[] = [];
  let cursor = 0;
  let prevEnd = -1;
  for (const b of list) {
    if (prevEnd >= 0 && b.start < prevEnd) {
      issues.push({ kind: "overlap",
                    detail: tr(`${b.id} 与前一块重叠（start ${b.start} < ${prevEnd}）`,
                               `${b.id} overlaps the previous block (start ${b.start} < ${prevEnd})`) });
    }
    prevEnd = b.end;
    const start = Math.max(0, Math.min(b.start, n));
    const end = Math.max(start, Math.min(b.end, n));
    if (start > cursor) {
      segments.push({ id: null, start: cursor, end: start,
                      text: cut(cursor, start), chars: start - cursor,
                      clipped: false });
    }
    const clipped = b.end > n;
    if (clipped) {
      issues.push({ kind: "beyond_content",
                    detail: tr(`${b.id} 的结束位置 ${b.end} 超出了可见文本长度 ${n}（原文被截断过）`,
                               `${b.id} ends at ${b.end}, beyond the visible text length ${n} (the original was truncated)`) });
    }
    segments.push({ id: b.id, start, end, text: cut(start, end),
                    chars: b.chars, clipped });
    cursor = Math.max(cursor, end);
  }
  if (cursor < n) {
    segments.push({ id: null, start: cursor, end: n, text: cut(cursor, n),
                    chars: n - cursor, clipped: false });
  }

  const claimed = list.reduce((s, b) => s + (b.chars || 0), 0);
  if (chars > 0 && claimed > chars) {
    issues.push({ kind: "chars_mismatch",
                  detail: tr(`各块声称的 ${claimed} 字符多于这条消息的 ${chars}`,
                               `The blocks claim ${claimed} characters, more than this message's ${chars}`) });
  }
  return { segments, issues };
}

// ── Share bar ────────────────────────────────────────────────────────────

export type ShareKey = "static" | "block" | "unattributed" | "history" | "tools";

export type Share = {
  key: ShareKey;
  id?: string;
  label: string;
  chars: number;
  /** Percentage: an integer, and the shares sum to exactly 100. */
  pct: number;
  /** This part is an estimate (the tool surface is measured as at graph build time). */
  estimated: boolean;
};

/** Largest-remainder method: rounding each value separately can sum to 99 or 101, and a
 *  share bar that does not fill up suggests that some block was not counted. */
function toPercents(values: number[]): number[] {
  const total = values.reduce((a, b) => a + b, 0);
  if (total <= 0) return values.map(() => 0);
  const exact = values.map((v) => (v * 100) / total);
  const floors = exact.map((v) => Math.floor(v));
  let remainder = 100 - floors.reduce((a, b) => a + b, 0);
  const order = exact
    .map((v, i) => ({ i, frac: v - Math.floor(v) }))
    .sort((a, b) => b.frac - a.frac);
  const out = [...floors];
  for (const { i } of order) {
    if (remainder <= 0) break;
    const cur = out[i];
    if (cur === undefined) continue;
    out[i] = cur + 1;
    remainder -= 1;
  }
  return out;
}

export function composeShares(args: {
  segments: Segment[];
  history: CapturedMessage[];
  toolsChars: number | null | undefined;
  labelFor: (id: string) => string;
}): Share[] {
  const parts: Omit<Share, "pct">[] = [];
  for (const seg of args.segments) {
    if (seg.chars <= 0) continue;
    if (seg.id === null) {
      parts.push({ key: "unattributed", label: tr("未归属", "Unattributed"), chars: seg.chars,
                   estimated: false });
    } else {
      const isStatic = seg.id.startsWith("agent.") || seg.id === "system.base";
      parts.push({ key: isStatic ? "static" : "block", id: seg.id,
                   label: args.labelFor(seg.id), chars: seg.chars,
                   estimated: false });
    }
  }
  // History uses the original `chars`, not the possibly truncated `content.length`.
  const histChars = args.history.reduce((s, m) => s + (m.chars || 0), 0);
  if (histChars > 0) {
    parts.push({ key: "history", label: tr(`对话历史（${args.history.length} 条）`, `Conversation history (${args.history.length})`),
                 chars: histChars, estimated: false });
  }
  // null differs from 0: null = not measured (do not draw this part), 0 = measured and truly empty.
  if (args.toolsChars !== null && args.toolsChars !== undefined) {
    parts.push({ key: "tools", label: tr("工具面（估算）", "Tool surface (estimate)"), chars: args.toolsChars,
                 estimated: true });
  }
  if (parts.length === 0) return [];
  const pcts = toPercents(parts.map((p) => p.chars));
  return parts.map((p, i) => ({ ...p, pct: pcts[i] ?? 0 }));
}

// ── Matrix ───────────────────────────────────────────────────────────────

export type MatrixRow = {
  block: InjectionBlock;
  cells: boolean[];
  shared: boolean;
  exclusive: boolean;
};

export function buildMatrix(
  matrix: InjectionMatrix | undefined,
  columns: string[],
): MatrixRow[] {
  const blocks = matrix?.blocks ?? [];
  const cells = matrix?.cells ?? {};
  return blocks.map((block) => {
    const row = cells[block.id] ?? {};
    const agents = block.agents ?? [];
    return {
      block,
      cells: columns.map((c) => Boolean(row[c])),
      shared: agents.includes(ALL_AGENTS),
      exclusive: Boolean(block.exclusive),
    };
  });
}

export type Footprint = {
  /** Characters in blocks whose size is known. */
  knownChars: number;
  /** Blocks with content only in a real request: counted, never treated as 0. */
  unknownCount: number;
  sharedCount: number;
  exclusiveCount: number;
};

export function agentFootprint(
  matrix: InjectionMatrix | undefined,
  backendId: string,
): Footprint {
  const out: Footprint = { knownChars: 0, unknownCount: 0, sharedCount: 0,
                           exclusiveCount: 0 };
  for (const block of matrix?.blocks ?? []) {
    if (!appliesTo(block, backendId)) continue;
    if (canShowText(block)) out.knownChars += block.effective_chars ?? 0;
    else out.unknownCount += 1;
    if ((block.agents ?? []).includes(ALL_AGENTS)) out.sharedCount += 1;
    else if (block.exclusive) out.exclusiveCount += 1;
  }
  return out;
}

// ── Miscellaneous ────────────────────────────────────────────────────────

export function fmtChars(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  if (n < 1000) return tr(`${n} 字符`, `${n} chars`);
  return tr(`${(n / 1000).toFixed(1)}k 字符`, `${(n / 1000).toFixed(1)}k chars`);
}

export function fmtAge(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return tr(`${s} 秒前`, `${s} s ago`);
  if (s < 3600) return tr(`${Math.round(s / 60)} 分钟前`, `${Math.round(s / 60)} min ago`);
  return tr(`${(s / 3600).toFixed(1)} 小时前`, `${(s / 3600).toFixed(1)} h ago`);
}

/** Segment anchor id. `.` is kept: replacing it with `-` would make `mw.a.b` and `mw.a_b` collide. */
export function segmentAnchor(id: string): string {
  return `ctx-seg-${id.replace(/[^a-zA-Z0-9_.-]/g, "_")}`;
}

/** Explanation for when no snapshot is available. The four causes call for four different actions and must not be merged into a single "no data". */
export function captureEmptyMessage(reasonCode: string, reason: string): string {
  return reason || tr(`没有快照（${reasonCode || "原因不明"}）。`, `No snapshot (${reasonCode || "reason unknown"}).`);
}
