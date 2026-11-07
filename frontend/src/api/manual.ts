// 手动控制面板用到的读接口（契约 §13.1）。
//
//   · GET /api/manual/devices —— 每台电动装置的描述（面板完全按它渲染）。404 读成「后端还没有
//     这个端点」（旧版本 MAST），不是「没有设备」。
//   · 页头：针尖 / 反馈状态（/api/hardware/live-readings，与顶栏共用一份缓存）和仪器令牌的持有者
//     （/api/scope/switch-advisory 的 instrument_holder，只读内存，永不阻塞）。
//
// 全部只读；后台标签页不轮（refetchIntervalInBackground: false）。

import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";
import { useWsEvent } from "@/hooks/useWsEvents";
import { tr } from "@/lib/i18n";
import {
  dueReadouts,
  MAX_READOUT_IDS,
  normalizeManualDevices,
  normalizeReadouts,
  type LocalReadout,
  type ManualSnapshot,
  type ReadoutPlan,
} from "@/lib/manual/protocol";

export const MANUAL_DEVICES_POLL_MS = 5_000;

export type ManualDevicesResult = ({ deployed: true } & ManualSnapshot) | { deployed: false };

export async function fetchManualDevices(): Promise<ManualDevicesResult> {
  const { data, error, response } = await api.GET("/api/manual/devices");
  const status = response.status;
  if (status === 404) return { deployed: false };
  if (error || !data) throw new Error(`GET /api/manual/devices → HTTP ${status}`);
  // 有类型也照样过一遍规范化：缺字段给安全的缺省、认不出的项丢掉
  return { deployed: true, ...normalizeManualDevices(data) };
}

/**
 * 动作做完以后重取一次装置清单（描述里的状态 —— 光学组的步数里程、连没连上、占用方 —— 随动作变）。
 * 防抖：最后一次调用之后 800 ms 才取，按住连续点动时不会每一下都取。
 */
export function useRefreshManualDevicesSoon(): () => void {
  const qc = useQueryClient();
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (timer.current !== null) clearTimeout(timer.current);
    },
    [],
  );
  return useCallback(() => {
    if (timer.current !== null) clearTimeout(timer.current);
    timer.current = setTimeout(() => {
      timer.current = null;
      void qc.invalidateQueries({ queryKey: ["manual", "devices"] });
    }, 800);
  }, [qc]);
}

export function useManualDevices() {
  const qc = useQueryClient();
  // 卫星连上 / 断开 / 重启、设备状态变化：描述里的可用性跟着变，立刻重取
  useWsEvent("satellite", () => {
    void qc.invalidateQueries({ queryKey: ["manual", "devices"] });
  });
  return useQuery({
    queryKey: ["manual", "devices"],
    queryFn: fetchManualDevices,
    refetchInterval: MANUAL_DEVICES_POLL_MS,
    refetchIntervalInBackground: false,
  });
}

/**
 * 本机设备的读数：GET /api/manual/readouts?ids=a,b（批量只读端点，**不走技能执行入口**，不入账）。
 * null = 后端还没有这个端点（404）。
 */
export async function fetchManualReadouts(ids: string[]): Promise<Record<string, LocalReadout> | null> {
  const { data, error, response } = await api.GET("/api/manual/readouts", { params: { query: { ids: ids.join(",") } } });
  const status = response.status;
  if (status === 404) return null;
  if (error || !data) throw new Error(`GET /api/manual/readouts → HTTP ${status}`);
  return normalizeReadouts(data);
}

export interface ManualReadoutsState {
  /** 按卡合并的最近读数（卡离开屏幕后留着最后一次，滚回来不闪成「—」）。 */
  readouts: Record<string, LocalReadout>;
  /** false = 后端还没有 /api/manual/readouts（404）。 */
  deployed: boolean;
  /** 最近一拍整批失败的原话（某一台失败在它自己的 error 里）。 */
  error: string | null;
}

/**
 * 按拍子轮询本机读数（readoutPlan 算拍子与每台的间隔）：一拍最多一次请求，只带上到点的卡
 * （dueReadouts）—— 各按描述里自己的 interval_s。上一拍没回来不发下一拍；页面隐藏时停，
 * 回来立刻补一拍。
 */
export function useManualReadouts(plan: ReadoutPlan | null): ManualReadoutsState {
  const [state, setState] = useState<ManualReadoutsState>({ readouts: {}, deployed: true, error: null });
  const planRef = useRef(plan);
  planRef.current = plan;
  /** 每台上次发出读请求的时刻（跨拍、跨屏幕上卡的变化保留）。 */
  const lastRead = useRef<Record<string, number>>({});
  const key = plan ? `${plan.ids.join(",")}|${plan.intervalMs}` : "";

  useEffect(() => {
    if (!key) return;
    let alive = true;
    let running = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const tick = async () => {
      timer = null;
      if (!alive || running) return;
      const p = planRef.current;
      if (!p) return;
      if (document.visibilityState === "visible") {
        const now = Date.now();
        const due = dueReadouts(p, lastRead.current, now);
        if (due.length) {
          running = true;
          for (const id of due) lastRead.current[id] = now;
          try {
            // 端点一次最多认 MAX_READOUT_IDS 个 id：多出来的分几次（实际一屏远到不了）
            for (let i = 0; i < due.length; i += MAX_READOUT_IDS) {
              const r = await fetchManualReadouts(due.slice(i, i + MAX_READOUT_IDS));
              setState((s) => (r === null ? { ...s, deployed: false, error: null } : { readouts: { ...s.readouts, ...r }, deployed: true, error: null }));
              if (r === null) break;
            }
          } catch (e) {
            setState((s) => ({ ...s, error: e instanceof Error ? e.message : String(e) }));
          } finally {
            running = false;
          }
        }
      }
      if (alive) timer = setTimeout(() => void tick(), p.intervalMs);
    };
    void tick();
    const onVis = () => {
      if (document.visibilityState !== "visible" || !alive || running) return;
      if (timer !== null) clearTimeout(timer);
      void tick();
    };
    document.addEventListener("visibilitychange", onVis);
    return () => {
      alive = false;
      if (timer !== null) clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [key]);

  return state;
}

/** 针尖 / 反馈 / 扫描状态。queryKey 与顶栏、右栏相同：同一份请求，不多一倍流量。 */
export function useTipState() {
  return useQuery({
    queryKey: ["hardware", "live-readings"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/hardware/live-readings");
      if (error) throw error;
      return data;
    },
    refetchInterval: 2_000,
    refetchIntervalInBackground: false,
  });
}

export interface InstrumentHolder {
  owner: string;
  skill: string;
  heldS: number | null;
}

/** 仪器令牌此刻在谁手里（现读一次，不走缓存；只读内存，永不阻塞）；null = 没人拿着。读不到就抛。 */
export async function fetchInstrumentHolder(): Promise<InstrumentHolder | null> {
  const { data, error } = await api.GET("/api/scope/switch-advisory");
  if (error || !data) throw new Error(tr("读不到仪器令牌的持有者", "Cannot read the instrument token holder"));
  const h = (data.instrument_holder ?? null) as Record<string, unknown> | null;
  if (!h) return null;
  return {
    owner: typeof h.owner === "string" && h.owner ? h.owner : tr("另一个入口", "another client"),
    skill: typeof h.skill === "string" ? h.skill : "",
    heldS: typeof h.held_s === "number" ? h.held_s : null,
  };
}

/** 页头显示用：2 s 一刷。 */
export function useInstrumentHolder() {
  return useQuery({
    queryKey: ["scope", "switch-advisory"],
    queryFn: fetchInstrumentHolder,
    refetchInterval: 2_000,
    refetchIntervalInBackground: false,
  });
}
