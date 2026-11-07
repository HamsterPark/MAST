import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";
import { Card, Badge, Spinner, ErrorNote, DegradedNote } from "@/components/ui";
import { Field, TextField, Toggle, Button } from "@/components/controls";
import { LibraryRef, LibraryTargetNote } from "@/components/literature/LibraryTargetNote";
import { tr } from "@/lib/i18n";
import type { components } from "@/api/schema";

type IngestResponse = components["schemas"]["IngestResponse"];

// Ingest readiness probe (GET /api/literature/ingest-status). Surfaces the
// dependency/key requirements BEFORE the user spends an upload.

export function IngestReadiness() {
  const q = useQuery({
    queryKey: ["literature", "ingest-status"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/literature/ingest-status");
      if (error) throw error;
      return data;
    },
  });

  const s = q.data;
  return (
    <Card>
      <div className="mb-2 flex items-center justify-between">
        <h3 className="text-sm font-semibold">{tr("摄取就绪", "Ingest readiness")}</h3>
        <Button variant="ghost" onClick={() => q.refetch()}>
          {tr("刷新", "Refresh")}
        </Button>
      </div>
      {q.isPending && <Spinner />}
      {q.isError && <ErrorNote error={q.error} />}
      {s?.degraded && <DegradedNote what={tr("摄取就绪探针", "Ingest readiness probe")} />}
      {s && !s.degraded && (
        <div className="space-y-2">
          <p
            className={
              "text-sm " + (s.available ? "text-mast-auto" : "text-mast-warn")
            }
          >
            {s.detail}
          </p>
          <div className="flex flex-wrap gap-2 text-xs">
            <Badge tone={s.available ? "AUTO" : "DANGEROUS"}>
              numpy/pandas {s.available ? "✓" : "✗"}
            </Badge>
            <Badge tone={s.can_extract_pdf ? "AUTO" : "WARN"}>
              PyMuPDF {s.can_extract_pdf ? "✓" : "✗"}
            </Badge>
            <Badge tone={s.has_embedder_key ? "AUTO" : "WARN"}>
              DashScope key {s.has_embedder_key ? "✓" : "✗"}
            </Badge>
            <Badge tone={s.can_fetch ? "AUTO" : "WARN"}>
              {tr("取文", "Fetch")}(httpx) {s.can_fetch ? "✓" : "✗"}
            </Badge>
          </div>
        </div>
      )}
    </Card>
  );
}

// PDF ingest by server path (advanced) — POST /api/literature/ingest. Takes a
// SERVER-visible pdf_path (e.g. a file already on the machine, or a path an
// agent fetched). For the common case of uploading a PDF from your browser, use
// the "Upload PDF" panel above (POST /api/literature/upload-pdf, multipart) — this
// server-path route stays for files that are already on disk.

export function IngestPanel({ libraryId }: { libraryId: string | null }) {
  const qc = useQueryClient();
  const [pdfPath, setPdfPath] = useState("");
  const [workId, setWorkId] = useState("");
  const [firstAuthor, setFirstAuthor] = useState("");
  const [year, setYear] = useState("");
  const [promote, setPromote] = useState(true);

  const m = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/literature/ingest", {
        body: {
          pdf_path: pdfPath.trim(),
          work_id: workId.trim(),
          library_id: libraryId ?? "",
          source: "user_pdf",
          promote,
          first_author: firstAuthor.trim(),
          year: year.trim(),
        },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["literature", "libraries"] });
      qc.invalidateQueries({ queryKey: ["literature", "fetch-board"] });
    },
  });

  const res = m.data;
  return (
    <Card>
      <h3 className="mb-2 text-sm font-semibold">{tr("PDF 摄取（服务器路径 · 高级）", "Ingest PDF (server path · advanced)")}</h3>
      <p className="mb-3 text-xs text-mast-muted">
        {tr(
          "摄取一篇 PDF（服务器可见路径；浏览器上传请用上方「上传 PDF」）→ 摘要促进进大库",
          "Ingest a PDF (server-visible path; to upload from your browser use \"Upload PDF\" above) → abstract promoted into the big library",
        )}
        {libraryId ? (
          <>
            {tr("，并作为指针加入库 ", ", and added as a pointer to library ")}
            <code>{libraryId}</code>
          </>
        ) : (
          tr("（未指定库 → 见下方落点）", " (no library specified → see the destination below)")
        )}
        {tr("。", ".")}
      </p>
      <LibraryTargetNote libraryId={libraryId} />
      <div className="space-y-3">
        <Field label={tr("PDF 路径（服务器可见）", "PDF path (server-visible)")} hint={tr("例如：D:/papers/foo.pdf", "e.g. D:/papers/foo.pdf")}>
          <TextField value={pdfPath} onChange={setPdfPath} placeholder="…/paper.pdf" mono />
        </Field>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <Field label={tr("work_id（可选）", "work_id (optional)")} hint={tr("留空 → 解析/铸造", "Leave empty → resolve / mint")}>
            <TextField value={workId} onChange={setWorkId} placeholder="W…" mono />
          </Field>
          <Field label={tr("第一作者（铸造提示，可选）", "First author (minting hint, optional)")}>
            <TextField value={firstAuthor} onChange={setFirstAuthor} />
          </Field>
          <Field label={tr("年份（铸造提示，可选）", "Year (minting hint, optional)")}>
            <TextField value={year} onChange={setYear} />
          </Field>
        </div>
        <label className="flex items-center gap-2 text-sm text-mast-muted">
          <Toggle checked={promote} onChange={setPromote} label="promote" />
          {tr("促进摘要进大库", "Promote abstract into the big library")}
        </label>
        <Button
          variant="primary"
          disabled={!pdfPath.trim() || m.isPending}
          onClick={() => m.mutate()}
        >
          {m.isPending ? tr("摄取中…", "Ingesting…") : tr("摄取 PDF", "Ingest PDF")}
        </Button>
      </div>
      <IngestResult result={res} isError={m.isError} error={m.error} />
    </Card>
  );
}

function IngestResult({
  result,
  isError,
  error,
}: {
  result?: IngestResponse;
  isError: boolean;
  error: unknown;
}) {
  if (isError)
    return (
      <p className="mt-3 text-sm text-mast-danger">
        {tr("摄取失败：", "Ingest failed: ")}{String((error as Error)?.message ?? error)}
      </p>
    );
  const r = result;
  if (!r) return null;
  return (
    <div className="mt-3 space-y-1 text-sm">
      <div className="flex items-center gap-2">
        <Badge tone={r.ok ? "AUTO" : r.degraded ? "WARN" : "DANGEROUS"}>
          {r.status || (r.ok ? "ingested" : "failed")}
        </Badge>
        {r.promoted && <Badge tone="INFO">{tr("已促进", "Promoted")}</Badge>}
      </div>
      {r.degraded && <DegradedNote what={tr("摄取后端", "Ingest backend")} />}
      {r.work_id && (
        <p className="text-mast-muted">
          work_id <code>{r.work_id}</code>
          {r.title && <> · {r.title}</>}
          {typeof r.n_chunks === "number" && r.n_chunks > 0 && (
            <> · {r.n_chunks} {tr("块", "chunks")}</>
          )}
          {r.library_id && (
            <>
              {" "}
              · {tr("库", "library")} <LibraryRef libraryId={r.library_id} />
            </>
          )}
        </p>
      )}
      {r.detail && <p className="text-mast-muted">{r.detail}</p>}
    </div>
  );
}

// Fetch by DOI/URL — POST /api/literature/fetch. Best-effort, ToS-respecting full-text
// fetch (auto_oa resolves an OA PDF from the DOI). No paywall bypass exists in
// the core. On success it returns a pdf_path the user can then ingest above.

export function FetchPanel({ onFetchedPath }: { onFetchedPath?: (path: string) => void }) {
  const [doiOrUrl, setDoiOrUrl] = useState("");
  const [oaUrl, setOaUrl] = useState("");
  const [autoOa, setAutoOa] = useState(true);

  const m = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/literature/fetch", {
        body: {
          doi_or_url: doiOrUrl.trim(),
          oa_url: oaUrl.trim(),
          auto_oa: autoOa,
        },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (d) => {
      if (d?.pdf_path && onFetchedPath) onFetchedPath(d.pdf_path);
    },
  });

  const res = m.data;
  return (
    <Card>
      <h3 className="mb-2 text-sm font-semibold">{tr("DOI / URL 取文（实验性）", "Fetch by DOI / URL (experimental)")}</h3>
      <p className="mb-3 text-xs text-mast-muted">
        {tr(
          "尽力且尊重 robots/ToS 地获取开放获取全文；不绕付费墙。取到后可在上方摄取。",
          "Best-effort retrieval of open-access full text that respects robots/ToS; paywalls are not bypassed. Once fetched, ingest it above.",
        )}
      </p>
      <div className="space-y-3">
        <Field label={tr("DOI 或 URL", "DOI or URL")} hint={tr("10.xxxx/… 或 https://…", "10.xxxx/… or https://…")}>
          <TextField value={doiOrUrl} onChange={setDoiOrUrl} placeholder="10.1103/PhysRevLett…" mono />
        </Field>
        <Field label={tr("已知 OA PDF URL（可选，优先尝试）", "Known OA PDF URL (optional, tried first)")}>
          <TextField value={oaUrl} onChange={setOaUrl} placeholder="https://…/paper.pdf" mono />
        </Field>
        <label className="flex items-center gap-2 text-sm text-mast-muted">
          <Toggle checked={autoOa} onChange={setAutoOa} label="auto_oa" />
          {tr("从 DOI 自动解析 OA PDF", "Resolve an OA PDF from the DOI automatically")}
        </label>
        <Button
          variant="primary"
          disabled={!doiOrUrl.trim() || m.isPending}
          onClick={() => m.mutate()}
        >
          {m.isPending ? tr("取文中…", "Fetching…") : tr("取文", "Fetch")}
        </Button>
      </div>
      {m.isError && (
        <p className="mt-3 text-sm text-mast-danger">
          {tr("取文失败：", "Fetch failed: ")}{String((m.error as Error)?.message ?? m.error)}
        </p>
      )}
      {res && (
        <div className="mt-3 space-y-1 text-sm">
          <div className="flex items-center gap-2">
            <Badge tone={res.pdf_path ? "AUTO" : res.degraded ? "WARN" : "WARN"}>
              {res.status}
            </Badge>
            {res.source && <Badge tone="INFO">{res.source}</Badge>}
          </div>
          {res.degraded && <DegradedNote what={tr("取文后端", "Fetch backend")} />}
          {res.pdf_path && (
            <p className="text-mast-muted">
              {tr("已取到全文 → ", "Full text fetched → ")}<code>{res.pdf_path}</code>
              {onFetchedPath && tr("（已填入上方 PDF 路径）", " (filled into the PDF path above)")}
            </p>
          )}
          {res.message && <p className="text-mast-muted">{res.message}</p>}
          {res.url && (
            <p className="text-xs text-mast-muted">
              {tr("来源：", "Source: ")}<code>{res.url}</code>
            </p>
          )}
        </div>
      )}
    </Card>
  );
}
