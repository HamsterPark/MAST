import { useCallback, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";
import { Card, EmptyNote, ErrorNote, Section, Spinner } from "@/components/ui";
import { Button, SelectField, useToast } from "@/components/controls";
import { useWsConnection, useWsEvent } from "@/hooks/useWsEvents";
import { getLang, tr } from "@/lib/i18n";
import {
  CONDUCT_FRAMES,
  CONDUCT_SETTINGS_TITLE,
  frameAction,
  statusLabel,
} from "@/lib/conduct";
import { DecisionCard } from "@/components/conduct/DecisionCard";
import { WaitCard } from "@/components/conduct/WaitCard";
import { OpBar } from "@/components/conduct/OpBar";
import { ApproveCard, type ApproveOutcome } from "@/components/conduct/ApproveCard";
import { NewConductCard } from "@/components/conduct/NewConductCard";
import {
  CurrentCard,
  GateHistory,
  NotAvailable,
  StallBanner,
  StatusHeader,
  Timeline,
  Vitals,
} from "@/components/conduct/PanelBody";

// Multi-day conduct panel: the page to watch while an experiment runs for days.
//
// One endpoint, one point in time: the whole panel renders from a single
// `GET /api/conducts/{id}` response. When a panel is assembled from several endpoints,
// each has its own timestamp, so "status is RUNNING" and "the wait card is still lit"
// can be shown together even though each is individually correct.
//
// WS frames only trigger a refetch; they are not an incremental state source. On a
// conduct frame the query is invalidated and no field is taken from the frame. The bus
// only replays the last 100 frames, so a multi-day conduct will drop frames, and a client
// that accumulates state from frames would keep showing a wrong state indefinitely. This
// is deliberately the opposite of the current-monitoring page (where
// `patchStatusFromWsEvent` merges frames into the cache): there frames are a 1 Hz scalar
// stream and a dropped frame is covered by the next one, whereas here frames are state
// transitions and a dropped frame leaves a permanent gap.
//
// Engine off is not the same as no conduct. `conduct.cd_enabled` defaults to off; the
// read endpoint then returns 200 + degraded + a reason. The page shows that reason and
// where to enable it instead of a blank page, which would look like a broken feature.
//
// The UI never freezes. The worst outcome of a button is "no effect plus a toast"; a
// failed enqueue does not lock the UI or spin waiting for a reply, and with no active
// conduct the page still renders (empty state) instead of sitting in loading.

const LIST_KEY = ["conduct", "list"] as const;
const CONFIG_KEY = ["conduct", "config"] as const;

/** Panel polling interval. Slowed to a keep-alive rate while the WS is up; frames provide timeliness. */
const DETAIL_POLL_MS = 5_000;
const DETAIL_WS_KEEPALIVE_POLL_MS = 30_000;

export default function ConductPage() {
  const ws = useWsConnection();
  const qc = useQueryClient();
  const { toast, node } = useToast();
  // The selection lives in the URL rather than plain state so it can be bookmarked and
  // handed to the next shift, and a wishlist request `conduct:<id>` has a link target.
  // A selection held in component state cannot answer "which conduct does this link point to".
  const [params, setParams] = useSearchParams();
  const picked = params.get("id") ?? "";
  const setPicked = (id: string) => setParams(id ? { id } : {}, { replace: true });

  // Whether the new-conduct form is open. Not stored in the URL: `?id=` means "which
  // conduct to view", and a half-filled form is not a conduct; a shared link would open an
  // empty form for the recipient, who would think they are looking at that conduct.
  const [newOpen, setNewOpen] = useState(false);

  // Whether the engine is enabled
  const config = useQuery({
    queryKey: CONFIG_KEY,
    refetchInterval: 60_000,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/conducts/config");
      if (error) throw error;
      return data;
    },
  });

  // List (single-active invariant: usually only one)
  const list = useQuery({
    queryKey: LIST_KEY,
    refetchInterval: ws.polling ? 15_000 : 60_000,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/conducts");
      if (error) throw error;
      return data;
    },
  });

  // Which one is shown: the user's pick first, else the unfinished one, else the latest.
  // This is not written into a state default; otherwise after a conduct ends and a new one
  // is created the panel would stay on the old one with nothing explaining why.
  const activeId = list.data?.active_conduct_id || "";
  const rows = list.data?.conducts ?? [];
  const currentId = picked || activeId || rows[0]?.conduct_id || "";

  // Single data source of the panel
  const detail = useQuery({
    queryKey: ["conduct", "detail", currentId],
    enabled: Boolean(currentId),
    refetchInterval: ws.polling ? DETAIL_POLL_MS : DETAIL_WS_KEEPALIVE_POLL_MS,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/conducts/{conduct_id}", {
        params: { path: { conduct_id: currentId } },
      });
      if (error) throw error;
      return data;
    },
  });

  // WS: only triggers a refetch
  const onFrame = useCallback(
    (event: { type?: string; data: unknown }) => {
      if (frameAction(event.type ?? "", event.data, currentId) !== "refetch") return;
      qc.invalidateQueries({ queryKey: ["conduct", "detail", currentId] });
      // The status column in the list changes too (and create/abort change active_conduct_id).
      qc.invalidateQueries({ queryKey: LIST_KEY });
    },
    [qc, currentId],
  );
  // One handler for all three frame types: they differ only in what triggered them on the backend; for the panel each means "re-read once".
  useWsEvent(CONDUCT_FRAMES[0], onFrame);
  useWsEvent(CONDUCT_FRAMES[1], onFrame);
  useWsEvent(CONDUCT_FRAMES[2], onFrame);

  // Intents
  //
  // All of them only enqueue: the Director is the single writer of the status column and
  // applies them on the next tick. The success message therefore says "queued", not
  // "paused", which would describe something that has not happened yet.
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["conduct", "detail", currentId] });
    qc.invalidateQueries({ queryKey: LIST_KEY });
  };

  const op = useMutation({
    mutationFn: async (v: { path: "pause" | "resume" | "takeover"; by: string }) => {
      // The three paths are written out literally instead of building a string and casting
      // it back: a cast would turn a misspelled path from a compile-time error into a
      // runtime 404, which on this page looks like "pressed it and nothing happened".
      const body = { by: v.by, note: "" };
      const params = { path: { conduct_id: currentId } };
      const r =
        v.path === "pause"
          ? await api.POST("/api/conducts/{conduct_id}/pause", { params, body })
          : v.path === "resume"
            ? await api.POST("/api/conducts/{conduct_id}/resume", { params, body })
            : await api.POST("/api/conducts/{conduct_id}/takeover", { params, body });
      if (r.error) throw r.error;
      return r.data;
    },
    onSuccess: (d) => {
      toast(
        d?.queued
          ? tr("已排队，下一拍生效", "Queued; takes effect on the next tick")
          : d?.errors?.[0] || tr("没有排上", "Not queued"),
        d?.queued ? "ok" : "err",
      );
      refresh();
    },
    onError: (e) =>
      toast(tr(`发不出去：${String((e as Error)?.message ?? e)}`, `Could not send: ${String((e as Error)?.message ?? e)}`), "err"),
  });

  const abort = useMutation({
    mutationFn: async (reason: string) => {
      const { data, error } = await api.POST("/api/conducts/{conduct_id}/abort", {
        params: { path: { conduct_id: currentId } },
        body: { by: "operator", reason },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (d) => {
      // `abort_signalled` means the current step has already received the stop signal
      // (no waiting for a tick). It is separate from `queued`: queued alone means no step is running right now.
      toast(
        d?.abort_signalled
          ? tr("已中止：当前这一步已收到停止信号", "Aborted: the current step has received the stop signal")
          : d?.queued
            ? tr("已排队中止（当前没有在跑的步）", "Abort queued (no step is running right now)")
            : d?.errors?.[0] || tr("没有排上", "Not queued"),
        d?.queued ? "ok" : "err",
      );
      refresh();
    },
    onError: (e) =>
      toast(tr(`中止发不出去：${String((e as Error)?.message ?? e)}`, `Could not send abort: ${String((e as Error)?.message ?? e)}`), "err"),
  });

  const ack = useMutation({
    mutationFn: async (v: { waitId: string; note: string }) => {
      const { data, error } = await api.POST("/api/conducts/{conduct_id}/ack", {
        params: { path: { conduct_id: currentId } },
        body: { wait_id: v.waitId, by: "operator", note: v.note },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (d) => {
      // Tell the person who pressed the button what is still missing; the other half of the double gate may be open.
      const still = d?.wait_echo?.length
        ? tr(`　还缺：${d.wait_echo.join("、")}`, `  Still missing: ${d.wait_echo.join(", ")}`)
        : "";
      toast(
        d?.ok
          ? tr(`已确认。${still}`, `Confirmed.${still}`)
          : d?.errors?.[0] || tr("确认没被接受", "Confirmation was not accepted"),
        d?.ok ? "ok" : "err",
      );
      refresh();
    },
    onError: (e) =>
      toast(tr(`确认发不出去：${String((e as Error)?.message ?? e)}`, `Could not send confirmation: ${String((e as Error)?.message ?? e)}`), "err"),
  });

  const waive = useMutation({
    mutationFn: async (v: { waitId: string; reason: string }) => {
      const { data, error } = await api.POST("/api/conducts/{conduct_id}/waive-condition", {
        params: { path: { conduct_id: currentId } },
        body: { wait_id: v.waitId, by: "operator", reason: v.reason },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (d) => {
      toast(
        d?.queued
          ? tr("已记下：条件闸由人提供证据（留痕）", "Recorded: condition gate released on operator-provided evidence (logged)")
          : d?.errors?.[0] || tr("没有排上", "Not queued"),
        d?.queued ? "ok" : "err",
      );
      refresh();
    },
    onError: (e) =>
      toast(tr(`发不出去：${String((e as Error)?.message ?? e)}`, `Could not send: ${String((e as Error)?.message ?? e)}`), "err"),
  });

  // Release of a gate decision. Same discipline as waive (logged, permanently displayed)
  // but a different question: waive says "I provide the evidence for this physical
  // condition", this says "I have reviewed this verdict, continue". Without it a conduct
  // stopped by a gate could only be aborted or taken over.
  const override = useMutation({
    mutationFn: async (v: { decisionId: number; reason: string }) => {
      const { data, error } = await api.POST("/api/conducts/{conduct_id}/override-decision", {
        params: { path: { conduct_id: currentId } },
        body: { decision_id: v.decisionId, by: "operator", reason: v.reason },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (d) => {
      toast(
        d?.queued
          ? tr("已记下：这一次判定由人放行（留痕；下一次到这道闸还会重新判）", "Recorded: this decision was released by the operator (logged; the gate is judged again next time)")
          : d?.errors?.[0] || tr("没有排上", "Not queued"),
        d?.queued ? "ok" : "err",
      );
      refresh();
    },
    onError: (e) =>
      toast(tr(`发不出去：${String((e as Error)?.message ?? e)}`, `Could not send: ${String((e as Error)?.message ?? e)}`), "err"),
  });

  // Approve is a human action and this page is the only entry point (never registered as
  // an agent tool). A 409 is therefore not an error path but the thing to explain: the
  // findings and checks_skipped of the response go to the card as is, not merged into
  // one "validation failed" line.
  const [approveOutcome, setApproveOutcome] = useState<ApproveOutcome | null>(null);
  const approve = useMutation({
    mutationFn: async (by: string) => {
      const { data, error, response } = await api.POST(
        "/api/conducts/{conduct_id}/approve",
        {
          params: { path: { conduct_id: currentId } },
          body: { approved_by: by },
        },
      );
      // The body of a 409 is the useful part (what is missing), so do not throw; pass it on
      // as a result. Throwing would leave only a generic "failed to load" line.
      if (error && response.status !== 409) throw error;
      return (data ?? (error as ApproveOutcome)) as ApproveOutcome;
    },
    onSuccess: (d) => {
      setApproveOutcome(d);
      toast(
        d?.ok
          ? tr(
              `已批准。快照：${d.spec_doc_path || "（未渲染）"}`,
              `Approved. Snapshot: ${d.spec_doc_path || "(not rendered)"}`,
            )
          : tr("批不下去，见下方原因", "Cannot approve; see the reason below"),
        d?.ok ? "ok" : "err",
      );
      refresh();
    },
    onError: (e) =>
      toast(tr(`批准发不出去：${String((e as Error)?.message ?? e)}`, `Could not send approval: ${String((e as Error)?.message ?? e)}`), "err"),
  });

  const attended = useMutation({
    mutationFn: async (next: boolean) => {
      const { data, error } = await api.POST("/api/conducts/{conduct_id}/attended", {
        params: { path: { conduct_id: currentId } },
        body: { attended: next, by: "operator" },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (d) => {
      toast(
        d?.queued
          ? tr("值守模式已排队", "Attendance mode change queued")
          : d?.errors?.[0] || tr("没有排上", "Not queued"),
        d?.queued ? "ok" : "err",
      );
      refresh();
    },
    onError: (e) =>
      toast(tr(`发不出去：${String((e as Error)?.message ?? e)}`, `Could not send: ${String((e as Error)?.message ?? e)}`), "err"),
  });

  const busy = op.isPending || abort.isPending || ack.isPending || waive.isPending
    || attended.isPending || approve.isPending;

  // Engine off: say so, and where to enable it
  const engineOff = config.data && !config.data.enabled;
  const d = detail.data;

  return (
    <div className="mx-auto max-w-5xl px-4 py-6">
      <Section
        title={tr("多天 conduct", "Multi-day conduct")}
        subtitle={tr("一次跑几天的实验：现在在哪一步、卡在什么上、要不要你确认", "Experiments that run for days: which step it is on, what it is stuck on, and whether it needs your confirmation")}
        actions={
          <div className="flex items-center gap-2">
            {rows.length > 1 && (
              <div className="w-64">
                <SelectField
                  value={currentId}
                  onChange={setPicked}
                  options={rows.map((r) => ({
                    value: r.conduct_id,
                    label: `${r.title || r.spec_id} (${statusLabel(r.status)})`,
                  }))}
                />
              </div>
            )}
            {/* "New" must be reachable while a conduct exists too: if only the empty state
                offered it, the next one could never be created after the first finishes. */}
            {!newOpen && (
              <Button onClick={() => setNewOpen(true)}>{tr("新建 conduct", "New conduct")}</Button>
            )}
          </div>
        }
      >
        {engineOff && (
          <div className="mb-4 rounded-mast-ctl border border-mast-warn-border bg-mast-warn-bg px-4 py-3 text-sm text-mast-warn">
            <p className="font-semibold">{tr("conduct 指挥线程未启用", "Conduct conductor thread is not enabled")}</p>
            <p className="mt-1">
              {getLang() === "zh" ? (
                <>
                  这不是故障，是<b>默认关</b>：打开之前系统里根本没有这条常驻线程。
                  去「设置 → {CONDUCT_SETTINGS_TITLE}」打开<b>启用 conduct 指挥线程</b>。
                  已有 conduct 的状态现在仍然读得到，中止也仍然按得下。
                </>
              ) : (
                <>
                  This is not a fault: it is <b>off by default</b>, and until it is enabled the resident
                  thread does not exist. Enable <b>Enable conduct conductor thread</b> in
                  Settings → {CONDUCT_SETTINGS_TITLE}. The state of existing conducts can still be read
                  and abort still works.
                </>
              )}
            </p>
          </div>
        )}

        {list.isPending && <Spinner label={tr("载入 conduct 列表…", "Loading conduct list…")} />}
        {list.isError && <ErrorNote error={list.error} />}

        {newOpen && (
          <div className="mb-4">
            <NewConductCard
              activeConductId={activeId}
              onClose={() => setNewOpen(false)}
              onCreated={(id) => {
                setNewOpen(false);
                // Switch the panel to the new conduct: it is a draft, the next step is approval,
                // and the approve card is on that view.
                setPicked(id);
                refresh();
              }}
            />
          </div>
        )}

        {!list.isPending && !currentId && (
          <EmptyNote
            label={
              list.data?.degraded
                ? list.data.reason || tr("引擎未启用", "Engine not enabled")
                : newOpen
                  ? tr("还没有 conduct —— 上面那张表填完就有了。", "No conduct yet; fill in the form above to create one.")
                  : tr("还没有 conduct。点右上角「新建 conduct」。", "No conduct yet. Click \"New conduct\" at the top right.")
            }
          />
        )}

        {currentId && detail.isPending && <Spinner label={tr("载入面板…", "Loading panel…")} />}
        {currentId && detail.isError && <ErrorNote error={detail.error} />}

        {d && d.degraded && !d.ok && (
          <div className="mb-4 rounded-mast-ctl border border-mast-warn-border bg-mast-warn-bg px-4 py-3 text-sm text-mast-warn">
            {d.reason || tr("面板数据暂时读不到", "Panel data is temporarily unreadable")}
          </div>
        )}

        {d?.ok && (
          <div className="space-y-4">
            <StallBanner d={d} />
            <StatusHeader d={d} />

            {/* The only way out of draft. Without it a conduct would sit in "draft" with
                nothing explaining why it cannot advance. */}
            {d.status === "draft" && (
              <ApproveCard
                busy={busy}
                outcome={approveOutcome}
                onApprove={(by) => approve.mutate(by)}
              />
            )}

            {/* The wait card comes before the timeline: "do I need to do something?" is the
                first question when opening this page. */}
            {d.active_wait && (
              <WaitCard
                wait={d.active_wait}
                busy={busy}
                onAck={(note) =>
                  ack.mutate({ waitId: d.active_wait?.wait_id ?? "", note })
                }
                onWaive={(reason) =>
                  waive.mutate({ waitId: d.active_wait?.wait_id ?? "", reason })
                }
              />
            )}

            {/* Decision card: an escalated verdict. Mutually exclusive with the wait card:
                the backend's pending_decision is null while active_wait is set. */}
            {d.pending_decision && (
              <DecisionCard
                pending={d.pending_decision}
                busy={busy}
                onOverride={(reason) =>
                  override.mutate({
                    decisionId: d.pending_decision?.decision_id ?? 0,
                    reason,
                  })
                }
              />
            )}

            <CurrentCard d={d} />

            <Card>
              <OpBar
                status={d.status}
                attended={d.attended}
                busy={busy}
                onOp={(path) => op.mutate({ path, by: "operator" })}
                onAbort={(reason) => abort.mutate(reason)}
                onAttended={(next) => attended.mutate(next)}
              />
            </Card>

            <NotAvailable d={d} />

            <div>
              <h3 className="mb-2 text-sm font-semibold text-mast-muted">{tr("阶段进度", "Stage progress")}</h3>
              <Timeline d={d} />
            </div>

            <Vitals d={d} />

            <div>
              <h3 className="mb-2 text-sm font-semibold text-mast-muted">{tr("闸门史（近 20 条）", "Gate history (last 20)")}</h3>
              <Card>
                <GateHistory d={d} />
              </Card>
            </div>
          </div>
        )}
      </Section>

      {/* Manual refresh: when the WS is down and polling is throttled in a background tab, there must still be a button. */}
      <div className="flex items-center gap-3">
        <Button variant="ghost" onClick={refresh}>
          {tr("刷新", "Refresh")}
        </Button>
        <span className="text-xs text-mast-faint">
          {ws.warn
            ? tr("实时推送不可用，正在轮询", "Live push unavailable, polling")
            : tr("实时推送已连接（帧只用来触发重读）", "Live push connected (frames only trigger a re-read)")}
        </span>
      </div>
      {node}
    </div>
  );
}
