import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";
import { Card, ErrorNote } from "@/components/ui";
import { getLang, tr } from "@/lib/i18n";

// Coarse-motion vacuum interlock status strip + operator attestation.
//
// Attestation lives only here, with no matching agent tool: what is attested is
// a physical state MAST cannot observe (chamber vented to atmosphere, or pumped
// to high vacuum with the gauge unavailable). A model signing it would only turn
// the same ignorance into a permission, so it is an operator action by definition.
//
// While no verdict is available the strip shows the warning colour, not a neutral
// one: an interlock indicator that cannot compute a result must never look like a
// green light.

// Must mirror mast.core.vacuum_interlock.ATTESTATION_REASONS — the backend
// rejects an unknown reason rather than accepting a free-text one, because a
// signature that does not say WHAT was attested is not auditable afterwards.
const REASONS = [
  { value: "vented_to_atmosphere", label: tr("已通大气（腔体在常压）", "Vented to atmosphere (chamber at ambient pressure)") },
  { value: "high_vacuum_gauge_unavailable", label: tr("已抽至高真空，但真空计不可用", "Pumped to high vacuum, but the gauge is unavailable") },
] as const;

export function VacuumInterlockStrip() {
  const qc = useQueryClient();
  const [reason, setReason] = useState<string>(REASONS[0].value);
  const [signedBy, setSignedBy] = useState("");
  const [ttlHours, setTtlHours] = useState(8);
  const [open, setOpen] = useState(false);

  const q = useQuery({
    queryKey: ["coarse-map", "vacuum"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/coarse-map/vacuum");
      if (error) throw error;
      return data;
    },
    refetchInterval: 15000,
  });

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ["coarse-map"] });
  };

  const attest = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/coarse-map/vacuum/attest", {
        body: { reason, signed_by: signedBy, ttl_hours: ttlHours, note: "" },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: invalidate,
  });

  const revoke = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.DELETE("/api/coarse-map/vacuum/attest", {});
      if (error) throw error;
      return data;
    },
    onSuccess: invalidate,
  });

  const v = q.data;
  const allow = v?.allow === true;

  return (
    <Card className="space-y-2 text-sm">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="font-semibold">{tr("粗动真空互锁", "Coarse-motion vacuum interlock")}</span>
        <span
          className={`rounded px-2 py-0.5 text-xs font-semibold ${
            allow
              ? "bg-mast-accent/15 text-mast-accent"
              : "bg-mast-warn/15 text-mast-warn"
          }`}
        >
          {allow ? tr("允许粗动", "Coarse motion allowed") : tr("禁止粗动", "Coarse motion blocked")}
        </span>
      </div>

      {q.isError && <ErrorNote error={q.error} />}

      <p className="text-xs text-mast-muted">{v?.reason || tr("读取中…", "Loading…")}</p>

      {v && (
        <div className="flex flex-wrap gap-x-5 gap-y-1 text-xs text-mast-muted">
          {v.pressure_pa != null && (
            <span className="tabular-nums">
              {tr("压强", "Pressure")} {v.pressure_pa.toExponential(2)} Pa
              {v.age_s != null
                ? tr(`（${v.age_s.toFixed(0)} 秒前）`, ` (${v.age_s.toFixed(0)} s ago)`)
                : ""}
            </span>
          )}
          {v.over_range && <span className="text-mast-warn">{tr("超量程", "Over range")}</span>}
          <span>
            {tr("模式", "Mode")} {v.mode || "—"}
          </span>
          {v.attested && (
            <span className="text-mast-accent">
              {tr("已签署", "Attested")}
              {v.attestation_remaining_h != null
                ? tr(
                    `（剩 ${v.attestation_remaining_h.toFixed(1)} h）`,
                    ` (${v.attestation_remaining_h.toFixed(1)} h left)`,
                  )
                : ""}
            </span>
          )}
        </div>
      )}

      {/* Offer attestation only when the gauge cannot answer; signing over a valid reading is unnecessary and unsafe. */}
      {v && !allow && v.mode !== "gauge_only" && (
        <div className="rounded border border-mast-border bg-mast-panel p-2">
          {!open ? (
            <button
              type="button"
              onClick={() => setOpen(true)}
              className="text-xs text-mast-muted underline hover:text-mast-text"
            >
              {tr("签署「当前气压安全」…", "Attest “current pressure is safe”…")}
            </button>
          ) : (
            <div className="space-y-2">
              <p className="text-xs text-mast-muted">
                {getLang() === "zh" ? (
                  <>
                    只有你能确认 MAST 看不到的东西。签署<b>有时效</b>，
                    并且<b>重启即失效</b>——重启本身就是一次换场，重签只要十秒，
                    而一个活过了它所依据条件的许可，代价是一整个压电叠堆。
                  </>
                ) : (
                  <>
                    Only you can confirm what MAST cannot see. An attestation is{" "}
                    <b>time-limited</b> and <b>expires on restart</b>: a restart is
                    itself a change of conditions, re-attesting takes seconds, while
                    a permission that outlives the conditions it was based on can
                    cost an entire piezo stack.
                  </>
                )}
              </p>
              <select
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                className="w-full rounded border border-mast-border bg-mast-bg px-2 py-1 text-xs"
              >
                {REASONS.map((r) => (
                  <option key={r.value} value={r.value}>
                    {r.label}
                  </option>
                ))}
              </select>
              <div className="flex gap-2">
                <input
                  value={signedBy}
                  onChange={(e) => setSignedBy(e.target.value)}
                  placeholder={tr("署名", "Signed by")}
                  className="min-w-0 flex-1 rounded border border-mast-border bg-mast-bg px-2 py-1 text-xs"
                />
                <input
                  type="number"
                  min={0.5}
                  max={24}
                  step={0.5}
                  value={ttlHours}
                  onChange={(e) => setTtlHours(Number(e.target.value) || 8)}
                  className="w-20 rounded border border-mast-border bg-mast-bg px-2 py-1 text-xs tabular-nums"
                />
                <span className="self-center text-xs text-mast-muted">{tr("小时", "hours")}</span>
              </div>
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={() => attest.mutate()}
                  disabled={attest.isPending}
                  className="rounded border border-mast-border bg-mast-bg px-2 py-1 text-xs hover:text-mast-text disabled:opacity-50"
                >
                  {attest.isPending ? tr("签署中…", "Signing…") : tr("签署", "Attest")}
                </button>
                <button
                  type="button"
                  onClick={() => setOpen(false)}
                  className="rounded border border-mast-border bg-mast-bg px-2 py-1 text-xs text-mast-muted"
                >
                  {tr("取消", "Cancel")}
                </button>
              </div>
              {attest.isError && <ErrorNote error={attest.error} />}
            </div>
          )}
        </div>
      )}

      {v?.attested && (
        <button
          type="button"
          onClick={() => revoke.mutate()}
          disabled={revoke.isPending}
          className="text-xs text-mast-muted underline hover:text-mast-warn disabled:opacity-50"
        >
          {revoke.isPending
            ? tr("撤销中…", "Revoking…")
            : tr("撤销签署（例如开始抽气时）", "Revoke attestation (e.g. when pumping starts)")}
        </button>
      )}
    </Card>
  );
}
