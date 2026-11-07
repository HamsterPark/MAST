// ════════════════════════════════════════════════════════════════════════════
// Tests for src/api/opticalLayout.ts and src/lib/opticalLayout/parse.ts + saveFlow.ts
// against a fake server.
//
//     cd frontend && npm run test:unit
//
// What is pinned here is the part of the contract the page depends on:
//   * every call goes to /api/optical-layouts, every write carries a JSON body with the
//     operator's name, and no call goes to /api/ext (an agent comments there, the
//     operator edits here);
//   * a 409 keeps its body (the revision the server has now), a 422 its reasons;
//   * a build without the routes is recognised (404, or the page's own HTML with status
//     200) instead of being read as "no layouts yet";
//   * PNG export degrades to a clear message instead of an error.
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  LAYOUTS_PATH,
  LayoutApiError,
  PngUnavailableError,
  createOpticalLayoutApi,
  isUnsupported,
  type FetchLike,
} from "../src/api/opticalLayout.ts";
import {
  LayoutParseError,
  authorKind,
  layoutForWrite,
  openCommentCount,
  parseComment,
  parseComments,
  parseDocument,
  parseLayout,
  parseRevisions,
  parseSummaries,
  parseTrace,
} from "../src/lib/opticalLayout/parse.ts";
import {
  classifySaveError,
  conflictRevision,
  detailMessages,
  layoutIdProblem,
  saveFailureText,
  suggestCopyId,
  withOperator,
} from "../src/lib/opticalLayout/saveFlow.ts";
import { traceLayout } from "../src/lib/opticalLayout/trace.ts";
import { foldLayout } from "./helpers/opticalLayout.ts";

interface Seen {
  method: string;
  url: string;
  headers: Record<string, string>;
  body: unknown;
  hasBody: boolean;
}

function fakeServer(reply: (req: Seen) => Response | Promise<Response>) {
  const seen: Seen[] = [];
  const fetchImpl: FetchLike = async (input, init) => {
    const req: Seen = {
      method: init?.method ?? "GET",
      url: input,
      headers: { ...(init?.headers as Record<string, string> | undefined) },
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
      hasBody: init?.body !== undefined,
    };
    seen.push(req);
    return reply(req);
  };
  return { seen, api: createOpticalLayoutApi(fetchImpl) };
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

/** A layout as the server sends it, with the trace and link state beside it. */
const serverLayout = (over: Record<string, unknown> = {}) => {
  const l = foldLayout();
  return { ...l, id: "main", title: "SHG", revision: 12, updated_at: "2026-11-03T12:00:00+00:00", updated_by: "ana", trace: traceLayout(l), links_state: {}, ...over };
};

describe("reading layouts", () => {
  it("lists the layouts", async () => {
    const { api, seen } = fakeServer(() =>
      json({ layouts: [{ id: "main", title: "SHG", revision: 12, updated_at: "t", updated_by: "ana", components: 7, open_comments: 2 }, { id: "x" }, { title: "no id" }] }),
    );
    const list = await api.list();
    assert.deepEqual(
      list.map((s) => [s.id, s.revision, s.components, s.open_comments]),
      [
        ["main", 12, 7, 2],
        ["x", 0, 0, 0],
      ],
    );
    assert.equal(seen[0]!.method, "GET");
    assert.equal(seen[0]!.url, "/api/optical-layouts");
    assert.equal(seen[0]!.hasBody, false);
  });

  it("gets one layout with its trace and link state", async () => {
    const { api, seen } = fakeServer(() =>
      json(serverLayout({ links_state: { m1: { device: { id: "witec", enabled: true, connected: false }, slot: { id: "excitation_waveplate", value: "hwp_532", valid: true } } } })),
    );
    const doc = await api.get("main");
    assert.equal(seen[0]!.url, "/api/optical-layouts/main");
    assert.equal(doc.layout.id, "main");
    assert.equal(doc.layout.revision, 12);
    assert.equal(doc.layout.components.length, 3);
    assert.equal(doc.trace?.[0]?.source, "las1");
    assert.equal(doc.linksState["m1"]?.device?.connected, false);
    assert.equal(doc.linksState["m1"]?.slot?.value, "hwp_532");
    assert.ok(!("trace" in doc.layout), "the trace is not part of the layout");
  });

  it("puts the id into the path escaped", async () => {
    const { api, seen } = fakeServer(() => json(serverLayout()));
    await api.get("a b/c");
    assert.equal(seen[0]!.url, "/api/optical-layouts/a%20b%2Fc");
  });

  it("history comes newest first; a revision is a plain layout", async () => {
    const { api, seen } = fakeServer((r) =>
      r.url.includes("/history")
        ? json({ revisions: [{ revision: 3, updated_by: "a", updated_at: "t3", components: 4, title: "T" }, { revision: 5, updated_by: "b", updated_at: "t5" }, { nope: 1 }] })
        : json(serverLayout({ revision: 3 })),
    );
    const h = await api.history("main", 20);
    assert.deepEqual(h.map((x) => x.revision), [5, 3]);
    assert.equal(seen[0]!.url, "/api/optical-layouts/main/history?limit=20");
    const rev = await api.revision("main", 3);
    assert.equal(seen[1]!.url, "/api/optical-layouts/main/revisions/3");
    assert.equal(rev.revision, 3);
  });
});

describe("writing layouts", () => {
  it("PUT carries the layout, the revision it builds on and the operator's name, as JSON", async () => {
    const { api, seen } = fakeServer(() => json(serverLayout({ revision: 13 })));
    const layout = foldLayout();
    const doc = await api.put("main", { layout, base_revision: 12, by: "ana" });
    const r = seen[0]!;
    assert.equal(r.method, "PUT");
    assert.equal(r.url, "/api/optical-layouts/main");
    assert.equal(r.headers["Content-Type"], "application/json");
    assert.deepEqual(Object.keys(r.body as object).sort(), ["base_revision", "by", "layout"]);
    assert.equal((r.body as { base_revision: number }).base_revision, 12);
    assert.equal((r.body as { by: string }).by, "ana");
    assert.equal(doc.layout.revision, 13);
  });

  it("a new layout is saved against revision 0", async () => {
    const { api, seen } = fakeServer(() => json(serverLayout({ revision: 1 })));
    await api.put("fresh", { layout: foldLayout(), base_revision: 0, by: "ana" });
    assert.equal((seen[0]!.body as { base_revision: number }).base_revision, 0);
  });

  it("archive, comment and resolve are POSTs with a JSON body each", async () => {
    const { api, seen } = fakeServer((r) => (r.url.endsWith("/delete") ? json({ ok: true }) : json({ id: "c1", by: "ana", text: "t", kind: "operator", resolved: false })));
    await api.archive("main", { by: "ana", base_revision: 12 });
    await api.addComment("main", { by: "ana", text: "check the angle", component_id: "m1" });
    await api.resolveComment("main", "c1", { by: "ana" });
    assert.deepEqual(
      seen.map((r) => [r.method, r.url]),
      [
        ["POST", "/api/optical-layouts/main/delete"],
        ["POST", "/api/optical-layouts/main/comments"],
        ["POST", "/api/optical-layouts/main/comments/c1/resolve"],
      ],
    );
    assert.deepEqual(seen[0]!.body, { by: "ana", base_revision: 12 });
    assert.deepEqual(seen[1]!.body, { by: "ana", text: "check the angle", component_id: "m1" });
    assert.deepEqual(seen[2]!.body, { by: "ana" });
  });

  it("every call goes to /api/optical-layouts, none to /api/ext, and every write has a body", async () => {
    const { api, seen } = fakeServer((r) => (r.url.includes("/comments") && r.method === "GET" ? json({ comments: [] }) : json(serverLayout())));
    await api.list();
    await api.get("main");
    await api.put("main", { layout: foldLayout(), base_revision: 1, by: "ana" });
    await api.archive("main", { by: "ana", base_revision: 1 });
    await api.history("main");
    await api.revision("main", 1);
    await api.comments("main");
    await api.addComment("main", { by: "ana", text: "x" });
    await api.resolveComment("main", "c", { by: "ana" });
    for (const r of seen) {
      assert.ok(r.url.startsWith(LAYOUTS_PATH), r.url);
      assert.ok(!r.url.includes("/api/ext"), r.url);
      if (r.method !== "GET") {
        assert.equal(r.hasBody, true, `${r.method} ${r.url} has no body`);
        assert.equal(r.headers["Content-Type"], "application/json");
      }
    }
  });

  it("sends only the fields of section 3 (no trace, no link state)", () => {
    const sent = layoutForWrite({ ...foldLayout(), trace: [1], links_state: {} } as never);
    assert.deepEqual(Object.keys(sent).sort(), ["components", "id", "notes", "revision", "schema", "table", "texts", "title", "updated_at", "updated_by"]);
  });
});

describe("failures", () => {
  it("a 409 keeps its body: the revision the server has now", async () => {
    const { api } = fakeServer(() => json({ detail: "base_revision 3 is behind revision 5", current_revision: 5 }, 409));
    await assert.rejects(
      () => api.put("main", { layout: foldLayout(), base_revision: 3, by: "ana" }),
      (e: unknown) => {
        assert.ok(e instanceof LayoutApiError);
        assert.equal(e.status, 409);
        assert.equal(e.message, "base_revision 3 is behind revision 5");
        const f = classifySaveError(e);
        assert.equal(f.kind, "conflict");
        assert.equal(f.kind === "conflict" && f.currentRevision, 5);
        return true;
      },
    );
  });

  it("a 422 keeps FastAPI's list of reasons", async () => {
    const detail = [
      { loc: ["body", "layout", "components", 1, "props", "edge_nm"], msg: "field required", type: "missing" },
      { loc: ["body", "layout", "table", "hole_pitch_mm"], msg: "ensure this value is greater than 0", type: "value_error" },
    ];
    const { api } = fakeServer(() => json({ detail }, 422));
    await assert.rejects(
      () => api.put("main", { layout: foldLayout(), base_revision: 1, by: "ana" }),
      (e: unknown) => {
        const f = classifySaveError(e);
        assert.equal(f.kind, "invalid");
        assert.deepEqual(f.kind === "invalid" && f.messages, ["components.1.props.edge_nm: field required", "table.hole_pitch_mm: ensure this value is greater than 0"]);
        assert.match(saveFailureText(f), /edge_nm/);
        return true;
      },
    );
  });

  it("nothing answering is status 0, an abort is left alone", async () => {
    const down = createOpticalLayoutApi(async () => {
      throw new TypeError("Failed to fetch");
    });
    await assert.rejects(() => down.list(), (e: unknown) => e instanceof LayoutApiError && e.status === 0 && classifySaveError(e).kind === "network");
    const aborted = createOpticalLayoutApi(async () => {
      throw Object.assign(new Error("aborted"), { name: "AbortError" });
    });
    await assert.rejects(() => aborted.list(), (e: unknown) => (e as Error).name === "AbortError");
  });

  it("a build without the routes is recognised: 404, or HTML with status 200", async () => {
    const missing = fakeServer(() => json({ detail: "Not Found" }, 404));
    await assert.rejects(() => missing.api.list(), (e: unknown) => isUnsupported(e));
    const html = fakeServer(() => new Response("<!doctype html><html></html>", { status: 200, headers: { "content-type": "text/html" } }));
    await assert.rejects(() => html.api.list(), (e: unknown) => e instanceof LayoutApiError && e.notJson && isUnsupported(e));
    assert.equal(isUnsupported(new LayoutApiError("x", 500)), false);
    assert.equal(isUnsupported(new LayoutApiError("x", 409)), false);
    assert.equal(isUnsupported(null), false);
  });
});

describe("PNG export", () => {
  it("returns the image when the server has a renderer", async () => {
    const { api, seen } = fakeServer(() => new Response(new Uint8Array([137, 80, 78, 71]), { status: 200, headers: { "content-type": "image/png" } }));
    const blob = await api.renderPng("main", 2000.4);
    assert.equal(seen[0]!.url, "/api/optical-layouts/main/render.png?width=2000&fit=table");
    assert.equal(blob.size, 4);
  });

  it("asks for a width the server takes (200 to 3000) and for the whole table unless told otherwise", async () => {
    const { api, seen } = fakeServer(() => new Response(new Uint8Array([1]), { status: 200, headers: { "content-type": "image/png" } }));
    await api.renderPng("main", 50);
    await api.renderPng("main", 9000, undefined, "content");
    assert.equal(seen[0]!.url, "/api/optical-layouts/main/render.png?width=200&fit=table");
    assert.equal(seen[1]!.url, "/api/optical-layouts/main/render.png?width=3000&fit=content");
  });

  it("is 'not available' for 404, 405, 501 and for HTML that is not an image", async () => {
    for (const status of [404, 405, 501]) {
      const { api } = fakeServer(() => json({ detail: "x" }, status));
      await assert.rejects(() => api.renderPng("main", 1000), (e: unknown) => e instanceof PngUnavailableError && e.status === status);
    }
    const html = fakeServer(() => new Response("<html></html>", { status: 200, headers: { "content-type": "text/html" } }));
    await assert.rejects(() => html.api.renderPng("main", 1000), (e: unknown) => e instanceof PngUnavailableError);
  });

  it("other failures stay errors", async () => {
    const { api } = fakeServer(() => json({ detail: "boom" }, 500));
    await assert.rejects(() => api.renderPng("main", 1000), (e: unknown) => e instanceof LayoutApiError && e.status === 500);
  });
});

describe("comments", () => {
  it("tell the operator from the agent", () => {
    assert.equal(authorKind({ kind: "agent" }), "agent");
    assert.equal(authorKind({ kind: "operator" }), "operator");
    assert.equal(authorKind({ author_kind: "agent" }), "agent");
    assert.equal(authorKind({ source: "ext" }), "agent");
    assert.equal(authorKind({ source: "web" }), "operator");
    assert.equal(authorKind({ is_agent: true }), "agent");
    assert.equal(authorKind({}), "operator");
  });

  it("parse with their resolved state and the component they are about", () => {
    const c = parseComment({ id: "c1", by: "claude-1", kind: "agent", text: "M2 looks off", component_id: "m2", created_at: "t", resolved: true, resolved_by: "ana", resolved_at: "t2" })!;
    assert.deepEqual([c.id, c.by, c.kind, c.component_id, c.resolved, c.resolved_by], ["c1", "claude-1", "agent", "m2", true, "ana"]);
    assert.equal(parseComment({ id: "c2", text: "t" })!.component_id, null);
    assert.equal(parseComment({ text: "no id" }), null);
    assert.equal(parseComment("x"), null);
    const list = parseComments({ comments: [{ id: "a", resolved: false }, { id: "b", resolved: true }, "junk"] });
    assert.equal(list.length, 2);
    assert.equal(openCommentCount(list), 1);
  });
});

describe("parsing a layout", () => {
  it("fills what an older server leaves out", () => {
    const l = parseLayout({ id: "x", components: [{ id: "m1", type: "mirror", x_mm: "10.5", y_mm: 2, angle_deg: 135 }] });
    const c = l.components[0]!;
    assert.equal(c.x_mm, 10.5);
    assert.equal(c.aperture_mm, 25.4);
    assert.equal(c.label, "m1");
    assert.equal(c.locked, false);
    assert.deepEqual(c.links, { device_id: null, slot: null });
    assert.equal(c.annotations.motorized, false);
    assert.deepEqual(c.annotations.extra, []);
    assert.equal(l.table.hole_pitch_mm, 25);
    assert.equal(l.revision, 0);
  });

  it("keeps annotations, links, props and texts as sent", () => {
    const l = parseLayout({
      id: "x",
      table: { width_mm: 1500, height_mm: 1000, hole_pitch_mm: 25.4, hole_offset_mm: 12.7, grid: "metric" },
      components: [
        {
          id: "hwp1", type: "waveplate", label: "HWP1", x_mm: 1, y_mm: 2, angle_deg: 3, aperture_mm: 12.7, locked: true,
          props: { retardance: "half" },
          annotations: { model: "K10CR1", manufacturer: "Thorlabs", motorized: true, actuator: "K10CR1", connection: "COM15", extra: [{ key: "mount", value: "KM100" }, "junk"] },
          links: { device_id: "hwp_rot", slot: "excitation_waveplate" },
        },
      ],
      texts: [{ id: "t1", x_mm: 3, y_mm: 4, text: "door", size: 1.5 }, "junk"],
      notes: "n",
    });
    const c = l.components[0]!;
    assert.equal(c.annotations.connection, "COM15");
    assert.equal(c.annotations.motorized, true);
    assert.deepEqual(c.annotations.extra, [{ key: "mount", value: "KM100" }]);
    assert.deepEqual(c.links, { device_id: "hwp_rot", slot: "excitation_waveplate" });
    assert.equal(c.locked, true);
    assert.deepEqual(c.props, { retardance: "half" });
    assert.equal(l.table.width_mm, 1500);
    assert.equal(l.texts.length, 1);
    assert.equal(l.texts[0]!.size, 1.5);
    assert.equal(l.notes, "n");
  });

  it("refuses what it cannot draw, rather than repairing it behind the operator's back", () => {
    assert.throws(() => parseLayout(null), LayoutParseError);
    assert.throws(() => parseLayout({ components: [] }), LayoutParseError, "no id");
    assert.throws(() => parseLayout({ id: "x", components: "nope" }), LayoutParseError);
    assert.throws(() => parseLayout({ id: "x", components: [{ id: "a", type: "teleporter" }] }), /teleporter/);
    assert.throws(() => parseLayout({ id: "x", components: [5] }), LayoutParseError);
  });

  it("a document carries the layout and, apart from it, the trace and the link state", () => {
    const d = parseDocument(serverLayout());
    assert.equal(d.layout.id, "main");
    assert.equal(d.trace?.length, 1);
    assert.deepEqual(d.linksState, {});
    assert.equal(parseDocument({ id: "y" }).trace, null);
  });

  it("the server's trace is read loosely", () => {
    const t = parseTrace([{ source: "a", wavelength_nm: 532, branches: [{ branch: "0", fraction: 1, end: "weird", segments: [{ x0: 0, y0: 0, x1: 1, y1: 1 }], events: [{ component: "m1", action: "reflect", x: 1, y: 1, offset_mm: 0, incidence_deg: 45, path_mm: 1 }] }, { branch: "0.r", fraction: 0.5, end: "split", segments: [], events: [] }] }, "junk"])!;
    assert.equal(t.length, 1);
    assert.equal(t[0]!.branches[0]!.end, "stop", "an end it does not know is read as a plain stop");
    assert.equal(t[0]!.branches[1]!.end, "split", "split is one of the ends");
    assert.equal(parseTrace(undefined), null);
  });

  it("summaries and revisions accept bare lists", () => {
    assert.equal(parseSummaries([{ id: "a" }]).length, 1);
    assert.equal(parseSummaries(null).length, 0);
    assert.deepEqual(parseRevisions([{ revision: 1 }, { revision: 2 }]).map((r) => r.revision), [2, 1]);
    assert.deepEqual(parseRevisions({ history: [{ revision: 7 }] }).map((r) => r.revision), [7]);
  });
});

describe("save failures", () => {
  it("are classified by status", () => {
    const c = (status: number, body?: unknown, message = "") => classifySaveError({ status, body, message });
    assert.equal(c(409).kind, "conflict");
    assert.equal(c(422).kind, "invalid");
    assert.equal(c(400).kind, "invalid");
    assert.equal(c(403).kind, "forbidden");
    assert.equal(c(404).kind, "missing");
    assert.equal(c(413).kind, "too-large");
    assert.equal(c(0).kind, "network");
    assert.equal(c(500, undefined, "boom").kind, "server");
    assert.equal(classifySaveError(new Error("odd")).kind, "network", "an error with no status counts as nothing answering");
    assert.equal(classifySaveError(null).kind, "network");
  });

  it("each has a text to show", () => {
    for (const status of [409, 422, 403, 404, 413, 0, 500]) {
      assert.ok(saveFailureText(classifySaveError({ status, message: "why" })).length > 4, String(status));
    }
    assert.match(saveFailureText(classifySaveError({ status: 500, message: "boom" })), /boom/);
  });

  it("the revision named by a 409 is read from either place", () => {
    assert.equal(conflictRevision({ current_revision: 7 }), 7);
    assert.equal(conflictRevision({ detail: { current_revision: "8", message: "x" } }), 8);
    assert.equal(conflictRevision({ detail: "text" }), null);
    assert.equal(conflictRevision(null), null);
  });

  it("detail is read as text, list or object", () => {
    assert.deepEqual(detailMessages({ detail: "plain" }), ["plain"]);
    assert.deepEqual(detailMessages({ detail: { message: "nested" } }), ["nested"]);
    assert.deepEqual(detailMessages({ error: "e" }), ["e"]);
    assert.deepEqual(detailMessages({ detail: [{ msg: "no loc" }, "bare"] }), ["no loc", "bare"]);
    assert.deepEqual(detailMessages(undefined), []);
  });

  it("'save as new' suggests an id that is free and fits", () => {
    assert.equal(suggestCopyId("main", new Set(["main"])), "main-copy");
    assert.equal(suggestCopyId("main", new Set(["main", "main-copy"])), "main-copy2");
    const long = "x".repeat(40);
    const id = suggestCopyId(long, new Set());
    assert.equal(id.length, 40);
    assert.ok(id.endsWith("-copy"));
  });

  it("a layout id must be well formed and free", () => {
    assert.equal(layoutIdProblem("main", new Set()), null);
    assert.ok(layoutIdProblem("", new Set()));
    assert.ok(layoutIdProblem("has space", new Set()));
    assert.ok(layoutIdProblem("x".repeat(41), new Set()));
    assert.ok(layoutIdProblem("main", new Set(["main"])));
  });

  it("every write names the operator, tidied and capped at 80 characters", () => {
    const ok = withOperator("  Ana   Ruiz ", { text: "x" });
    assert.ok(ok.ok);
    assert.deepEqual(ok.value, { text: "x", by: "Ana Ruiz" });
    assert.equal(withOperator("", { text: "x" }).ok, false);
    assert.equal(withOperator("   ", { text: "x" }).ok, false);
    assert.equal(withOperator(undefined, {}).ok, false);
    const long = withOperator("n".repeat(200), {});
    assert.ok(long.ok && long.value.by.length === 80);
  });
});
