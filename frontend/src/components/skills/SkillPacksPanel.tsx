import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";
import { Section, Card, Badge, Spinner, ErrorNote, DegradedNote, EmptyNote } from "@/components/ui";
import { Button, TextField } from "@/components/controls";
import { tr } from "@/lib/i18n";

// ── Signed skill packs ──────────────────────────────────────────────────────
//
// This panel answers two different questions:
//   · which packs are installed?
//   · do they still verify now?
// The second is not settled by verifying once at install time: the files can be modified in the
// interval, and the overlay labels everything under _packs/ as "signed". Every refresh therefore
// recomputes verification.

type Pack = {
  pack_id: string;
  version: string;
  author: string;
  description: string;
  n_files: number;
  valid: boolean;
  reasons: string[];
  signature: string;
};

export function SkillPacksPanel({ pin }: { pin: string }) {
  const qc = useQueryClient();
  const [packId, setPackId] = useState("");
  const [fetchNote, setFetchNote] = useState("");

  const q = useQuery({
    queryKey: ["skill-overlay", "packs"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/skill-overlay/packs");
      if (error) throw error;
      return data;
    },
  });

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["skill-overlay"] });
  };

  const remove = useMutation({
    mutationFn: async (pack_id: string) => {
      const { data, error } = await api.POST("/api/skill-overlay/packs/remove", {
        body: { pack_id, pin },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: invalidate,
  });

  const setAuto = useMutation({
    mutationFn: async (enabled: boolean) => {
      const { data, error } = await api.POST("/api/skill-overlay/packs/auto-enable", {
        body: { enabled, pin },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: invalidate,
  });

  // Fetch a pack from the push server and install it. Without this entry point only installed
  // packs would be visible and installing a new one would require running a script on the machine.
  //
  // The default does not reload: installing is an edit, making it take effect is a separate
  // decision (same as /skill-overlay/entry). Hence two buttons rather than one with a checkbox.
  const fetchPack = useMutation({
    mutationFn: async (v: { pack_id: string; reload_now: boolean }) => {
      const { data, error } = await api.POST("/api/skill-overlay/packs/fetch", {
        body: { pack_id: v.pack_id, pin, reload_now: v.reload_now },
      });
      if (error) throw error;
      return data as unknown as {
        ok: boolean; reason: string; summary: string; needs_reload: boolean;
        reloaded: boolean; shadowed: string[];
        agent_path_pending: boolean | null; fingerprint_matches: boolean | null;
      };
    },
    onSuccess: (d) => {
      invalidate();
      // Show the backend summary verbatim: it covers how many were installed, whether any are
      // shadowed and whether it took effect, and rewriting it here would collapse those.
      setFetchNote(d.ok
        ? d.summary + (d.agent_path_pending ? tr("（agent 侧尚未跟上）", " (agent side not caught up yet)") : "")
        : d.reason || tr("拉取失败", "Fetch failed"));
    },
    onError: (e: any) => setFetchNote(String(e?.message || e)),
  });

  const packs = (q.data?.packs ?? []) as Pack[];
  const auto = q.data?.auto_enable ?? true;
  const bad = packs.filter((p) => !p.valid);

  return (
    <Section title={tr("签名技能包", "Signed skill packs")}>
      {q.isLoading && <Spinner />}
      {q.error && <ErrorNote error={q.error} />}
      {q.data?.degraded && (
        <DegradedNote what={tr(`签名技能包（${q.data.reason || "不可用"}）`, `signed skill packs (${q.data.reason || "unavailable"})`)} />
      )}

      {q.data && !q.data.degraded && (
        <>
          <Card>
            <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
              <Badge tone={auto ? "AUTO" : "INFO"}>
                {auto ? tr("自动启用：开", "Auto-enable: on") : tr("自动启用：关", "Auto-enable: off")}
              </Badge>
              <div style={{ flex: 1, opacity: 0.8, fontSize: "0.9em", lineHeight: 1.7 }}>
                {auto
                  ? tr("签名包装进来就登记进清单并启用 —— 但仍要一次重载才生效。", "A signed pack is added to the manifest and enabled as soon as it is installed, but it still needs a reload to take effect.")
                  : tr("签名包照装，但不自动登记 —— 什么时候换由你决定（适合正在跑长实验的机器）。", "Signed packs are still installed but not registered automatically, so you decide when to switch (suited to machines running a long experiment).")}
              </div>
              <Button
                onClick={() => setAuto.mutate(!auto)}
                loading={setAuto.isPending}
              >
                {auto ? tr("关掉", "Turn off") : tr("打开", "Turn on")}
              </Button>
            </div>
            {q.data.reason && (
              <div style={{ marginTop: 8 }}>
                <Badge tone="DANGEROUS">{tr("清单读不出来", "Manifest unreadable")}</Badge>{" "}
                {q.data.reason} {tr(
                  "—— 这种状态下装包不会自动启用（当成空清单写回去会抹掉你已经启用的条目）。",
                  "— in this state installed packs are not enabled automatically (writing back an empty manifest would erase the entries you already enabled).",
                )}
              </div>
            )}
            <div style={{ marginTop: 10, display: "flex", gap: 8, alignItems: "center",
                          flexWrap: "wrap" }}>
              <div style={{ width: 200 }}>
                <TextField value={packId} onChange={setPackId}
                           placeholder={tr("pack_id（从推送服务器索引取）", "pack_id (from the push server index)")} mono />
              </div>
              <Button
                disabled={!packId.trim() || fetchPack.isPending}
                loading={fetchPack.isPending}
                onClick={() => fetchPack.mutate({ pack_id: packId.trim(), reload_now: false })}
              >
                {tr("拉取并安装", "Fetch and install")}
              </Button>
              <Button
                variant="primary"
                disabled={!packId.trim() || fetchPack.isPending}
                onClick={() => fetchPack.mutate({ pack_id: packId.trim(), reload_now: true })}
              >
                {tr("拉取并立即生效", "Fetch and apply now")}
              </Button>
              <span style={{ fontSize: "0.85em", opacity: 0.75 }}>
                {tr("验签 · 逐文件哈希 · 装不上就整包拒绝", "Signature check · per-file hash · the whole pack is rejected if any part fails")}
              </span>
            </div>
            {fetchNote && (
              <div style={{ marginTop: 6, fontSize: "0.9em" }}>{fetchNote}</div>
            )}
          </Card>

          {bad.length > 0 && (
            <Card>
              <Badge tone="DANGEROUS">{tr("复核不过", "Re-check failed")}</Badge>{" "}
              {tr(
                `${bad.length} 个包现在验不过了 —— 落盘之后被改动过，或者不是通过签名通道装进来的。`,
                `${bad.length} pack(s) no longer verify: modified after being written to disk, or not installed through the signed channel.`,
              )}
              <div style={{ opacity: 0.75, marginTop: 4, fontSize: "0.9em" }}>
                {tr("它们的条目在下一次重载时会被", "On the next reload their entries will be ")}
                <strong>{tr("整包拒绝", "rejected as a whole")}</strong>
                {tr(
                  "，对应的技能退回内置版。这是有意的：「已签名」那个标记必须有凭据。",
                  ", and the matching skills fall back to the built-in version. This is intentional: the \"signed\" label must be backed by a valid signature.",
                )}
              </div>
            </Card>
          )}

          {packs.length === 0 ? (
            <EmptyNote label={tr("这台机器还没有装过签名技能包", "No signed skill packs have been installed on this machine")} />
          ) : (
            <div style={{ display: "grid", gap: 8 }}>
              {packs.map((p) => (
                <Card key={p.pack_id}>
                  <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
                    <Badge tone={p.valid ? "AUTO" : "DANGEROUS"}>
                      {p.valid ? tr("已验证", "Verified") : tr("验不过", "Fails verification")}
                    </Badge>
                    <code style={{ fontWeight: 600 }}>{p.pack_id}</code>
                    <span style={{ opacity: 0.8 }}>v{p.version}</span>
                    <span style={{ opacity: 0.6, fontSize: "0.9em" }}>
                      {tr(`${p.n_files} 个文件`, `${p.n_files} files`)}
                    </span>
                    <span style={{ flex: 1 }} />
                    <code style={{ opacity: 0.7, fontSize: "0.85em" }}>
                      {p.signature}
                    </code>
                    <Button
                      variant="danger"
                      onClick={() => remove.mutate(p.pack_id)}
                      loading={remove.isPending}
                    >
                      {tr("移除", "Remove")}
                    </Button>
                  </div>
                  {(p.author || p.description) && (
                    <div style={{ opacity: 0.75, marginTop: 6, fontSize: "0.9em" }}>
                      {p.author && <>{tr("作者 ", "Author ")}{p.author}{"  "}</>}
                      {p.description}
                    </div>
                  )}
                  {p.reasons.length > 0 && (
                    <div style={{ marginTop: 6 }}>
                      {p.reasons.map((r, i) => (
                        <div key={i} style={{ fontSize: "0.9em" }}>
                          · {r}
                        </div>
                      ))}
                    </div>
                  )}
                </Card>
              ))}
            </div>
          )}
        </>
      )}
    </Section>
  );
}
