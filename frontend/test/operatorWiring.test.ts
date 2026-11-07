// ════════════════════════════════════════════════════════════════════════════
// Operator-prompt UI wiring — what the unit tests of the pure logic cannot see.
//
//     cd frontend && npm run test:unit
//
// The components are .tsx (Node cannot run JSX), so the places where a piece of correct
// logic is easy to leave unconnected are pinned from source, in the style of
// degradedWiring / transcriptWiring:
//
//   · the nav badge and the alert host really read the fast prompt feed (the old badge
//     was a 60 s poll of the free-text board and cannot be allowed to come back as the
//     only source), and there is exactly ONE owner of the polling;
//   · polling continues while the tab is in the background — the operator is looking at
//     the vendor software, so a poll that pauses with the tab defeats the whole alert;
//   · every write is JSON with a body (a body-less write on a loopback service is a
//     one-request CSRF door) and goes to /api/operator, never /api/ext: an agent can
//     post and wait for a prompt but must never be able to answer one;
//   · the page keeps its free-text board and shows the new sections without depending
//     on the wishlist query;
//   · STM stations stay as they were: the optical panel and the prompt board are gated on the
//     station profile, and so is every request under /api/operator, the nav badge's prompt
//     count, the toast, the sound, the desktop notification and the tab title;
//   · identity strings (kind, status, severity, slot ids) are never passed through tr().
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it } from "node:test";

const HERE = fileURLToPath(new URL(".", import.meta.url));
const SRC = join(HERE, "..", "src");
const read = (...p: string[]) => readFileSync(join(SRC, ...p), "utf8").replace(/\r\n/g, "\n");

function files(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) files(p, out);
    else if (/\.tsx?$/.test(name)) out.push(p);
  }
  return out;
}

const APP_LAYOUT = read("layout", "AppLayout.tsx");
const WISHLIST = read("pages", "WishlistPage.tsx");
const API = read("api", "operator.ts");
const BOARD = read("components", "operator", "PromptBoard.tsx");
const PANEL = read("components", "operator", "OpticalStatePanel.tsx");
const HOST = read("components", "operator", "OperatorAlertHost.tsx");

/** Everything this feature owns. */
const OPERATOR_SOURCES = [
  join(SRC, "api", "operator.ts"),
  join(SRC, "lib", "operatorPrompts.ts"),
  join(SRC, "lib", "opticalState.ts"),
  join(SRC, "hooks", "useOperatorPrefs.ts"),
  ...files(join(SRC, "components", "operator")),
];

describe("the nav badge and the alert host read the fast feed", () => {
  it("AppLayout polls the open prompts once and hands the feed to the alert host", () => {
    assert.match(APP_LAYOUT, /const operatorPrompts = useOpenPromptFeed\(\);/);
    assert.match(APP_LAYOUT, /<OperatorAlertHost feed=\{operatorPrompts\} \/>/);
  });

  it("the badge on the Operator Requests tab is the prompts plus the free-text requests, and keeps the old source", () => {
    assert.match(APP_LAYOUT, /wishlistBadge\(operatorPrompts\.openCount, openAgentRequests\)/);
    assert.match(APP_LAYOUT, /t\.to === "\/wishlist" && wishlistCount > 0/);
    assert.match(APP_LAYOUT, /const openAgentRequests = useOpenAgentRequests\(\);/, "the 60 s board count is still part of the badge");
    assert.match(APP_LAYOUT, /urgent=\{operatorPrompts\.hasSafety\}/, "a safety prompt turns the badge red");
  });

  it("the alert host is mounted only while the feed is enabled, which only an optical station's is", () => {
    assert.match(APP_LAYOUT, /\{operatorPrompts\.enabled && <OperatorAlertHost feed=\{operatorPrompts\} \/>\}/);
  });

  it("the alert host is not behind the legacy-features switch", () => {
    const line = APP_LAYOUT.split("\n").find((l) => l.includes("<OperatorAlertHost"));
    assert.ok(line && !line.includes("legacy"), "the operator alert must show with legacy features off");
  });

  it("exactly one reader polls the open prompts; every other reader shares the cache", () => {
    const calls = files(SRC).flatMap((f) => {
      const text = readFileSync(f, "utf8");
      return [...text.matchAll(/useOpenPromptFeed\(([^)]*)\)/g)].map((m) => ({ f, args: m[1]!.trim() }));
    });
    const polling = calls.filter((c) => !/poll:\s*false/.test(c.args) && !c.f.endsWith("operator.ts"));
    assert.equal(polling.length, 1, `polling callers: ${JSON.stringify(polling)}`);
    assert.ok(polling[0]!.f.endsWith("AppLayout.tsx"));
    assert.match(BOARD, /useOpenPromptFeed\(\{ poll: false \}\)/);
  });
});

describe("polling", () => {
  it("keeps going while the tab is in the background and does not retry a dead service in a loop", () => {
    assert.match(API, /refetchIntervalInBackground: true/);
    assert.match(API, /retry: false/);
  });

  it("the interval comes from pollDelayMs, so an error slows it down", () => {
    assert.match(API, /pollDelayMs\(q\.state\.error/);
  });

  it("the service's operator_prompt push event triggers an immediate refetch, owned by the one polling reader", () => {
    assert.match(API, /useWsEvent\(\s*"operator_prompt"/);
    assert.match(API, /\},\s*poll && optical,\s*\);/, "only the polling reader, and only on an optical station, subscribes");
    assert.match(API, /what === "optical_state" \? OPTICAL_KEY : PROMPTS_KEY/);
  });

  it("new-prompt detection starts from a baseline instead of announcing what is already open", () => {
    assert.match(HOST, /seen\.current === null/);
    assert.match(HOST, /freshPrompts\(seen\.current, prompts\)/);
  });

  it("a desktop notification is only raised for someone not looking at the page", () => {
    assert.match(HOST, /shouldDesktopNotify\(/);
    assert.match(HOST, /document\.hidden/);
    assert.match(HOST, /document\.hasFocus\(\)/);
  });
});

describe("only the operator answers", () => {
  /** The top-level arguments of every `call<…>(…)` in the client. A balanced scan, because
   *  the paths contain `${encodeURIComponent(id)}` and a regex would stop at its `)`. */
  function callArgs(source: string): string[][] {
    const out: string[][] = [];
    for (const m of source.matchAll(/(?<!function )\bcall<[^>]*>\(/g)) {
      let depth = 1;
      let cur = "";
      let quote = "";
      const args: string[] = [];
      for (let i = m.index! + m[0].length; i < source.length; i++) {
        const c = source[i]!;
        if (quote) {
          cur += c;
          if (c === "\\") cur += source[++i] ?? "";
          else if (c === quote) quote = "";
          continue;
        }
        if (c === '"' || c === "`" || c === "'") quote = c;
        if (c === "(" || c === "{" || c === "[") depth++;
        if (c === ")" || c === "}" || c === "]") depth--;
        if (depth === 0) break;
        if (c === "," && depth === 1) {
          args.push(cur.trim());
          cur = "";
          continue;
        }
        cur += c;
      }
      args.push(cur.trim());
      out.push(args);
    }
    return out;
  }
  const calls = callArgs(API);

  it("the client found its calls", () => {
    // open/all list, answer, dismiss, optical state, optical history, set slot, clear slot
    assert.equal(calls.length, 7, JSON.stringify(calls.map((a) => a.slice(0, 2))));
  });

  it("every call goes to /api/operator", () => {
    for (const [, path] of calls) assert.match(path!, /^[`"]\/api\/operator\//, `${path}`);
  });

  it("every write carries a JSON body", () => {
    const writes = calls.filter(([verb]) => verb !== '"GET"');
    assert.equal(writes.length, 4, "answer, dismiss, set slot, clear slot");
    for (const [verb, path, body] of writes) assert.ok(body && body !== "undefined", `${verb} ${path} has no body`);
    assert.match(API, /"Content-Type"\] = "application\/json"/);
  });

  it("nothing of this feature builds an external-agent URL", () => {
    for (const f of OPERATOR_SOURCES) {
      // string literals only: comments say outright that /api/ext is NOT what this talks to
      const literals = [...readFileSync(f, "utf8").matchAll(/(["'`])((?:\\.|(?!\1)[^\\])*)\1/g)].map((m) => m[2]!);
      assert.deepEqual(literals.filter((l) => l.includes("/api/ext")), [], `${f} builds an /api/ext URL`);
    }
  });
});

describe("the page", () => {
  it("shows the prompt board and the optical panel above the free-text board, without waiting for it", () => {
    const body = WISHLIST.slice(WISHLIST.indexOf("export default function WishlistPage"), WISHLIST.indexOf("function WishlistBoard"));
    const order = ["<OperatorPromptBoard />", "<OpticalStatePanel />", "<WishlistBoard />"].map((s) => body.indexOf(s));
    assert.ok(order.every((i) => i >= 0), "all three are rendered");
    assert.deepEqual(order, [...order].sort((a, b) => a - b), "in that order");
  });

  it("the free-text board is untouched", () => {
    assert.match(WISHLIST, /refetchInterval: 4000/);
    assert.match(WISHLIST, /if \(!legacy\) return <div className="space-y-2">\{requestsBoard\}<\/div>;/);
    assert.match(WISHLIST, /"\/api\/wishlist\/requests\/\{request_id\}\/resolve"/);
  });
});

describe("an STM station stays as it was", () => {
  it("the optical panel exists only for the optical profile", () => {
    assert.match(PANEL, /if \(!useIsOpticalStation\(\)\) return null;/);
    // the profile in force (GET /api/health), not the stored setting; shares the ["health"] query
    assert.match(API, /queryKey: \["health"\],\s*queryFn: async \(\) => \{\s*const \{ data, error \} = await api\.GET\("\/api\/health"\);/);
    assert.match(API, /isOpticalProfile\(\(q\.data as \{ station_profile\?: unknown \} \| undefined\)\?\.station_profile\)/);
  });

  it("the board exists only for the optical profile, and every hook that asks for something lives in its body", () => {
    assert.match(BOARD, /export function OperatorPromptBoard\(\) \{\s*if \(!useIsOpticalStation\(\)\) return null;\s*return <PromptBoardBody \/>;\s*\}/);
    const outer = BOARD.slice(BOARD.indexOf("export function OperatorPromptBoard"), BOARD.indexOf("function PromptBoardBody"));
    for (const hook of ["useOpenPromptFeed", "usePromptHistory", "useOpticalState", "useOperatorName", "useNow"]) {
      assert.ok(!outer.includes(`${hook}(`), `${hook} belongs in the body, which an STM station never mounts`);
    }
    assert.doesNotMatch(BOARD, /history\.length === 0\) return null/, "no half-hidden board that still asks for its history");
  });

  it("slot definitions are only fetched while a plain attestation is open", () => {
    assert.match(BOARD, /useOpticalState\(needSlots\)/);
  });
});

describe("nothing under /api/operator is asked for, and nothing sounds, unless the station is an optical one", () => {
  /** The source of every top-level function in a file, by name. */
  function functions(source: string): Map<string, string> {
    const heads = [...source.matchAll(/^(?:export )?function (\w+)/gm)];
    return new Map(heads.map((m, i) => [m[1]!, source.slice(m.index!, heads[i + 1]?.index ?? source.length)]));
  }
  const apiFns = functions(API);
  /** Every module under src that matches, as [relative path, text]. */
  const sources = files(SRC).map((f) => [f.slice(SRC.length + 1).replaceAll("\\", "/"), readFileSync(f, "utf8").replace(/\r\n/g, "\n")] as const);
  const importers = (pattern: RegExp) => sources.filter(([, text]) => pattern.test(text)).map(([name]) => name).sort();

  it("there is one gate, and it is the station profile combined with the caller's own flag", () => {
    assert.match(
      apiFns.get("useOperatorQueryEnabled")!,
      /function useOperatorQueryEnabled\(enabled = true\): boolean \{\s*const optical = useIsOpticalStation\(\);\s*return optical && enabled;\s*\}/,
    );
  });

  it("every query of the client takes its `enabled` from that gate (the prompts, the optical state, its history)", () => {
    const queries = [...apiFns].filter(([name, body]) => /\buseQuery\(/.test(body) && name !== "useIsOpticalStation");
    assert.deepEqual(queries.map(([name]) => name).sort(), ["useOperatorPrompts", "useOpticalHistory", "useOpticalState"]);
    for (const [name, body] of queries) {
      assert.match(body, /const on = useOperatorQueryEnabled\(enabled\);/, `${name} must ask the gate`);
      assert.match(body, /\n\s+enabled: on,/, `${name} must run only when the gate is open`);
      assert.doesNotMatch(body, /\n\s+enabled,/, `${name} must not pass the caller's flag straight through`);
    }
  });

  it("the profile query is the only other query, and it is the shared health one", () => {
    const others = [...apiFns].filter(([, body]) => /\buseQuery\(/.test(body)).map(([name]) => name).sort();
    assert.deepEqual(others, ["useIsOpticalStation", "useOperatorPrompts", "useOpticalHistory", "useOpticalState"]);
  });

  it("the socket event that triggers an immediate refetch is listened to only on an optical station", () => {
    assert.match(apiFns.get("useOpenPromptFeed")!, /\},\s*poll && optical,\s*\);/);
  });

  it("the feed is shaped by the pure function, which hands every non-optical station the empty feed", () => {
    assert.match(apiFns.get("useOpenPromptFeed")!, /openPromptFeed\(\{ optical, list, loaded: q\.isSuccess, errorStatus, persistError \}\)/);
    assert.match(read("lib", "operatorPrompts.ts"), /if \(!input\.optical\) return NO_PROMPT_FEED;/);
  });

  it("the history ignores an earlier answer that is still cached once the station is not optical", () => {
    assert.match(apiFns.get("usePromptHistory")!, /const list = optical \? q\.data\?\.prompts : undefined;/);
  });

  it("only the owner of the polling keeps asking what the station is, and only until the service has answered", () => {
    assert.deepEqual(importers(/keepAsking:\s*poll/), ["api/operator.ts"]);
    assert.equal([...API.matchAll(/useIsOpticalStation\(\{ keepAsking: poll \}\)/g)].length, 1);
    assert.match(apiFns.get("useIsOpticalStation")!, /refetchInterval: keepAsking \? \(query\) => \(query\.state\.data === undefined \? PROFILE_RETRY_MS : false\) : false,/);
  });

  it("the nav badge reads the feed, so on an STM station it is the free-text requests alone", () => {
    assert.match(APP_LAYOUT, /wishlistBadge\(operatorPrompts\.openCount, openAgentRequests\)/);
    assert.match(APP_LAYOUT, /urgent=\{operatorPrompts\.hasSafety\}/);
  });

  it("the toast, the sound, the desktop notification and the tab title live in the alert host, which is mounted only while the feed is enabled", () => {
    assert.match(APP_LAYOUT, /\{operatorPrompts\.enabled && <OperatorAlertHost feed=\{operatorPrompts\} \/>\}/);
    assert.deepEqual(importers(/OperatorAlertHost/).filter((n) => n !== "components/operator/OperatorAlertHost.tsx"), ["layout/AppLayout.tsx", "test/ignored"].slice(0, 1));
    // the beep, the notification and the unlock listeners have exactly two callers: the host and the board's own controls
    assert.deepEqual(
      importers(/from "\.\/alertEffects"|from "@\/components\/operator\/alertEffects"/),
      ["components/operator/AlertControls.tsx", "components/operator/OperatorAlertHost.tsx"],
    );
    assert.deepEqual(importers(/import \{ AlertControls \}/), ["components/operator/PromptBoard.tsx"]);
    assert.match(HOST, /document\.title = titleWithCount\(/);
    assert.deepEqual(importers(/titleWithCount\(/).filter((n) => n.startsWith("components") || n.startsWith("layout") || n.startsWith("pages")), ["components/operator/OperatorAlertHost.tsx"]);
  });

  it("no other module of the app builds an /api/operator URL (an import of the client is not one)", () => {
    // string literals only: the comments say outright which endpoints the feature talks to
    const code = (text: string) => text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:"'`\\])\/\/.*$/gm, "$1");
    const offenders = sources
      .filter(([name]) => name !== "api/operator.ts" && !name.startsWith("api/schema"))
      // the optical layout page's dev mock answers these URLs, it does not ask them; it never reaches a build
      // (opticalLayoutWiring.test.ts: only main.tsx imports it, behind import.meta.env.DEV)
      .filter(([name]) => name !== "lib/opticalLayout/devMock.ts")
      .filter(([, text]) => [...code(text).matchAll(/(["'`])((?:\\.|(?!\1)[^\\])*)\1/g)].some((m) => m[2]!.startsWith("/api/operator")))
      .map(([name]) => name);
    assert.deepEqual(offenders, []);
  });
});

describe("identity strings are never translated", () => {
  // kind, status, severity, source and slot ids are compared as they arrive. A tr() on
  // one of them still compares fine in the language it was written in and silently
  // breaks in the other (the lesson from the launcher's "new conversation" title). They
  // are SHOWN through a label function that switches on the raw value.

  it("no comparison involves a tr() call", () => {
    const bad: string[] = [];
    for (const f of OPERATOR_SOURCES) {
      const text = readFileSync(f, "utf8");
      for (const m of text.matchAll(/(?:[=!]==?\s*tr\(|\btr\([^()]*\)\s*[=!]==?)/g)) bad.push(`${f}: ${m[0]}`);
    }
    assert.deepEqual(bad, []);
  });

  it("labels are looked up from the raw value", () => {
    const lib = read("lib", "operatorPrompts.ts");
    for (const id of ["open", "answered", "dismissed", "cancelled", "timeout"]) assert.match(lib, new RegExp(`case "${id}":`));
    for (const id of ["safety", "info"]) assert.match(lib, new RegExp(`case "${id}":`));
    assert.match(lib, /source === "web"/);
    assert.match(lib, /source === "launcher"/);
  });

  it("the code compares the raw status / slot id", () => {
    assert.match(BOARD, /p\.status === "answered"/);
    assert.match(PANEL, /slot\.id === LASER_AREA_SAFE/);
    assert.match(read("lib", "operatorPrompts.ts"), /export function isOpen\(p: OperatorPrompt\): boolean \{\s*return p\.status === "open";/);
  });
});
