// src/lib/apiError.ts — readable errors from API bodies, and the retry-while-error policy.
//
//     cd frontend && npm run test:unit

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { ApiError, ERROR_RETRY_MS, apiError, errorMessage, refetchWhileError } from "../src/lib/apiError.ts";

describe("errorMessage", () => {
  it("string detail", () => {
    assert.equal(errorMessage({ detail: "测试用的错误" }), "测试用的错误");
  });
  it("FastAPI validation list (loc dropped of body/query/path prefixes)", () => {
    const body = { detail: [{ loc: ["body", "name"], msg: "Field required", type: "missing" }, { loc: ["query", "n"], msg: "bad int" }] };
    assert.equal(errorMessage(body), "name: Field required; n: bad int");
  });
  it("list entries without loc keep the bare msg", () => {
    assert.equal(errorMessage({ detail: [{ msg: "oops" }] }), "oops");
  });
  it("message / error fields, and a nested detail object", () => {
    assert.equal(errorMessage({ message: "m" }), "m");
    assert.equal(errorMessage({ error: "e" }), "e");
    assert.equal(errorMessage({ detail: { message: "nested" } }), "nested");
  });
  it("a plain object with nothing readable shows its JSON, never [object Object]", () => {
    assert.equal(errorMessage({ code: 7 }), '{"code":7}');
    assert.equal(errorMessage({}), "");
  });
  it("a real Error keeps its message; a string is itself; null is empty", () => {
    assert.equal(errorMessage(new TypeError("Failed to fetch")), "Failed to fetch");
    assert.equal(errorMessage("plain"), "plain");
    assert.equal(errorMessage(null), "");
    assert.equal(errorMessage(undefined), "");
  });
  it("is clipped, and whitespace collapses", () => {
    const long = "x".repeat(1000);
    assert.ok(errorMessage(long).length <= 300);
    assert.equal(errorMessage("a\n  b"), "a b");
  });
  it("never returns '[object Object]'", () => {
    for (const v of [{}, { a: { b: 1 } }, [], { detail: {} }, { detail: [] }]) {
      assert.ok(!errorMessage(v).includes("[object Object]"), JSON.stringify(v));
    }
  });
});

describe("apiError", () => {
  it("message plus status", () => {
    const e = apiError({ detail: "测试用的错误" }, { status: 500, statusText: "Internal Server Error" });
    assert.ok(e instanceof Error && e instanceof ApiError);
    assert.equal(e.message, "测试用的错误 (HTTP 500)");
    assert.equal(e.status, 500);
  });
  it("no readable body: falls back to the status line", () => {
    assert.equal(apiError({}, { status: 502, statusText: "Bad Gateway" }).message, "HTTP 502 Bad Gateway");
    assert.equal(apiError(null, { status: 404 }).message, "HTTP 404");
  });
  it("no response at all", () => {
    assert.equal(apiError({ detail: "x" }).message, "x");
    assert.equal(apiError(undefined).message, "request failed");
    assert.equal(apiError(undefined).status, undefined);
  });
  it("an Error body passes through its message", () => {
    assert.equal(apiError(new Error("boom"), { status: 500 }).message, "boom (HTTP 500)");
  });
});

describe("refetchWhileError", () => {
  it("polls only while the query is in the error state", () => {
    assert.equal(refetchWhileError({ state: { status: "error" } }), ERROR_RETRY_MS);
    for (const s of ["pending", "success"]) assert.equal(refetchWhileError({ state: { status: s } }), false);
  });
});
