import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";
import { Card, Badge, Spinner, ErrorNote, DegradedNote, EmptyNote } from "@/components/ui";
import { Button, useToast } from "@/components/controls";
import { tr } from "@/lib/i18n";
import type { components } from "@/api/schema";

type IngestResponse = components["schemas"]["IngestResponse"];

// Full-text request board — GET /api/literature/fetch-board. The literature agent
// posts a full-text request here when the big library only has the abstract; the
// operator satisfies it by uploading the PDF right on the request row.
// Resolve via POST /api/literature/fetch-board/{request_id}/resolve
// {action:done|dismissed}.
//
// Uploading in place, rather than sending the operator to the ingest sub-page
// with a copied work_id, matters for more than convenience: the upload carries
// the request's own work_id, so the paper is filed against the experiment frozen
// on that request and the requesting agent is woken up. A hand-retyped id in
// another form could break that chain.

const STATUS_ICON: Record<string, string> = {
  pending: "🟡",
  fulfilled: "✅",
  failed: "🔴",
  dismissed: "⚪",
};

const STATUS_TONE: Record<string, string> = {
  pending: "WARN",
  fulfilled: "AUTO",
  failed: "DANGEROUS",
  dismissed: "INFO",
};

const FILTERS = [
  { id: "", label: tr("全部", "All") },
  { id: "pending", label: tr("待处理", "Pending") },
  { id: "fulfilled", label: tr("已满足", "Fulfilled") },
  { id: "failed", label: tr("失败", "Failed") },
  { id: "dismissed", label: tr("已忽略", "Dismissed") },
] as const;

/** Satisfy one request in place: upload its full text, then optionally its SI. */
function SatisfyRow({
  workId,
  onDone,
}: {
  workId: string;
  onDone: (msg: string, ok: boolean) => void;
}) {
  const qc = useQueryClient();
  const pdfInput = useRef<HTMLInputElement>(null);
  const siInput = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState<"" | "pdf" | "si">("");

  const uploadPdf = async (file: File) => {
    setBusy("pdf");
    try {
      const fd = new FormData();
      fd.append("file", file);
      fd.append("work_id", workId);
      fd.append("promote", "true");
      const resp = await fetch("/api/literature/upload-pdf", { method: "POST", body: fd });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const d = (await resp.json()) as IngestResponse;
      if (d.ok) {
        const woke = (d.fulfilled_requests ?? 0) > 0;
        onDone(
          woke
            ? tr(
                "全文已入库，取文请求已关闭 —— 文献 agent 会自动继续之前的工作。",
                "Full text ingested and the request closed — the literature agent will resume its earlier work.",
              )
            : tr("全文已入库。", "Full text ingested."),
          true,
        );
      } else {
        onDone(d.detail || d.status || tr("入库失败", "Ingest failed"), false);
      }
      qc.invalidateQueries({ queryKey: ["literature", "fetch-board"] });
      qc.invalidateQueries({ queryKey: ["literature", "libraries"] });
    } catch (e) {
      onDone(tr(`上传失败：${String((e as Error)?.message ?? e)}`, `Upload failed: ${String((e as Error)?.message ?? e)}`), false);
    } finally {
      setBusy("");
      if (pdfInput.current) pdfInput.current.value = "";
    }
  };

  const uploadSi = async (file: File) => {
    setBusy("si");
    try {
      const fd = new FormData();
      fd.append("file", file);
      fd.append("work_id", workId);
      fd.append("label", file.name);
      const resp = await fetch("/api/literature/attach-si", { method: "POST", body: fd });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const d = (await resp.json()) as { ok?: boolean; error?: string; duplicate?: boolean };
      if (d.ok) onDone(d.duplicate ? tr("该补充材料已经挂过了。", "This supplementary file is already attached.") : tr("补充材料已附加。", "Supplementary file attached."), true);
      else onDone(d.error || tr("附加失败", "Attach failed"), false);
      qc.invalidateQueries({ queryKey: ["literature", "attachments", workId] });
    } catch (e) {
      onDone(tr(`附加失败：${String((e as Error)?.message ?? e)}`, `Attach failed: ${String((e as Error)?.message ?? e)}`), false);
    } finally {
      setBusy("");
      if (siInput.current) siInput.current.value = "";
    }
  };

  return (
    <div className="mt-1.5 flex flex-wrap items-center gap-2 text-xs">
      <input
        ref={pdfInput}
        type="file"
        accept="application/pdf,.pdf"
        disabled={!workId || busy !== ""}
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) void uploadPdf(f);
        }}
        className="max-w-[15rem] text-xs text-mast-muted file:mr-2 file:rounded file:border-0 file:bg-mast-accent/20 file:px-2 file:py-0.5 file:text-xs file:text-mast-accent disabled:opacity-40"
        title={tr("选择这篇论文的全文 PDF，直接满足该请求", "Choose the full-text PDF of this paper to fulfil the request")}
      />
      <input
        ref={siInput}
        type="file"
        accept="application/pdf,.pdf"
        disabled={!workId || busy !== ""}
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) void uploadSi(f);
        }}
        className="max-w-[15rem] text-xs text-mast-muted file:mr-2 file:rounded file:border-0 file:bg-mast-bg/60 file:px-2 file:py-0.5 file:text-xs file:text-mast-muted disabled:opacity-40"
        title={tr("附加补充材料（SI）—— 需要先上传正文", "Attach supplementary information (SI) — upload the main text first")}
      />
      <span className="text-mast-muted">
        {busy === "pdf"
          ? tr("上传全文中…", "Uploading full text…")
          : busy === "si"
            ? tr("附加 SI 中…", "Attaching SI…")
            : tr("左：全文 PDF ｜ 右：补充材料 SI", "Left: full-text PDF | Right: supplementary SI")}
      </span>
    </div>
  );
}

export function FetchBoard() {
  const [status, setStatus] = useState<string>("");
  const { toast, node: toastNode } = useToast();
  const qc = useQueryClient();

  const resolveM = useMutation({
    mutationFn: async (vars: { requestId: string; action: "done" | "dismissed" }) => {
      const { data, error } = await api.POST(
        "/api/literature/fetch-board/{request_id}/resolve",
        {
          params: { path: { request_id: vars.requestId } },
          body: { action: vars.action, note: "" },
        },
      );
      if (error) throw error;
      return data;
    },
    onSuccess: (d, vars) => {
      if (d?.degraded) {
        toast(tr("取文请求板后端不可用，未能处理。", "Fetch board backend unavailable; request not processed."), "err");
      } else if (d?.ok) {
        toast(vars.action === "done" ? tr("已标记完成", "Marked done") : tr("已忽略", "Dismissed"), "ok");
      } else {
        toast(d?.message || tr("未能处理该请求", "Could not process the request"), "err");
      }
      qc.invalidateQueries({ queryKey: ["literature", "fetch-board"] });
    },
    onError: (e) =>
      toast(tr(`处理失败：${String((e as Error)?.message ?? e)}`, `Failed: ${String((e as Error)?.message ?? e)}`), "err"),
  });

  const copyWorkId = async (wid: string) => {
    if (!wid) return;
    try {
      await navigator.clipboard.writeText(wid);
      toast(tr(`已复制 work_id：${wid}`, `Copied work_id: ${wid}`), "ok");
    } catch {
      toast(tr("复制失败（剪贴板不可用）", "Copy failed (clipboard unavailable)"), "err");
    }
  };

  const q = useQuery({
    queryKey: ["literature", "fetch-board", status],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/literature/fetch-board", {
        params: { query: { status: status || null } },
      });
      if (error) throw error;
      return data;
    },
  });

  const reqs = q.data?.requests ?? [];

  return (
    <Card>
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <h3 className="text-sm font-semibold">{tr("取文请求板", "Full-text request board")}</h3>
          {q.data && q.data.pending_count > 0 && (
            <Badge tone="WARN">{q.data.pending_count} {tr("待处理", "pending")}</Badge>
          )}
        </div>
        <div className="flex flex-wrap gap-1">
          {FILTERS.map((f) => (
            <button
              key={f.id}
              onClick={() => setStatus(f.id)}
              className={
                "rounded px-2 py-1 text-xs " +
                (status === f.id
                  ? "bg-mast-accent/20 text-mast-accent"
                  : "text-mast-muted hover:bg-mast-bg/40 hover:text-mast-text")
              }
            >
              {f.label}
            </button>
          ))}
          <Button variant="ghost" onClick={() => q.refetch()}>
            {tr("刷新", "Refresh")}
          </Button>
        </div>
      </div>

      {q.isPending && <Spinner />}
      {q.isError && <ErrorNote error={q.error} />}
      {q.data?.degraded && <DegradedNote what={tr("取文请求板", "Full-text request board")} />}
      {q.data && !q.data.degraded && reqs.length === 0 && (
        <EmptyNote label={tr("取文请求板为空。文献 agent 需要某篇全文时会在此提交请求，你上传 PDF 或取文即可满足。", "The request board is empty. When the literature agent needs the full text of a paper it posts a request here; upload the PDF or fetch it to fulfil it.")} />
      )}

      {q.data && !q.data.degraded && reqs.length > 0 && (
        <div className="divide-y divide-mast-border overflow-hidden rounded border border-mast-border">
          {reqs.map((r) => (
            <div key={r.request_id || r.work_id} className="px-3 py-2 text-sm">
              <div className="flex flex-wrap items-center gap-1.5">
                <span>{STATUS_ICON[r.status] ?? "•"}</span>
                <code className="text-mast-accent">{r.work_id || tr("(无 work_id)", "(no work_id)")}</code>
                {r.title && <span className="text-mast-text">«{r.title}»</span>}
                <Badge tone={STATUS_TONE[r.status] ?? "INFO"}>{r.status}</Badge>
                <span className="text-xs text-mast-muted">
                  {r.requested_by}
                  {r.request_id && (
                    <>
                      {" · "}
                      <code>{r.request_id}</code>
                    </>
                  )}
                </span>
              </div>
              {r.doi && (
                <div className="mt-0.5 text-xs text-mast-muted">
                  DOI <code>{r.doi}</code>
                </div>
              )}
              {r.reason && (
                <div className="mt-0.5 text-mast-muted">{tr("理由：", "Reason: ")}{r.reason}</div>
              )}
              {r.note && (
                <div className="mt-0.5 text-xs text-mast-muted">{r.note}</div>
              )}
              <div className="mt-1.5 flex flex-wrap gap-1">
                <button
                  onClick={() => copyWorkId(r.work_id)}
                  disabled={!r.work_id}
                  className="rounded px-2 py-0.5 text-xs text-mast-accent hover:bg-mast-accent/20 disabled:opacity-30"
                >
                  {tr("复制 work_id", "Copy work_id")}
                </button>
                <button
                  onClick={() =>
                    r.request_id &&
                    resolveM.mutate({ requestId: r.request_id, action: "done" })
                  }
                  disabled={!r.request_id || r.status !== "pending" || resolveM.isPending}
                  title={
                    r.request_id
                      ? tr("标记此取文请求为已完成", "Mark this full-text request as done")
                      : tr("该请求缺少 request_id，无法处理", "This request has no request_id and cannot be processed")
                  }
                  className="rounded px-2 py-0.5 text-xs text-mast-accent hover:bg-mast-accent/20 disabled:opacity-30"
                >
                  {tr("标记完成", "Mark done")}
                </button>
                <button
                  onClick={() =>
                    r.request_id &&
                    resolveM.mutate({
                      requestId: r.request_id,
                      action: "dismissed",
                    })
                  }
                  disabled={!r.request_id || r.status !== "pending" || resolveM.isPending}
                  title={
                    r.request_id
                      ? tr("忽略此取文请求", "Dismiss this full-text request")
                      : tr("该请求缺少 request_id，无法处理", "This request has no request_id and cannot be processed")
                  }
                  className="rounded px-2 py-0.5 text-xs text-mast-muted hover:bg-mast-bg/40 hover:text-mast-text disabled:opacity-30"
                >
                  {tr("忽略", "Dismiss")}
                </button>
              </div>
              {r.status === "pending" && (
                <SatisfyRow
                  workId={r.work_id ?? ""}
                  onDone={(msg, ok) => toast(msg, ok ? "ok" : "err")}
                />
              )}
            </div>
          ))}
        </div>
      )}

      <p className="mt-3 px-1 text-xs text-mast-muted">
        {tr(
          "提示：直接在请求行选择全文 PDF 即可满足它 —— 论文会入大库、请求自动关闭，提出请求的文献 agent 会自动继续之前的工作。右侧可再附加补充材料（SI，需先传正文），精读时会一并读。也可切到「摄取与取文」子页按 DOI 取文；无需取文则点「忽略」（仅对待处理请求可用）。「标记完成」只改状态、不代表全文已入库，因此不会唤醒 agent。",
          "Tip: pick the full-text PDF right on the request row to fulfil it — the paper is added to the big library, the request closes, and the requesting literature agent resumes its earlier work. You can also attach supplementary information (SI, main text first) with the right-hand picker; it is read along with the paper. Or switch to the Ingest & fetch sub-page to fetch by DOI; use \"Dismiss\" if no fetch is needed (pending requests only). \"Mark done\" only changes the status — it does not mean the full text was ingested, so it does not wake the agent.",
        )}
      </p>
      {toastNode}
    </Card>
  );
}
