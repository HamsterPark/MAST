import { Badge, Card, EmptyNote } from "@/components/ui";
import type { components } from "@/api/schema";
import {
  badgeTone,
  budgetText,
  fmtClock,
  fmtDuration,
  heartbeatBanner,
  ignitionText,
  statusLabel,
  statusTone,
  timelineProgress,
} from "@/lib/conduct";
import { tr } from "@/lib/i18n";

type Detail = components["schemas"]["ConductDetail"];

// The rest of the panel: status header / stall banner / timeline / current card /
// gate history / budget / folder.
//
// Everything comes from the single `GET /api/conducts/{id}` endpoint. That is deliberate:
// when a panel is assembled from several endpoints, each has its own timestamp, so
// "status is RUNNING" and "the wait card is still lit" can be shown together even though
// each is individually correct. One endpoint, one point in time.

/** Status header: what state it is in now, plus the reason. */
export function StatusHeader({ d }: { d: Detail }) {
  return (
    <Card>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone={badgeTone(statusTone(d.status))}>{statusLabel(d.status)}</Badge>
            {d.detour?.active && (
              // detour is an orthogonal flag, not a status: a detour still has RUNNING/WAITING
              // sub-states, so a second status badge would give the conduct two identities.
              <Badge tone="WARN">{tr(`绕道中（回 ${d.detour.return_stage}）`, `On detour (returns to ${d.detour.return_stage})`)}</Badge>
            )}
            {!d.attended && <Badge tone="INFO">{tr("无人值守", "Unattended")}</Badge>}
            {d.director_running ? null : <Badge tone="default">{tr("指挥线程未在跑", "Conductor thread not running")}</Badge>}
          </div>
          {/* status_reason is the only explanation the user sees; the backend never leaves it empty in abnormal states. */}
          {d.status_reason && (
            <p className="mt-2 whitespace-pre-wrap text-sm text-mast-text">{d.status_reason}</p>
          )}
          <p className="mt-1 text-xs text-mast-faint">
            {d.title || d.spec_id}　·　{tr("模板", "Template")} {d.spec_id} v{d.spec_version}
            　·　{tr("实验", "Experiment")} {d.experiment_id || "—"}
            　·　{tr("证据代次", "Evidence epoch")} {d.evidence_epoch}
            {d.approved_by &&
              `　·　${tr(`${d.approved_by} 批于 ${d.approved_at}`, `approved by ${d.approved_by} at ${d.approved_at}`)}`}
          </p>
        </div>
      </div>
      {ignitionText(d.ignition) && (
        <p className="mt-3 rounded-mast-ctl border border-mast-warn-border bg-mast-warn-bg px-3 py-2 text-xs text-mast-warn">
          {ignitionText(d.ignition)}
        </p>
      )}
      {d.detour?.active && d.detour.reason && (
        <p className="mt-3 rounded-mast-ctl border border-mast-warn-border bg-mast-warn-bg px-3 py-2 text-xs text-mast-warn">
          {tr("进绕道的理由：", "Reason for the detour: ")}{d.detour.reason}
          　—— {tr(
            "进绕道即作废此前采的针尖/仪器证据（代次已 +1），闸门只认当前代次。",
            "entering a detour invalidates the tip/instrument evidence collected so far (epoch +1); gates only accept the current epoch.",
          )}
        </p>
      )}
    </Card>
  );
}

/** Stall banner. Not drawn when there is nothing to report, so it keeps its meaning. */
export function StallBanner({ d }: { d: Detail }) {
  const b = heartbeatBanner(d.heartbeat, d.status);
  if (!b) return null;
  const crit = b.tone === "crit";
  return (
    <div
      className={
        crit
          ? "rounded-mast-ctl border border-mast-danger-border bg-mast-danger-bg px-4 py-3 text-sm text-mast-danger"
          : "rounded-mast-ctl border border-mast-warn-border bg-mast-warn-bg px-4 py-3 text-sm text-mast-warn"
      }
    >
      <p className="font-semibold">{b.title}</p>
      <p className="mt-1 whitespace-pre-wrap">{b.detail}</p>
    </div>
  );
}

/** Timeline: one row per stage, the current one highlighted. */
export function Timeline({ d }: { d: Detail }) {
  const rows = d.timeline ?? [];
  if (!rows.length) {
    return (
      <EmptyNote label={(d.not_available?.length ? d.not_available[0] : tr("这份 conduct 没有阶段", "This conduct has no stages"))} />
    );
  }
  return (
    <ol className="space-y-1.5">
      {rows.map((row) => {
        const cur = row.status === "current";
        const done = row.status === "done";
        const pct = row.steps_total ? (row.steps_done / row.steps_total) * 100 : 0;
        return (
          <li
            key={row.stage_id}
            className={
              cur
                ? "rounded-mast-ctl border border-mast-accent bg-mast-accent-soft px-3 py-2"
                : "rounded-mast-ctl border border-mast-border px-3 py-2"
            }
          >
            <div className="flex items-center justify-between gap-3 text-sm">
              <span className={cur ? "font-semibold text-mast-text" : "text-mast-muted"}>
                {done && "✓ "}
                {row.stage_id}　{row.title}
              </span>
              <span className="shrink-0 font-mono text-xs tabular-nums text-mast-faint">
                {timelineProgress(row.steps_done, row.steps_total)}
              </span>
            </div>
            <div className="mt-1.5 h-1 w-full overflow-hidden rounded bg-mast-border">
              <div
                className={done ? "h-full bg-mast-auto" : "h-full bg-mast-accent"}
                style={{ width: `${pct}%` }}
              />
            </div>
          </li>
        );
      })}
    </ol>
  );
}

/** Current card: which stage and step, and for how long. */
export function CurrentCard({ d }: { d: Detail }) {
  const hb = d.heartbeat;
  // In a degraded response these blocks may be absent altogether (all optional in the
  // schema). Render "—" rather than letting the page crash on an undefined.
  const stage = d.stage;
  const step = d.step;
  return (
    <Card>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm sm:grid-cols-4">
        <Cell label={tr("阶段", "Stage")} value={stage?.id ? `${stage.id}　${stage.title}` : "—"} />
        <Cell label={tr("步", "Step")} value={step?.id || "—"} sub={step?.kind} />
        <Cell
          label={tr("开始于", "Started at")}
          value={fmtClock(step?.started_at)}
          // Outside a step the backend gives no start time (rather than a stale one); say so.
          sub={
            d.active_run_id
              ? tr(`已跑 ${fmtDuration(hb?.step_elapsed_s)}`, `running for ${fmtDuration(hb?.step_elapsed_s)}`)
              : tr("当前不在步里", "not in a step")
          }
        />
        <Cell
          label={tr("心跳", "Heartbeat")}
          value={hb?.age_s != null ? tr(`${fmtDuration(hb.age_s)}前`, `${fmtDuration(hb.age_s)} ago`) : "—"}
          sub={hb?.in_step ? tr("执行长步时心跳不更新，那是设计", "The heartbeat does not update during a long step; this is by design") : hb?.reason || ""}
        />
      </dl>
    </Card>
  );
}

function Cell({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-mast-faint">{label}</dt>
      <dd className="truncate text-mast-text" title={value}>
        {value}
      </dd>
      {sub && <p className="truncate text-xs text-mast-faint" title={sub}>{sub}</p>}
    </div>
  );
}

/**
 * Provenance of a gate decision: made by a rule or by a model, and which model.
 *
 * Without this line an LLM verdict and a rule verdict look identical in the table, yet
 * they are audited differently (one checks the criterion, the other checks the verdict
 * itself: which model answered, how it was parsed, how many wake-ups remain in this
 * stage). When no verdict happened (timeout / model could not be built / return value
 * outside the closed set) that is shown too.
 */
function GateProvenance({ g }: { g: components["schemas"]["GateHistoryRow"] }) {
  if (g.kind !== "llm") return null;
  const bits: string[] = [];
  // If the model name is unknown, say so instead of leaving it blank (blank would read as "not decided by a model").
  bits.push(`${tr("模型", "Model")} ${g.llm_model || tr("(没记到)", "(not recorded)")}`);
  if (g.llm_parse_path) bits.push(g.llm_parse_path);
  if (g.llm_wakes_max != null) bits.push(`${tr("本段唤醒", "Wake-ups this stage")} ${g.llm_wakes_used ?? "?"}/${g.llm_wakes_max}`);
  return (
    <p className="mt-0.5 text-xs text-mast-faint">
      <span className="mr-1 rounded bg-mast-border px-1 py-0.5 font-mono text-[10px]">LLM</span>
      {bits.join("　·　")}
      {g.llm_unavailable && (
        <span className="ml-1 text-mast-warn">　{tr("判决没发生:", "No verdict made: ")}{g.llm_unavailable}</span>
      )}
      {g.escaped && !g.llm_unavailable && <span className="ml-1 text-mast-warn">　{tr("模型弃权", "Model abstained")}</span>}
    </p>
  );
}

/** Gate history table (last 20). */
export function GateHistory({ d }: { d: Detail }) {
  const gates = d.gates_history ?? [];
  if (!gates.length) return <EmptyNote label={tr("还没有闸门判定", "No gate decisions yet")} />;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead className="text-xs text-mast-faint">
          <tr>
            <th className="py-1 pr-3 font-normal">{tr("时刻", "Time")}</th>
            <th className="py-1 pr-3 font-normal">{tr("阶段", "Stage")}</th>
            <th className="py-1 pr-3 font-normal">{tr("闸门", "Gate")}</th>
            <th className="py-1 pr-3 font-normal">{tr("裁决", "Verdict")}</th>
            <th className="py-1 font-normal">{tr("理由", "Reason")}</th>
          </tr>
        </thead>
        <tbody>
          {[...gates].reverse().map((g, i) => (
            <tr key={`${g.ts}-${g.gate_id}-${i}`} className="border-t border-mast-border">
              <td className="whitespace-nowrap py-1.5 pr-3 font-mono text-xs text-mast-muted">
                {g.ts}
              </td>
              <td className="py-1.5 pr-3 text-mast-muted">{g.stage_id}</td>
              <td className="py-1.5 pr-3 text-mast-text">{g.gate_id}</td>
              <td className="py-1.5 pr-3">
                <Badge tone={g.verdict === "pass" ? "AUTO" : "WARN"}>{g.verdict}</Badge>
              </td>
              <td className="py-1.5 text-xs text-mast-muted">
                {g.note}
                <GateProvenance g={g} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Budget + LLM wake-ups + experiment folder outputs. */
export function Vitals({ d }: { d: Detail }) {
  const budget = budgetText(d.budget);
  const wakes = Object.entries(d.llm_wakes ?? {});
  const folder = d.folder;
  return (
    <div className="grid gap-3 sm:grid-cols-3">
      <Card>
        <p className="text-xs text-mast-faint">{tr("花销", "Spend")}</p>
        <p
          className={
            budget.unreadable
              ? "mt-1 text-lg text-mast-warn"
              : "mt-1 text-lg tabular-nums text-mast-text"
          }
        >
          {budget.text}
        </p>
        {/* Say "unreadable" explicitly: an empty budget would read as "nothing spent". */}
        {budget.hint && <p className="mt-1 text-xs text-mast-faint">{budget.hint}</p>}
      </Card>

      <Card>
        <p className="text-xs text-mast-faint">{tr("LLM 唤醒（每阶段）", "LLM wake-ups (per stage)")}</p>
        {wakes.length ? (
          <ul className="mt-1 space-y-0.5 text-sm text-mast-text">
            {wakes.map(([stage, n]) => (
              <li key={stage} className="tabular-nums">
                {tr(`${stage}：${n} 次`, `${stage}: ${n}`)}
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-1 text-sm text-mast-muted">{tr("还没有唤醒过", "Never woken yet")}</p>
        )}
        <p className="mt-1 text-xs text-mast-faint">
          {tr("闸门里的 LLM **只选路不给数**，每条路的动作参数都是 spec 预写的。", "The LLM in a gate **only chooses a branch and never supplies numbers**; the action parameters of every branch are pre-written in the spec.")}
        </p>
      </Card>

      <Card>
        <p className="text-xs text-mast-faint">{tr("实验文件夹", "Experiment folder")}</p>
        {folder?.path ? (
          <>
            <p className="mt-1 break-all font-mono text-xs text-mast-muted">{folder.path}</p>
            <p className="mt-1 text-sm text-mast-text">
              {folder.spec_doc || tr("还没有快照", "No snapshot yet")}
              　·　progress{" "}
              {folder.progress_lines == null
                ? tr("读不到", "unreadable")
                : tr(`${folder.progress_lines} 行`, `${folder.progress_lines} lines`)}
            </p>
          </>
        ) : (
          <p className="mt-1 text-sm text-mast-warn">{folder?.reason || tr("没有落点", "No destination")}</p>
        )}
        <p className="mt-1 text-xs text-mast-faint">
          {tr("人读副本，不是真源；恢复流程不读它。", "Human-readable copy, not the source of truth; the recovery procedure does not read it.")}
        </p>
      </Card>
    </div>
  );
}

/** Things this response could not compute. Not drawn when empty; shown whenever present. */
export function NotAvailable({ d }: { d: Detail }) {
  if (!d.not_available?.length) return null;
  return (
    <div className="rounded-mast-ctl border border-dashed border-mast-border-strong px-3 py-2 text-xs text-mast-muted">
      <span className="text-mast-faint">{tr("这次没能算出来：", "Could not be computed this time: ")}</span>
      {d.not_available.join(tr("；", "; "))}
    </div>
  );
}
