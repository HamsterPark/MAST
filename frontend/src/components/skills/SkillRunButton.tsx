// A "click to run one skill" button: the single entry shape for every instrument action on the page.
//
// It only goes through POST /api/skills/{name}/execute (`skill_exec`): the safety gate, instrument
// token, abort and recording all live on that path and none is bypassed here (contract §13: no
// separate direct-to-driver route).
//
// Confirmation flow:
//   · safety = auto and the caller set immediate (stop-type skills) -> no dialog, runs on click.
//     A stop must take effect at once.
//   · otherwise a confirm dialog first: skill name, safety level, description, and the
//     parameters about to be sent (with units).
//   · After confirming, the dialog closes immediately; progress and result stay inline next to
//     the button (not a toast). This differs from ConfirmDialog, which stays open on failure, on
//     purpose: a spectrum acquisition can run for minutes, and a Modal is a full-page overlay
//     (z-50) above the top-bar emergency stop (z-30), so a confirm dialog held open during a long
//     run would make the emergency stop unreachable exactly when it is needed.
//     Inline and persistent keeps the intent of the contract: a failure must not look like success.

import { useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import clsx from "clsx";
import { api } from "@/api/client";
import type { components } from "@/api/schema";
import { Badge } from "@/components/ui";
import { Button } from "@/components/controls";
import { ConfirmDialog } from "@/components/scope/ConfirmDialog";
import { useNow } from "@/hooks/useLive";
import { getLang, tr } from "@/lib/i18n";

type SkillCard = components["schemas"]["SkillCard"];
type ExecResp = components["schemas"]["SkillExecuteResponse"];

const SAFETY_TONE: Record<string, string> = { auto: "AUTO", confirm: "WARN", dangerous: "DANGEROUS" };
const SAFETY_LABEL: Record<string, string> = { auto: tr("自动", "Auto"), confirm: tr("需确认", "Confirm"), dangerous: tr("危险", "Dangerous") };

export type RunOutcome =
  | { kind: "ok"; resp: ExecResp }
  | { kind: "failed"; message: string; resp?: ExecResp };

export function useSkillCard(name: string) {
  return useQuery({
    queryKey: ["skill", name],
    queryFn: async (): Promise<SkillCard> => {
      const r = await api.GET("/api/skills/{name}", { params: { path: { name } } });
      const status = r.response.status;
      // With an empty error body openapi-fetch returns error "" (falsy) and data undefined:
      // check both so undefined is never handed to react-query.
      if (r.error || !r.data) throw new Error(tr(`读技能卡失败（HTTP ${status}）${errText(r.error)}`, `Failed to read skill card (HTTP ${status}) ${errText(r.error)}`));
      return r.data;
    },
    staleTime: 5 * 60_000,
  });
}

/** Run a skill. Never throws: failures are folded into {kind:"failed"} and keep the server's wording. */
export async function runSkill(name: string, params: Record<string, unknown>): Promise<RunOutcome> {
  try {
    const { data, error } = await api.POST("/api/skills/{name}/execute", {
      params: { path: { name } },
      body: { params },
    });
    if (error || !data) return { kind: "failed", message: tr(`请求被拒：${errText(error)}`, `Request rejected: ${errText(error)}`) };
    if (!data.ok) {
      const miss = data.missing?.length ? tr(`（缺 ${data.missing.join("、")}）`, ` (missing ${data.missing.join(", ")})`) : "";
      return { kind: "failed", message: tr(`执行入口不可用${miss}：${data.error || "未连接内核"}`, `Execution endpoint unavailable${miss}: ${data.error || "kernel not connected"}`), resp: data };
    }
    if (!data.success) return { kind: "failed", message: data.error || tr("技能报告失败（没有给原因）", "Skill reported failure (no reason given)"), resp: data };
    return { kind: "ok", resp: data };
  } catch (e) {
    return { kind: "failed", message: tr(`未送达（网络或后端不可达）：${errText(e)}`, `Not delivered (network or backend unreachable): ${errText(e)}`) };
  }
}

function errText(e: unknown): string {
  if (!e) return "";
  if (e instanceof Error) return e.message;
  if (typeof e === "string") return e;
  try {
    return JSON.stringify(e);
  } catch {
    return String(e);
  }
}

export function SkillRunButton({
  skill,
  label,
  params,
  validate,
  immediate = false,
  variant = "default",
  describe,
  renderResult,
  onDone,
}: {
  skill: string;
  label: ReactNode;
  /** Evaluated on click (values typed into inputs are only final at that moment). */
  params: () => Record<string, unknown>;
  /** Client-side precheck: a returned message blocks the request and is shown inline. Only checks like "not a number"; limits belong to the server. */
  validate?: () => string | null;
  /**
   * Stop-type / read-only: no confirm dialog, runs on click, unless the skill card says it is
   * confirm / dangerous (an admin changed the level). It also runs directly when the card cannot
   * be read: a stop must not gain an extra gate because one card read failed.
   */
  immediate?: boolean;
  variant?: "default" | "primary" | "danger" | "ghost";
  /** Extra note in the confirm dialog (e.g. "the laser is not software-controlled"). */
  describe?: ReactNode;
  /** What to show inline on success (default: the skill summary). Read-only skills use it to print the reading. */
  renderResult?: (resp: ExecResp) => ReactNode;
  onDone?: (o: RunOutcome) => void;
}) {
  const card = useSkillCard(skill);
  const [confirming, setConfirming] = useState<Record<string, unknown> | null>(null);
  const [runningSince, setRunningSince] = useState<number | null>(null);
  const [outcome, setOutcome] = useState<RunOutcome | null>(null);
  const [precheck, setPrecheck] = useState<string | null>(null);
  const now = useNow(runningSince !== null ? 1000 : 60_000);

  const c = card.data;
  const safety = String(c?.safety ?? "").toLowerCase();
  const notFound = !!c && !c.degraded && c.found === false;
  const disabledReason = notFound ? tr(`技能 ${skill} 没有注册（后端还没有这个技能）`, `Skill ${skill} is not registered (the backend does not have it yet)`) : null;

  const go = async (p: Record<string, unknown>) => {
    setConfirming(null);
    setOutcome(null);
    setRunningSince(Date.now());
    const o = await runSkill(skill, p);
    setRunningSince(null);
    setOutcome(o);
    onDone?.(o);
  };

  const click = () => {
    setPrecheck(null);
    const bad = validate?.() ?? null;
    if (bad) {
      setPrecheck(bad);
      return;
    }
    const p = params();
    if (immediate && safety !== "confirm" && safety !== "dangerous") {
      void go(p);
      return;
    }
    setConfirming(p);
  };

  const units = new Map((c?.parameters ?? []).map((x) => [x.name, x.unit ?? ""]));

  return (
    <span className="inline-flex flex-col gap-1">
      <span className="inline-flex flex-wrap items-center gap-2">
        <span title={disabledReason ?? undefined}>
          <Button
            variant={variant}
            onClick={click}
            loading={runningSince !== null}
            disabled={notFound || runningSince !== null}
          >
            {label}
          </Button>
        </span>
        {runningSince !== null && (
          <span className="text-xs text-mast-muted">{tr("执行中", "Running")} {Math.max(0, Math.round((now - runningSince) / 1000))} s…</span>
        )}
      </span>
      {notFound && <span className="text-[11px] text-mast-faint">{tr("未注册：", "Not registered: ")}{skill}</span>}
      {precheck && <span className="text-xs text-mast-danger">{precheck}</span>}
      {outcome && (
        <span
          role={outcome.kind === "failed" ? "alert" : "status"}
          className={clsx(
            "inline-flex max-w-md items-start gap-1 text-xs",
            outcome.kind === "ok" ? "text-mast-auto" : "text-mast-danger",
          )}
        >
          <span className="min-w-0 break-words">
            {outcome.kind === "ok"
              ? renderResult
                ? renderResult(outcome.resp)
                : tr(
                    `完成${outcome.resp.summary ? `：${outcome.resp.summary}` : ""}（${outcome.resp.elapsed_s?.toFixed(1) ?? "?"} s）`,
                    `Done${outcome.resp.summary ? `: ${outcome.resp.summary}` : ""} (${outcome.resp.elapsed_s?.toFixed(1) ?? "?"} s)`,
                  )
              : outcome.message}
          </span>
          <button
            type="button"
            onClick={() => setOutcome(null)}
            className="shrink-0 text-mast-faint hover:text-mast-text"
            aria-label={tr("清除这条结果", "Dismiss this result")}
          >
            ✕
          </button>
        </span>
      )}

      <ConfirmDialog
        open={confirming !== null}
        title={tr(`执行 ${c?.zh ? `${c.zh}（${skill}）` : skill}`, `Run ${skill}`)}
        confirmLabel={tr("确认执行", "Confirm and run")}
        tone={safety === "dangerous" ? "danger" : "primary"}
        onConfirm={() => confirming && void go(confirming)}
        onClose={() => setConfirming(null)}
      >
        <div className="space-y-3 text-sm">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-mast-text">{skill}</span>
            {safety && <Badge tone={SAFETY_TONE[safety] ?? "default"}>{SAFETY_LABEL[safety] ?? safety}</Badge>}
            {card.isLoading && <span className="text-xs text-mast-faint">{tr("读取技能卡…", "Loading skill card…")}</span>}
            {c?.degraded && <span className="text-xs text-mast-warn">{tr("技能卡读不到（内核未连）", "Skill card unavailable (kernel not connected)")}</span>}
          </div>
          {(c?.description_zh || c?.description) && (
            <p className="text-xs text-mast-muted">
              {getLang() === "zh" ? c.description_zh || c.description : c.description || c.description_zh}
            </p>
          )}
          <div>
            <div className="mb-1 text-xs text-mast-muted">{tr("这次发送的参数", "Parameters to be sent")}</div>
            {confirming && Object.keys(confirming).length ? (
              <table className="w-full text-xs">
                <tbody>
                  {Object.entries(confirming).map(([k, v]) => (
                    <tr key={k} className="border-t border-mast-border">
                      <td className="py-1 pr-3 font-mono text-mast-muted">{k}</td>
                      <td className="py-1 font-mono text-mast-text">
                        {typeof v === "object" ? JSON.stringify(v) : String(v)}
                        {units.get(k) ? ` ${units.get(k)}` : ""}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <span className="text-xs text-mast-faint">{tr("（无参数）", "(no parameters)")}</span>
            )}
          </div>
          {describe}
          <p className="text-[11px] text-mast-faint">
            {tr(
              "确认后这个窗口会关上，进度和结果显示在按钮旁边 —— 执行期间页面上的停止按钮和顶栏急停都可以按。安全闸门、仪器占用和中止在服务端照常生效。",
              "This window closes after you confirm; progress and result appear next to the button. The page's stop button and the top-bar emergency stop stay usable during execution. The safety gate, instrument lock and abort still apply on the server.",
            )}
          </p>
        </div>
      </ConfirmDialog>
    </span>
  );
}
