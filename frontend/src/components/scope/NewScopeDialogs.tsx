// Confirmation dialogs for creating an experiment or a sample.
//
// The point is not an extra click but showing the consequences, and any existing
// item with the same name, before the user commits. Creating always without a
// check used to produce duplicate experiments minutes apart; the preflight block
// prevents that at the source.
//
// Pattern follows admin/DeviceScanner: the title is the action, the body says what
// will happen, and the primary button describes the outcome.

import { useState } from "react";
import { Field, SelectField, TextField } from "../controls";
import { ConfirmDialog } from "./ConfirmDialog";
import { tr } from "@/lib/i18n";
import {
  useCreateExperiment,
  useCreateSample,
  usePreflight,
  useSwitchExperiment,
} from "@/api/scope";

export const SAMPLE_TYPES = [
  "",
  "metal",
  "semiconductor",
  "molecule",
  "2D",
  "oxide",
  "superconductor",
  "other",
] as const;

function errText(e: unknown): string {
  if (!e) return "";
  if (e instanceof Error) return e.message;
  return typeof e === "string" ? e : JSON.stringify(e);
}

export function NewExperimentDialog({
  open,
  onClose,
  currentName,
}: {
  open: boolean;
  onClose: () => void;
  currentName?: string;
}) {
  const [name, setName] = useState("");
  const [goal, setGoal] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const create = useCreateExperiment();
  const switchTo = useSwitchExperiment();
  const pre = usePreflight(name, open);
  const existing = pre.data?.existing;

  function reset() {
    setName("");
    setGoal("");
    setErr(null);
    onClose();
  }

  async function doCreate() {
    setErr(null);
    try {
      await create.mutateAsync({ name: name.trim(), goal: goal.trim() });
      reset();
    } catch (e) {
      // Keep the dialog open and show the reason inline; a silent failure is the
      // costliest failure mode here.
      setErr(errText(e) || tr("新建失败——请重试或检查服务状态", "Creation failed. Retry or check the service status."));
    }
  }

  async function doReuse() {
    if (!existing) return;
    setErr(null);
    try {
      await switchTo.mutateAsync({ experimentId: existing.id });
      reset();
    } catch (e) {
      setErr(errText(e) || tr("切换失败", "Switch failed"));
    }
  }

  return (
    <ConfirmDialog
      open={open}
      onClose={reset}
      title={tr("新建实验 — 请确认这是一个新项目", "New experiment: confirm this is a new project")}
      confirmLabel={tr("创建实验并切换过去", "Create experiment and switch to it")}
      busy={create.isPending}
      busyLabel={tr("创建中…", "Creating…")}
      disabled={name.trim().length === 0}
      error={err}
      onConfirm={doCreate}
    >
      <Field label={tr("实验名称", "Experiment name")}>
        <TextField value={name} onChange={setName} placeholder={tr("例如：NiI2 质量表征", "e.g. NiI2 quality characterization")} />
      </Field>
      <Field label={tr("目标（可选）", "Goal (optional)")}>
        <TextField value={goal} onChange={setGoal} placeholder={tr("想验证什么", "What do you want to verify")} />
      </Field>

      {existing && (
        <div className="rounded border border-mast-warn/40 bg-mast-warn/10 px-3 py-2 text-sm">
          <p className="text-mast-text">
            {tr(
              `已存在实验「${existing.name}」—— ${existing.sample_count} 个样品 · ${existing.action_count} 条动作`,
              `Experiment "${existing.name}" already exists: ${existing.sample_count} samples · ${existing.action_count} actions`,
            )}
          </p>
          <p className="mt-1 text-xs text-mast-muted">
            {tr("要继续做这个实验，还是确实要另开一个新项目？", "Continue this experiment, or really start a separate new project?")}
          </p>
          <button
            className="mt-2 rounded-mast-ctl bg-mast-accent px-3 py-1.5 text-sm font-semibold text-mast-accent-ink hover:opacity-90"
            onClick={doReuse}
            disabled={switchTo.isPending}
          >
            {switchTo.isPending ? tr("切换中…", "Switching…") : tr("继续该实验", "Continue this experiment")}
          </button>
        </div>
      )}

      <p className="text-xs text-mast-faint">
        {currentName
          ? tr(
              `当前实验「${currentName}」不会被关闭，你随时可以切回去——实验永远可以继续。`,
              `The current experiment "${currentName}" is not closed; you can switch back at any time.`,
            )
          : tr("新建后，之后的扫描/谱学都会记到这个实验下。", "After creation, later scans and spectroscopy are recorded under this experiment.")}
        {tr(" 不会结束任何实验，也不会影响已有的对话。", " No experiment is ended and existing conversations are unaffected.")}
      </p>
    </ConfirmDialog>
  );
}

export function NewSampleDialog({
  open,
  onClose,
  experimentId,
  experimentName,
  currentSampleName,
}: {
  open: boolean;
  onClose: () => void;
  experimentId?: string | null;
  experimentName?: string;
  currentSampleName?: string;
}) {
  const [name, setName] = useState("");
  const [type, setType] = useState<string>("");
  const [desc, setDesc] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const create = useCreateSample();

  function reset() {
    setName("");
    setType("");
    setDesc("");
    setErr(null);
    onClose();
  }

  async function doCreate() {
    if (!experimentId) {
      setErr(tr("没有当前实验——请先新建或选择一个实验", "No current experiment. Create or select one first."));
      return;
    }
    setErr(null);
    try {
      await create.mutateAsync({
        experimentId,
        name: name.trim(),
        description: desc.trim(),
        sample_type: type,
      });
      reset();
    } catch (e) {
      setErr(errText(e) || tr("新建样品失败——请重试", "Sample creation failed. Please retry."));
    }
  }

  return (
    <ConfirmDialog
      open={open}
      onClose={reset}
      title={tr(`在实验「${experimentName || "—"}」下新建样品 — 请确认`, `New sample in experiment "${experimentName || "—"}": confirm`)}
      confirmLabel={tr("创建样品并切换过去", "Create sample and switch to it")}
      busy={create.isPending}
      busyLabel={tr("创建中…", "Creating…")}
      disabled={name.trim().length === 0 || !experimentId}
      error={err}
      onConfirm={doCreate}
    >
      <Field label={tr("样品名称", "Sample name")}>
        <TextField value={name} onChange={setName} placeholder={tr("例如：film-A", "e.g. film-A")} />
      </Field>
      <Field label={tr("类型", "Type")}>
        <SelectField
          value={type}
          onChange={setType}
          options={SAMPLE_TYPES.map((t) => ({ value: t, label: t || tr("（未分类）", "(uncategorized)") }))}
        />
      </Field>
      <Field label={tr("描述（可选）", "Description (optional)")}>
        <TextField value={desc} onChange={setDesc} placeholder={tr("制备方法 / 来源", "Preparation / source")} />
      </Field>
      <p className="text-xs text-mast-faint">
        {currentSampleName
          ? tr(`当前样品「${currentSampleName}」不会被结束，可随时切回。`, `The current sample "${currentSampleName}" is not ended; you can switch back at any time.`)
          : tr("创建后它将成为当前样品。", "It becomes the current sample once created.")}
        {tr(" 新样品的数据会存进实验文件夹下它自己的目录，扫描文件名也会随之改变。", " Data for the new sample is stored in its own directory under the experiment folder, and scan file names change accordingly.")}
      </p>
    </ConfirmDialog>
  );
}
