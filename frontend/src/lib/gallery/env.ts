// ════════════════════════════════════════════════════════════════════════════
// 数据图库 —— 环境标签（docs/v2/design/data_env_tags.md §6.3）。
//
// 索引条目上的 `env` 是后端算好的摘要：每种量（T 温度 / B 磁场 / P 真空 / L 液位）一个主值，
// `v` + `u` 是**传感器自己的单位**（卡片照原样显示：报 Pa 就写 Pa，625 设在 kG 就写 kG），
// `c` 是规范值（K / T / Pa），**只给筛选用**；窗口 `wT/wB/...` 与值同一个单位。
//
// 值的状态：l 保存时的读数 · h 环境历史 1 分钟桶 · o 过期（`a` = 早于保存多少秒）· i 推断。
// 过期值不进范围筛选；推断的照常进（它有自己的「推断」标记）。
//
// 纯函数，没有 React，node --test 直接测（`import type` 会被整句擦除）。
// ════════════════════════════════════════════════════════════════════════════

import { tr } from "../i18n.ts";
import type { GalleryItem } from "./types.ts";

export type EnvKey = "T" | "B" | "P" | "L";
export type EnvState = "l" | "h" | "o" | "i";

export interface EnvValue {
  v?: number | null;
  u?: string;
  c?: number | null;
  s?: EnvState;
  a?: number | null;
}

export interface EnvSummary {
  s?: "r" | "i";
  T?: EnvValue | null;
  B?: EnvValue | null;
  P?: EnvValue | null;
  L?: EnvValue | null;
  wT?: number[] | null;
  wB?: number[] | null;
  wP?: number[] | null;
  wL?: number[] | null;
  /** 窗口是逐条读数算出来的那几种（正好盖住采集期间）；其余的窗口按 1 分钟桶算，比采集期间宽。 */
  wr?: string[] | null;
}

/** 卡片上显示的三种（液位只在详情与提示里）。 */
export const CARD_KEYS: readonly EnvKey[] = ["T", "B", "P"];

export function envOf(it: Pick<GalleryItem, "env">): EnvSummary | null {
  const e = (it as { env?: EnvSummary | null }).env;
  return e && typeof e === "object" ? e : null;
}

/** 负号用真的减号（U+2212），与偏压的写法一致。 */
const minus = (s: string): string => s.replace(/^-/, "−");

/** 三位有效数字，不出现科学计数（温度、磁场、液位）。 */
export function fmt3(v: number): string {
  if (!Number.isFinite(v)) return "?";
  if (v === 0) return "0";
  const a = Math.abs(v);
  if (a >= 1000) return minus(String(Math.round(v)));
  if (a < 1e-3) return minus(v.toExponential(2));
  return minus(v.toPrecision(3));
}

/** 科学计数两位有效数字（真空）：3.1e-8。 */
export function fmtExp(v: number): string {
  if (!Number.isFinite(v)) return "?";
  if (v === 0) return "0";
  return minus(v.toExponential(1));
}

export function fmtNumber(key: EnvKey, v: number): string {
  return key === "P" ? fmtExp(v) : fmt3(v);
}

export function fmtValue(key: EnvKey, v: number, unit?: string): string {
  return `${fmtNumber(key, v)}${unit ? ` ${unit}` : ""}`;
}

/** 秒 → 「N 秒 / N 分钟 / N 小时 / N 天」（过期值早了多久）。 */
export function ageText(sec: number): string {
  const s = Math.max(0, Math.round(sec));
  if (s < 90) return tr(`${s} 秒`, `${s} s`);
  if (s < 5400) return tr(`${Math.round(s / 60)} 分钟`, `${Math.round(s / 60)} min`);
  if (s < 129600) return tr(`${Math.round(s / 3600)} 小时`, `${Math.round(s / 3600)} h`);
  return tr(`${Math.round(s / 86400)} 天`, `${Math.round(s / 86400)} d`);
}

export const KEY_LABEL: Record<EnvKey, string> = {
  T: tr("温度", "Temperature"),
  B: tr("磁场", "Field"),
  P: tr("真空", "Vacuum"),
  L: tr("液位", "Level"),
};

export const STATE_TITLE: Record<EnvState, string> = {
  l: tr("保存时的读数", "Reading at the time the file was saved"),
  h: tr("环境历史 1 分钟桶的均值（保存时刻在桶内）", "Mean of the 1-minute environment-history bucket holding the save time"),
  o: tr("过期：保存之前的最后一条读数", "Stale: the last reading before the save"),
  i: tr("推断：按环境历史推出来的（文件存的时候还没有环境标签）", "Inferred from the environment history (saved before env tags were recorded)"),
};

export interface EnvPart {
  key: EnvKey;
  /** 显示文字：值（窗口里变化明显时是「lo→hi」）加单位；history / 推断前面有「≈」。 */
  text: string;
  state: EnvState;
  /** 采集期间变过：窗口是逐条读数算的、而且变化明显（卡片标警示色）。按桶算的窗口照样显示
   *  「lo→hi」，但不算 —— 桶比采集期间宽，起伏可能发生在采集前后。 */
  changed: boolean;
  /** 过期值早于保存的时长（文字），其余为 null。 */
  stale: string | null;
  title: string;
}

/**
 * 卡片上要不要把采集窗口画成「lo→hi」（只是显示规则，记录里一律有精确的 min / max）：
 *   * 磁场：按显示精度（3 位有效数字）不相等就画 —— 扫描中途升降场正是要一眼看出来的；
 *   * 温度：跨度超过 2%（4.3 K 时约 0.09 K；按显示精度会把温控的抖动也画出来）；
 *   * 真空：最大 / 最小超过 2 倍（规范计读数本身就有百分之几的起伏）；
 *   * 液位不上卡片。
 * 窗口来自 1 分钟桶时覆盖比窗口宽、含整分钟的起伏 —— 阈值同样挡住这种噪声。
 */
export function windowChanged(key: EnvKey, lo: number, hi: number): boolean {
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) return false;
  if (key === "B") return fmtNumber(key, lo) !== fmtNumber(key, hi);
  if (key === "T") return Math.abs(hi - lo) > 0.02 * Math.max(Math.abs(lo), Math.abs(hi));
  if (key === "P") return lo > 0 && hi > 0 ? Math.max(hi / lo, lo / hi) > 2 : lo !== hi;
  return false;
}

function part(key: EnvKey, e: EnvSummary): EnvPart | null {
  const val = e[key];
  if (!val || val.v == null || !Number.isFinite(val.v)) return null;
  const state: EnvState = val.s ?? "l";
  const unit = val.u ?? "";
  const w = e[`w${key}` as "wT"] ?? null;
  let core = fmtNumber(key, val.v);
  let ranged = false;
  const [w0, w1] = w ?? [];
  if (w0 != null && w1 != null && windowChanged(key, w0, w1)) {
    core = `${fmtNumber(key, w0)}→${fmtNumber(key, w1)}`;
    ranged = true;
  }
  const changed = ranged && (e.wr ?? []).includes(key);
  const approx = state === "h" || state === "i" ? "≈" : "";
  const stale = state === "o" && val.a != null ? ageText(val.a) : null;
  let title = `${KEY_LABEL[key]} · ${STATE_TITLE[state]}`;
  if (stale) title += tr(`（早于保存 ${stale}）`, ` (${stale} before the save)`);
  if (changed) title += tr(" · 采集期间变过（窗口内最小→最大）", " · changed during the acquisition (min→max in the window)");
  else if (ranged)
    title += tr(
      " · 采集前后 1 分钟桶里的最小→最大（桶比采集期间宽，不一定是采集期间变的）",
      " · min→max over the 1-minute buckets around the acquisition (wider than the acquisition; not necessarily a change during it)",
    );
  return { key, text: `${approx}${core}${unit ? ` ${unit}` : ""}`, state, changed, stale, title };
}

/**
 * 设置页「新鲜度阈值」输入框 → 要发给服务端的秒数：
 *   * 留空（含只有空格）⇒ `{ value: null }`，不改（原来 `Number("")` 是 0，被夹成 5 s 存下）；
 *   * 不是数 ⇒ `null`，不保存、提示操作员；
 *   * 是数 ⇒ 夹到服务端的范围 5–600 s。
 */
export function parseMaxAge(text: string): { value: number | null } | null {
  const t = text.trim();
  if (t === "") return { value: null };
  const v = Number(t);
  if (!Number.isFinite(v)) return null;
  return { value: Math.min(600, Math.max(5, v)) };
}

/** 设置页计数行用到的记录器计数（服务端 GalleryEnvTagStatus 的一部分）。 */
export interface EnvTagCounts {
  written?: number;
  superseded?: number;
  reused?: number;
  same?: number;
  cooldown_skipped?: number;
  failed_write?: number;
  empty?: number;
  pending?: number;
  cooling_dirs?: number;
  dropped_full?: number;
  dropped_disabled?: number;
  central_failed?: number;
  errors?: unknown;
}

/**
 * 设置页「环境标签」一节的计数行（设计 §6.3：写成、只写中央记录、丢弃、失败都要看得见）。丢弃与失败有数时括号里拆开：队列满 / 关着时，中央记录写失败 / 记录器出错。
 */
export function envTagStatusLine(st: EnvTagCounts): string {
  const n = (v: unknown): number => (typeof v === "number" && Number.isFinite(v) ? v : 0);
  const written = n(st.written) + n(st.superseded);
  const centralOnly = n(st.cooldown_skipped) + n(st.failed_write);
  const dropped = n(st.dropped_full) + n(st.dropped_disabled);
  const failed = n(st.central_failed) + n(st.errors);
  const parts = [
    tr(`写成 ${written}`, `written ${written}`),
    tr(`副本补写 ${n(st.reused)}`, `copies ${n(st.reused)}`),
    tr(`已有不动 ${n(st.same)}`, `already there ${n(st.same)}`),
    tr(`只写中央记录 ${centralOnly}`, `central record only ${centralOnly}`),
    tr(`当时什么都不知道 ${n(st.empty)}`, `nothing known ${n(st.empty)}`),
    tr(`丢弃 ${dropped}`, `dropped ${dropped}`) +
      (dropped
        ? tr(`（队列满 ${n(st.dropped_full)} · 关着时 ${n(st.dropped_disabled)}）`,
             ` (queue full ${n(st.dropped_full)} · switched off ${n(st.dropped_disabled)})`)
        : ""),
    tr(`失败 ${failed}`, `failed ${failed}`) +
      (failed
        ? tr(`（中央记录 ${n(st.central_failed)} · 出错 ${n(st.errors)}）`,
             ` (central record ${n(st.central_failed)} · errors ${n(st.errors)})`)
        : ""),
    tr(`待办 ${n(st.pending)}`, `pending ${n(st.pending)}`),
  ];
  if (n(st.cooling_dirs)) parts.push(tr(`写不进的目录 ${n(st.cooling_dirs)}`, `unwritable directories ${n(st.cooling_dirs)}`));
  return tr("本次运行：", "This run: ") + parts.join(" · ");
}

/** 卡片那一行：温度、磁场、真空（有几个给几个）。 */
export function envParts(it: Pick<GalleryItem, "env">, keys: readonly EnvKey[] = CARD_KEYS): EnvPart[] {
  const e = envOf(it);
  if (!e) return [];
  const out: EnvPart[] = [];
  for (const k of keys) {
    const p = part(k, e);
    if (p) out.push(p);
  }
  return out;
}

/** 纯文字版（测试、复制）：「4.31 K · 20.0 kG · 3.1e-8 Pa」。 */
export function envLine(it: Pick<GalleryItem, "env">): string {
  return envParts(it)
    .map((p) => (p.stale ? `${p.text} (${p.stale})` : p.text))
    .join(" · ");
}

// ── 筛选 ──────────────────────────────────────────────────────────────────

export type EnvFilter = "" | "any" | "rec" | "inf" | "none";
export const ENV_FILTERS: readonly EnvFilter[] = ["", "any", "rec", "inf", "none"];

export interface EnvFilterFields {
  env: EnvFilter;
  /** 温度范围，K（规范值）。null = 不限。 */
  tmin: number | null;
  tmax: number | null;
  /** 磁场范围，T（规范值，带符号）。null = 不限。 */
  bmin: number | null;
  bmax: number | null;
}

/** 条目的环境标签从哪来：保存时记录的 / 推断的 / 没有。 */
export function envPresence(it: Pick<GalleryItem, "env">): "rec" | "inf" | "none" {
  const e = envOf(it);
  if (!e) return "none";
  return e.s === "i" ? "inf" : "rec";
}

/** 用于范围筛选的规范值：过期值（`o`）不算。 */
export function filterValue(it: Pick<GalleryItem, "env">, key: EnvKey): number | null {
  const v = envOf(it)?.[key];
  if (!v || v.s === "o" || v.c == null || !Number.isFinite(v.c)) return null;
  return v.c;
}

function inRange(x: number | null, lo: number | null, hi: number | null): boolean {
  if (lo == null && hi == null) return true;
  if (x == null) return false;
  return (lo == null || x >= lo) && (hi == null || x <= hi);
}

export function envMatches(it: Pick<GalleryItem, "env">, F: EnvFilterFields): boolean {
  if (F.env) {
    const p = envPresence(it);
    if (F.env === "any" && p === "none") return false;
    if (F.env !== "any" && F.env !== p) return false;
  }
  if (!inRange(filterValue(it, "T"), F.tmin, F.tmax)) return false;
  if (!inRange(filterValue(it, "B"), F.bmin, F.bmax)) return false;
  return true;
}

export interface EnvFacets {
  /** 这批条目里有没有任何环境标签。 */
  any: boolean;
  /** 有没有可筛选的温度 / 磁场。 */
  T: boolean;
  B: boolean;
}

export function envFacets(items: readonly Pick<GalleryItem, "env">[]): EnvFacets {
  const out: EnvFacets = { any: false, T: false, B: false };
  for (const it of items) {
    if (envOf(it)) out.any = true;
    if (!out.T && filterValue(it, "T") != null) out.T = true;
    if (!out.B && filterValue(it, "B") != null) out.B = true;
    if (out.any && out.T && out.B) break;
  }
  return out;
}

/** 输入框里的文字 → 数；空或不是数 ⇒ null。 */
export function parseBound(s: string): number | null {
  const t = s.trim().replace("−", "-");
  if (!t) return null;
  const v = Number(t);
  return Number.isFinite(v) ? v : null;
}

// ── 详情（大图侧栏）──────────────────────────────────────────────────────

/** 详情端点的 URL。条目 id 里有斜杠：逐段编码，斜杠保留（后端是 `{path:path}`）。 */
export function envDetailUrl(id: string): string {
  return "/api/gallery/env/" + id.split("/").map(encodeURIComponent).join("/");
}

export const KIND_LABEL: Record<string, string> = {
  temperature: tr("温度", "Temperature"),
  field: tr("磁场", "Field"),
  pressure: tr("真空", "Vacuum"),
  level: tr("液位", "Level"),
  magnet_current: tr("磁体电流", "Magnet current"),
  other: tr("其它", "Other"),
};

export const KIND_ORDER = ["temperature", "field", "pressure", "level", "magnet_current", "other"];

export const SOURCE_LABEL: Record<string, string> = {
  live: tr("读数", "reading"),
  history: tr("历史桶", "history bucket"),
  inferred: tr("推断", "inferred"),
};

export const ORIGIN_LABEL: Record<string, string> = {
  skill: tr("MAST 技能存的", "saved by a MAST skill"),
  watcher: tr("文件监视发现", "found by the file watcher"),
  copy: tr("实验文件夹副本", "experiment-folder copy"),
  inferred: tr("图库推断", "inferred by the gallery"),
};

export const HOW_LABEL: Record<string, string> = {
  sidecar: tr("保存时记录（sidecar）", "Recorded at save (sidecar)"),
  central: tr("保存时记录（中央记录；sidecar 不在）", "Recorded at save (central record; no sidecar here)"),
  inferred: tr("推断（环境历史 1 分钟桶）", "Inferred (1-minute environment-history buckets)"),
  none: tr("没有环境标签", "No environment tags"),
};

/** 读数时刻相对保存的说明：「−1.2 s」/「早于保存 7 分钟」/「晚于保存 2 分钟」；
 *  桶值（`bucket=true`）落在桶里时是「保存时刻在桶内」。 */
export function offsetText(dt: number | null | undefined, fresh = true, bucket = false): string {
  if (dt == null || !Number.isFinite(dt)) return "";
  if (bucket && dt === 0) return tr("保存时刻在桶内", "save time inside the bucket");
  if (fresh && Math.abs(dt) < 90) return `${dt < 0 ? "−" : "+"}${Math.abs(dt).toFixed(1)} s`;
  return dt < 0 ? tr(`早于保存 ${ageText(-dt)}`, `${ageText(-dt)} before the save`)
    : tr(`晚于保存 ${ageText(dt)}`, `${ageText(dt)} after the save`);
}
