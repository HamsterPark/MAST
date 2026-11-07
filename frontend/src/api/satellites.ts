// GET /api/satellites —— 卫星节点与它代管的设备（卫星枢纽的端点，契约 §6 list_satellites）。
//
// 带类型的调用；404 读成「卫星模块没部署」（不是报错），字段差异由 lib/satellites.ts 的规范化兜住。
//
// 刷新：每 2 s 轮一次（只读枢纽内存里的状态，不碰任何设备）；/ws/events 上来一条
// `satellite` 事件（连接状态变化）就立刻重取一次。

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";
import { useWsEvent } from "@/hooks/useWsEvents";
import {
  normalizePortGuard,
  normalizeSatellites,
  type PortGuardSnapshot,
  type SatellitesSnapshot,
} from "@/lib/satellites";

export const SATELLITES_POLL_MS = 2_000;
/** 端口归属：读的是上一次检查的结果（不重查），所以轮得勤一点也不贵。 */
export const PORT_GUARD_POLL_MS = 10_000;

export type SatellitesResult =
  | ({ deployed: true } & SatellitesSnapshot)
  /** 后端还没有这个端点（404）—— 卫星模块没部署，不是「没有卫星」。 */
  | { deployed: false };

export async function fetchSatellites(): Promise<SatellitesResult> {
  const { data, error, response } = await api.GET("/api/satellites");
  const status = response.status;
  if (status === 404) return { deployed: false };
  if (error || !data) throw new Error(`GET /api/satellites → HTTP ${status}`);
  return { deployed: true, ...normalizeSatellites(data) };
}

export function useSatellites() {
  const qc = useQueryClient();
  // data.kind ∈ link / restart / device / deadman / error / manual / estop / estop_ack / file / port_guard：
  // 只当触发用，收到就重拉（port_guard 那一类也重拉端口归属）
  useWsEvent("satellite", (ev) => {
    void qc.invalidateQueries({ queryKey: ["satellites"] });
    const kind = (ev.data as { kind?: unknown } | null)?.kind;
    if (kind === "port_guard") void qc.invalidateQueries({ queryKey: ["port-guard"] });
  });
  return useQuery({
    queryKey: ["satellites"],
    queryFn: fetchSatellites,
    refetchInterval: SATELLITES_POLL_MS,
    // 后台标签页不轮（react-query 默认就停 refetchInterval；写明是为了不让人改掉）
    refetchIntervalInBackground: false,
  });
}

export type PortGuardResult = ({ deployed: true } & PortGuardSnapshot) | { deployed: false };

async function fetchPortGuard(refresh: boolean): Promise<PortGuardResult> {
  const { data, error, response } = await api.GET("/api/port-guard", { params: { query: refresh ? { refresh: true } : {} } });
  const status = response.status;
  if (status === 404) return { deployed: false };
  if (error || !data) throw new Error(`GET /api/port-guard → HTTP ${status}`);
  return { deployed: true, ...normalizePortGuard(data) };
}

/**
 * GET /api/port-guard（契约 §10.5）：Nanonis 端口和本机光学设备是不是被 MAST 以外的程序
 * 占着。只报告、不处理 —— MAST 不替用户关别的程序（契约 §15）。
 */
export function usePortGuard() {
  return useQuery({
    queryKey: ["port-guard"],
    queryFn: () => fetchPortGuard(false),
    refetchInterval: PORT_GUARD_POLL_MS,
    refetchIntervalInBackground: false,
  });
}

/** 现查一次（?refresh=true）。只读：列进程与连接，不碰任何端口。 */
export function useRefreshPortGuard() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => fetchPortGuard(true),
    onSuccess: (data) => qc.setQueryData(["port-guard"], data),
  });
}
