import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";
import { conductIdFromAgentId, conductPanelHref } from "@/lib/conduct";
import {
  Section,
  Card,
  Badge,
  Spinner,
  ErrorNote,
  DegradedNote,
  EmptyNote,
} from "@/components/ui";
import type { components } from "@/api/schema";
import { useLegacyFeatures } from "@/hooks/useLegacyFeatures";
import { tr } from "@/lib/i18n";
import { OperatorPromptBoard } from "@/components/operator/PromptBoard";
import { OpticalStatePanel } from "@/components/operator/OpticalStatePanel";

// Operator requests page. Above the two boards below sit the structured operator
// prompts (confirm / choice / number / text / attest, answered here or in the
// launcher's desktop popup) and, on an optical station, the manual optical state.
// Both draw nothing until there is something to show, and they do not depend on the
// wishlist query, so a slow or failing wishlist cannot hide a prompt that is waiting.
//
// Two boards over the typed FastAPI seam:
//   1) Wishes / feedback (legacy features only): submission form + list
//      (queued/sent/failed); submissions are forwarded to the update server.
//   2) Agent requests: things an agent (including external agents such as Claude
//      Code via mast_ask_operator) asks the operator to do; they can be marked
//      done or dismissed.
// All field names + enums are type-checked against the generated OpenAPI schema.

type Wish = components["schemas"]["Wish"];
type AgentRequest = components["schemas"]["AgentRequest"];

// ── status → (label, tailwind tone) ─────────────────────────────────────────
const WISH_STATUS: Record<Wish["status"], { label: string; cls: string }> = {
  queued: { label: tr("待发送", "Queued"), cls: "bg-mast-warn-bg text-mast-warn" },
  sent: { label: tr("已上报", "Sent"), cls: "bg-mast-auto-bg text-mast-auto" },
  failed: { label: tr("上报失败", "Failed"), cls: "bg-mast-danger-bg text-mast-danger" },
};

const REQ_STATUS: Record<AgentRequest["status"], { label: string; cls: string }> = {
  pending: { label: tr("待处理", "Pending"), cls: "bg-mast-warn-bg text-mast-warn" },
  done: { label: tr("已完成", "Done"), cls: "bg-mast-auto-bg text-mast-auto" },
  dismissed: { label: tr("已忽略", "Dismissed"), cls: "bg-mast-border text-mast-muted" },
};

// `value` is sent to the backend as the category; `label` is display only.
const WISH_CATEGORIES: { value: string; label: string }[] = [
  { value: "功能建议", label: tr("功能建议", "Feature request") },
  { value: "bug反馈", label: tr("bug反馈", "Bug report") },
  { value: "体验改进", label: tr("体验改进", "UX improvement") },
  { value: "其它", label: tr("其它", "Other") },
];

function fmtTs(iso?: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso).slice(0, 16);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function StatusPill({ label, cls }: { label: string; cls: string }) {
  return <span className={`rounded-full px-2 py-0.5 text-xs ${cls}`}>{label}</span>;
}

// ── data hook (polls so async relay status flips show up) ───────────────────
function useWishlist() {
  return useQuery({
    queryKey: ["wishlist"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/wishlist");
      if (error) throw error;
      return data;
    },
    refetchInterval: 4000,
  });
}

export default function WishlistPage() {
  return (
    <div className="space-y-2">
      <OperatorPromptBoard />
      <OpticalStatePanel />
      <WishlistBoard />
    </div>
  );
}

function WishlistBoard() {
  const qc = useQueryClient();
  const board = useWishlist();

  // Tag each wish with the running app version so the maintainers can see which
  // build it came from.
  const { enabled: legacy } = useLegacyFeatures();
  const health = useQuery({
    queryKey: ["health"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/health");
      if (error) throw error;
      return data;
    },
    staleTime: 5 * 60_000,
  });
  const clientVersion = health.data?.version ?? "";

  // ── submit-wish form state ──
  const [text, setText] = useState("");
  const [category, setCategory] = useState(WISH_CATEGORIES[0]!.value);
  const [formMsg, setFormMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const submitWish = useMutation({
    mutationFn: async (payload: { text: string; category: string }) => {
      const { data, error } = await api.POST("/api/wishlist/wishes", {
        body: { text: payload.text, category: payload.category, client_version: clientVersion },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (data) => {
      setFormMsg({ ok: data.ok, text: data.message || (data.ok ? tr("已提交", "Submitted") : tr("提交失败", "Submit failed")) });
      if (data.ok) setText("");
      qc.invalidateQueries({ queryKey: ["wishlist"] });
    },
    onError: (err) => setFormMsg({ ok: false, text: String((err as Error)?.message ?? err) }),
  });

  // ── resolve-request mutation ──
  const resolveReq = useMutation({
    mutationFn: async (args: {
      id: string;
      action: "done" | "dismissed";
      note: string;
      path: string;
    }) => {
      const { data, error } = await api.POST(
        "/api/wishlist/requests/{request_id}/resolve",
        {
          params: { path: { request_id: args.id } },
          body: { action: args.action, note: args.note, path: args.path },
        },
      );
      if (error) throw error;
      return data;
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["wishlist"] }),
  });

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const t = text.trim();
    if (!t) {
      setFormMsg({ ok: false, text: tr("请先填写内容", "Please enter some text first") });
      return;
    }
    submitWish.mutate({ text: t, category });
  };

  // ── render guards ──
  if (board.isPending) return <Spinner />;
  if (board.isError) return <ErrorNote error={board.error} />;

  const data = board.data!;
  const wishes = data.wishes ?? [];
  const requests = data.requests ?? [];

  const requestsBoard = (
    <Section
      title={
        legacy
          ? tr("Agent 请求（需你处理）", "Agent requests (need your action)")
          : tr("操作员请求", "Operator requests")
      }
      actions={
        data.pending_count > 0 ? (
          <Badge tone="WARN">{tr(`${data.pending_count} 待处理`, `${data.pending_count} pending`)}</Badge>
        ) : undefined
      }
    >
      {!legacy && (
        <p className="mb-3 text-sm text-mast-muted">
          {tr(
            "agent（包括通过 mast_ask_operator 连接的 Claude Code 等外部 agent）需要你操作时的请求会出现在这里，处理后点标记。",
            "Requests from agents, including external agents such as Claude Code (via mast_ask_operator), that need you to do something appear here. Mark them when handled.",
          )}
        </p>
      )}
      {data.degraded && <DegradedNote what={tr("心愿单", "operator requests")} />}
      {!data.degraded && requests.length === 0 && (
        <EmptyNote
          label={tr(
            "暂无来自 agent 的请求。agent 需要你配合时(如上传全文、操作硬件),会出现在这里。",
            "No agent requests. When an agent needs your help (e.g. uploading a full text or operating hardware), it appears here.",
          )}
        />
      )}
      <div className="space-y-2">
        {requests.map((r) => (
          <RequestRow
            key={r.id}
            req={r}
            busy={resolveReq.isPending}
            onResolve={(action, note, path) =>
              resolveReq.mutate({ id: r.id, action, note, path })
            }
          />
        ))}
      </div>
      {resolveReq.isError && (
        <p className="mt-2 text-sm text-mast-danger">
          {tr("处理失败：", "Failed to resolve: ")}
          {String((resolveReq.error as Error)?.message ?? resolveReq.error)}
        </p>
      )}
    </Section>
  );

  if (!legacy) return <div className="space-y-2">{requestsBoard}</div>;

  return (
    <div className="space-y-2">
      <Section title={tr("心愿单 / 反馈", "Wishlist / feedback")}>
        <p className="text-sm text-mast-muted">
          {tr("提交愿望与反馈，会", "Wishes and feedback you submit are ")}
          <strong className="text-mast-text">
            {tr("自动上报到更新服务器", "forwarded to the update server automatically")}
          </strong>
          {tr(
            "供开发者改进。下方「Agent 请求」是各 agent 需要你配合的事项（如文献 agent 请你上传全文、仪器 agent 请你操作硬件），处理完点标记。",
            " for the developers. The agent requests below are things agents need you to do (e.g. the literature agent asks you to upload a full text, the instrument agent asks you to operate hardware); mark them when handled.",
          )}
        </p>
      </Section>

      <Section title={tr("你的愿望 / 反馈", "Your wishes / feedback")}>
        <Card>
          <form onSubmit={onSubmit} className="space-y-3">
            <textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              rows={3}
              placeholder={tr(
                "例如：希望支持批量 STS 自动采集 / 某处用起来卡…",
                "e.g. support batch automatic STS acquisition / something feels slow…",
              )}
              className="w-full resize-y rounded border border-mast-border bg-mast-bg px-3 py-2 text-sm text-mast-text placeholder:text-mast-muted focus:border-mast-accent focus:outline-none"
            />
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
              <span className="text-xs text-mast-muted">{tr("类别", "Category")}</span>
              {WISH_CATEGORIES.map((c) => (
                <label key={c.value} className="flex items-center gap-1.5 text-sm text-mast-text">
                  <input
                    type="radio"
                    name="wish-category"
                    value={c.value}
                    checked={category === c.value}
                    onChange={() => setCategory(c.value)}
                  />
                  {c.label}
                </label>
              ))}
            </div>
            <div className="flex flex-wrap items-center gap-3">
              <button
                type="submit"
                disabled={submitWish.isPending}
                className="rounded bg-mast-accent/20 px-4 py-1.5 text-sm text-mast-accent hover:bg-mast-accent/30 disabled:opacity-50"
              >
                {submitWish.isPending ? tr("提交中…", "Submitting…") : tr("提交并上报", "Submit and send")}
              </button>
              {/* The board polls every 4 s; the manual refresh is for impatience. */}
              <button
                type="button"
                disabled={board.isFetching}
                onClick={() => qc.invalidateQueries({ queryKey: ["wishlist"] })}
                className="rounded border border-mast-border px-4 py-1.5 text-sm text-mast-muted hover:text-mast-text disabled:opacity-50"
              >
                {board.isFetching ? tr("刷新中…", "Refreshing…") : tr("刷新", "Refresh")}
              </button>
              {formMsg && (
                <span className={`text-sm ${formMsg.ok ? "text-mast-auto" : "text-mast-danger"}`}>
                  {formMsg.text}
                </span>
              )}
            </div>
          </form>
        </Card>
      </Section>

      <Section title={tr("我的愿望", "My wishes")}>
        {!data.degraded && wishes.length === 0 && (
          <EmptyNote
            label={tr(
              "尚无愿望/反馈。在上方提交，会自动上报到更新服务器。",
              "No wishes or feedback yet. Submit above; it is forwarded to the update server automatically.",
            )}
          />
        )}
        <div className="space-y-2">
          {wishes.map((w) => (
            <WishRow key={w.id} wish={w} />
          ))}
        </div>
      </Section>

      {requestsBoard}
    </div>
  );
}

// ── single wish card ───────────────────────────────────────────────────────
function WishRow({ wish }: { wish: Wish }) {
  const st = WISH_STATUS[wish.status] ?? { label: wish.status, cls: "bg-mast-border text-mast-muted" };
  return (
    <Card>
      <div className="flex items-center gap-2 text-xs text-mast-muted">
        <StatusPill label={st.label} cls={st.cls} />
        <span>{wish.category}</span>
        <span className="ml-auto">{fmtTs(wish.created_at)}</span>
      </div>
      <p className="mt-2 whitespace-pre-wrap text-sm text-mast-text">{wish.text}</p>
      {wish.server_ack && (
        <p className="mt-1 text-xs text-mast-muted">{tr("服务器回执：", "Server receipt: ")}{wish.server_ack}</p>
      )}
      {wish.error && <p className="mt-1 text-xs text-mast-danger">{wish.error}</p>}
    </Card>
  );
}

// ── single agent-request card (with resolve / dismiss actions) ─────────────
function RequestRow({
  req,
  busy,
  onResolve,
}: {
  req: AgentRequest;
  busy: boolean;
  onResolve: (action: "done" | "dismissed", note: string, path: string) => void;
}) {
  const st = REQ_STATUS[req.status] ?? { label: req.status, cls: "bg-mast-border text-mast-muted" };
  const [note, setNote] = useState("");
  const [path, setPath] = useState("");
  const open = req.status === "pending";

  return (
    <Card>
      <div className="flex items-center gap-2 text-xs text-mast-muted">
        <StatusPill label={st.label} cls={st.cls} />
        <b className="text-mast-accent">{req.agent_id}</b>
        <span>{req.kind}</span>
        {/* Requests raised by conduct link straight to the conduct panel, where the
            other half of the double gate (physical conditions) is visible. */}
        {conductIdFromAgentId(req.agent_id) && (
          <Link
            to={conductPanelHref(conductIdFromAgentId(req.agent_id))}
            className="text-mast-accent underline hover:opacity-80"
          >
            {tr("去 conduct 面板", "Open conduct panel")}
          </Link>
        )}
        {req.experiment_id && <span>· {tr("实验", "Experiment")} {req.experiment_id}</span>}
        <span className="ml-auto">
          #{req.id} · {fmtTs(req.created_at)}
        </span>
      </div>
      <p className="mt-2 whitespace-pre-wrap text-sm text-mast-text">{req.message}</p>
      {req.path && (
        <p className="mt-1 font-mono text-xs text-mast-accent">{tr("路径：", "Path: ")}{req.path}</p>
      )}
      {req.note && <p className="mt-1 text-xs text-mast-auto">{tr("备注：", "Note: ")}{req.note}</p>}
      {open && (
        <div className="mt-3 space-y-2">
          {/* The path is its own field: the most common agent ask is "where is the
              file?", and a path buried in a free-text note is not a reliable channel. */}
          <label className="flex flex-wrap items-center gap-2">
            <span className="shrink-0 text-xs font-medium text-mast-accent">{tr("路径", "Path")}</span>
            <input
              value={path}
              onChange={(e) => setPath(e.target.value)}
              spellCheck={false}
              placeholder={tr(
                "智能体索要的文件/目录，例如 D:\\Data\\stm\\Au111_001.sxm",
                "File or directory the agent asked for, e.g. D:\\Data\\stm\\Au111_001.sxm",
              )}
              className="min-w-0 flex-1 rounded border border-mast-accent/40 bg-mast-bg px-3 py-1.5 font-mono text-xs text-mast-text placeholder:text-mast-muted focus:border-mast-accent focus:outline-none"
            />
          </label>
          <div className="flex flex-wrap items-center gap-2">
            <input
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder={tr(
                "备注（可选）——说明你做了什么 / 还有什么要注意的",
                "Note (optional): what you did / anything to watch out for",
              )}
              className="min-w-0 flex-1 rounded border border-mast-border bg-mast-bg px-3 py-1.5 text-sm text-mast-text placeholder:text-mast-muted focus:border-mast-accent focus:outline-none"
            />
          <button
            type="button"
            disabled={busy}
            onClick={() => onResolve("done", note, path)}
            className="rounded bg-mast-auto-bg px-3 py-1.5 text-sm text-mast-auto hover:bg-mast-auto-border disabled:opacity-50"
          >
            {tr("标记完成", "Mark done")}
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => onResolve("dismissed", note, path)}
            className="rounded bg-mast-border px-3 py-1.5 text-sm text-mast-muted hover:bg-mast-border/70 disabled:opacity-50"
          >
            {tr("忽略", "Dismiss")}
          </button>
          </div>
          <p className="text-[11px] text-mast-muted">
            {tr(
              "答复会实时转发给正在运行的智能体；若当前没有运行中的任务，它会",
              "Your reply is forwarded to the running agent; if no task is running, it is ",
            )}
            <strong className="text-mast-text">{tr("保存下来", "saved")}</strong>
            {tr("，智能体下次运行时自己读回（", " and the agent reads it back on its next run (")}
            <code className="font-mono">check_my_requests</code>
            {tr("）——你不必再手动复述一遍。", "), so you do not need to repeat it.")}
          </p>
        </div>
      )}
      {req.resolved_at && (
        <p className="mt-2 text-xs text-mast-muted">{tr("处理于 ", "Handled ")}{fmtTs(req.resolved_at)}</p>
      )}
    </Card>
  );
}
