import { useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";
import { Card, Badge, DegradedNote } from "@/components/ui";
import { Field, TextField, Toggle, Button } from "@/components/controls";
import { LibraryRef, LibraryTargetNote } from "@/components/literature/LibraryTargetNote";
import { tr } from "@/lib/i18n";
import type { components } from "@/api/schema";

type IngestResponse = components["schemas"]["IngestResponse"];

// Direct entry management and PDF attachment.
//   PdfUploadPanel   — pick a PDF in the browser → multipart POST
//                      /api/literature/upload-pdf → ingested into the big library
//                      + a pointer added to the chosen library.
//   ManualEntryPanel — hand-enter title/abstract/authors → POST
//                      /api/literature/manual-entry for a paper not in OpenAlex
//                      and with no PDF (mints a local:<hash> work_id).

// ── shared result renderer ───────────────────────────────────────────────────
function IngestResultView({
  result,
  isError,
  error,
  label,
}: {
  result?: IngestResponse;
  isError: boolean;
  error: unknown;
  label: string;
}) {
  if (isError)
    return (
      <p className="mt-3 text-sm text-mast-danger">
        {label}{String((error as Error)?.message ?? error)}
      </p>
    );
  const r = result;
  if (!r) return null;
  const ok = r.ok;
  return (
    <div className="mt-3 space-y-1 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={ok ? "AUTO" : r.degraded ? "WARN" : "DANGEROUS"}>
          {r.status || (ok ? "ingested" : "failed")}
        </Badge>
        {r.promoted && <Badge tone="INFO">{tr("已促进大库", "Promoted to big library")}</Badge>}
        {r.pointer_added && <Badge tone="INFO">{tr("已加入库指针", "Pointer added")}</Badge>}
        {r.fulfilled_requests > 0 && (
          <Badge tone="INFO">{tr(`满足 ${r.fulfilled_requests} 条取文请求`, `Fulfilled ${r.fulfilled_requests} full-text request(s)`)}</Badge>
        )}
      </div>
      {r.degraded && <DegradedNote what={tr("文献库后端", "Literature backend")} />}
      {r.work_id && (
        <p className="text-mast-muted">
          work_id <code>{r.work_id}</code>
          {r.title && <> · {r.title}</>}
          {/* Echo the library actually used, not the (possibly empty) one requested:
              an empty library_id resolves to the effective library. */}
          {r.pointer_library_id && (
            <>
              {" "}
              · {tr("已入库", "Filed in")}{" "}
              <LibraryRef
                libraryId={r.pointer_library_id}
                source={r.pointer_library_source}
              />
              {r.pointer_library_source === "experiment" && tr("（当前实验的专属库）", " (the current experiment's own library)")}
              {r.pointer_library_source === "manual" && tr("（手动指定的库）", " (manually selected library)")}
              {r.pointer_library_source === "fallback" && tr("（默认兜底库）", " (default fallback library)")}
            </>
          )}
          {r.fulltext_ref && (
            <>
              {" "}
              · {tr("全文", "full text")} <code>{r.fulltext_ref}</code>
            </>
          )}
        </p>
      )}
      {r.detail && <p className="text-mast-muted">{r.detail}</p>}
    </div>
  );
}

// ── PDF upload ──────────────────────────────────────────────────────────────────
export function PdfUploadPanel({ libraryId }: { libraryId: string | null }) {
  const qc = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const [workId, setWorkId] = useState("");
  const [firstAuthor, setFirstAuthor] = useState("");
  const [year, setYear] = useState("");
  const [promote, setPromote] = useState(true);
  const fileInput = useRef<HTMLInputElement>(null);

  const m = useMutation({
    mutationFn: async () => {
      if (!file) throw new Error(tr("先选择一个 PDF 文件", "Choose a PDF file first"));
      // The multipart body carries a File → a raw fetch with FormData is the
      // clean path (openapi-fetch types the binary field as string). The
      // response is the same typed IngestResponse the JSON endpoints return.
      const fd = new FormData();
      fd.append("file", file);
      fd.append("library_id", libraryId ?? "");
      fd.append("work_id", workId.trim());
      fd.append("promote", String(promote));
      fd.append("first_author", firstAuthor.trim());
      fd.append("year", year.trim());
      const resp = await fetch("/api/literature/upload-pdf", {
        method: "POST",
        body: fd,
      });
      if (!resp.ok) throw new Error(tr(`上传失败 (HTTP ${resp.status})`, `Upload failed (HTTP ${resp.status})`));
      return (await resp.json()) as IngestResponse;
    },
    onSuccess: (d) => {
      if (d?.ok) {
        setFile(null);
        setWorkId("");
        if (fileInput.current) fileInput.current.value = "";
      }
      qc.invalidateQueries({ queryKey: ["literature", "libraries"] });
      qc.invalidateQueries({ queryKey: ["literature", "fetch-board"] });
    },
  });

  return (
    <Card>
      <h3 className="mb-2 text-sm font-semibold">{tr("上传 PDF", "Upload PDF")}</h3>
      <p className="mb-3 text-xs text-mast-muted">
        {tr(
          "从本机选一个 PDF 上传 → 提取摘要/元数据并促进进大库",
          "Pick a PDF from this machine → its abstract and metadata are extracted and promoted into the big library",
        )}
        {libraryId ? (
          <>
            {tr("，并作为指针加入库 ", ", and added as a pointer to library ")}
            <code>{libraryId}</code>
          </>
        ) : (
          tr("（未指定库 → 见下方落点）", " (no library specified → see the destination below)")
        )}
        {tr(
          "。扫描版/无文本层 PDF 会被识别并拒绝（不会污染大库）。",
          ". Scanned PDFs without a text layer are detected and rejected (so they do not pollute the big library).",
        )}
      </p>
      <LibraryTargetNote libraryId={libraryId} />
      <div className="space-y-3">
        <Field label={tr("PDF 文件", "PDF file")}>
          <input
            ref={fileInput}
            type="file"
            accept="application/pdf,.pdf"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            className="block w-full text-sm text-mast-muted file:mr-3 file:rounded file:border-0 file:bg-mast-accent/20 file:px-3 file:py-1.5 file:text-sm file:text-mast-accent hover:file:bg-mast-accent/30"
          />
        </Field>
        {file && (
          <p className="text-xs text-mast-muted">
            {tr("已选：", "Selected: ")}<code>{file.name}</code> ({(file.size / 1024 / 1024).toFixed(2)} MB)
          </p>
        )}
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
          disabled={!file || m.isPending}
          onClick={() => m.mutate()}
        >
          {m.isPending ? tr("上传摄取中…", "Uploading and ingesting…") : tr("上传并摄取", "Upload and ingest")}
        </Button>
      </div>
      <IngestResultView result={m.data} isError={m.isError} error={m.error} label={tr("上传失败：", "Upload failed: ")} />
    </Card>
  );
}

// ── Manual entry ──────────────────────────────────────────────────────────────
export function ManualEntryPanel({ libraryId }: { libraryId: string | null }) {
  const qc = useQueryClient();
  const [title, setTitle] = useState("");
  const [abstract, setAbstract] = useState("");
  const [authors, setAuthors] = useState("");
  const [year, setYear] = useState("");
  const [doi, setDoi] = useState("");
  const [journal, setJournal] = useState("");

  const m = useMutation({
    mutationFn: async () => {
      if (!title.trim() && !abstract.trim())
        throw new Error(tr("至少填写标题或摘要", "Enter at least a title or an abstract"));
      const { data, error } = await api.POST("/api/literature/manual-entry", {
        body: {
          title: title.trim(),
          abstract: abstract.trim(),
          work_id: "",
          first_author: "",
          authors: authors.trim(),
          year: year.trim(),
          doi: doi.trim(),
          journal: journal.trim(),
          library_id: libraryId ?? "",
        },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (d) => {
      if (d?.ok) {
        setTitle("");
        setAbstract("");
        setAuthors("");
        setYear("");
        setDoi("");
        setJournal("");
      }
      qc.invalidateQueries({ queryKey: ["literature", "libraries"] });
    },
  });

  return (
    <Card>
      <h3 className="mb-2 text-sm font-semibold">{tr("手动新增条目", "Add entry manually")}</h3>
      <p className="mb-3 text-xs text-mast-muted">
        {tr("为不在 OpenAlex、也没有 PDF 的论文手动建条目（会铸造一个 ", "Create an entry by hand for a paper that is not in OpenAlex and has no PDF (a ")}
        <code>local:</code>
        {tr(
          " work_id）。填了摘要即可按 work_id 检索；有嵌入 key 时还会向量化，进而支持语义检索。",
          " work_id is minted). With an abstract it can be looked up by work_id; with an embedding key it is also vectorised, which enables semantic search.",
        )}
        {libraryId ? (
          <>
            {" "}
            {tr("条目会作为指针加入库 ", "The entry is added as a pointer to library ")}
            <code>{libraryId}</code>
            {tr("。", ".")}
          </>
        ) : (
          tr("（未指定库 → 见下方落点）", " (no library specified → see the destination below)")
        )}
      </p>
      <LibraryTargetNote libraryId={libraryId} />
      <div className="space-y-3">
        <Field label={tr("标题", "Title")}>
          <TextField value={title} onChange={setTitle} placeholder={tr("论文标题", "Paper title")} />
        </Field>
        <Field label={tr("摘要", "Abstract")}>
          <textarea
            value={abstract}
            onChange={(e) => setAbstract(e.target.value)}
            rows={4}
            placeholder={tr("摘要 / 概要（用于检索）", "Abstract / summary (used for search)")}
            className="w-full rounded-md border border-mast-border bg-mast-bg px-2.5 py-1.5 text-sm text-mast-text outline-none focus:border-mast-accent"
          />
        </Field>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field label={tr("作者（可选）", "Authors (optional)")}>
            <TextField value={authors} onChange={setAuthors} placeholder="A. Bee, C. Dee" />
          </Field>
          <Field label={tr("期刊 / 会议（可选）", "Journal / conference (optional)")}>
            <TextField value={journal} onChange={setJournal} placeholder="Phys. Rev. Lett." />
          </Field>
          <Field label={tr("年份（可选）", "Year (optional)")}>
            <TextField value={year} onChange={setYear} placeholder="2021" />
          </Field>
          <Field label={tr("DOI（可选）", "DOI (optional)")}>
            <TextField value={doi} onChange={setDoi} placeholder="10.1103/…" mono />
          </Field>
        </div>
        <Button
          variant="primary"
          disabled={(!title.trim() && !abstract.trim()) || m.isPending}
          onClick={() => m.mutate()}
        >
          {m.isPending ? tr("新增中…", "Adding…") : tr("新增条目", "Add entry")}
        </Button>
      </div>
      <IngestResultView result={m.data} isError={m.isError} error={m.error} label={tr("新增失败：", "Add failed: ")} />
    </Card>
  );
}
