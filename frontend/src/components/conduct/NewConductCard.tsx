import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/api/client";
import { Card, EmptyNote, Spinner } from "@/components/ui";
import { Button, Field, SelectField, TextField } from "@/components/controls";
import {
  buildCreateParams,
  createOutcomeMessage,
  envelopeHint,
  experimentPickState,
  initialValues,
  normalizeCreateError,
  parseParamInput,
  pickTemplate,
  preSubmitNote,
  serverFieldErrors,
  templateApprovalNote,
  templateListState,
  templateOptionLabel,
  unassignedErrors,
  type CreateOutcomeLike,
  type ParamSpecLike,
  type TemplateRowLike,
} from "@/lib/conductForm";
import { tr } from "@/lib/i18n";

// Creating a conduct. This card is the only entry point; the action must be reachable
// from the UI, because an action that cannot be done looks the same on screen as one
// that does not exist (approve is UI-only for the same reason).
//
// The form pre-fills no physical quantity. Every `ParamSpec.default` is None on
// purpose: if a value cannot be filled in, the working point of the experiment is not
// decided yet and the operator should stop. The backend `ParamSpecRow` contract has no
// default field at all, so nothing can be pre-filled by accident.
//
// The user is never locked out:
// 1. The submit button is always enabled. Local validation only warns in advance; the
//    backend is the source of truth, so a local check stricter than `check_params`
//    cannot trap the user.
// 2. If the experiment dropdown cannot be loaded, the id can be typed by hand
//    (`experiment_id` is required, so a dead dropdown would block creation).
// 3. A template that cannot be approved can still be used to create a draft; it is the
//    later approve step that is blocked.
//
// Per-field echo: the `params_echo` of a 400 response is grouped per field by the
// backend and is shown on the matching input. Entries in `errors` that map to no input
// (single-active invariant, unknown template, ...) must all be shown as well; the
// per-field echo must not become a funnel that swallows everything else.

export function NewConductCard({
  activeConductId,
  onCreated,
  onClose,
}: {
  /** The currently unfinished conduct (single-active invariant). Non-empty means creating will be rejected with 409. */
  activeConductId: string;
  onCreated: (conductId: string) => void;
  onClose: () => void;
}) {
  const [specId, setSpecId] = useState("");
  const [experimentId, setExperimentId] = useState("");
  const [values, setValues] = useState<Record<string, string>>({});
  const [outcome, setOutcome] = useState<CreateOutcomeLike | null>(null);
  const [serverErrs, setServerErrs] = useState<Record<string, string>>({});
  const [note, setNote] = useState<{ text: string; tone: "ok" | "err" } | null>(null);
  const [busy, setBusy] = useState(false);

  const templates = useQuery({
    queryKey: ["conduct", "templates"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/conducts/templates");
      if (error) throw error;
      return data;
    },
  });

  const experiments = useQuery({
    queryKey: ["experiments", "list"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/experiments");
      if (error) throw error;
      return data;
    },
  });

  const listState = templateListState(templates);
  const rows = (templates.data?.templates ?? []) as TemplateRowLike[];
  const picked = pickTemplate(rows, specId);
  const specs = (picked?.params_schema ?? []) as ParamSpecLike[];
  const approval = templateApprovalNote(picked);

  const expState = experimentPickState(experiments);
  const expRows = experiments.data?.experiments ?? [];

  const built = useMemo(() => buildCreateParams(specs, values), [specs, values]);

  const chooseTemplate = (id: string) => {
    setSpecId(id);
    // A different template means a whole new parameter set. Initial values are all empty (see file header).
    setValues(initialValues(pickTemplate(rows, id)?.params_schema ?? []));
    setOutcome(null);
    setServerErrs({});
    setNote(null);
  };

  const setOne = (name: string, v: string) => {
    setValues((prev) => ({ ...prev, [name]: v }));
    // The backend error refers to the value that was last submitted. Once the value
    // changes it is stale, so it is dropped rather than shown against the new input.
    setServerErrs((prev) => {
      if (!(name in prev)) return prev;
      const next = { ...prev };
      delete next[name];
      return next;
    });
  };

  const submit = async () => {
    setBusy(true);
    setNote(null);
    try {
      const { data, error, response } = await api.POST("/api/conducts", {
        body: {
          spec_id: specId,
          experiment_id: experimentId.trim(),
          params: built.params,
          // A hand-made conduct is not compiled from an APPROVED plan. The empty string
          // means "no source"; inventing a plan id would put a nonexistent origin into the audit trail.
          from_plan_id: "",
        },
      });
      // The body of a 4xx response is the useful part (which field is wrong), so do not
      // throw: that would leave only a generic "failed to load" line.
      const out: CreateOutcomeLike = response.ok
        ? ((data ?? {}) as CreateOutcomeLike)
        : normalizeCreateError(error ?? data, response.status);
      setOutcome(out);
      setServerErrs(serverFieldErrors(out));
      const msg = createOutcomeMessage(out, Boolean(response.ok && out.ok));
      setNote(msg);
      if (response.ok && out.ok && out.conduct_id) onCreated(String(out.conduct_id));
    } catch (e) {
      // Request could not be sent (network/proxy): show one line; never freeze or spin forever.
      setNote({
        text: tr(
          `发不出去：${String((e as Error)?.message ?? e)}`,
          `Could not send request: ${String((e as Error)?.message ?? e)}`,
        ),
        tone: "err",
      });
    } finally {
      setBusy(false);
    }
  };

  const topErrors = unassignedErrors(outcome);
  const pre = preSubmitNote(built);

  return (
    <Card className="border-mast-accent">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="text-[17px] font-semibold text-mast-text">{tr("新建一份 conduct", "New conduct")}</h3>
          <p className="mt-1 text-sm text-mast-muted">
            {tr(
              "建出来的是**草稿**：它不会自己跑起来，下一步还要有人批准。参数一个都不预填——填不出来就说明这次实验的工作点还没定。",
              "This creates a **draft**: it will not run by itself and still needs approval. No parameter is pre-filled; if you cannot fill one in, the working point of this experiment is not decided yet.",
            )}
          </p>
        </div>
        <Button variant="ghost" onClick={onClose}>
          {tr("收起", "Collapse")}
        </Button>
      </div>

      {/* Template */}
      <div className="mt-4">
        {listState.kind === "loading" && <Spinner label={listState.message} />}
        {listState.kind === "unreadable" && (
          <div className="rounded-mast-ctl border border-mast-danger-border bg-mast-danger-bg px-3 py-2.5 text-sm text-mast-danger">
            {listState.message}
          </div>
        )}
        {listState.kind === "empty" && <EmptyNote label={listState.message} />}
        {listState.kind === "ready" && (
          <div className="max-w-lg">
            <Field
              label={tr("模板（必填）", "Template (required)")}
              hint={tr("模板决定阶段与步骤；数值一个都不在模板里，全在下面那些格子里。", "The template defines stages and steps; none of the values are in the template, they are all in the fields below.")}
            >
              <SelectField
                value={specId}
                onChange={chooseTemplate}
                options={[
                  { value: "", label: tr("（请选择一个模板）", "(choose a template)") },
                  ...rows.map((r) => ({
                    value: String(r.spec_id ?? ""),
                    label: templateOptionLabel(r),
                  })),
                ]}
              />
            </Field>
          </div>
        )}
      </div>

      {picked && (
        <>
          {picked.stages?.length ? (
            <p className="mt-2 text-xs text-mast-faint">
              {tr("阶段：", "Stages: ")}{picked.stages.join(" → ")}
            </p>
          ) : null}

          {/* Not approvable does not mean not creatable; the next step is what is blocked. */}
          {approval.blocked && (
            <div className="mt-3 rounded-mast-ctl border border-mast-warn-border bg-mast-warn-bg px-3 py-2.5 text-sm text-mast-warn">
              <p>{approval.text}</p>
              {picked.findings?.length ? (
                <>
                  <p className="mt-2 text-xs text-mast-faint">{tr("发现：", "Findings:")}</p>
                  <ul className="ml-4 list-disc text-xs">
                    {picked.findings.map((f) => (
                      <li key={f}>{f}</li>
                    ))}
                  </ul>
                </>
              ) : null}
              {picked.checks_skipped?.length ? (
                <>
                  <p className="mt-2 text-xs text-mast-faint">{tr("这些检查**没能跑**：", "These checks **did not run**:")}</p>
                  <ul className="ml-4 list-disc text-xs">
                    {picked.checks_skipped.map((s) => (
                      <li key={s}>{s}</li>
                    ))}
                  </ul>
                </>
              ) : null}
            </div>
          )}

          {/* Experiment binding */}
          <div className="mt-4 max-w-lg">
            {expState.kind === "list" ? (
              <Field
                label={tr("绑定的实验（必填）", "Bound experiment (required)")}
                hint={tr("没有实验归属的 conduct，产物没有落点：spec 快照与 progress.jsonl 都写进那个实验的文件夹。", "A conduct without an experiment has nowhere to write its output: the spec snapshot and progress.jsonl go into that experiment's folder.")}
              >
                <SelectField
                  value={experimentId}
                  onChange={setExperimentId}
                  options={[
                    { value: "", label: tr("（请选择一个实验）", "(choose an experiment)") },
                    ...expRows.map((e) => ({
                      value: String(e.id ?? ""),
                      label: `${e.name || e.id}${e.sample_name ? ` (${e.sample_name})` : ""}`,
                    })),
                  ]}
                />
              </Field>
            ) : (
              <Field label={tr("绑定的实验 ID（必填）", "Bound experiment ID (required)")} hint={expState.message}>
                <TextField value={experimentId} onChange={setExperimentId} mono
                  placeholder={tr("实验 ID", "Experiment ID")} />
              </Field>
            )}
          </div>

          {/* Per-field inputs */}
          {specs.length ? (
            <div className="mt-4">
              <h4 className="mb-2 text-sm font-semibold text-mast-muted">
                {tr(`这个模板要你填的参数（${specs.length} 个）`, `Parameters this template needs (${specs.length})`)}
              </h4>
              <div className="grid gap-3 sm:grid-cols-2">
                {specs.map((p) => (
                  <ParamField
                    key={p.name}
                    spec={p}
                    raw={values[p.name] ?? ""}
                    serverError={serverErrs[p.name] ?? ""}
                    onChange={(v) => setOne(p.name, v)}
                  />
                ))}
              </div>
            </div>
          ) : (
            <p className="mt-4 text-sm text-mast-faint">
              {tr("这个模板没有声明可填参数。", "This template declares no fillable parameters.")}
            </p>
          )}

          {/* Submit */}
          {activeConductId && (
            <div className="mt-4 rounded-mast-ctl border border-mast-warn-border bg-mast-warn-bg px-3 py-2.5 text-sm text-mast-warn">
              {tr(
                `已经有一份未了结的 conduct（${activeConductId}）。单活跃不变式由数据库执行，所以这一次多半会被拒 —— 按钮**不拦你**，那句拒绝由后端说，说得比这里准。`,
                `There is already an unfinished conduct (${activeConductId}). The single-active invariant is enforced by the database, so this will most likely be rejected. The button **does not stop you**; the rejection comes from the backend, which words it more precisely.`,
              )}
            </div>
          )}

          {topErrors.length > 0 && (
            <div className="mt-4 rounded-mast-ctl border border-mast-danger-border bg-mast-danger-bg px-3 py-2.5 text-sm text-mast-danger">
              <p className="font-semibold">{tr("没建成", "Not created")}</p>
              <ul className="ml-4 mt-1 list-disc text-xs">
                {topErrors.map((e) => (
                  <li key={e}>{e}</li>
                ))}
              </ul>
            </div>
          )}

          <div className="mt-4 flex flex-wrap items-center gap-3">
            <Button variant="primary" loading={busy} onClick={submit}>
              {tr("建立草稿", "Create draft")}
            </Button>
            {/* Locally detected problems are shown here but the button is not disabled:
                a form that can never be submitted after a local false alarm is a dead end. */}
            {pre && <span className="text-xs text-mast-warn">{pre}</span>}
            {note && (
              <span
                className={
                  note.tone === "ok" ? "text-xs text-mast-auto" : "text-xs text-mast-danger"
                }
              >
                {note.text}
              </span>
            )}
          </div>
        </>
      )}
    </Card>
  );
}

/**
 * One parameter field.
 *
 * Unit, purpose and envelope are written next to the field, not hidden in a tooltip,
 * so the unit and the upper limit are visible while typing.
 *
 * The read-back (`= 1.50 nA (= 1.5e-9 A)`) is the most important part: it shows the
 * number the system actually received, so a wrong order of magnitude is visible at once.
 * The envelope only complains when a value is out of range; magnitude errors inside the
 * envelope (e.g. 5 pA typed instead of 50 pA) are only caught by the read-back.
 */
function ParamField({
  spec,
  raw,
  serverError,
  onChange,
}: {
  spec: ParamSpecLike;
  raw: string;
  serverError: string;
  onChange: (v: string) => void;
}) {
  const parsed = parseParamInput(raw, spec);
  const hint = envelopeHint(spec);
  // The backend message wins (source of truth); the local one fills in before the first submit.
  const error = serverError || parsed.error;
  const required = spec.required !== false;

  return (
    <div className="flex flex-col gap-1">
      <label className="flex flex-col gap-1 text-sm">
        <span className="text-mast-muted">
          <span className="font-mono text-mast-text">{spec.name}</span>
          {spec.unit ? <span className="ml-1 text-xs">({spec.unit})</span> : null}
          {required ? (
            <span className="ml-1 text-xs text-mast-danger">{tr("必填", "Required")}</span>
          ) : (
            <span className="ml-1 text-xs text-mast-faint">{tr("选填（留空则用模板的默认值）", "Optional (leave empty to use the template default)")}</span>
          )}
        </span>
        <input
          type="text"
          value={raw}
          onChange={(e) => onChange(e.target.value)}
          placeholder={spec.unit ? `${tr("如 ", "e.g. ")}1.5${spec.unit === "A" ? "n" : ""}${spec.unit}` : ""}
          className="rounded-mast-ctl border border-mast-border-strong bg-mast-panel px-2.5 py-2 font-mono text-sm tabular-nums text-mast-text outline-none focus:border-mast-accent"
        />
      </label>

      {/* The number the system actually received. */}
      {parsed.echo && (
        <span className="font-mono text-xs text-mast-accent">= {parsed.echo}</span>
      )}
      {hint && <span className="text-xs text-mast-faint">{hint}</span>}
      {spec.help && <span className="text-xs text-mast-faint">{spec.help}</span>}
      {error && <span className="text-xs text-mast-danger">{error}</span>}
    </div>
  );
}
