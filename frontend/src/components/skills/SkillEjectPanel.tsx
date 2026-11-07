import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";
import { Section, Card, Badge, Spinner, ErrorNote, DegradedNote } from "@/components/ui";
import { Field, TextField, Button, Modal } from "@/components/controls";
import { tr } from "@/lib/i18n";

// ── Export to overlay ───────────────────────────────────────────────────────
//
// In the packaged build all sources live in the PYZ archive embedded in MAST.exe, so no .py
// files exist on disk and a skill cannot be edited without extracting it. This panel extracts it.
//
// Its main job is the warning shown after export: the overlay replaces the skill class
// registered in the registry, but `from mast.skills.builtins.bias import SetBias` holds the old
// reference on the module, which hot reload cannot swap. If those import sites were not listed
// here, an edit would silently have no effect while the UI looked normal. A successful export
// therefore always opens a result dialog instead of a toast.

type Mod = {
  dotted: string;
  rel: string;
  n_bytes: number;
  already: boolean;
  skill_classes: string[];
  undecidable: string[];
};

type Site = {
  file: string;
  line: number;
  kind: string;
  names: string[];
  text: string;
  binds_skill: boolean;
};

type EjectResult = {
  ok: boolean;
  reason: string;
  dotted: string;
  rel: string;
  path: string;
  sha256: string;
  n_bytes: number;
  skill_classes: string[];
  undecidable: string[];
  n_skill_binds: number;
  warning: string;
  importers: Site[];
};

/** Export result: the three warning kinds lead to three different decisions, so the badges differ. */
function WarnBadge({ r }: { r: EjectResult }) {
  if (r.undecidable.length > 0) return <Badge tone="INFO">{tr("判断不了", "Undecidable")}</Badge>;
  if (r.skill_classes.length === 0) return <Badge tone="DANGEROUS">{tr("不会生效", "Will not take effect")}</Badge>;
  if (r.n_skill_binds > 0) return <Badge tone="WARN">{tr("部分生效", "Partly effective")}</Badge>;
  return <Badge tone="AUTO">{tr("全线生效", "Fully effective")}</Badge>;
}

export function SkillEjectPanel({ pin }: { pin: string }) {
  const qc = useQueryClient();
  const [q, setQ] = useState("");
  const [result, setResult] = useState<EjectResult | null>(null);
  const [busy, setBusy] = useState("");

  const list = useQuery({
    queryKey: ["skill-overlay", "ejectable", q],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/skill-overlay/ejectable", {
        params: { query: { q } },
      });
      if (error) throw error;
      return data;
    },
  });

  const eject = useMutation({
    mutationFn: async (v: { dotted: string; overwrite: boolean }) => {
      const { data, error } = await api.POST("/api/skill-overlay/eject", {
        body: { dotted: v.dotted, overwrite: v.overwrite, pin },
      });
      if (error) throw error;
      return data as EjectResult;
    },
    onSettled: () => setBusy(""),
    onSuccess: (d) => {
      qc.invalidateQueries({ queryKey: ["skill-overlay"] });
      // Show the dialog on both success and failure: a refusal (the overlay already has edits)
      // matters more to read than a success.
      setResult(d);
    },
  });

  const mods = (list.data?.modules ?? []) as Mod[];
  const searching = q.trim().length > 0;

  return (
    <Section title={tr("导出内置技能到覆盖层", "Export built-in skills to the overlay")}>
      <Card>
        <div style={{ opacity: 0.8, fontSize: "0.92em", lineHeight: 1.7 }}>
          {tr("打包版里源码全在 ", "In the packaged build all sources live inside ")}
          <code>MAST.exe</code>
          {tr("，磁盘上没有 ", " and there is no ")}
          <code>.py</code>
          {tr(" 文件。", " on disk. ")}
          {tr("导出会把那份字节原样写进覆盖层目录，", "Export writes the bytes unchanged into the overlay directory, ")}
          <strong>{tr("不启用", "without enabling it")}</strong>
          {tr(
            " —— 改完之后回上面勾选启用，再点「重新加载技能」。",
            ". After editing, enable it above and click \"Reload skills\".",
          )}
          <br />
          {tr("搜索既认模块名，也认技能类名（搜 ", "Search matches module names and skill class names (searching ")}
          <code>SetBias</code>
          {tr(" 会找到", " finds")}{" "}
          <code>builtins/bias.py</code>
          {tr("）。", ")." )}
        </div>
        <div style={{ marginTop: 10, maxWidth: 420 }}>
          <Field label={tr("搜索", "Search")} hint={tr("留空则列出全部可导出模块", "Leave empty to list all exportable modules")}>
            <TextField value={q} onChange={setQ} placeholder={tr("SetBias 或 builtins.bias", "SetBias or builtins.bias")} mono />
          </Field>
        </div>
      </Card>

      {list.isLoading && <Spinner />}
      {list.error && <ErrorNote error={list.error} />}
      {list.data?.degraded && (
        <DegradedNote what={tr(`导出源码（${list.data.reason || "不可用"}）`, `source export (${list.data.reason || "unavailable"})`)} />
      )}

      {list.data && !list.data.degraded && (
        <Card>
          <div style={{ opacity: 0.75, fontSize: "0.9em", marginBottom: 8 }}>
            {tr(`${mods.length} 个模块`, `${mods.length} modules`)}
            {searching
              ? tr("（已按搜索过滤）", " (filtered by search)")
              : tr("（未搜索时不做类分析，所以这里不列技能名）", " (class analysis is skipped without a search, so skill names are not listed)")}
            {tr("，源码来自 ", ", sources from ")}<code>{list.data.source_root}</code>
          </div>
          <div style={{ display: "grid", gap: 6, maxHeight: 460, overflowY: "auto" }}>
            {mods.map((m) => (
              <div
                key={m.dotted}
                style={{
                  display: "flex", gap: 10, alignItems: "center",
                  padding: "4px 0", borderBottom: "1px solid rgba(128,128,128,.15)",
                }}
              >
                <code style={{ flex: 1 }}>{m.dotted}</code>
                {m.skill_classes.length > 0 && (
                  <span style={{ opacity: 0.7, fontSize: "0.85em" }}>
                    {m.skill_classes.slice(0, 3).join(tr("、", ", "))}
                    {m.skill_classes.length > 3 && ` +${m.skill_classes.length - 3}`}
                  </span>
                )}
                {m.undecidable.length > 0 && <Badge tone="INFO">{tr("判断不了", "Undecidable")}</Badge>}
                {m.already && <Badge tone="WARN">{tr("已在覆盖层", "In overlay")}</Badge>}
                <span style={{ opacity: 0.55, fontSize: "0.85em" }}>
                  {(m.n_bytes / 1024).toFixed(1)} KB
                </span>
                <Button
                  onClick={() => {
                    setBusy(m.dotted);
                    eject.mutate({ dotted: m.dotted, overwrite: false });
                  }}
                  loading={busy === m.dotted}
                  disabled={busy !== ""}
                >
                  {tr("导出", "Export")}
                </Button>
              </div>
            ))}
          </div>
        </Card>
      )}

      <Modal
        open={result !== null}
        onClose={() => setResult(null)}
        title={result?.ok ? tr("已导出到覆盖层", "Exported to overlay") : tr("没有导出", "Not exported")}
        wide
      >
        {result && (
          <div style={{ display: "grid", gap: 10, fontSize: "0.94em" }}>
            {!result.ok ? (
              <Card>
                <Badge tone="DANGEROUS">{tr("未导出", "Not exported")}</Badge>
                <div style={{ marginTop: 6, whiteSpace: "pre-wrap" }}>{result.reason}</div>
                {result.reason.includes("overwrite") && (
                  <div style={{ marginTop: 10 }}>
                    <Button
                      variant="danger"
                      onClick={() => {
                        const d = result.dotted;
                        setResult(null);
                        setBusy(d);
                        eject.mutate({ dotted: d, overwrite: true });
                      }}
                    >
                      {tr("仍然覆盖（会丢掉覆盖层里现有那份）", "Overwrite anyway (discards the copy currently in the overlay)")}
                    </Button>
                  </div>
                )}
              </Card>
            ) : (
              <>
                <div>
                  <code>{result.dotted}</code> → <code>{result.rel}</code>{" ("}
                  {(result.n_bytes / 1024).toFixed(1)} KB, sha{" "}
                  <code>{result.sha256.slice(0, 12)}</code>)
                </div>
                <Card>
                  <WarnBadge r={result} />
                  <div style={{ marginTop: 6, lineHeight: 1.7 }}>{result.warning}</div>
                </Card>
                <div style={{ opacity: 0.8 }}>
                  {tr("这个模块里的技能类：", "Skill classes in this module: ")}
                  {result.skill_classes.join(tr("、", ", ")) || tr("（一个都没有）", "(none)")}
                </div>
                {result.importers.length > 0 && (
                  <Card>
                    <div style={{ fontWeight: 600, marginBottom: 6 }}>
                      {tr(
                        `直接 import 这个模块的地方（${result.importers.length}）`,
                        `Direct importers of this module (${result.importers.length})`,
                      )}
                    </div>
                    <div style={{ opacity: 0.75, fontSize: "0.9em", marginBottom: 8 }}>
                      {tr("标「绑技能类」的那些拿的是模块上的旧引用，", "Sites tagged \"binds skill class\" hold the old reference on the module, which ")}
                      <strong>{tr("热重载换不动", "hot reload cannot swap")}</strong>
                      {tr(
                        "：它们会继续跑内置版。要一起改，仍然得发版本。其余的是基类/常量/辅助函数。",
                        ": they keep running the built-in version. Changing them requires a new release. The rest are base classes, constants and helpers.",
                      )}
                    </div>
                    <div style={{ display: "grid", gap: 4, maxHeight: 280, overflowY: "auto" }}>
                      {result.importers.map((s, i) => (
                        <div key={i} style={{ display: "flex", gap: 8, alignItems: "baseline" }}>
                          {s.binds_skill ? (
                            <Badge tone="WARN">{tr("绑技能类", "Binds skill class")}</Badge>
                          ) : (
                            <span style={{ width: 68, flexShrink: 0 }} />
                          )}
                          <code style={{ flex: 1 }}>
                            {s.file}:{s.line}
                          </code>
                          <code style={{ opacity: 0.7 }}>{s.text}</code>
                        </div>
                      ))}
                    </div>
                  </Card>
                )}
                <Card>
                  {tr("下一步：编辑 ", "Next: edit ")}<code>{result.path}</code>
                  {tr("，回到上面把它", ", go back up and ")}
                  <strong>{tr("启用", "enable")}</strong>
                  {tr(
                    "，再点「重新加载技能」。导出本身不会改变任何正在运行的东西。",
                    " it, then click \"Reload skills\". The export itself does not change anything that is running.",
                  )}
                </Card>
              </>
            )}
          </div>
        )}
      </Modal>
    </Section>
  );
}
