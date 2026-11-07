import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";
import { Badge, ErrorNote, Section, Spinner } from "@/components/ui";
import { MarkdownView } from "@/components/MarkdownView";
import { fmtTime } from "@/components/records/shared";
import { tr } from "@/lib/i18n";

// Documents section of the experiment detail page: reports, plans and paper
// drafts that belong to the experiment.
//
// Two relations are kept apart in the UI:
//   primary  - the document is stored in this experiment's folder (0..N per experiment)
//   related  - the document is primarily owned by another experiment but declares
//              a relation to this one (a paper often synthesises several experiments)

const KIND_TONE: Record<string, "INFO" | "WARN" | "AUTO" | "default"> = {
  literature_report: "INFO",
  experiment_plan: "WARN",
  experiment_report: "default",
  paper_draft: "AUTO",
  review: "INFO",
};

function verdictTone(v?: string | null) {
  if (v === "ACCEPT") return "AUTO" as const;
  if (v === "REJECT") return "DANGEROUS" as const;
  if (v === "REVISE") return "WARN" as const;
  return "INFO" as const;
}

export type DocRow = {
  doc_id: string;
  kind: string;
  kind_label?: string | null;
  title: string;
  version?: number | null;
  versions_count?: number | null;
  relation?: string | null;
  experiment_id?: string | null;
  experiment_name?: string | null;
  verdict?: string | null;
  words?: number | null;
  updated_at?: string | null;
  created_by?: string | null;
  conversation_id?: string | null;
  root_kind?: string | null;
  path?: string | null;
};

export function ExperimentDocuments({ experimentId }: { experimentId: string }) {
  const q = useQuery({
    queryKey: ["documents", "experiment", experimentId],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/documents", {
        params: { query: { experiment_id: experimentId } },
      });
      if (error) throw error;
      return data;
    },
    refetchInterval: 60_000,
  });

  if (q.isLoading) return <Section title={tr("文档", "Documents")}><Spinner label={tr("加载文档…", "Loading documents…")} /></Section>;
  if (q.isError) return <Section title={tr("文档", "Documents")}><ErrorNote error={q.error} /></Section>;

  const docs = ((q.data as { documents?: DocRow[] } | undefined)?.documents ??
    []) as DocRow[];
  const primary = docs.filter((d) => (d.relation ?? "primary") === "primary");
  const related = docs.filter((d) => d.relation === "related");

  return (
    <Section
      title={`${tr("文档", "Documents")} (${docs.length})`}
      subtitle={tr(
        "文献报告 / 实验计划 / 实验报告 / 论文草稿 / 评审 —— 都存在这个实验的文件夹里，每次修订都是新版本，永不覆盖。",
        "Literature reports / experiment plans / experiment reports / paper drafts / reviews, all stored in this experiment's folder. Every revision is a new version; nothing is overwritten.",
      )}
    >
      {docs.length === 0 ? (
        <div className="space-y-1 text-sm text-mast-muted">
          <p>{tr("这个实验还没有文档。", "This experiment has no documents yet.")}</p>
          <p className="text-xs">
            {tr(
              "让智能体写一份报告或文献综述后，它会保存到实验文件夹的",
              "When an agent writes a report or literature review, it is saved under ",
            )}{" "}
            <code className="font-mono text-[11px]">reports/</code>{" "}
            {tr("下；实验计划保存到", "in the experiment folder; experiment plans go to")}{" "}
            <code className="font-mono text-[11px]">plans/</code>
            {tr("。", ".")}
          </p>
        </div>
      ) : (
        <div className="space-y-4">
          <DocList rows={primary} />
          {related.length > 0 && (
            <div className="space-y-2">
              <p className="text-xs text-mast-muted">
                {tr("关联文档（主归属在其他实验，声明了与本实验相关）", "Related documents (owned by other experiments, declared related to this one)")}
              </p>
              <DocList rows={related} showOwner />
            </div>
          )}
        </div>
      )}
    </Section>
  );
}

/** Document list with click-to-read full text; shared by the experiment detail page and the global Reports tab. */
export function DocList({ rows, showOwner = false }: { rows: DocRow[]; showOwner?: boolean }) {
  const [openId, setOpenId] = useState<string | null>(null);
  if (rows.length === 0) return null;
  return (
    <ul className="space-y-2">
      {rows.map((d) => (
        <li key={d.doc_id} className="rounded-lg border border-mast-border">
          <button
            className="flex w-full flex-wrap items-center gap-2 px-3 py-2 text-left hover:bg-mast-bg/40"
            onClick={() => setOpenId(openId === d.doc_id ? null : d.doc_id)}
          >
            <Badge tone={KIND_TONE[d.kind] ?? "default"}>
              {d.kind_label || d.kind}
            </Badge>
            <span className="text-sm font-medium text-mast-text">{d.title}</span>
            <span
              className="font-mono text-[11px] text-mast-muted"
              title={tr(`共 ${d.versions_count ?? d.version ?? 1} 个版本`, `${d.versions_count ?? d.version ?? 1} versions`)}
            >
              v{String(d.version ?? 1).padStart(3, "0")}
            </span>
            {d.verdict && <Badge tone={verdictTone(d.verdict)}>{d.verdict}</Badge>}
            {typeof d.words === "number" && (
              <span className="text-[11px] text-mast-muted">{tr(`${d.words} 词`, `${d.words} words`)}</span>
            )}
            {showOwner && d.experiment_name && (
              <span className="text-[11px] text-mast-muted">
                {tr(`属于《${d.experiment_name}》`, `in "${d.experiment_name}"`)}
              </span>
            )}
            {d.created_by && (
              <span className="text-[11px] text-mast-muted" title={tr("谁写的这一版", "Who wrote this version")}>
                {d.created_by.startsWith("agent:")
                  ? d.created_by.slice("agent:".length)
                  : d.created_by === "operator"
                    ? tr("用户", "Operator")
                    : d.created_by}
              </span>
            )}
            <span className="ml-auto text-[11px] text-mast-muted">
              {d.updated_at ? fmtTime(d.updated_at) : ""}
            </span>
          </button>
          {openId === d.doc_id && (
            <DocBody
              docId={d.doc_id}
              path={d.path ?? ""}
              conversationId={d.conversation_id ?? null}
              rootKind={d.root_kind ?? null}
            />
          )}
        </li>
      ))}
    </ul>
  );
}

function DocBody({
  docId,
  path,
  conversationId = null,
  rootKind = null,
}: {
  docId: string;
  path: string;
  conversationId?: string | null;
  rootKind?: string | null;
}) {
  const [version, setVersion] = useState<number | null>(null);
  const q = useQuery({
    queryKey: ["document", docId, version],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/documents/{doc_id}", {
        params: {
          path: { doc_id: docId },
          ...(version ? { query: { version } } : {}),
        },
      });
      if (error) throw error;
      return data;
    },
  });

  if (q.isLoading) return <Spinner label={tr("加载全文…", "Loading full text…")} />;
  if (q.isError) return <ErrorNote error={q.error} />;

  const d = q.data as
    | { content?: string | null; body?: string | null; detail?: string | null;
        versions?: { version: number; created_by?: string | null;
                     created_at?: string | null; words?: number | null }[] }
    | undefined;
  // Body field name compatibility: the current contract uses `content`, the older one `body`.
  const text = d?.content ?? d?.body ?? "";
  const versions = d?.versions ?? [];

  if (!text && d?.detail) {
    return <p className="px-3 pb-3 text-xs text-mast-danger">{d.detail}</p>;
  }

  return (
    <div className="border-t border-mast-border">
      <div className="flex flex-wrap items-center gap-2 border-b border-mast-border px-3 py-1.5">
        {versions.length > 1 && (
          <label className="flex items-center gap-1 text-[11px] text-mast-muted">
            {tr("版本", "Version")}
            <select
              className="rounded border border-mast-border bg-transparent px-1 py-0.5 font-mono text-[11px]"
              value={version ?? versions.at(-1)?.version ?? 1}
              onChange={(e) => setVersion(Number(e.target.value))}
            >
              {[...versions].reverse().map((v) => (
                <option key={v.version} value={v.version}>
                  v{String(v.version).padStart(3, "0")}
                  {v.created_by ? ` · ${v.created_by}` : ""}
                </option>
              ))}
            </select>
          </label>
        )}
        {/* Full path on disk */}
        <p
          className="select-all font-mono text-[10px] text-mast-muted"
          title={tr("文件在磁盘上的完整路径（可直接复制）", "Full path of the file on disk (copyable)")}
        >
          {path}
        </p>
        {/* Which conversation produced it. Transcripts are in the experiment folder under chats/, named with the first 8 characters of this id. */}
        {conversationId && (
          <p
            className="select-all font-mono text-[10px] text-mast-muted"
            title={tr(
              "产出这份文档的对话 id —— 转录在实验文件夹的 chats/ 里，文件名以它的前 8 位开头",
              "Id of the conversation that produced this document; the transcript is in the experiment folder under chats/, with a file name starting with its first 8 characters",
            )}
          >
            {tr("对话", "Conversation")} {conversationId.slice(0, 8)}
          </p>
        )}
      </div>
      <div className="max-h-[520px] overflow-auto px-3 py-2">
        <MarkdownView text={text} />
      </div>
      <ExportRow docId={docId} version={version} />
      <DiscardControls docId={docId} rootKind={rootKind} />
    </div>
  );
}

/** Export in three formats. The exported version must be the one on screen, hence `version` is passed along. */
function ExportRow({ docId, version }: { docId: string; version: number | null }) {
  // a.download is deliberately not set, so the server's content-disposition
  // `filename*=UTF-8''<title>_vNNN.<ext>` applies and downloads get meaningful names.
  function go(format: "md" | "html" | "docx") {
    const q = new URLSearchParams({ format });
    if (version) q.set("version", String(version));
    const a = document.createElement("a");
    a.href = `/api/documents/${encodeURIComponent(docId)}/export?${q}`;
    document.body.appendChild(a);
    a.click();
    a.remove();
  }
  return (
    <div className="flex flex-wrap items-center gap-2 border-t border-mast-border px-3 py-1.5">
      <span className="text-[11px] text-mast-muted">{tr("导出", "Export")}</span>
      <button
        className="rounded border border-mast-border px-2 py-0.5 text-[11px] text-mast-muted hover:bg-mast-bg/40"
        onClick={() => go("md")}
        title={tr("原始 markdown —— 工作格式，图是相对链接（离开实验文件夹会断）", "Raw markdown, the working format; figures are relative links (they break outside the experiment folder)")}
      >
        Markdown
      </button>
      <button
        className="rounded border border-mast-border px-2 py-0.5 text-[11px] text-mast-muted hover:bg-mast-bg/40"
        onClick={() => go("html")}
        title={tr("自包含 HTML —— 图 base64 内嵌，单个文件双击就开，发给别人「看」", "Self-contained HTML with figures embedded as base64; a single file that opens on double-click, for sharing to read")}
      >
        HTML
      </button>
      <button
        className="rounded border border-mast-border px-2 py-0.5 text-[11px] text-mast-muted hover:bg-mast-bg/40"
        onClick={() => go("docx")}
        title={tr("Word 文档 —— 全文挂 Word 内置样式，收件人可用修订/批注来「改」，也能套期刊模板", "Word document using built-in Word styles; recipients can edit with tracked changes and comments, and apply a journal template")}
      >
        Word
      </button>
      <span className="text-[11px] text-mast-muted">
        {tr("HTML 发给别人看，Word 发给别人改。每次导出都在实验的", "Send HTML to be read, Word to be edited. Every export leaves a timestamped copy in the experiment's")}{" "}
        <code className="font-mono text-[10px]">exports/</code>
        {tr(" 里留一份带时间戳的档。", " folder.")}
      </span>
    </div>
  );
}

/** Discard / restore. There is no delete: discarding only moves the directory into `_discarded`, no bytes are lost. */
function DiscardControls({
  docId,
  rootKind,
}: {
  docId: string;
  rootKind?: string | null;
}) {
  const qc = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const [notice, setNotice] = useState("");
  const invalidate = () => qc.invalidateQueries({ queryKey: ["documents"] });

  const discard = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/documents/{doc_id}/discard", {
        params: { path: { doc_id: docId }, query: { reason: "用户在界面上废弃" } },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (r) => {
      setConfirming(false);
      setNotice((r as { detail?: string })?.detail || tr("已废弃（可恢复）", "Discarded (restorable)"));
      invalidate();
    },
    onError: (e) => setNotice(tr(`废弃失败：${String((e as Error)?.message ?? e)}`, `Discard failed: ${String((e as Error)?.message ?? e)}`)),
  });

  const restore = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/documents/{doc_id}/restore", {
        params: { path: { doc_id: docId }, query: { experiment_id: "" } },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (r) => {
      setNotice((r as { detail?: string })?.detail || tr("已恢复到未归属区，可再认领", "Restored to the unfiled area; it can be claimed again"));
      invalidate();
    },
    onError: (e) => setNotice(tr(`恢复失败：${String((e as Error)?.message ?? e)}`, `Restore failed: ${String((e as Error)?.message ?? e)}`)),
  });

  const discarded = rootKind === "discarded";
  return (
    <div className="flex flex-wrap items-center gap-2 border-t border-mast-border px-3 py-1.5">
      {discarded ? (
        <button
          className="rounded border border-mast-border px-2 py-0.5 text-[11px] text-mast-muted hover:bg-mast-bg/40"
          onClick={() => restore.mutate()}
          disabled={restore.isPending}
        >
          {tr("恢复这份文档", "Restore this document")}
        </button>
      ) : confirming ? (
        <>
          <button
            className="rounded border border-mast-warn-border bg-mast-warn-bg px-2 py-0.5 text-[11px] text-mast-warn"
            onClick={() => discard.mutate()}
            disabled={discard.isPending}
          >
            {tr("确认废弃", "Confirm discard")}
          </button>
          <button
            className="rounded border border-mast-border px-2 py-0.5 text-[11px] text-mast-muted hover:bg-mast-bg/40"
            onClick={() => setConfirming(false)}
          >
            {tr("取消", "Cancel")}
          </button>
          <span className="text-[11px] text-mast-muted">
            {tr("不会删除任何文件 —— 只是从列表里收起来，随时可恢复。", "No files are deleted; the document is only hidden from the list and can be restored at any time.")}
          </span>
        </>
      ) : (
        <button
          className="rounded border border-mast-border px-2 py-0.5 text-[11px] text-mast-muted hover:bg-mast-bg/40"
          onClick={() => setConfirming(true)}
          title={tr("搬进 _discarded 区；不删除任何字节，随时可恢复", "Moves it into the _discarded area; no bytes are deleted and it can be restored at any time")}
        >
          {tr("废弃", "Discard")}
        </button>
      )}
      {notice && <span className="text-[11px] text-mast-muted">{notice}</span>}
    </div>
  );
}
