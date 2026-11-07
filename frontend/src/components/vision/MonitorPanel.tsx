import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";
import { Card, ErrorNote, DegradedNote } from "@/components/ui";
import { Button } from "@/components/controls";
import { useToast } from "@/components/controls";
import { TimeTraceChart } from "@/components/experimental/TimeTraceChart";
import { tr } from "@/lib/i18n";

// Long-term monitor — periodic single-channel reads appended to a CSV by a core daemon.
// Typed replacement for gui/exp_capture.build_monitor_panel:
//  · channel (string: index or 'current') + interval slider
//  · start / stop monitoring (POST monitor/start | monitor/stop)
//  · live status (GET monitor/status, poll 2s) — running / count / last value / csv path
// The daemon lives in the core; closing this page does NOT stop it.

function Reading({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between border-b border-mast-border py-1.5 last:border-b-0">
      <span className="text-sm text-mast-muted">{label}</span>
      <span className="text-sm font-medium tabular-nums">{value}</span>
    </div>
  );
}

export function MonitorPanel() {
  const qc = useQueryClient();
  const { toast, node } = useToast();
  const [channel, setChannel] = useState("current");
  const [intervalS, setIntervalS] = useState(5);

  const status = useQuery({
    queryKey: ["experimental", "monitor", "status"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/experimental/monitor/status");
      if (error) throw error;
      return data;
    },
    refetchInterval: 2000,
  });

  const st = status.data;
  const running = st?.running ?? false;

  // Channel dropdown, reusing the capture tab's channel list.
  const signals = useQuery({
    queryKey: ["experimental", "signals"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/experimental/signals");
      if (error) throw error;
      return data;
    },
    staleTime: 60_000,
  });
  const channels = signals.data?.channels ?? [];

  // Live chart. The monitor daemon reports only the LAST value per
  // poll, so accumulate a client-side series (one point per NEW sample = count
  // change) while this page is open. The full history is always in the CSV.
  const [series, setSeries] = useState<{ t: number[]; v: number[] }>({ t: [], v: [] });
  const lastCount = useRef(-1);
  const t0 = useRef<number | null>(null);
  const clearSeries = () => {
    setSeries({ t: [], v: [] });
    lastCount.current = -1;
    t0.current = null;
  };
  useEffect(() => {
    if (!st || !running) return;
    const count = st.count ?? 0;
    if (count !== lastCount.current && st.last_value != null) {
      lastCount.current = count;
      if (t0.current == null) t0.current = Date.now() / 1000;
      const elapsed = Date.now() / 1000 - t0.current;
      setSeries((s) => ({
        t: [...s.t, elapsed].slice(-3000),
        v: [...s.v, st.last_value as number].slice(-3000),
      }));
    }
  }, [st, running]);

  const startMut = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/experimental/monitor/start", {
        body: { channel, interval_s: intervalS },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (d) => {
      if (d.ok) {
        toast(tr("已开始监控", "Monitoring started"));
        clearSeries();
      } else toast(d.message || tr("无法开始监控", "Could not start monitoring"), "err");
      qc.invalidateQueries({ queryKey: ["experimental", "monitor", "status"] });
    },
    onError: (e) => toast(String((e as Error)?.message ?? e), "err"),
  });

  const stopMut = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/experimental/monitor/stop", {});
      if (error) throw error;
      return data;
    },
    onSuccess: (d) => {
      if (d.ok) toast(tr("已停止监控", "Monitoring stopped"));
      else toast(d.message || tr("停止失败", "Stop failed"), "err");
      qc.invalidateQueries({ queryKey: ["experimental", "monitor", "status"] });
    },
    onError: (e) => toast(String((e as Error)?.message ?? e), "err"),
  });

  return (
    <div className="space-y-3">
      {node}
      <p className="text-xs text-mast-muted">
        {tr(
          "长期监控 / Long-term monitor — 每隔设定秒数读取一次所选信号通道，持续追加到 CSV 长期保存。后台线程记录，切走本页也继续，直到点「停止监控」或程序退出。适合过夜观察漂移 / 稳定性。",
          "Long-term monitor — reads the selected signal channel at the set interval and appends each reading to a CSV. A background thread does the recording, so it keeps running when you leave this page, until you press “Stop monitoring” or the program exits. Suited to overnight drift and stability checks.",
        )}
      </p>

      {status.isError && <ErrorNote error={status.error} />}
      {st?.degraded && <DegradedNote what={tr("长期监控", "Long-term monitor")} />}

      <Card className="space-y-3">
        <div className="flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1 text-xs">
            <span className="text-mast-muted">{tr("监控通道", "Monitor channel")}</span>
            <select
              value={channel}
              onChange={(e) => setChannel(e.target.value)}
              disabled={running}
              className="w-60 rounded-md border border-mast-border bg-mast-bg px-2 py-1.5 text-sm disabled:opacity-50"
            >
              <option value="current">{tr("current（隧道电流）", "current (tunneling current)")}</option>
              {channels.map((c) => (
                <option key={c.index} value={String(c.index)}>
                  {c.index}: {c.name}
                  {c.unit ? ` (${c.unit})` : ""}
                </option>
              ))}
            </select>
            <span className="text-mast-muted">
              {signals.isLoading
                ? tr("加载通道…", "Loading channels…")
                : signals.data?.degraded
                  ? tr("⚠ 未连硬件，为默认通道列表", "⚠ No hardware connected; showing the default channel list")
                  : tr(`${channels.length} 个通道`, `${channels.length} channels`)}
              {" · "}
              <button
                type="button"
                onClick={() => signals.refetch()}
                disabled={running || signals.isFetching}
                className="text-mast-accent hover:underline disabled:opacity-50"
              >
                {tr("刷新", "Refresh")}
              </button>
            </span>
          </label>
          <label className="flex flex-1 flex-col gap-1 text-xs">
            <span className="text-mast-muted">{tr(`采样间隔：${intervalS.toFixed(1)} 秒/点`, `Sampling interval: ${intervalS.toFixed(1)} s/point`)}</span>
            <input
              type="range"
              min={0.5}
              max={600}
              step={0.5}
              value={intervalS}
              onChange={(e) => setIntervalS(Number(e.target.value))}
              disabled={running}
              className="w-full"
            />
          </label>
          <Button
            variant="primary"
            onClick={() => startMut.mutate()}
            disabled={running || startMut.isPending}
          >
            {startMut.isPending ? tr("启动中…", "Starting…") : tr("开始监控", "Start monitoring")}
          </Button>
          <Button variant="danger" onClick={() => stopMut.mutate()} disabled={!running || stopMut.isPending}>
            {stopMut.isPending ? tr("停止中…", "Stopping…") : tr("停止监控", "Stop monitoring")}
          </Button>
        </div>
      </Card>

      <Card>
        <div className="mb-2 flex items-center gap-2">
          <span className="text-sm font-semibold">{tr("监控状态", "Monitor status")}</span>
          <span
            className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs ${
              running ? "bg-mast-auto-bg text-mast-auto" : "bg-mast-bg text-mast-muted"
            }`}
          >
            <span className={`h-1.5 w-1.5 rounded-full ${running ? "bg-mast-auto" : "bg-mast-muted"}`} />
            {running ? tr("运行中", "Running") : tr("未运行", "Not running")}
          </span>
        </div>
        {!st?.started && !running ? (
          <p className="text-sm text-mast-muted">{tr("未开始监控。", "Monitoring has not started.")}</p>
        ) : (
          <>
            <Reading label={tr("通道", "Channel")} value={st?.channel || "—"} />
            <Reading label={tr("采样间隔", "Sampling interval")} value={st ? `${st.interval_s.toFixed(1)} s` : "—"} />
            <Reading label={tr("已采集点数", "Points collected")} value={st?.count ?? 0} />
            <Reading
              label={tr("最新值", "Latest value")}
              value={
                st?.last_value != null
                  ? `${st.last_value.toPrecision(6)}${st.unit ? " " + st.unit : ""}`
                  : "—"
              }
            />
            <Reading label={tr("最新时间", "Latest time")} value={st?.last_t || "—"} />
            <Reading
              label={tr("CSV 路径", "CSV path")}
              value={
                st?.csv_path ? (
                  <span className="font-mono text-xs" title={st.csv_path}>
                    {st.csv_path}
                  </span>
                ) : (
                  "—"
                )
              }
            />
            {st?.error && <p className="mt-2 text-sm text-mast-danger">{st.error}</p>}
          </>
        )}
      </Card>

      {/* Real-time chart of the monitored channel */}
      <Card>
        <div className="mb-2 flex items-center justify-between">
          <span className="text-sm font-semibold">{tr("实时曲线", "Live trace")}</span>
          {series.v.length > 0 && (
            <button
              type="button"
              onClick={clearSeries}
              className="text-xs text-mast-muted hover:text-mast-accent"
            >
              {tr("清空", "Clear")}
            </button>
          )}
        </div>
        {series.v.length === 0 ? (
          <p className="py-6 text-center text-sm text-mast-muted">
            {running
              ? tr(
                  `等待采样点…（每 ${intervalS.toFixed(1)} 秒一个）`,
                  `Waiting for samples… (one every ${intervalS.toFixed(1)} s)`,
                )
              : tr(
                  "开始监控后，这里实时显示所选通道随时间的曲线。",
                  "Once monitoring starts, the selected channel is plotted here over time.",
                )}
          </p>
        ) : (
          <>
            <TimeTraceChart
              timestampsS={series.t}
              samples={series.v}
              unit={st?.unit || ""}
              channelName={st?.channel || channel}
            />
            <p className="mt-1 text-xs text-mast-faint">
              {tr(
                `本页打开期间累积的 ${series.v.length} 个点（横轴＝经过秒数）；完整历史见 CSV。`,
                `${series.v.length} points accumulated while this page was open (x axis = elapsed seconds); the full history is in the CSV.`,
              )}
            </p>
          </>
        )}
      </Card>
    </div>
  );
}
