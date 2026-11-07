import clsx from "clsx";
import { getLang, tr } from "@/lib/i18n";
import {
  type Spec,
  type SpecNode,
  type SpecParam,
  isExpr,
  deep,
  allIds,
  findIn,
  renameRefs,
  specUpdate,
  ROUTE_RE,
} from "./builderSpec";

// Inspector: edits the selected node (or, when nothing is selected, the workflow
// metadata + params + outputs + version history). `card` is the full skill card
// fetched on demand from GET /api/skills/{name}.

export interface SkillCardParam {
  name: string;
  type?: string;
  unit?: string | null;
  required?: boolean;
  default?: unknown;
  min?: number | null;
  max?: number | null;
  allowed_values?: unknown[] | null;
  description?: string | null;
}
export interface SkillCard {
  name?: string;
  version?: string;
  description?: string;
  parameters?: SkillCardParam[];
  extra?: Record<string, unknown>;
}

const labelCls = "text-xs text-mast-muted";
const inputCls =
  "w-full rounded-md border border-mast-border bg-mast-bg px-2 py-1 text-sm text-mast-text outline-none focus:border-mast-accent";
const exprCls = clsx(inputCls, "font-mono");

const AGENTS: [string, string][] = [
  ["literature", tr("文献检索", "literature search")],
  ["data_processing", tr("数据分析", "data analysis")],
  ["experiment_design", tr("实验设计", "experiment design")],
  ["paper_writing", tr("论文写作", "paper writing")],
  ["paper_review", tr("论文评审", "paper review")],
];

export interface ValReport {
  ok?: boolean;
  problems?: string[];
  steps?: { id: string; skill: string; errors: string[]; warnings: string[] }[];
}

export function BuilderInspector({
  spec,
  setSpec,
  selId,
  cardOf,
  report,
  onFocusNode,
}: {
  spec: Spec;
  setSpec: (s: Spec) => void;
  selId: string | null;
  cardOf: (name: string) => SkillCard | null;
  report: ValReport | null;
  onFocusNode: (id: string) => void;
}) {
  const hit = selId ? findIn(spec.nodes, selId, null) : null;
  const patch = (p: Record<string, unknown>) => selId && setSpec(specUpdate(spec, selId, p));

  const reportBlock = report ? (
    <div className="mt-2 space-y-1 rounded-md border border-mast-border bg-mast-bg p-2">
      <div className="text-sm font-medium text-mast-text">
        {tr("校验结果", "Validation result")}{" "}
        {report.ok ? (
          <span className="text-mast-auto">{tr("通过", "passed")}</span>
        ) : (
          <span className="text-mast-danger">{tr("未通过", "failed")}</span>
        )}
      </div>
      {(report.problems || []).map((p, i) => (
        <div key={i} className={clsx("text-xs", p.startsWith("警告") ? "text-mast-warn" : "text-mast-danger")}>
          {p}
        </div>
      ))}
      {(report.steps || [])
        .filter((s) => s.errors.length || s.warnings.length)
        .map((s) => (
          <div key={s.id}>
            {s.errors.map((e, i) => (
              <div key={`e${i}`} onClick={() => onFocusNode(s.id)} className="cursor-pointer text-xs text-mast-danger">
                [{s.id}] {e}
              </div>
            ))}
            {s.warnings.map((w, i) => (
              <div key={`w${i}`} onClick={() => onFocusNode(s.id)} className="cursor-pointer text-xs text-mast-warn">
                [{s.id}] {w}
              </div>
            ))}
          </div>
        ))}
      {report.ok && !(report.problems || []).length && (
        <div className="text-xs text-mast-auto">{tr("✓ 全部检查通过", "✓ All checks passed")}</div>
      )}
    </div>
  ) : null;

  // ── workflow meta (nothing selected) ──
  if (!hit) {
    return (
      <div className="space-y-3">
        <h3 className="text-sm font-semibold text-mast-text">
          {tr("工作流", "Workflow")}{" "}
          <span className="rounded bg-mast-bg px-1.5 text-xs">{spec.name || tr("未命名", "Untitled")}</span>
        </h3>
        <Row label={tr("名称（字母/数字/_/-/中文，≤81）", "Name (letters / digits / _ / - / CJK, ≤81)")}>
          <input className={inputCls} value={spec.name} onChange={(e) => setSpec({ ...spec, name: e.target.value })} />
        </Row>
        <Row label={tr("描述", "Description")}>
          <textarea
            rows={2}
            className={inputCls}
            value={spec.description}
            onChange={(e) => setSpec({ ...spec, description: e.target.value })}
          />
        </Row>
        <Row label={tr("safety_level（须 ≥ 子技能最高级）", "safety_level (must be ≥ the highest sub-skill level)")}>
          <select
            className={inputCls}
            value={spec.safety_level}
            onChange={(e) => setSpec({ ...spec, safety_level: e.target.value })}
          >
            <option value="auto">{tr("auto — 自动执行", "auto — runs automatically")}</option>
            <option value="confirm">{tr("confirm — 需确认", "confirm — needs confirmation")}</option>
            <option value="dangerous">{tr("dangerous — 需人工批准", "dangerous — needs operator approval")}</option>
          </select>
        </Row>

        <Row label={tr("工作流参数（供调用方传入，$expr 中按名引用）", "Workflow parameters (supplied by the caller; referenced by name in $expr)")}>
          {(spec.params || []).map((p, i) => (
            <div key={i} className="mb-1 flex gap-1">
              <input
                className={clsx(inputCls, "w-20")}
                placeholder={tr("名", "name")}
                value={p.name}
                onChange={(e) => {
                  const ps = deep(spec.params);
                  ps[i]!.name = e.target.value;
                  setSpec({ ...spec, params: ps });
                }}
              />
              <select
                className={clsx(inputCls, "w-20")}
                value={p.type}
                onChange={(e) => {
                  const ps = deep(spec.params);
                  ps[i]!.type = e.target.value as SpecParam["type"];
                  setSpec({ ...spec, params: ps });
                }}
              >
                {["int", "float", "str", "bool"].map((t) => (
                  <option key={t}>{t}</option>
                ))}
              </select>
              <input
                className={clsx(inputCls, "flex-1")}
                placeholder={tr("默认值", "default")}
                value={p.default == null ? "" : String(p.default)}
                onChange={(e) => {
                  const ps = deep(spec.params);
                  const raw = e.target.value;
                  ps[i]!.default =
                    raw === ""
                      ? null
                      : p.type === "int"
                        ? parseInt(raw, 10) || 0
                        : p.type === "float"
                          ? parseFloat(raw) || 0
                          : p.type === "bool"
                            ? raw === "true"
                            : raw;
                  setSpec({ ...spec, params: ps });
                }}
              />
              <ClearBtn onClick={() => setSpec({ ...spec, params: spec.params.filter((_, j) => j !== i) })} />
            </div>
          ))}
          <MiniBtn
            onClick={() =>
              setSpec({
                ...spec,
                params: [
                  ...(spec.params || []),
                  { name: `p${(spec.params || []).length + 1}`, type: "float", default: null, description: "", required: false },
                ],
              })
            }
          >
            {tr("＋ 参数", "＋ Parameter")}
          </MiniBtn>
        </Row>

        <Row label={tr("输出签名（工作流结束时按表达式求值，进结果与技能卡）", "Output signature (expressions evaluated when the workflow ends; shown in the result and skill card)")}>
          {(spec.outputs || []).map((o, i) => (
            <div key={i} className="mb-1 flex gap-1">
              <input
                className={clsx(exprCls, "w-24")}
                placeholder={tr("名", "name")}
                value={o.name || ""}
                onChange={(e) => {
                  const os = deep(spec.outputs);
                  os[i]!.name = e.target.value;
                  setSpec({ ...spec, outputs: os });
                }}
              />
              <input
                className={clsx(exprCls, "flex-1")}
                placeholder={tr("表达式，如 q['quality']", "expression, e.g. q['quality']")}
                value={o.expr || ""}
                onChange={(e) => {
                  const os = deep(spec.outputs);
                  os[i]!.expr = e.target.value;
                  setSpec({ ...spec, outputs: os });
                }}
              />
              <ClearBtn onClick={() => setSpec({ ...spec, outputs: spec.outputs.filter((_, j) => j !== i) })} />
            </div>
          ))}
          <MiniBtn
            onClick={() =>
              setSpec({
                ...spec,
                outputs: [...(spec.outputs || []), { name: `out${(spec.outputs || []).length + 1}`, expr: "" }],
              })
            }
          >
            {tr("＋ 输出", "＋ Output")}
          </MiniBtn>
        </Row>

        <Row label={tr("最终裁决 success_when（可选；布尔表达式，决定工作流成功/失败）", "Final verdict success_when (optional; boolean expression deciding workflow success / failure)")}>
          <input
            className={exprCls}
            value={String(spec.success_when ?? "")}
            placeholder={tr("如 succeeded >= 1、target_reached（留空=所有步骤跑完即成功）", "e.g. succeeded >= 1, target_reached (empty = success once all steps finish)")}
            onChange={(e) => setSpec({ ...spec, success_when: e.target.value })}
          />
        </Row>
        <Row label={tr("fail_message（可选；success_when 为假时的失败原因表达式）", "fail_message (optional; failure-reason expression used when success_when is false)")}>
          <input
            className={exprCls}
            value={String(spec.fail_message ?? "")}
            placeholder={tr("如 'no point succeeded'", "e.g. 'no point succeeded'")}
            onChange={(e) => setSpec({ ...spec, fail_message: e.target.value })}
          />
        </Row>
        {reportBlock}
      </div>
    );
  }

  const n = hit.node;
  const kind = String(n.type);

  if (kind === "step") {
    const card = cardOf(String(n.skill));
    return (
      <div className="space-y-3">
        <h3 className="text-sm font-semibold text-mast-text">
          step <span className="rounded bg-mast-accent/15 px-1.5 text-xs text-mast-accent">{String(n.skill)}</span>
        </h3>
        <Row label={tr("节点 id（$expr 中按此名引用本步结果；改名自动级联引用）", "Node id (referenced by name in $expr; renaming updates references automatically)")}>
          <input
            className={exprCls}
            value={String(n.id)}
            onChange={(e) => {
              const nid = e.target.value;
              if (!nid || allIds(spec.nodes).has(nid)) return;
              let s2 = renameRefs(spec, String(n.id), nid);
              s2 = specUpdate(s2, String(n.id), { id: nid });
              setSpec(s2);
              onFocusNode(nid);
            }}
          />
        </Row>
        {card?.description && (
          <p className="text-xs text-mast-muted">
            {card.description}
            {card.extra && (card.extra as any).when_use ? (
              <span className="block">{tr("适用：", "Use when: ")}{String((card.extra as any).when_use)}</span>
            ) : null}
          </p>
        )}
        {card && (
          <label className="flex items-center gap-2 text-xs text-mast-muted">
            <input
              type="checkbox"
              checked={!!n.skill_version}
              onChange={(e) => patch({ skill_version: e.target.checked ? card.version : undefined })}
            />
            {tr("钉住版本", "Pin version")}{" "}
            {n.skill_version ? (
              <span className="rounded bg-mast-accent/15 px-1 text-mast-accent">@{String(n.skill_version)}</span>
            ) : (
              <span className="rounded bg-mast-bg px-1">{tr(`当前 @${card.version}（随升级漂移）`, `current @${card.version} (follows upgrades)`)}</span>
            )}
          </label>
        )}
        <ParamForm node={n} card={card} onPatch={patch} />
        <Row label={tr("optional（失败不中止整个工作流）", "optional (a failure does not abort the workflow)")}>
          <select
            className={inputCls}
            value={n.optional ? "true" : "false"}
            onChange={(e) => patch({ optional: e.target.value === "true" })}
          >
            <option value="false">{tr("否（默认，失败即中止）", "No (default; a failure aborts)")}</option>
            <option value="true">{tr("是", "Yes")}</option>
          </select>
        </Row>
        {reportBlock}
      </div>
    );
  }

  if (kind === "if") {
    return (
      <div className="space-y-3">
        <Head kind="if" id={String(n.id)} />
        <Row label={tr("cond（布尔表达式；可引用节点 id / 工作流参数）", "cond (boolean expression; may reference node ids / workflow parameters)")}>
          <input className={exprCls} value={String(n.cond ?? "")} onChange={(e) => patch({ cond: e.target.value })} />
        </Row>
        <p className="text-xs text-mast-muted">{tr("例：", "e.g. ") + "q['quality'] > 0.6, attempt < max_tries, last['success']"}</p>
        {reportBlock}
      </div>
    );
  }

  if (kind === "loop") {
    const mode = String(n.mode ?? "repeat");
    const maxIter = Number(n.max_iter ?? 0);
    return (
      <div className="space-y-3">
        <Head kind="loop" id={String(n.id)} />
        <p className="text-xs text-mast-muted">
          {getLang() === "zh" ? <>循环体（body）会重复执行。下面设定<strong>什么时候停</strong>：固定次数 / 遍历序列 / 满足条件。</> : <>The loop body repeats. Set below <strong>when it stops</strong>: a fixed count / iterating a sequence / while a condition holds.</>}
        </p>
        <Row label={tr("模式", "Mode")}>
          <select className={inputCls} value={mode} onChange={(e) => patch({ mode: e.target.value })}>
            <option value="repeat">{tr("repeat — 固定次数", "repeat — fixed count")}</option>
            <option value="foreach">{tr("foreach — 遍历一个序列", "foreach — iterate a sequence")}</option>
            <option value="while">{tr("while — 满足条件就继续", "while — continue while the condition holds")}</option>
          </select>
        </Row>
        {mode === "repeat" && (
          <Row label={tr("count（重复几次，可填数字或表达式）", "count (number of repeats; a number or an expression)")}>
            <input className={exprCls} value={String(n.count ?? "")} placeholder={tr("如 3 或 n_sites", "e.g. 3 or n_sites")} onChange={(e) => patch({ count: e.target.value })} />
          </Row>
        )}
        {mode === "foreach" && (
          <>
            <Row label={tr("iterable（要遍历的序列表达式）", "iterable (sequence expression to iterate)")}>
              <input className={exprCls} value={String(n.iterable ?? "")} placeholder={tr("如 grid['points']", "e.g. grid['points']")} onChange={(e) => patch({ iterable: e.target.value })} />
            </Row>
            <Row label={tr("var（循环变量名，body 内按此名取当前元素）", "var (loop variable name; the current element inside body)")}>
              <input className={exprCls} value={String(n.var ?? "")} placeholder={tr("如 pt", "e.g. pt")} onChange={(e) => patch({ var: e.target.value })} />
            </Row>
          </>
        )}
        {mode === "while" && (
          <Row label={tr("cond（布尔表达式，为真就再跑一轮）", "cond (boolean expression; run another iteration while true)")}>
            <input className={exprCls} value={String(n.cond ?? "")} placeholder={tr("如 q['quality'] < 0.6", "e.g. q['quality'] < 0.6")} onChange={(e) => patch({ cond: e.target.value })} />
          </Row>
        )}
        <Row label={tr("max_iter（硬上限；触达硬件的循环 ≤100）", "max_iter (hard cap; loops that touch hardware ≤100)")}>
          <input
            type="number"
            className={inputCls}
            value={maxIter || ""}
            onChange={(e) => patch({ max_iter: parseInt(e.target.value, 10) || undefined })}
          />
          {maxIter > 100 && <span className="text-xs text-mast-warn">{tr("⚠ 超过 100 — 仅纯计算循环可接受", "⚠ Above 100 — acceptable only for pure-computation loops")}</span>}
          {!maxIter && <span className="text-xs text-mast-warn">{tr("⚠ 未设上限（默认过大，对硬件循环危险）", "⚠ No cap set (the default is very large; dangerous for hardware loops)")}</span>}
        </Row>
        {reportBlock}
      </div>
    );
  }

  if (kind === "llm") return <LlmPanel n={n} patch={patch} reportBlock={reportBlock} />;

  if (kind === "agent") {
    return (
      <div className="space-y-3">
        <Head kind="agent" id={String(n.id)} />
        <Row label={tr("委托给（instrument_control 不可委托——仪器动作必须走 step）", "Delegate to (instrument_control cannot be delegated — instrument actions must be steps)")}>
          <select className={inputCls} value={String(n.agent ?? "literature")} onChange={(e) => patch({ agent: e.target.value })}>
            {AGENTS.map(([v, zh]) => (
              <option key={v} value={v}>
                {v} — {zh}
              </option>
            ))}
          </select>
        </Row>
        <Row label={tr("task（任务描述；{名} 插值 inputs）", "task (task description; {name} interpolates inputs)")}>
          <textarea rows={3} className={inputCls} value={String(n.task ?? "")} onChange={(e) => patch({ task: e.target.value })} />
        </Row>
        <InputsEditor n={n} patch={patch} />
        <Row label={tr("预算（硬上限 12 次模型调用）", "Budget (hard cap of 12 model calls)")}>
          <div className="flex gap-2">
            <input
              type="number"
              className={clsx(inputCls, "w-24")}
              title="max_model_calls"
              value={Number(n.max_model_calls ?? 8)}
              onChange={(e) => patch({ max_model_calls: Math.min(12, parseInt(e.target.value, 10) || 8) })}
            />
            <input
              type="number"
              className={clsx(inputCls, "w-28")}
              title={tr("timeout_s（秒）", "timeout_s (seconds)")}
              value={Number(n.timeout_s ?? 600)}
              onChange={(e) => patch({ timeout_s: parseInt(e.target.value, 10) || 600 })}
            />
          </div>
          <span className="text-xs text-mast-muted">
            {tr(`结果文本绑定为 ${String(n.id)}['text']，失败/超时走画布 on_error 槽。`, `The result text is bound to ${String(n.id)}['text']; failure / timeout goes to the on_error slot on the canvas.`)}
          </span>
        </Row>
        {reportBlock}
      </div>
    );
  }

  if (kind === "human") {
    const routes = (n.routes as Record<string, SpecNode[]>) || {};
    return (
      <div className="space-y-3">
        <Head kind="human" id={String(n.id)} />
        <Row label={tr("message（展示给用户；{名} 插值 inputs）", "message (shown to the operator; {name} interpolates inputs)")}>
          <textarea rows={2} className={inputCls} value={String(n.message ?? "")} onChange={(e) => patch({ message: e.target.value })} />
        </Row>
        <InputsEditor n={n} patch={patch} />
        <Row label={tr("用户可选出口（= 画布槽位；不设则单纯确认后继续）", "Operator choices (= canvas slots; if none, the workflow just continues after confirmation)")}>
          {Object.keys(routes).map((rname) => (
            <div key={rname} className="mb-1 flex gap-1">
              <input
                className={clsx(exprCls, "flex-1")}
                defaultValue={rname}
                onBlur={(e) => {
                  const nm = e.target.value.trim();
                  if (!nm || nm === rname || routes[nm] || !ROUTE_RE.test(nm)) return;
                  const next: Record<string, SpecNode[]> = {};
                  for (const [k, v] of Object.entries(routes)) next[k === rname ? nm : k] = v;
                  patch({ routes: next });
                }}
              />
              <ClearBtn
                onClick={() => {
                  if (Object.keys(routes).length <= 1) return;
                  const next = { ...routes };
                  delete next[rname];
                  patch({ routes: next });
                }}
              />
            </div>
          ))}
          <MiniBtn
            onClick={() => {
              let i = Object.keys(routes).length + 1;
              let nm = `route_${i}`;
              while (routes[nm]) nm = `route_${++i}`;
              patch({ routes: { ...routes, [nm]: [] } });
            }}
          >
            {tr("＋ 出口", "＋ Choice")}
          </MiniBtn>
          <p className="text-xs text-mast-muted">
            {tr("运行到此节点时工作流暂停（HITL interrupt），用户选出口后从断点续跑。", "The workflow pauses at this node (HITL interrupt) and resumes from that point after the operator picks a choice.")}
          </p>
        </Row>
        {reportBlock}
      </div>
    );
  }

  if (kind === "try") {
    return (
      <div className="space-y-3">
        <Head kind="try" id={String(n.id)} />
        <p className="text-xs text-mast-muted">
          {getLang() === "zh" ? (
            <>
              <strong>try / finally</strong>：body 里的步骤<strong>失败也不会中止整个工作流</strong>
              （解释器强制其 optional；用 if 检查 <code>'_failed' in 节点id</code> 自行处理失败）；
              finally 里的步骤<strong>无论 body 正常完成、失败、还是 break/succeed/fail 提前退出都会执行</strong>
              （用于恢复 Z 反馈、复位扫描框等清理）。在画布里向 body / finally 槽添加步骤。
            </>
          ) : (
            <>
              <strong>try / finally</strong>: steps in body <strong>do not abort the workflow when they fail</strong>
              {" "}(the interpreter forces them optional; use an if with <code>'_failed' in node_id</code> to handle the failure);
              steps in finally <strong>run whether body completes, fails, or exits early via break / succeed / fail</strong>
              {" "}(for cleanup such as restoring Z feedback or resetting the scan frame). Add steps to the body / finally slots on the canvas.
            </>
          )}
        </p>
        <p className="text-xs text-mast-muted/80">
          {tr("注：外部硬中止（E-stop / 看门狗）下 finally 不保证执行——那是硬停，靠 EmergencyRetract 兜底。", "Note: under an external hard abort (E-stop / watchdog) finally is not guaranteed to run — that is a hard stop, backed by EmergencyRetract.")}
        </p>
        {reportBlock}
      </div>
    );
  }

  if (kind === "break" || kind === "continue") {
    return (
      <div className="space-y-3">
        <Head kind={kind} id={String(n.id)} />
        <p className="text-xs text-mast-muted">
          {kind === "break"
            ? tr("跳出最近的循环（loop）。只能放在循环体内。", "Exit the nearest loop. Only valid inside a loop body.")
            : tr("结束本轮、进入循环下一轮。只能放在循环体内。", "End this iteration and start the next. Only valid inside a loop body.")}
        </p>
        {reportBlock}
      </div>
    );
  }

  if (kind === "succeed" || kind === "fail") {
    return (
      <div className="space-y-3">
        <Head kind={kind} id={String(n.id)} />
        <p className="text-xs text-mast-muted">
          {kind === "succeed"
            ? tr("立即以「成功」结束整个工作流（覆盖默认裁决）。", "End the whole workflow immediately as a success (overrides the default verdict).")
            : tr("立即以「失败」结束整个工作流（即使所有步骤都成功）。", "End the whole workflow immediately as a failure (even if every step succeeded).")}
          {tr("外层 try 的 finally 仍会执行。", " The finally of an enclosing try still runs.")}
        </p>
        <Row label={tr("reason（原因表达式；字符串字面量要加引号，如 'done'）", "reason (reason expression; string literals need quotes, e.g. 'done')")}>
          <input
            className={exprCls}
            value={String(n.reason ?? "")}
            placeholder={tr("如 'target reached' 或 'after ' + str(attempt) + ' tries'", "e.g. 'target reached' or 'after ' + str(attempt) + ' tries'")}
            onChange={(e) => patch({ reason: e.target.value })}
          />
        </Row>
        {reportBlock}
      </div>
    );
  }

  // set
  return (
    <div className="space-y-3">
      <Head kind="set" id={String(n.id)} />
      <Row label={tr("var（变量名）", "var (variable name)")}>
        <input className={exprCls} value={String(n.var ?? "")} onChange={(e) => patch({ var: e.target.value })} />
      </Row>
      <Row label={tr("value（表达式）", "value (expression)")}>
        <input className={exprCls} value={String(n.value ?? "")} onChange={(e) => patch({ value: e.target.value })} />
      </Row>
      <p className="text-xs text-mast-muted">{tr("例：", "e.g. ")}attempt + 1, scan['file_path']</p>
      {reportBlock}
    </div>
  );
}

// ── per-step parameter form (typed by skill card) ──
function ParamForm({
  node,
  card,
  onPatch,
}: {
  node: SpecNode;
  card: SkillCard | null;
  onPatch: (p: Record<string, unknown>) => void;
}) {
  const params = (card && card.parameters) || [];
  const nodeParams = (node.params as Record<string, unknown>) || {};
  const setP = (name: string, val: unknown) => {
    const p = { ...nodeParams };
    if (val === undefined) delete p[name];
    else p[name] = val;
    onPatch({ params: p });
  };

  if (!card) {
    return <p className="text-xs text-mast-muted">{tr("技能卡加载中…（无法连接内核时不可编辑参数）", "Loading skill card… (parameters cannot be edited while the kernel is unreachable)")}</p>;
  }
  if (!params.length) return <p className="text-xs text-mast-muted">{tr("该技能无参数。", "This skill has no parameters.")}</p>;

  return (
    <div className="space-y-2">
      {params.map((p) => {
        const cur = nodeParams[p.name];
        const exprMode = isExpr(cur);
        const canExpr = !(node.skill === "MotorMove" && p.name === "direction");
        let input: React.ReactNode;
        if (exprMode) {
          input = (
            <input className={exprCls} value={(cur as { $expr: string }).$expr} onChange={(e) => setP(p.name, { $expr: e.target.value })} />
          );
        } else if (p.allowed_values && p.allowed_values.length) {
          input = (
            <select className={inputCls} value={cur === undefined ? "" : String(cur)} onChange={(e) => setP(p.name, e.target.value === "" ? undefined : e.target.value)}>
              <option value="">{tr("（默认", "(default")}{p.default != null ? `: ${String(p.default)}` : ""}{tr("）", ")")}</option>
              {p.allowed_values.map((v) => (
                <option key={String(v)} value={String(v)}>
                  {String(v)}
                </option>
              ))}
            </select>
          );
        } else if (p.type === "bool") {
          input = (
            <select className={inputCls} value={cur === undefined ? "" : String(cur)} onChange={(e) => setP(p.name, e.target.value === "" ? undefined : e.target.value === "true")}>
              <option value="">{tr("（默认）", "(default)")}</option>
              <option value="true">true</option>
              <option value="false">false</option>
            </select>
          );
        } else if (p.type === "int" || p.type === "float") {
          input = (
            <input
              type="number"
              step={p.type === "int" ? 1 : "any"}
              className={inputCls}
              placeholder={p.default != null ? tr(`默认 ${String(p.default)}`, `default ${String(p.default)}`) : ""}
              value={cur === undefined ? "" : (cur as number)}
              onChange={(e) => {
                if (e.target.value === "") return setP(p.name, undefined);
                const v = p.type === "int" ? parseInt(e.target.value, 10) : parseFloat(e.target.value);
                if (Number.isNaN(v)) return;
                setP(p.name, v);
              }}
              onBlur={() => {
                if (typeof cur !== "number") return;
                let v: number = cur;
                if (p.min != null && v < p.min) v = p.min;
                if (p.max != null && v > p.max) v = p.max;
                if (v !== cur) setP(p.name, v);
              }}
            />
          );
        } else {
          input = (
            <input
              className={inputCls}
              placeholder={p.default != null ? tr(`默认 ${String(p.default)}`, `default ${String(p.default)}`) : ""}
              value={cur === undefined ? "" : String(cur)}
              onChange={(e) => setP(p.name, e.target.value === "" ? undefined : e.target.value)}
            />
          );
        }
        return (
          <div key={p.name}>
            <div className={labelCls}>
              {p.name}
              {p.unit ? <span className="text-mast-muted/70">({p.unit})</span> : null}
              {p.required ? <span className="text-mast-danger"> *</span> : null}
              {p.min != null ? <span className="text-mast-muted/70"> [{p.min}, {p.max}]</span> : null}
            </div>
            <div className="flex items-center gap-1">
              <div className="flex-1">{input}</div>
              {canExpr && (
                <button
                  title={tr("表达式模式（引用上游节点 id / 工作流参数）", "Expression mode (reference upstream node ids / workflow parameters)")}
                  onClick={() => setP(p.name, exprMode ? undefined : { $expr: "" })}
                  className={clsx(
                    "rounded border px-1.5 py-1 font-mono text-xs",
                    exprMode ? "border-mast-accent bg-mast-accent/15 text-mast-accent" : "border-mast-border text-mast-muted",
                  )}
                >
                  ƒx
                </button>
              )}
              {cur !== undefined && <ClearBtn onClick={() => setP(p.name, undefined)} text={tr("清除", "Clear")} />}
            </div>
            {p.description ? <div className="text-[11px] text-mast-muted">{p.description}</div> : null}
          </div>
        );
      })}
    </div>
  );
}

function LlmPanel({
  n,
  patch,
  reportBlock,
}: {
  n: SpecNode;
  patch: (p: Record<string, unknown>) => void;
  reportBlock: React.ReactNode;
}) {
  const routes = (n.routes as Record<string, SpecNode[]>) || {};
  const mode = String(n.mode ?? "route");
  const schema = (n.output_schema as Record<string, string>) || {};
  const renameRoute = (oldName: string, newName: string) => {
    if (!newName || routes[newName] || !ROUTE_RE.test(newName) || newName === "uncertain") return;
    const next: Record<string, SpecNode[]> = {};
    for (const [k, v] of Object.entries(routes)) next[k === oldName ? newName : k] = v;
    const rd = { ...((n.route_descriptions as Record<string, string>) || {}) };
    if (oldName in rd) {
      rd[newName] = rd[oldName] ?? "";
      delete rd[oldName];
    }
    patch({ routes: next, route_descriptions: rd, escape: n.escape === oldName ? newName : n.escape });
  };
  const addRoute = () => {
    let i = Object.keys(routes).length + 1;
    let nm = `route_${i}`;
    while (routes[nm]) nm = `route_${++i}`;
    patch({ routes: { ...routes, [nm]: [] } });
  };
  const delRoute = (name: string) => {
    if (Object.keys(routes).length <= 2) return;
    const next = { ...routes };
    delete next[name];
    const rd = { ...((n.route_descriptions as Record<string, string>) || {}) };
    delete rd[name];
    patch({ routes: next, route_descriptions: rd, escape: n.escape === name ? Object.keys(next)[0] : n.escape });
  };

  return (
    <div className="space-y-3">
      <Head kind="llm" id={String(n.id)} />
      <Row label={tr("职责（唯一需写的自然语言；prompt 由系统拼装）", "Responsibility (the only natural language you write; the prompt is assembled by the system)")}>
        <textarea rows={2} className={inputCls} value={String(n.responsibility ?? "")} onChange={(e) => patch({ responsibility: e.target.value })} />
      </Row>
      <Row label={tr("模式", "Mode")}>
        <select className={inputCls} value={mode} onChange={(e) => patch({ mode: e.target.value })}>
          <option value="route">{tr("route — 闭集分支选择（恰好点亮一个槽位）", "route — closed-set branch choice (exactly one slot is taken)")}</option>
          <option value="data">{tr("data — 类型化结构输出（失败走 on_error 槽）", "data — typed structured output (failure goes to the on_error slot)")}</option>
        </select>
      </Row>
      <InputsEditor n={n} patch={patch} label={tr("inputs（喂给决策的数据；$expr 引用上游节点 id / 变量）", "inputs (data fed to the decision; $expr references upstream node ids / variables)")} />
      {mode === "route" ? (
        <Row label={tr("routes（闭集分支 = 画布槽位；⚑ = escape 安全出口）", "routes (closed-set branches = canvas slots; ⚑ = escape, the safe exit)")}>
          {Object.keys(routes).map((rname) => (
            <div key={rname} className="mb-1 flex items-center gap-1">
              <input type="radio" name={`esc-${String(n.id)}`} title={tr("设为 escape", "Set as escape")} checked={n.escape === rname} onChange={() => patch({ escape: rname })} />
              <input className={clsx(exprCls, "w-28")} defaultValue={rname} onBlur={(e) => e.target.value !== rname && renameRoute(rname, e.target.value.trim())} />
              <input
                className={clsx(inputCls, "flex-1")}
                placeholder={tr("描述（拼进 prompt）", "description (added to the prompt)")}
                value={((n.route_descriptions as Record<string, string>) || {})[rname] || ""}
                onChange={(e) => patch({ route_descriptions: { ...((n.route_descriptions as Record<string, string>) || {}), [rname]: e.target.value } })}
              />
              <ClearBtn onClick={() => delRoute(rname)} />
            </div>
          ))}
          <MiniBtn onClick={addRoute}>{tr("＋ 分支", "＋ Branch")}</MiniBtn>
          <p className="text-xs text-mast-muted">{tr('模型还可答 "uncertain"（弃权）→ 自动走 ⚑escape。', 'The model may also answer "uncertain" (abstain) → takes ⚑escape automatically.')}</p>
        </Row>
      ) : (
        <Row label={tr("output_schema（类型化输出字段）", "output_schema (typed output fields)")}>
          {Object.entries(schema).map(([k, t]) => (
            <div key={k} className="mb-1 flex gap-1">
              <input className={clsx(inputCls, "w-28")} value={k} readOnly />
              <select className={inputCls} value={t} onChange={(e) => patch({ output_schema: { ...schema, [k]: e.target.value } })}>
                {["str", "float", "int", "bool"].map((x) => (
                  <option key={x}>{x}</option>
                ))}
              </select>
              <ClearBtn
                onClick={() => {
                  const next = { ...schema };
                  delete next[k];
                  patch({ output_schema: next });
                }}
              />
            </div>
          ))}
          <MiniBtn
            onClick={() => {
              const nm = window.prompt(tr("字段名：", "Field name:"));
              if (nm && !schema[nm]) patch({ output_schema: { ...schema, [nm]: "str" } });
            }}
          >
            {tr("＋ 字段", "＋ Field")}
          </MiniBtn>
        </Row>
      )}
      {reportBlock}
    </div>
  );
}

// ── shared bits ──
function InputsEditor({ n, patch, label = tr("inputs（$expr 绑定上游数据）", "inputs ($expr binds upstream data)") }: { n: SpecNode; patch: (p: Record<string, unknown>) => void; label?: string }) {
  const inputs = (n.inputs as Record<string, unknown>) || {};
  const setInput = (name: string, expr: string | undefined) => {
    const next = { ...inputs };
    if (expr === undefined) delete next[name];
    else next[name] = { $expr: expr };
    patch({ inputs: next });
  };
  return (
    <Row label={label}>
      {Object.entries(inputs).map(([k, v]) => (
        <div key={k} className="mb-1 flex gap-1">
          <input className={clsx(inputCls, "w-24")} value={k} readOnly />
          <input className={clsx(exprCls, "flex-1")} value={isExpr(v) ? v.$expr : String(v)} onChange={(e) => setInput(k, e.target.value)} />
          <ClearBtn onClick={() => setInput(k, undefined)} />
        </div>
      ))}
      <MiniBtn
        onClick={() => {
          const nm = window.prompt(tr("输入名（prompt 中的字段名）：", "Input name (field name in the prompt):"));
          if (nm && !inputs[nm]) setInput(nm, "");
        }}
      >
        {tr("＋ 输入", "＋ Input")}
      </MiniBtn>
    </Row>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1">
      <div className={labelCls}>{label}</div>
      {children}
    </div>
  );
}

function Head({ kind, id }: { kind: string; id: string }) {
  return (
    <h3 className="text-sm font-semibold text-mast-text">
      {kind} <span className="rounded bg-mast-bg px-1.5 text-xs text-mast-muted">{id}</span>
    </h3>
  );
}

function MiniBtn({ children, onClick }: { children: React.ReactNode; onClick: () => void }) {
  return (
    <button onClick={onClick} className="rounded border border-mast-border px-2 py-0.5 text-xs text-mast-muted hover:border-mast-accent hover:text-mast-accent">
      {children}
    </button>
  );
}

function ClearBtn({ onClick, text = "✕" }: { onClick: () => void; text?: string }) {
  return (
    <button onClick={onClick} className="rounded px-1.5 text-xs text-mast-muted hover:text-mast-danger">
      {text}
    </button>
  );
}
