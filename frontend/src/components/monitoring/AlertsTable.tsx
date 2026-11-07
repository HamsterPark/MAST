import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";
import type { components } from "@/api/schema";
import { Badge, EmptyNote, Spinner } from "@/components/ui";
import { langTag, tr } from "@/lib/i18n";
import { verdictView } from "@/lib/monitoring";
import { SegmentCurveThumb } from "@/components/monitoring/SegmentCurveThumb";

type AlertRow = components["schemas"]["AlertRow"];

// Alert history. The list endpoint deliberately omits the evidence PNG — a page of
// 50 alerts each carrying a base64 plot is a megabyte of payload nobody looked
// at. Opening a row fetches that one image, and react-query caches it forever
// because an alert's evidence is written once and never changes.

function EvidenceImage({ alertId }: { alertId: number }) {
  const q = useQuery({
    queryKey: ["monitoring", "alert-evidence", alertId],
    staleTime: Infinity,
    gcTime: 10 * 60_000,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/monitoring/alerts/{alert_id}/evidence", {
        params: { path: { alert_id: alertId } },
      });
      if (error) throw error;
      return data;
    },
  });

  if (q.isPending) return <Spinner label={tr("载入证据图…", "Loading evidence plot…")} />;
  if (q.isError) {
    return <p className="text-xs text-mast-danger">{tr("证据图载入失败。", "Failed to load the evidence plot.")}</p>;
  }
  if (!q.data?.ok || !q.data.png_b64) {
    return (
      <p className="text-xs text-mast-muted">
        {q.data?.detail || tr("该告警没有可用的证据图。", "No evidence plot is available for this alert.")}
      </p>
    );
  }
  return (
    <img
      src={`data:image/png;base64,${q.data.png_b64}`}
      alt={tr("告警证据：时域波形与频谱", "Alert evidence: time-domain waveform and spectrum")}
      className="max-w-full rounded-lg border border-mast-border bg-mast-bg"
    />
  );
}

/**
 * What to show when an alert row is opened. Three outcomes, not two.
 *
 * `evidence_available` only means "a rendered PNG exists on disk", not "there is
 * something to look at". It is false for every WARN (deliberately — rendering
 * each one would put matplotlib on the monitor's hot path), and false for a
 * CRITICAL whose render lost the 60 s rate-limit race. Meanwhile `seg_id` is in
 * the same row, and the segment behind it stays drawable: the raw `.npy` is
 * swept at `cm_keep_hours`, but the min/max envelope lives in the row and rows
 * are permanent. Only when there is neither a PNG nor a segment is there
 * genuinely nothing to show.
 */
function AlertEvidence({ alert }: { alert: AlertRow }) {
  if (alert.evidence_available) return <EvidenceImage alertId={alert.id} />;

  if (alert.seg_id != null) {
    return (
      <div className="space-y-1">
        <SegmentCurveThumb
          segId={alert.seg_id}
          hint={tr(`${alert.rule} 判定所依据的电流段`, `Current segment behind the ${alert.rule} verdict`)}
          wide
        />
        <p className="text-xs text-mast-muted">
          {tr(
            `这是判定所依据的那一段电流本身。没有渲染证据图${alert.level === "warn" ? "——WARN 一律不渲染，是刻意的" : ""}，但波形一直在，点开可看完整时域与频谱。`,
            `This is the current segment the verdict was based on. No evidence plot was rendered${alert.level === "warn" ? " (WARN alerts are never rendered, by design)" : ""}, but the waveform is kept; open it to see the full time-domain trace and spectrum.`,
          )}
        </p>
      </div>
    );
  }

  return (
    <p className="text-xs text-mast-muted">
      {tr(
        "该告警既没有证据图，也没有关联的电流段——文字判定仍然有效。",
        "This alert has neither an evidence plot nor a linked current segment; the written verdict still stands.",
      )}
    </p>
  );
}

/**
 * Who knows about this alert: two subjects, two cells.
 *
 * `delivered_agent` = the alert entered the context of the agent operating the instrument.
 * `acked`           = a person acknowledged it here.
 *
 * They are kept separate on purpose: collapsing them into one "handled" flag
 * could not answer whether the agent actually saw an alert, and "not seen" is
 * colour-coded.
 */
function KnownBy({ alert }: { alert: AlertRow }) {
  return (
    <span className="inline-flex items-center gap-1">
      {alert.delivered_agent ? (
        <span className="text-mast-accent" title={tr("agent 已在上下文里看到这条", "The agent has seen this in its context")}>🤖</span>
      ) : alert.level === "critical" ? (
        <span className="text-mast-danger" title={tr("agent 还没看到这条 CRITICAL", "The agent has not seen this CRITICAL yet")}>🤖✗</span>
      ) : (
        <span
          className="text-mast-muted"
          title={tr(
            "agent 未看到(WARN 可能被折叠或按配置不打扰)",
            "The agent has not seen it (WARNs may be folded or suppressed by configuration)",
          )}
        >
          ·
        </span>
      )}
      {alert.acked ? (
        <span className="text-mast-muted" title={tr("已由人确认", "Acknowledged by a person")}>✓</span>
      ) : null}
    </span>
  );
}

function AckButton({ alert }: { alert: AlertRow }) {
  const qc = useQueryClient();
  const m = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/monitoring/alerts/{alert_id}/ack", {
        params: { path: { alert_id: alert.id } },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["monitoring"] });
    },
  });

  if (alert.acked) {
    return <p className="text-xs text-mast-muted">{tr("已确认。", "Acknowledged.")}</p>;
  }
  return (
    <div className="space-y-1">
      <button
        type="button"
        disabled={m.isPending}
        onClick={(e) => {
          e.stopPropagation();
          m.mutate();
        }}
        className="rounded-md border border-mast-border px-2 py-1 text-xs text-mast-text hover:bg-mast-panel-2 disabled:opacity-50"
      >
        {m.isPending ? tr("确认中…", "Acknowledging…") : tr("我知道了(确认)", "Got it (acknowledge)")}
      </button>
      {/* State what the button does NOT do, so it is not mistaken for suppressing the alert for the agent. */}
      <p className="text-xs text-mast-muted">
        {tr(
          "只是记下「人已知悉」。**不会**让这条不再送给 agent,也不代表 agent 看过。",
          "This only records that a person has seen it. It does NOT stop the alert being sent to the agent, and does not mean the agent has seen it.",
        )}
      </p>
      {m.isError ? (
        <p className="text-xs text-mast-danger">{tr("确认失败,请重试。", "Acknowledge failed; please retry.")}</p>
      ) : null}
    </div>
  );
}

export function AlertsTable({
  alerts,
  degraded,
  detail,
}: {
  alerts: AlertRow[];
  degraded: boolean;
  detail?: string | null;
}) {
  const [openId, setOpenId] = useState<number | null>(null);

  // When degraded, never claim "no alerts": a failed `alerts_query` is reported by
  // the route as `degraded=true` + `detail`, and an unreadable store is not the
  // same as an empty one.
  if (degraded) {
    return (
      <EmptyNote
        label={
          detail
            ? tr(`读不到告警记录：${detail}`, `Cannot read alert records: ${detail}`)
            : tr(
                "读不到告警记录（监控模块未装载，或查询失败）——这不等于没有告警。",
                "Cannot read alert records (monitoring module not loaded, or the query failed). This does not mean there are no alerts.",
              )
        }
      />
    );
  }
  if (!alerts.length) return <EmptyNote label={tr("暂无告警——这是好消息。", "No alerts. Good news.")} />;

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-xs text-mast-muted">
            <th className="py-1 pr-3 font-normal">{tr("时间", "Time")}</th>
            <th className="py-1 pr-3 font-normal">{tr("级别", "Level")}</th>
            <th className="py-1 pr-3 font-normal">{tr("规则", "Rule")}</th>
            <th className="py-1 pr-3 font-normal">{tr("说明", "Description")}</th>
            <th
              className="py-1 pr-3 font-normal"
              title={tr("🤖=agent 已看到 / ✓=人已确认", "🤖 = seen by the agent / ✓ = acknowledged by a person")}
            >
              {tr("谁知道了", "Known by")}
            </th>
            <th className="py-1 font-normal">{tr("段", "Segment")}</th>
          </tr>
        </thead>
        <tbody>
          {alerts.map((a) => {
            const v = verdictView(a.level);
            const open = openId === a.id;
            return [
              <tr
                key={a.id}
                onClick={() => setOpenId(open ? null : a.id)}
                className="cursor-pointer border-t border-mast-border hover:bg-mast-panel-2"
                title={
                  a.evidence_available
                    ? tr("点击展开证据图", "Click to expand the evidence plot")
                    : a.seg_id != null
                      ? tr("点击展开触发段的电流曲线", "Click to expand the triggering segment's current trace")
                      : tr("点击展开", "Click to expand")
                }
              >
                <td className="whitespace-nowrap py-1.5 pr-3 text-xs text-mast-muted">
                  {new Date(a.ts * 1000).toLocaleString(langTag(), { hour12: false })}
                </td>
                <td className="py-1.5 pr-3">
                  <Badge tone={v.tone}>{v.label}</Badge>
                </td>
                <td className="py-1.5 pr-3 font-mono text-xs text-mast-text">{a.rule}</td>
                <td className="py-1.5 pr-3 text-mast-text">{a.summary_zh}</td>
                <td className="py-1.5 pr-3 text-xs">
                  <KnownBy alert={a} />
                </td>
                <td className="py-1.5 font-mono text-xs text-mast-muted">
                  {a.seg_id ?? "—"}
                  {/* Two distinct markers: ◧ = rendered evidence plot, ∿ = raw waveform of the triggering segment. */}
                  {a.evidence_available ? (
                    <span className="ml-1 text-mast-accent" title={tr("有证据图", "Has evidence plot")}>◧</span>
                  ) : a.seg_id != null ? (
                    <span className="ml-1 text-mast-muted" title={tr("有触发段波形", "Has triggering-segment waveform")}>∿</span>
                  ) : null}
                </td>
              </tr>,
              open ? (
                <tr key={`${a.id}-ev`} className="border-t border-mast-border bg-mast-bg/40">
                  <td colSpan={6} className="p-3">
                    <div className="space-y-3">
                      <AlertEvidence alert={a} />
                      <AckButton alert={a} />
                    </div>
                  </td>
                </tr>
              ) : null,
            ];
          })}
        </tbody>
      </table>
    </div>
  );
}
