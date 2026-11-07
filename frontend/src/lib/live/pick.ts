// 页面上的固定面板（「实时光谱」「TERS 图」「PMT」）各自该显示哪一个流。
//
// 规则：同类的流里，名字匹配面板用途的排在前面，其次进行中的，再次最近有数据的。
// 「跟随最新」（followLatest）：上一个 run 结束、新的 run 一声明就自动切过去 —— 正在构建
// 的 TERS 图不需要人去下拉框里找；但两个都在来数据的流之间不来回换。
//
// 名字规则（流 id 形如 <来源>/<设备>/<通道>，契约 §7）是**偏好**不是**过滤**：
// 匹配不上的同类流照样能选，免得某个生产方换了命名，面板就说「没有数据」。

import type { StreamState } from "./store.ts";

export const STREAM_HINTS = {
  /** 拉曼 CCD 的谱：卫星转发的 <sat>/<dev>/spectrum，AcquireSpectrum 的 local/spectra/<dev>。 */
  spectrum: /\/(ccd|spectr\w*)(\/|$)/i,
  /**
   * MAST 采的谱（AcquireSpectrum 的默认流：每次采集的平均谱，本机设备还有逐帧）。卫星
   * 转发的 <sat>/<dev>/spectrum 是 CCD 出的每一帧（含人工采的），两者同时在来时面板显示这个。
   * 枢纽本机的流一律是 local/ 前缀（契约 §7）。
   */
  acquired: /^local\/spectra\//i,
  /** 正在构建的 TERS 图、对准图（LaserFocusMap / OptimizeOpticalSignal）。 */
  tersMap: /ters_map|tersmap|alignment|laser_focus|focus_map|optical_signal|ters_/i,
  /** PMT / 光子计数的时间序列。 */
  pmt: /\/pmt(\/|$)|photon/i,
  /** 光学台页：泵浦-探测（PumpProbeScan → local/pump_probe/<run>，单台 xy、双台 map2d）。 */
  pumpProbe: /pump_probe|pumpprobe/i,
  /** 光学台页：光栅扫描（OpticalStageScan → local/optics_scan/<run>，二维 map2d、一维 xy）。 */
  raster: /optics?_scan|stage_scan|raster/i,
  /** 卫星转发的轴位置（<sat>/<dev>/angle | position | steps）。 */
  axis: /\/(angle|position|steps)$/i,
} as const;

/** 进行中 0 / 已结束 1 / 服务端已不保留 2。 */
function liveRank(s: StreamState): number {
  return s.gone ? 2 : s.meta.ended ? 1 : 0;
}

/** 进行中 > 已结束 > 服务端已不保留；同级按最近收到数据排。 */
export function byLiveness(a: StreamState, b: StreamState): number {
  return liveRank(a) - liveRank(b) || (b.lastRecvMs ?? -1) - (a.lastRecvMs ?? -1) || a.meta.id.localeCompare(b.meta.id);
}

function kindsOf(kind: string | readonly string[]): readonly string[] {
  return typeof kind === "string" ? [kind] : kind;
}

/** kind 符合的流；名字匹配 hint 的排前面（各自再按活性排）。 */
export function candidates(states: StreamState[], kind: string | readonly string[], hint?: RegExp): StreamState[] {
  const kinds = kindsOf(kind);
  const same = states.filter((s) => kinds.includes(s.meta.kind));
  if (!hint) return same.sort(byLiveness);
  const hit = same.filter((s) => hint.test(s.meta.id)).sort(byLiveness);
  const rest = same.filter((s) => !hint.test(s.meta.id)).sort(byLiveness);
  return [...hit, ...rest];
}

/** 只要名字匹配的（光学台页按用途分面板时用）。 */
export function matching(states: StreamState[], kind: string | readonly string[], hint: RegExp): StreamState[] {
  const kinds = kindsOf(kind);
  return states.filter((s) => kinds.includes(s.meta.kind) && hint.test(s.meta.id)).sort(byLiveness);
}

/**
 * 面板当前显示哪个流：用户选过且还在 → 用它；否则 → 候选里的第一个。
 * 用户选的流从列表里消失了（服务端淘汰、改名）→ 退回候选第一，而不是显示空白。
 * （StreamPanel 的「跟随最新」不用候选第一，用 followLatest。）
 */
export function resolvePick(cands: StreamState[], chosen: string | null): string | null {
  if (chosen && cands.some((s) => s.meta.id === chosen)) return chosen;
  return cands[0]?.meta.id ?? null;
}

/** 「跟随最新」里算「正在来数据」的窗口。 */
export const FOLLOW_FRESH_MS = 20_000;

/** 0：prefer 的流正在来数据；1：别的流正在来数据；2：没在来（或看不出来）。 */
function freshness(s: StreamState, nowMs: number, prefer?: RegExp): number {
  if (s.lastLiveMs === null || nowMs - s.lastLiveMs > FOLLOW_FRESH_MS) return 2;
  return prefer?.test(s.meta.id) ? 0 : 1;
}

/**
 * 「跟随最新」该显示哪个流。StreamPanel 在列表变化时、以及每隔几秒（新鲜度会变）问一次，
 * 把上一次的答案作为 current 传回来。
 *
 * 候选池：名字对得上用途（hint）的流有就只在它们里面挑，没有才用全部候选 —— 和
 * candidates() 的「名字优先」一致。池里按三档比：活性（进行中 > 已结束 > 服务端不保留）、
 * 新鲜度（prefer 的流 FOLLOW_FRESH_MS 内来过数据 > 别的流来过 > 都没来）、最近一帧的时刻。
 *
 * 粘住当前的流：第一名只有在前两档上严格胜过它才换，光是「最近一帧更晚」不换。否则两个
 * 交替来数据的同类流（卫星转发的逐帧原始谱、AcquireSpectrum 的平均谱）会让面板跟着最近
 * 一帧来回换，每换一次，瀑布图、叠加、暂停点都清零。
 *
 * 新鲜度看 lastLiveMs（store.ts），订阅时服务端重放的最新一帧不算 —— 否则一小时前的平均谱
 * 一订上就像刚来的。没订阅的流只在轮询模式下（目录 3 s 一份）看得出在不在来，所以
 * StreamPanel 会订阅 prefer 的候选。
 */
export function followLatest(
  cands: StreamState[],
  current: string | null,
  nowMs: number,
  opts: { hint?: RegExp; prefer?: RegExp } = {},
): string | null {
  const { hint, prefer } = opts;
  const hits = hint ? cands.filter((s) => hint.test(s.meta.id)) : [];
  const pool = hits.length ? hits : cands;
  const tier = (s: StreamState) => liveRank(s) * 3 + freshness(s, nowMs, prefer);
  const best = [...pool].sort(
    (a, b) =>
      tier(a) - tier(b) ||
      (b.lastLiveMs ?? -1) - (a.lastLiveMs ?? -1) ||
      (b.lastRecvMs ?? -1) - (a.lastRecvMs ?? -1) ||
      a.meta.id.localeCompare(b.meta.id),
  )[0];
  if (!best) return null;
  const cur = current === null ? undefined : pool.find((s) => s.meta.id === current);
  return cur && tier(cur) <= tier(best) ? cur.meta.id : best.meta.id;
}
