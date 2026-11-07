// What can go wrong when a layout is saved, and what to tell the operator
// (section 6.1 and section 8 of docs/v2/design/optical_layout.md).
//
//   409  somebody else saved first (two tabs, or the operator and a second person):
//        "changed elsewhere", with "load the newest" and "save as a new layout"
//   422  the server's own validation said no: show its reasons
//   403  the write guard (Origin / Host check) refused
//   404  the layout is gone (archived from another tab)
//   413  the layout is over the size limit
//   0    nothing answered
//
// Pure: a failure goes in (status, parsed body, message), a classified failure and its
// text come out, so every branch is tested (test/opticalLayoutSaveFlow.test.ts).

import { tr } from "../i18n.ts";
import { LIMITS } from "./types.ts";
import { layoutIdFormatProblem } from "./validate.ts";

export interface FailureLike {
  status?: unknown;
  body?: unknown;
  message?: unknown;
}

export type SaveFailure =
  | { kind: "conflict"; currentRevision: number | null; updatedBy: string; updatedAt: string; message: string }
  | { kind: "unavailable"; message: string }
  | { kind: "limit"; message: string }
  | { kind: "invalid"; messages: string[] }
  | { kind: "forbidden"; message: string }
  | { kind: "missing"; message: string }
  | { kind: "too-large"; message: string }
  | { kind: "network"; message: string }
  | { kind: "server"; message: string };

const isObj = (v: unknown): v is Record<string, unknown> => v !== null && typeof v === "object" && !Array.isArray(v);

/** The revision a 409 body names: at the top level or inside `detail`. */
export function conflictRevision(body: unknown): number | null {
  if (!isObj(body)) return null;
  const direct = body["current_revision"] ?? body["revision"];
  const nested = isObj(body["detail"]) ? (body["detail"]["current_revision"] ?? body["detail"]["revision"]) : undefined;
  const v = direct ?? nested;
  const n = typeof v === "number" ? v : typeof v === "string" ? Number(v) : Number.NaN;
  return Number.isFinite(n) ? Math.floor(n) : null;
}

/** Who saved the revision a 409 names, and when (section 11.3: `{detail, current_revision, updated_by, updated_at}`). */
export function conflictAuthor(body: unknown): { updatedBy: string; updatedAt: string } {
  const o = isObj(body) ? body : {};
  const nested = isObj(o["detail"]) ? o["detail"] : {};
  const pick = (k: string) => {
    const v = o[k] ?? nested[k];
    return typeof v === "string" ? v : "";
  };
  return { updatedBy: pick("updated_by"), updatedAt: pick("updated_at") };
}

/** FastAPI's reasons: `detail` is a text, or a list of `{loc, msg}`, or an object with `message`. */
export function detailMessages(body: unknown): string[] {
  if (!isObj(body)) return [];
  const d = body["detail"] ?? body["error"] ?? body["message"];
  if (typeof d === "string") return d ? [d] : [];
  if (Array.isArray(d)) {
    return d
      .map((x) => {
        if (!isObj(x)) return typeof x === "string" ? x : "";
        const loc = Array.isArray(x["loc"]) ? x["loc"].filter((p) => p !== "body" && p !== "layout").join(".") : "";
        const msg = typeof x["msg"] === "string" ? x["msg"] : "";
        return loc && msg ? `${loc}: ${msg}` : msg;
      })
      .filter(Boolean);
  }
  if (isObj(d)) {
    const m = d["message"] ?? d["detail"];
    return typeof m === "string" && m ? [m] : [];
  }
  return [];
}

export function classifySaveError(err: unknown): SaveFailure {
  const e = (err ?? {}) as FailureLike;
  const status = typeof e.status === "number" ? e.status : 0;
  const reasons = detailMessages(e.body);
  const text = reasons.join("; ") || (typeof e.message === "string" ? e.message : "");
  if (status === 409) return { kind: "conflict", currentRevision: conflictRevision(e.body), ...conflictAuthor(e.body), message: text };
  if (status === 503) return { kind: "unavailable", message: text };
  if (status === 429) return { kind: "limit", message: text };
  if (status === 422 || status === 400) return { kind: "invalid", messages: reasons.length > 0 ? reasons : text ? [text] : [] };
  if (status === 403 || status === 415) return { kind: "forbidden", message: text };
  if (status === 404) return { kind: "missing", message: text };
  if (status === 413) return { kind: "too-large", message: text };
  if (status === 0) return { kind: "network", message: text };
  return { kind: "server", message: text || `HTTP ${status}` };
}

/** The one line (or a short list) the operator reads. */
export function saveFailureText(f: SaveFailure): string {
  switch (f.kind) {
    case "conflict":
      return tr(
        "别处已经改过这张光路图（另一个标签页或另一个人先保存了）。你的修改还在这里，没有丢。",
        "This layout was changed elsewhere (another tab or another person saved first). Your edits are still here and not lost.",
      );
    case "invalid":
      return f.messages.length > 0
        ? tr(`服务器拒绝保存：${f.messages.join("；")}`, `The server refused to save: ${f.messages.join("; ")}`)
        : tr("服务器拒绝保存这张光路图。", "The server refused to save this layout.");
    case "forbidden":
      return tr(
        `服务器拒绝了这次写入（来源检查未通过）${f.message ? `：${f.message}` : "。"}`,
        `The server refused the write (its origin check failed)${f.message ? `: ${f.message}` : "."}`,
      );
    case "missing":
      return tr("服务器上找不到这张光路图（可能已在别处归档）。可以“另存为新图”保住修改。", "The server no longer has this layout (it may have been archived elsewhere). “Save as new” keeps your edits.");
    case "unavailable":
      return tr(
        `服务器现在写不了磁盘，什么都没有改变，你的修改还在这里${f.message ? `：${f.message}` : "。"}`,
        `The server cannot write to its disk right now; nothing was changed and your edits are still here${f.message ? `: ${f.message}` : "."}`,
      );
    case "limit":
      return tr(`已到数量上限${f.message ? `：${f.message}` : "。"}`, `A limit is reached${f.message ? `: ${f.message}` : "."}`);
    case "too-large":
      return tr(`光路图太大了（上限约 ${Math.round(LIMITS.jsonBytes / 1_000_000)} MB）。`, `The layout is too large (limit about ${Math.round(LIMITS.jsonBytes / 1_000_000)} MB).`);
    case "network":
      return tr("联系不上主服务，修改还没有保存。", "Could not reach the main service; your edits are not saved yet.");
    default:
      return tr(`保存失败：${f.message}`, `Saving failed: ${f.message}`);
  }
}

/** A new id for "save as new": `<id>-copy`, then `-copy2`, ... within 40 characters. */
export function suggestCopyId(id: string, taken: ReadonlySet<string>): string {
  const fit = (stem: string, suffix: string) => `${stem.slice(0, Math.max(1, LIMITS.idMax - suffix.length))}${suffix}`;
  for (let n = 1; n < 1000; n++) {
    const candidate = fit(id, n === 1 ? "-copy" : `-copy${n}`);
    if (!taken.has(candidate)) return candidate;
  }
  return fit(id, `-copy${Date.now() % 100000}`);
}

/** The reason a layout id is not usable, or null. */
export function layoutIdProblem(id: string, taken: ReadonlySet<string>): string | null {
  if (id === "") return tr("请填写编号。", "Enter an id.");
  const f = layoutIdFormatProblem(id);
  if (f === "format") return tr("编号只能含字母、数字、下划线和连字符，最多 40 个字符。", "Use letters, digits, underscore and hyphen only, at most 40 characters.");
  if (f === "reserved") return tr("这个编号是 Windows 的保留名（con、prn、aux、nul、com1–9、lpt1–9），不能做文件名。", "This id is a reserved Windows name (con, prn, aux, nul, com1-9, lpt1-9) and cannot be a file name.");
  if (taken.has(id)) return tr("已经有这个编号的光路图。", "A layout with this id already exists.");
  return null;
}

export type WriteBody<T> = { ok: true; value: T & { by: string } } | { ok: false; error: string };

/** Every write names the operator: no name, no request. */
export function withOperator<T extends object>(by: unknown, body: T): WriteBody<T> {
  const name = (typeof by === "string" ? by : "").split(/\s+/).filter(Boolean).join(" ").slice(0, 80);
  if (!name) return { ok: false, error: tr("请先填写你的姓名。", "Enter your name first.") };
  return { ok: true, value: { ...body, by: name } };
}
