// Everyday experiment/sample switcher for the right panel: returning to an
// earlier experiment or sample is a single click.
//
//  * There is no "End" action. Neither experiments nor samples are ever closed or
//    archived; an experiment may be resumed years later. The one real use case
//    left is "the sample was physically removed and no new one is mounted yet",
//    which is clear-sample (deselect) and writes no field on the sample row.
//  * There is no free-text input + New button. That was the create-always source of
//    duplicate experiments; creation goes through a confirm dialog that checks for
//    existing names.
//
// No new top-level tab: something that must be reachable everywhere should not be
// yet another tab.

import { useState } from "react";
import { Button } from "../controls";
import { tr } from "@/lib/i18n";
import { useClearSample, useCurrentScope, useSwitchExperiment, useSwitchSample } from "@/api/scope";
import { NewExperimentDialog, NewSampleDialog } from "./NewScopeDialogs";
import { ExperimentPicker, SamplePicker } from "./ScopePickers";
import { TipControls } from "./TipControls";

export function ScopeControls() {
  const scope = useCurrentScope();
  const switchExp = useSwitchExperiment();
  const switchSmp = useSwitchSample();
  const clearSmp = useClearSample();

  const [expPicker, setExpPicker] = useState(false);
  const [smpPicker, setSmpPicker] = useState(false);
  const [newExp, setNewExp] = useState(false);
  const [newSmp, setNewSmp] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const exp = scope.data?.experiment ?? null;
  const smp = scope.data?.sample ?? null;

  const flash = (m: string) => {
    setMsg(m);
    setTimeout(() => setMsg(null), 3000);
  };

  async function pickExperiment(id: string, name: string) {
    setExpPicker(false);
    try {
      const r = await switchExp.mutateAsync({ experimentId: id });
      // Failures must be reported; a switch button that does nothing reads as broken.
      if (!r.ok) flash(r.block_reason || tr("切换未生效", "Switch had no effect"));
      else if (r.changed) flash(tr(`已切换到「${name}」`, `Switched to "${name}"`));
    } catch {
      flash(tr("切换未送达——网络或后端不可达，请重试", "Switch was not delivered: network or backend unreachable. Please retry."));
    }
  }

  async function pickSample(id: string, name: string) {
    setSmpPicker(false);
    if (!exp) return;
    try {
      const r = await switchSmp.mutateAsync({ experimentId: exp.id, sampleId: id });
      if (!r.ok) flash(r.block_reason || tr("切换未生效", "Switch had no effect"));
      else if (r.changed) flash(tr(`已切换到样品「${name}」`, `Switched to sample "${name}"`));
    } catch {
      flash(tr("切换未送达——请重试", "Switch was not delivered. Please retry."));
    }
  }

  return (
    <div className="mt-2 space-y-2">
      <button
        className="flex w-full items-center justify-between gap-2 rounded-mast-ctl border border-mast-border-strong bg-mast-panel px-2.5 py-1.5 text-left text-xs hover:bg-mast-panel-2"
        onClick={() => setExpPicker(true)}
      >
        <span className="text-mast-muted">{tr("实验", "Experiment")}</span>
        <span className="min-w-0 flex-1 truncate text-right text-mast-text">
          {exp?.name || tr("未选择", "None selected")}
        </span>
        <span className="text-mast-faint">▾</span>
      </button>

      <button
        className="flex w-full items-center justify-between gap-2 rounded-mast-ctl border border-mast-border-strong bg-mast-panel px-2.5 py-1.5 text-left text-xs hover:bg-mast-panel-2 disabled:opacity-40"
        onClick={() => setSmpPicker(true)}
        disabled={!exp}
      >
        <span className="text-mast-muted">{tr("└ 样品", "└ Sample")}</span>
        <span
          className={
            "min-w-0 flex-1 truncate text-right " +
            (smp ? "text-mast-text" : "text-mast-danger")
          }
        >
          {smp?.name || tr("未选择", "None selected")}
        </span>
        <span className="text-mast-faint">▾</span>
      </button>

      {smp && (
        <Button variant="ghost" onClick={() => clearSmp.mutate()}>
          {tr("取消选中样品（物理出样时用）", "Deselect sample (use when the sample is physically removed)")}
        </Button>
      )}

      <div className="text-[10.5px] leading-snug text-mast-faint">
        {tr(
          "实验和样品都可以来回切换，切换不会结束任何东西。新开的对话/群聊会记在当前样品下 —— 在 实验记录 → 群聊记录 按 实验 → 样品 → 群聊 查看。",
          "Experiments and samples can be switched back and forth; switching ends nothing. New conversations and group chats are recorded under the current sample.",
        )}
      </div>
      {msg && <div className="text-[11px] text-mast-muted">{msg}</div>}

      {/* The tip is instrument-level: a sibling of experiment/sample, not a child.
          It sits here because it belongs to the same "what am I working on now"
          flow, but it persists across experiments. */}
      <div className="border-t border-mast-border pt-2">
        <TipControls />
      </div>

      <ExperimentPicker
        open={expPicker}
        onClose={() => setExpPicker(false)}
        onPick={pickExperiment}
        onCreate={() => {
          setExpPicker(false);
          setNewExp(true);
        }}
        currentId={exp?.id}
      />
      <SamplePicker
        open={smpPicker}
        onClose={() => setSmpPicker(false)}
        onPick={pickSample}
        onCreate={() => {
          setSmpPicker(false);
          setNewSmp(true);
        }}
        experimentId={exp?.id}
        experimentName={exp?.name}
        currentId={smp?.id}
      />
      <NewExperimentDialog
        open={newExp}
        onClose={() => setNewExp(false)}
        currentName={exp?.name}
      />
      <NewSampleDialog
        open={newSmp}
        onClose={() => setNewSmp(false)}
        experimentId={exp?.id}
        experimentName={exp?.name}
        currentSampleName={smp?.name}
      />
    </div>
  );
}

/** Persistent banner shown when no sample is selected. Informational only; conversations and queries are unaffected. */
export function NoSampleBanner() {
  const scope = useCurrentScope();
  const [dismissed, setDismissed] = useState(false);
  const d = scope.data;
  if (!d || d.degraded || dismissed) return null;
  if (d.has_sample) return null;
  if (!d.hint) return null;

  return (
    <div
      role="status"
      data-testid="no-sample-banner"
      className="flex items-center gap-3 border-b border-mast-warn/40 bg-mast-warn/10 px-4 py-1.5 text-xs text-mast-text"
    >
      <span className="flex-1">{d.hint}</span>
      <button
        className="text-mast-faint hover:text-mast-text"
        onClick={() => setDismissed(true)}
        aria-label={tr("关闭提示", "Dismiss hint")}
      >
        ✕
      </button>
    </div>
  );
}
