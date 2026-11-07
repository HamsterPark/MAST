/**
 * 设置 → 远程访问 helpers + the panel's wiring (RemoteAccessSection.tsx).
 *
 * Remote viewing can fail even when the Tailscale tunnel works, for reasons the
 * panel never mentioned — a system proxy without 100.* in its bypass list, and the
 * two machines sitting in different tailnets. The checklist must keep naming both.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it } from "node:test";

import { groupHex, lanUrls, troubleshootTips, viewerLine } from "../src/lib/remoteAccess.ts";

const HERE = fileURLToPath(new URL(".", import.meta.url));
const PANEL = join(HERE, "..", "src", "components", "settings", "RemoteAccessSection.tsx");

describe("viewerLine", () => {
  it("names the Tailscale path and the peer", () => {
    const s = viewerLine({ client_ip: "<rig-host>", host: "<rig-host>:7862", via: "tailscale" });
    assert.ok(s && s.includes("Tailscale") && s.includes("<rig-host>"));
  });
  it("local and LAN", () => {
    assert.equal(viewerLine({ client_ip: "127.0.0.1", via: "local" }), "本次访问：本机");
    assert.ok(viewerLine({ client_ip: "10.0.0.10", via: "lan" })?.includes("局域网"));
  });
  it("absent or unknown viewer renders nothing misleading", () => {
    assert.equal(viewerLine(undefined), null);
    assert.equal(viewerLine(null), null);
    assert.equal(viewerLine({ via: "other" }), null);
    assert.equal(viewerLine({ via: "other", client_ip: "203.0.113.42" }), "本次访问：来自 203.0.113.42");
  });
});

describe("lanUrls", () => {
  it("lists every candidate (VPN laptop: tunnel + Wi-Fi)", () => {
    assert.deepEqual(lanUrls("https", 7862, ["192.168.50.7", "10.0.1.79"], "192.168.50.7"), [
      "https://192.168.50.7:7862",
      "https://10.0.1.79:7862",
    ]);
  });
  it("falls back to the legacy single lan_ip, and to nothing", () => {
    assert.deepEqual(lanUrls("https", 7862, undefined, "10.0.0.11"), ["https://10.0.0.11:7862"]);
    assert.deepEqual(lanUrls("http", 7862, [], ""), []);
  });
  it("de-duplicates", () => {
    assert.deepEqual(lanUrls("https", 1, ["10.0.0.1", "10.0.0.1"]), ["https://10.0.0.1:1"]);
  });
});

describe("groupHex", () => {
  it("groups a 64-char pin into eight blocks", () => {
    const pin = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
    const g = groupHex(pin);
    assert.equal(g.split(" ").length, 8);
    assert.equal(g.replace(/ /g, ""), pin);
  });
  it("tolerates colons / case", () => {
    assert.equal(groupHex("AB:CD:EF", 2), "ab cd ef");
  });
});

describe("troubleshootTips", () => {
  const tips = troubleshootTips("user@example.com");
  it("tailnet first, with the name", () => {
    assert.ok(tips[0].includes("user@example.com"));
    assert.ok(tips[0].includes("同一个网络"));
  });
  it("names the proxy bypass range", () => {
    assert.ok(tips.some((t) => t.includes("100.64.0.0/10") && t.includes("代理")));
  });
  it("warns off .ts.net names", () => {
    assert.ok(tips.some((t) => t.includes(".ts.net")));
  });
  it("works without a tailnet name", () => {
    const first = troubleshootTips(undefined)[0];
    assert.ok(first.includes("同一个网络。"));
    assert.ok(!first.includes("同一个网络同一个网络"));
  });
});

describe("RemoteAccessSection wiring", () => {
  const src = readFileSync(PANEL, "utf8");
  it("renders the checklist, the viewer line and the pin", () => {
    assert.ok(src.includes("troubleshootTips("), "troubleshooting list not rendered");
    assert.ok(src.includes("viewerLine("), "viewer line not rendered");
    assert.ok(src.includes("tls.spki_sha256"), "SPKI pin not shown");
  });
  it("lists every LAN candidate instead of one guessed address", () => {
    assert.ok(src.includes("lanUrls("));
    assert.ok(!src.includes("{d.scheme}://{d.lan_ip}"), "single guessed LAN IP is back");
  });
});
