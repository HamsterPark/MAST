import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type ColumnDef } from "@tanstack/react-table";
import { api } from "@/api/client";
import { DataTable } from "@/components/DataTable";
import {
  Section,
  Card,
  Badge,
  Spinner,
  ErrorNote,
  DegradedNote,
  EmptyNote,
} from "@/components/ui";
import { Field, TextField, Button, Modal, useToast } from "@/components/controls";
import { SkillEjectPanel } from "./SkillEjectPanel";
import { SkillPacksPanel } from "./SkillPacksPanel";
import { tr } from "@/lib/i18n";

// ── Skill overlay — change a skill without shipping a new version ──────────
//
// The panel's first job is not listing entries but answering one question: is it in effect now?
//
// The feature has three independent failure paths, none of which raises an error:
//   · the reload was queued (a task is running), so the registry has not changed;
//   · the registry changed but the agent graph has not been rebuilt, so the model still holds
//     the old tools;
//   · the rebuild was scheduled but failed.
// Every backend response therefore carries fingerprint_matches / agent_path_pending, and the UI
// follows those, not `ok`. The state is three-valued: null means undecidable (process just
// started, tool table never built) and must render as a grey "Unknown", never a green
// "In effect".

type EntryRow = {
  path: string;
  enabled: boolean;
  exists: boolean;
  overlay_of: string;
  applied: boolean;
  skills: string[];
  sha256: string;
  error: string;
  valid_path: boolean;
  path_error: string;
};

type SkillInfo = {
  name: string;
  origin: string;
  module: string;
  source_path: string;
  short_sha: string;
  displaced_module: string;
  signature: string;
  described: string;
};

/** Whether the built-in version each entry was based on has changed since.
 *
 * This is a subtler variant of "assumed to be in effect": the overlay is in effect but is based
 * on code several versions old, so it reverts upstream bug fixes. It is therefore shown in the
 * main list rather than behind a button someone has to remember to click.
 */
function useDrift() {
  return useQuery({
    queryKey: ["skill-overlay", "drift"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/skill-overlay/drift");
      if (error) throw error;
      return data;
    },
  });
}

function useOverlay() {
  return useQuery({
    queryKey: ["skill-overlay"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/skill-overlay");
      if (error) throw error;
      return data;
    },
  });
}

/** Effect-status banner: three states, and "unknown" must not look like "in effect". */
function EffectBanner({
  matches,
  registryOverlaid,
  agentOverlaid,
  pendingRebuild,
}: {
  matches: boolean | null | undefined;
  registryOverlaid: string[];
  agentOverlaid: string[];
  pendingRebuild: boolean;
}) {
  if (pendingRebuild) {
    return (
      <Card>
        <Badge tone="WARN">{tr("已排队", "Queued")}</Badge>{" "}
        {tr("当前有任务在运行，覆盖层重载", "A task is running; the overlay reload is ")}
        <strong>{tr("已排队", "queued")}</strong>
        {tr("——任务结束后自动生效。", " and takes effect automatically when the task ends.")}
        <div style={{ opacity: 0.75, marginTop: 4, fontSize: "0.9em" }}>
          {tr(
            "正在跑的流程从头到尾用它启动时那一版：技能是逐步现查的，中途换会让同一次实验的前后半段行为不一致。",
            "A running workflow uses the version it started with: skills are looked up step by step, and swapping mid-run would make the two halves of one experiment behave differently.",
          )}
        </div>
      </Card>
    );
  }
  if (matches === true) {
    return (
      <Card>
        <Badge tone="AUTO">{tr("已生效", "In effect")}</Badge>{" "}
        {tr("agent 手上的工具表与注册表一致", "The agent's tool table matches the registry")}
        {agentOverlaid.length > 0 && (
          <>{tr(`（${agentOverlaid.length} 个覆盖版技能）`, ` (${agentOverlaid.length} overlaid skills)`)}</>
        )}
        {tr("。", ".")}
      </Card>
    );
  }
  if (matches === false) {
    const missing = registryOverlaid.filter((n) => !agentOverlaid.includes(n));
    return (
      <Card>
        <Badge tone="DANGEROUS">{tr("尚未生效", "Not in effect yet")}</Badge>{" "}
        {tr("注册表已经换了，但 ", "The registry has changed, but ")}
        <strong>{tr("agent 手上那张工具表还是旧的", "the agent's tool table is still the old one")}</strong>
        {tr(
          " —— 模型现在调的仍是覆盖之前的实现。点上面的「重新加载技能」。",
          ": the model is still calling the pre-overlay implementation. Click \"Reload skills\" above.",
        )}
        {missing.length > 0 && (
          <div style={{ opacity: 0.75, marginTop: 4, fontSize: "0.9em" }}>
            {tr("agent 还看不到：", "Not yet visible to the agent: ")}{missing.join(tr("、", ", "))}
          </div>
        )}
      </Card>
    );
  }
  return (
    <Card>
      <Badge tone="INFO">{tr("未知", "Unknown")}</Badge>{" "}
      {tr("还没有建过 agent 工具表（服务刚启动，或者从未跑过任务），", "The agent tool table has not been built yet (the service just started, or no task has run), so it ")}
      <strong>{tr("判断不了", "cannot be determined")}</strong>
      {tr("是否生效。", " whether the overlay is in effect.")}
      <div style={{ opacity: 0.75, marginTop: 4, fontSize: "0.9em" }}>
        {tr(
          "这不等于「没生效」，也不等于「已生效」—— 跑一次任务或点一次重载之后这里才有答案。",
          "This does not mean \"not in effect\" or \"in effect\"; the answer appears after a task runs or a reload is triggered.",
        )}
      </div>
    </Card>
  );
}

export function SkillOverlayManager() {
  const q = useOverlay();
  const qc = useQueryClient();
  const { toast, node: toastNode } = useToast();
  const [pin, setPin] = useState("");
  const [detail, setDetail] = useState<EntryRow | null>(null);
  const [lastReload, setLastReload] = useState<string>("");

  const reload = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/skill-overlay/reload", {
        body: { pin, reason: "ui" },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (d: any) => {
      setLastReload(d?.summary || "");
      qc.invalidateQueries({ queryKey: ["skill-overlay"] });
      qc.invalidateQueries({ queryKey: ["skills", "catalog"] });
      if (d?.degraded) {
        toast(d.reason || tr("覆盖层不可用", "Overlay unavailable"), "err");
      } else if (d?.status === "queued") {
        // Queued is not success. The toast only has ok/err levels, which cannot express three
        // states, so use err: it is better to look heavy than to have "scheduled" read as
        // "done", since the user would proceed as if it were in effect. The authoritative
        // three-state display is the EffectBanner below.
        toast(tr("尚未生效：任务运行中，已排队 —— 当前任务结束后自动生效", "Not in effect yet: a task is running, reload queued. It takes effect when the current task ends."), "err");
      } else if (d?.failed?.length) {
        toast(tr(`${d.failed.length} 个覆盖模块被拒绝，见下方原因`, `${d.failed.length} overlay module(s) rejected; see the reasons below`), "err");
      } else if (d?.agent_path_pending) {
        toast(tr("尚未生效：注册表已更新，但 agent 工具表还没跟上", "Not in effect yet: the registry is updated but the agent tool table has not caught up"), "err");
      } else {
        toast(tr(`已生效（${d?.applied?.length ?? 0} 个覆盖模块）`, `In effect (${d?.applied?.length ?? 0} overlay module(s))`), "ok");
      }
    },
    onError: (e: any) => toast(String(e?.message || e), "err"),
  });

  const setEntry = useMutation({
    mutationFn: async (v: { path: string; enabled: boolean }) => {
      const { data, error } = await api.POST("/api/skill-overlay/entry", {
        body: { path: v.path, enabled: v.enabled, pin, allow_removals: [], note: "" },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (d: any) => {
      qc.invalidateQueries({ queryKey: ["skill-overlay"] });
      if (!d?.ok) toast(d?.reason || tr("写入失败", "Write failed"), "err");
      // Deliberately no automatic reload: writing the manifest is an edit, making it take effect
      // is a separate decision. The message must say a reload is still needed, otherwise ticking
      // the checkbox looks like the change is done.
      else toast(tr("清单已更新，尚未生效 —— 点「重新加载技能」", "Manifest updated, not in effect yet: click \"Reload skills\""), "err");
    },
    onError: (e: any) => toast(String(e?.message || e), "err"),
  });

  const restore = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/skill-overlay/restore-all", {
        body: { pin, reason: "ui" },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (d: any) => {
      qc.invalidateQueries({ queryKey: ["skill-overlay"] });
      toast(d?.ok ? d.summary || tr("已恢复到启动基线", "Restored to the startup baseline") : d?.reason || tr("恢复失败", "Restore failed"),
            d?.ok ? "ok" : "err");
    },
    onError: (e: any) => toast(String(e?.message || e), "err"),
  });

  const driftQ = useDrift();
  const drifted = ((driftQ.data ?? []) as any[]).filter((d) => d.drifted === true);
  const driftUnknown = ((driftQ.data ?? []) as any[]).filter(
    (d) => d.known && d.drifted === null);

  const entries = (q.data?.entries ?? []) as EntryRow[];
  const untracked = (q.data?.untracked ?? []) as string[];
  const skills = (q.data?.overlaid_skills ?? []) as SkillInfo[];

  const cols = useMemo<ColumnDef<EntryRow>[]>(
    () => [
      { accessorKey: "path", header: tr("文件", "File") },
      {
        id: "覆盖",
        header: tr("覆盖", "Overrides"),
        cell: ({ row }) =>
          row.original.overlay_of ? (
            <code style={{ fontSize: "0.85em" }}>{row.original.overlay_of}</code>
          ) : (
            <span style={{ opacity: 0.6 }}>{tr("新增", "New")}</span>
          ),
      },
      {
        id: "状态",
        header: tr("状态", "Status"),
        cell: ({ row }) => {
          const r = row.original;
          if (!r.exists) return <Badge tone="DANGEROUS">{tr("文件不在", "File missing")}</Badge>;
          if (!r.valid_path) return <Badge tone="DANGEROUS">{tr("路径非法", "Invalid path")}</Badge>;
          if (!r.enabled) return <Badge tone="INFO">{tr("未启用", "Disabled")}</Badge>;
          if (r.applied) return <Badge tone="AUTO">{tr("已生效", "In effect")}</Badge>;
          // Enabled and present but not in effect: either rejected or not yet reloaded.
          return <Badge tone="WARN">{tr("未加载", "Not loaded")}</Badge>;
        },
      },
      {
        id: "技能",
        header: tr("技能", "Skills"),
        cell: ({ row }) =>
          row.original.skills.length ? (
            <span>{row.original.skills.join(tr("、", ", "))}</span>
          ) : (
            <span style={{ opacity: 0.6 }}>—</span>
          ),
      },
      {
        id: "sha",
        header: "sha",
        cell: ({ row }) => (
          <code style={{ fontSize: "0.85em", opacity: 0.75 }}>
            {row.original.sha256 ? row.original.sha256.slice(0, 8) : "—"}
          </code>
        ),
      },
      {
        id: "操作",
        header: "",
        cell: ({ row }) => (
          <div style={{ display: "flex", gap: 6 }}>
            <Button
              onClick={() =>
                setEntry.mutate({ path: row.original.path, enabled: !row.original.enabled })
              }
              disabled={setEntry.isPending}
            >
              {row.original.enabled ? tr("停用", "Disable") : tr("启用", "Enable")}
            </Button>
            <Button onClick={() => setDetail(row.original)}>{tr("详情", "Details")}</Button>
          </div>
        ),
      },
    ],
    [setEntry],
  );

  if (q.isPending) return <Spinner />;
  if (q.isError) return <ErrorNote error={q.error} />;
  if (q.data?.degraded)
    return <DegradedNote what={tr(`技能覆盖层（${q.data.reason || "不可用"}）`, `skill overlay (${q.data.reason || "unavailable"})`)} />;

  return (
    <div style={{ display: "grid", gap: 12 }}>
      <Card>
        <div style={{ display: "flex", gap: 8, alignItems: "flex-end", flexWrap: "wrap" }}>
          <Field label={tr("管理员 PIN（未设置则留空）", "Admin PIN (leave empty if not set)")}>
            <TextField value={pin} onChange={setPin} placeholder="" />
          </Field>
          <Button onClick={() => reload.mutate()} disabled={reload.isPending}>
            {reload.isPending ? tr("重新加载中…", "Reloading…") : tr("重新加载技能", "Reload skills")}
          </Button>
          <Button onClick={() => restore.mutate()} disabled={restore.isPending}>
            {tr("全部恢复内置", "Restore all built-ins")}
          </Button>
        </div>
        <div style={{ opacity: 0.75, marginTop: 8, fontSize: "0.9em" }}>
          {tr("覆盖层目录：", "Overlay directory: ")}<code>{q.data?.overlay_dir}</code>
          <br />
          {tr("把改好的 ", "Put the edited ")}<code>.py</code>
          {tr(" 放进去（路径就是它覆盖的模块，例如", " files in (the path is the module it overrides, e.g.")}{" "}
          <code>builtins/bias.py</code> {tr("覆盖", "overrides")} <code>mast.skills.builtins.bias</code>
          {tr("），启用，然后重新加载。覆盖", "), enable them, then reload. An overlay can ")}
          <strong>{tr("只能收紧", "only tighten")}</strong>
          {tr(
            "安全画像 —— 放宽包络请走「技能详情编辑」里的管理员覆写。",
            " the safety profile; to widen the envelope use the admin overrides in \"Skill editor\".",
          )}
        </div>
      </Card>

      {drifted.length > 0 && (
        <Card>
          <Badge tone="WARN">{tr("内置版已变", "Built-in changed")}</Badge>{" "}
          {tr(
            `${drifted.length} 份覆盖基于的是更早版本的内置代码：`,
            `${drifted.length} overlay(s) are based on an earlier version of the built-in code: `,
          )}
          {drifted.map((d) => d.rel).join(tr("、", ", "))}{tr("。", ".")}
          <div style={{ opacity: 0.75, marginTop: 4, fontSize: "0.9em" }}>
            {tr("覆盖是", "The overlay is ")}<strong>{tr("生效的", "in effect")}</strong>
            {tr(
              " —— 问题在于它可能把上游后来的修复一起盖了回去。重新导出一份，再把你的改动挪过去。",
              ", but it may revert later upstream fixes. Export a fresh copy and move your changes onto it.",
            )}
          </div>
        </Card>
      )}
      {driftUnknown.length > 0 && (
        <Card>
          <Badge tone="INFO">{tr("判断不了", "Undecidable")}</Badge>{" "}
          {tr(
            `${driftUnknown.length} 份覆盖对不出基于哪一版内置代码（`,
            `${driftUnknown.length} overlay(s) cannot be matched to a built-in version (`,
          )}
          {driftUnknown.map((d) => d.rel).join(tr("、", ", "))}{tr("）。", ").")}
          <div style={{ opacity: 0.75, marginTop: 4, fontSize: "0.9em" }}>
            {tr("这不等于「没变」 —— 手写的覆盖没有 sidecar，无从比对。", "This does not mean \"unchanged\": a hand-written overlay has no sidecar to compare against.")}
          </div>
        </Card>
      )}

      <EffectBanner
        matches={q.data?.fingerprint_matches as boolean | null | undefined}
        registryOverlaid={(q.data?.registry_overlaid ?? []) as string[]}
        agentOverlaid={(q.data?.agent_overlaid ?? []) as string[]}
        pendingRebuild={!!q.data?.pending_rebuild}
      />

      {(q.data?.baseline_drift ?? []).length > 0 && (
        <Card>
          <Badge tone="DANGEROUS">{tr("基线漂移", "Baseline drift")}</Badge>{" "}
          {tr(
            "下列技能既不是内置版、也不由覆盖层解释 —— 说明某次回滚没回干净：",
            "These skills are neither the built-in version nor explained by the overlay, so an earlier rollback was not clean: ",
          )}
          {(q.data!.baseline_drift as string[]).join(tr("、", ", "))}{tr("。", ".")}{" "}
          {tr("可以用「全部恢复内置」硬复位。", "Use \"Restore all built-ins\" for a hard reset.")}
        </Card>
      )}

      {lastReload && (
        <Card>
          <div style={{ fontWeight: 600, marginBottom: 4 }}>{tr("上一次重载", "Last reload")}</div>
          <pre style={{ margin: 0, whiteSpace: "pre-wrap", fontSize: "0.9em" }}>
            {lastReload}
          </pre>
        </Card>
      )}

      <Section title={tr(`覆盖层条目（${entries.length}）`, `Overlay entries (${entries.length})`)}>
        {entries.length === 0 ? (
          <EmptyNote label={tr("覆盖层里还没有条目", "No entries in the overlay yet")} />
        ) : (
          <DataTable columns={cols} data={entries} />
        )}
      </Section>

      {untracked.length > 0 && (
        <Card>
          <div style={{ fontWeight: 600, marginBottom: 6 }}>
            {tr(`目录里有、但清单里没登记（${untracked.length}）`, `In the directory but not in the manifest (${untracked.length})`)}
          </div>
          <div style={{ opacity: 0.75, fontSize: "0.9em", marginBottom: 8 }}>
            {tr("拷进目录", "Copying a file into the directory ")}<strong>{tr("不会", "does not")}</strong>
            {tr(
              "自动生效 —— 启用是一次显式动作。不单独列出来的话，这些文件会被当成「怎么改了没反应」。",
              " take effect automatically; enabling is an explicit action. Without this list these files would look like edits that do nothing.",
            )}
          </div>
          <div style={{ display: "grid", gap: 6 }}>
            {untracked.map((p) => (
              <div key={p} style={{ display: "flex", gap: 8, alignItems: "center" }}>
                <code style={{ flex: 1 }}>{p}</code>
                <Button onClick={() => setEntry.mutate({ path: p, enabled: true })}>
                  {tr("登记并启用", "Register and enable")}
                </Button>
              </div>
            ))}
          </div>
        </Card>
      )}

      {skills.length > 0 && (
        <Section title={tr(`已被覆盖的技能（${skills.length}）`, `Overlaid skills (${skills.length})`)}>
          <div style={{ display: "grid", gap: 6 }}>
            {skills.map((s) => (
              <Card key={s.name}>
                <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                  <strong>{s.name}</strong>
                  <Badge tone={s.signature.startsWith("verified") ? "AUTO" : "INFO"}>
                    {s.signature.startsWith("verified") ? tr("已签名", "Signed") : tr("本机未签名", "Unsigned locally")}
                  </Badge>
                </div>
                <div style={{ opacity: 0.8, fontSize: "0.9em", marginTop: 4 }}>
                  {s.described}
                </div>
              </Card>
            ))}
          </div>
        </Section>
      )}

      <Modal
        open={!!detail}
        onClose={() => setDetail(null)}
        title={detail ? tr(`覆盖层条目：${detail.path}`, `Overlay entry: ${detail.path}`) : ""}
        wide
      >
        {detail && (
          <div style={{ display: "grid", gap: 8 }}>
            <div>
              {tr("覆盖：", "Overrides: ")}
              <code>{detail.overlay_of || tr("（纯新增，不覆盖任何内置模块）", "(new file, overrides no built-in module)")}</code>
            </div>
            <div>{tr("文件存在：", "File exists: ")}{detail.exists ? tr("是", "Yes") : tr("否", "No")}</div>
            <div>{tr("已启用：", "Enabled: ")}{detail.enabled ? tr("是", "Yes") : tr("否", "No")}</div>
            <div>{tr("本轮生效：", "In effect this round: ")}{detail.applied ? tr("是", "Yes") : tr("否", "No")}</div>
            <div>{tr("提供技能：", "Provides skills: ")}{detail.skills.join(tr("、", ", ")) || "—"}</div>
            <div>
              sha256：<code>{detail.sha256 || "—"}</code>
            </div>
            {detail.path_error && (
              <Card>
                <Badge tone="DANGEROUS">{tr("路径非法", "Invalid path")}</Badge> {detail.path_error}
              </Card>
            )}
            {detail.error && (
              <Card>
                <Badge tone="DANGEROUS">{tr("被拒绝", "Rejected")}</Badge>
                <pre style={{ margin: "6px 0 0", whiteSpace: "pre-wrap" }}>
                  {detail.error}
                </pre>
              </Card>
            )}
          </div>
        )}
      </Modal>

      <SkillPacksPanel pin={pin} />
      <SkillEjectPanel pin={pin} />
      {toastNode}
    </div>
  );
}
