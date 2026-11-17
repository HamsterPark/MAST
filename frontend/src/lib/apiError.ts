// Turning what an API call hands back as `error` into something a person can read, and the
// "keep retrying while it is in the error state" policy for queries that have no polling of their own.
//
// openapi-fetch returns `{ data, error, response }` where `error` is the parsed response body
// (a plain object such as FastAPI's `{ "detail": "..." }`), not an Error. `throw error` therefore
// throws a plain object, and `String(error.message ?? error)` printed it as `[object Object]`.
// Pure functions, no React: `node --test` can run them.

/** Longest message shown; a stray HTML error page must not fill the screen. */
const MAX_LEN = 300;

function clip(s: string): string {
  const t = s.replace(/\s+/g, " ").trim();
  return t.length > MAX_LEN ? t.slice(0, MAX_LEN - 1) + "…" : t;
}

function fromDetail(d: unknown): string {
  if (typeof d === "string") return d;
  if (Array.isArray(d)) {
    // FastAPI validation errors: [{ loc: ["body","x"], msg: "...", type: "..." }, ...]
    const parts = d
      .map((it) => {
        if (typeof it === "string") return it;
        if (it && typeof it === "object") {
          const o = it as { loc?: unknown; msg?: unknown };
          const msg = typeof o.msg === "string" ? o.msg : "";
          const loc = Array.isArray(o.loc)
            ? o.loc.filter((x) => x !== "body" && x !== "query" && x !== "path").join(".")
            : "";
          return msg ? (loc ? `${loc}: ${msg}` : msg) : "";
        }
        return "";
      })
      .filter(Boolean);
    return parts.join("; ");
  }
  if (d && typeof d === "object") return fromObject(d as Record<string, unknown>);
  return "";
}

function fromObject(o: Record<string, unknown>): string {
  const viaDetail = fromDetail(o.detail);
  if (viaDetail) return viaDetail;
  for (const k of ["message", "error", "msg"]) {
    const v = o[k];
    if (typeof v === "string" && v) return v;
  }
  return "";
}

/**
 * A readable message for anything that can be thrown or returned as an error.
 * Priority: Error.message → a string → string `detail` → FastAPI `detail[].msg` list →
 * `message` / `error` / `msg` → "" (the caller adds the HTTP status). Never "[object Object]".
 */
export function errorMessage(e: unknown): string {
  if (e == null) return "";
  if (e instanceof Error) return clip(e.message);
  if (typeof e === "string") return clip(e);
  if (typeof e === "object") {
    const m = fromObject(e as Record<string, unknown>);
    if (m) return clip(m);
    // A plain object with nothing readable in it: show its JSON rather than "[object Object]".
    try {
      const j = JSON.stringify(e);
      return j && j !== "{}" ? clip(j) : "";
    } catch {
      return "";
    }
  }
  return clip(String(e));
}

/** An Error that remembers the HTTP status (when the call returned one). */
export class ApiError extends Error {
  readonly status: number | undefined;
  constructor(message: string, status?: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/**
 * Make the Error to throw for a failed API call: `throw apiError(error, response)`.
 * The message is the readable text plus the status, e.g. `测试用的错误 (HTTP 500)`; with no text
 * it falls back to the status line, and with nothing at all to a generic word.
 */
export function apiError(body: unknown, response?: { status?: number; statusText?: string } | null): ApiError {
  const status = typeof response?.status === "number" && response.status > 0 ? response.status : undefined;
  const text = errorMessage(body);
  const tail = status !== undefined ? ` (HTTP ${status})` : "";
  if (text) return new ApiError(text + tail, status);
  const st = response?.statusText ? ` ${response.statusText}` : "";
  return new ApiError(status !== undefined ? `HTTP ${status}${st}` : "request failed", status);
}

/** How soon a query that is in the error state is asked again. */
export const ERROR_RETRY_MS = 10_000;

/**
 * `refetchInterval` for a query with no polling of its own: ask again every
 * {@link ERROR_RETRY_MS} while it is in the error state, never otherwise. So a failed first load
 * heals by itself (no page refresh), and a healthy page does no steady polling.
 */
export function refetchWhileError(query: { state: { status: string } }): number | false {
  return query.state.status === "error" ? ERROR_RETRY_MS : false;
}
