import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import { useCurrentScope } from "../../api/scope";
import { libraryLabel } from "../../lib/libraryLabel";
import { tr } from "../../lib/i18n";

/** Banner stating which library a paper will be filed into.
 *
 *  When the ingest and upload panels submit an empty ``library_id``, the target is
 *  the *effective* library (the active experiment's own library when there is one),
 *  not a machine-wide "active library". If the UI did not say so, a user uploading
 *  a PDF would not know where it went, and a paper filed under the wrong
 *  experiment would go unnoticed.
 *
 *  Reads one existing query (``/api/literature/libraries`` with
 *  ``effective_library_id`` and ``effective_source``) and shares its cache with
 *  LibraryManager, so no extra request. Renders nothing while the backend is
 *  degraded or not yet loaded: guessing a target is worse than showing none.
 */
export function LibraryTargetNote({
  libraryId,
  verb,
}: {
  /** Library explicitly selected in the panel; ``null`` = let the backend pick the effective library. */
  libraryId: string | null;
  /** Optional verb phrase replacing the default "Will be added to". */
  verb?: string;
}) {
  const q = useQuery({
    queryKey: ["literature", "libraries"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/literature/libraries");
      if (error) throw error;
      return data;
    },
  });
  // Already polled by the TopBar; shares its cache, so no extra request.
  const scope = useCurrentScope();

  const data = q.data;
  if (!data || data.degraded) return null;

  const libs = data.libraries ?? [];
  const targetId = libraryId || data.effective_library_id || "";
  if (!targetId) return null;
  const explicit = Boolean(libraryId);
  const source = explicit ? "picked" : (data.effective_source ?? "");

  // Prefer the experiment name for experiment libraries; a bare exp_<id8> says
  // nothing. Before the library is lazily created the name is still available:
  // it is the current experiment's name.
  const { text: label, pending } = libraryLabel(
    targetId,
    source,
    libs,
    scope.data?.experiment?.name,
  );

  const why =
    source === "picked"
      ? tr("（你在左侧指定的库）", "(the library you picked)")
      : source === "experiment"
        ? tr("（当前实验的专属库）", "(the current experiment's own library)")
        : source === "manual"
          ? tr("（手动指定；当前没有活跃实验）", "(set manually; no active experiment)")
          : tr("（默认兜底库）", "(the default fallback library)");

  return (
    <p className="mt-2 rounded border border-mast-border bg-mast-bg/40 px-2 py-1.5 text-xs text-mast-muted">
      {verb ? tr(`将${verb}`, `Will ${verb}`) : tr("将加入", "Will be added to")} {label} <span className="opacity-70">{why}</span>
      {" · "}
      <code className="text-mast-accent">{targetId}</code>
      {source === "experiment" && (
        <>
          <br />
          {tr("书目记在这个实验的文件夹里（", "The bibliography is stored in this experiment's folder (")}
          <code>library/members.jsonl</code>
          {tr(
            "）；论文本身和全文都在本机大库，库只持有指针。",
            "); the papers and their full text live in the local big library, and a library only holds pointers.",
          )}
          {pending && tr(" 这个库还没建出来 —— 这一次收录就会建它。", " This library does not exist yet; it will be created by this addition.")}
        </>
      )}
    </p>
  );
}

/** Inline reference to the library a paper was filed into: name first, id after.
 *
 *  The "will be added to" banners and the "was added to" echoes (ingest and
 *  upload results) must both show the library name: right after an action, when
 *  the user checks that the paper went to the right place, a bare id is the least
 *  informative thing to show.
 *
 *  Shares the queryKey with :func:`LibraryTargetNote` and the same
 *  `libraryLabel`, so there is one naming rule and no extra request. When the name
 *  cannot be resolved it falls back to the bare id: a missing name is an
 *  inconvenience, a missing id is lost information.
 */
export function LibraryRef({
  libraryId,
  source = "",
}: {
  libraryId: string;
  /** `pointer_library_source` from the backend; leave empty if absent. */
  source?: string | null;
}) {
  const q = useQuery({
    queryKey: ["literature", "libraries"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/literature/libraries");
      if (error) throw error;
      return data;
    },
  });
  const scope = useCurrentScope();

  const id = (libraryId || "").trim();
  if (!id) return null;
  const libs = q.data && !q.data.degraded ? (q.data.libraries ?? []) : [];
  const { text } = libraryLabel(
    id,
    source || "",
    libs,
    scope.data?.experiment?.name,
  );
  return (
    <>
      {text && text !== id && <span className="text-mast-text">{text} </span>}
      <code className="text-mast-accent" title={id}>
        {id}
      </code>
    </>
  );
}
