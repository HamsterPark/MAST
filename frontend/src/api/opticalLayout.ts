// /api/optical-layouts: the operator side of the optical layout editor.
//
// Contract: docs/v2/design/optical_layout.md section 6.1 (backend:
// MASTv2/mast/api/routes/optical_layout.py). These endpoints are for the web page. The
// external-agent side (/api/ext/v1/optical-layouts: read, and comment only) is not
// called from here: an agent never writes geometry or resolves a comment.
//
// They are not in the generated `schema.d.ts` yet, so this is a small hand-typed fetch
// client, like api/operator.ts. Unlike that one it takes the fetch function as an
// argument and imports neither React nor the `@/` alias, so test/opticalLayoutApi.test.ts
// can drive it with a fake server. Same origin, so the browser attaches the Basic /
// session credentials and an Origin equal to the Host, which the service's cross-site
// write check wants. Every write carries a JSON body: a body-less write on a loopback
// service would be a one-request CSRF door.

import {
  parseComment,
  parseComments,
  parseDocument,
  parseRevisionLayout,
  parseRevisions,
  parseSummaries,
} from "../lib/opticalLayout/parse.ts";
import { detailMessages } from "../lib/opticalLayout/saveFlow.ts";
import type {
  LayoutComment,
  LayoutDocument,
  LayoutSummary,
  OpticalLayout,
  RevisionInfo,
} from "../lib/opticalLayout/types.ts";

export const LAYOUTS_PATH = "/api/optical-layouts";

export class LayoutApiError extends Error {
  /** HTTP status; 0 when nothing answered */
  status: number;
  /** the parsed JSON body of the error reply, if there was one */
  body: unknown;
  /** the reply was fine but not JSON: typically the page's own HTML where the route is missing */
  notJson: boolean;
  constructor(message: string, status: number, body: unknown = null, notJson = false) {
    super(message);
    this.name = "LayoutApiError";
    this.status = status;
    this.body = body;
    this.notJson = notJson;
  }
}

/** The server has no PNG renderer (yet), or no such layout is saved: the button says so instead of failing. */
export class PngUnavailableError extends Error {
  status: number;
  constructor(status: number) {
    super(`render.png is not available (HTTP ${status})`);
    this.name = "PngUnavailableError";
    this.status = status;
  }
}

export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

export interface PutBody {
  layout: OpticalLayout;
  /** the revision this edit builds on; 0 for a layout that does not exist yet */
  base_revision: number;
  by: string;
}

export interface ArchiveBody {
  by: string;
  base_revision: number;
}

export interface CommentBody {
  by: string;
  text: string;
  component_id?: string;
}

export interface ResolveBody {
  by: string;
}

const enc = encodeURIComponent;

/** Whether the service is a build without the optical layout routes. */
export function isUnsupported(e: unknown): boolean {
  const err = e as { status?: unknown; notJson?: unknown } | null;
  return err?.notJson === true || err?.status === 404 || err?.status === 405 || err?.status === 501;
}

export interface OpticalLayoutApi {
  list(signal?: AbortSignal): Promise<LayoutSummary[]>;
  get(id: string, signal?: AbortSignal): Promise<LayoutDocument>;
  put(id: string, body: PutBody): Promise<LayoutDocument>;
  archive(id: string, body: ArchiveBody): Promise<void>;
  history(id: string, limit?: number, signal?: AbortSignal): Promise<RevisionInfo[]>;
  revision(id: string, revision: number, signal?: AbortSignal): Promise<OpticalLayout>;
  comments(id: string, signal?: AbortSignal): Promise<LayoutComment[]>;
  addComment(id: string, body: CommentBody): Promise<LayoutComment | null>;
  resolveComment(id: string, commentId: string, body: ResolveBody): Promise<LayoutComment | null>;
  /** `fit`: the whole table (default) or just where the components and beams are */
  renderPng(id: string, width: number, signal?: AbortSignal, fit?: "table" | "content"): Promise<Blob>;
}

export function createOpticalLayoutApi(fetchImpl?: FetchLike): OpticalLayoutApi {
  const doFetch: FetchLike = fetchImpl ?? ((input, init) => fetch(input, init));

  async function send(method: "GET" | "POST" | "PUT", path: string, body?: object, signal?: AbortSignal): Promise<unknown> {
    const headers: Record<string, string> = { Accept: "application/json" };
    const init: RequestInit = { method, headers, signal, credentials: "same-origin" };
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    let res: Response;
    try {
      res = await doFetch(path, init);
    } catch (e) {
      if ((e as { name?: string } | null)?.name === "AbortError") throw e;
      throw new LayoutApiError(String((e as Error)?.message ?? e), 0);
    }
    const text = await res.text();
    let data: unknown = null;
    let parsed = true;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      parsed = false;
    }
    if (!res.ok) {
      const reason = detailMessages(data).join("; ");
      throw new LayoutApiError(reason || res.statusText || `HTTP ${res.status}`, res.status, data);
    }
    if (!parsed) throw new LayoutApiError("the reply is not JSON", res.status, null, true);
    return data;
  }

  return {
    async list(signal) {
      return parseSummaries(await send("GET", LAYOUTS_PATH, undefined, signal));
    },
    async get(id, signal) {
      return parseDocument(await send("GET", `${LAYOUTS_PATH}/${enc(id)}`, undefined, signal));
    },
    async put(id, body) {
      return parseDocument(await send("PUT", `${LAYOUTS_PATH}/${enc(id)}`, body));
    },
    async archive(id, body) {
      await send("POST", `${LAYOUTS_PATH}/${enc(id)}/delete`, body);
    },
    async history(id, limit = 50, signal) {
      return parseRevisions(await send("GET", `${LAYOUTS_PATH}/${enc(id)}/history?limit=${Math.max(1, Math.floor(limit))}`, undefined, signal));
    },
    async revision(id, revision, signal) {
      return parseRevisionLayout(await send("GET", `${LAYOUTS_PATH}/${enc(id)}/revisions/${Math.floor(revision)}`, undefined, signal));
    },
    async comments(id, signal) {
      return parseComments(await send("GET", `${LAYOUTS_PATH}/${enc(id)}/comments?status=all`, undefined, signal));
    },
    async addComment(id, body) {
      return parseComment(await send("POST", `${LAYOUTS_PATH}/${enc(id)}/comments`, body));
    },
    async resolveComment(id, commentId, body) {
      return parseComment(await send("POST", `${LAYOUTS_PATH}/${enc(id)}/comments/${enc(commentId)}/resolve`, body));
    },
    async renderPng(id, width, signal, fit = "table") {
      // the server takes 200 to 3000 pixels
      const w = Math.min(3000, Math.max(200, Math.round(width)));
      const url = `${LAYOUTS_PATH}/${enc(id)}/render.png?width=${w}&fit=${fit}`;
      let res: Response;
      try {
        res = await doFetch(url, { method: "GET", headers: { Accept: "image/png" }, signal, credentials: "same-origin" });
      } catch (e) {
        if ((e as { name?: string } | null)?.name === "AbortError") throw e;
        throw new LayoutApiError(String((e as Error)?.message ?? e), 0);
      }
      if (res.status === 404 || res.status === 405 || res.status === 501) throw new PngUnavailableError(res.status);
      if (!res.ok) throw new LayoutApiError(res.statusText || `HTTP ${res.status}`, res.status);
      // a build without the route may answer with the page's own HTML and status 200
      if (!(res.headers.get("content-type") ?? "").toLowerCase().includes("image/png")) throw new PngUnavailableError(res.status);
      return res.blob();
    },
  };
}
