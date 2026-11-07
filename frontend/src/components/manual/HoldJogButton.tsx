// Jog button: a click is one jog; when the description allows it and the card is unlocked, holding
// repeats bounded jogs (lib/manual/holdRepeat.ts). Release, pointer leave, window blur, page hide
// and holding longer than 30 s all stop it immediately.
//
// Hold mode listens to pointer events only (not click: the browser also fires a click on release,
// which would send one extra jog); Enter / Space on the keyboard is still a single jog. The
// stop notice goes to onNote (the card shows it in its result row; placing it under the button
// would widen the button).

import { useEffect, useRef, useState, type ReactNode } from "react";
import clsx from "clsx";
import { tr } from "@/lib/i18n";
import { createHoldRepeat, type HoldController, type HoldEndReason, type HoldSendResult, type HoldStopGuard } from "@/lib/manual/holdRepeat";

const END_LABEL: Record<HoldEndReason, string> = {
  release: tr("松开", "Released"),
  leave: tr("指针离开", "Pointer left"),
  blur: tr("窗口失焦", "Window lost focus"),
  hidden: tr("页面隐藏", "Page hidden"),
  timeout: tr("按住超过 30 s", "Held longer than 30 s"),
  failed: tr("这一下没成", "That jog failed"),
  refused: tr("被拒", "Refused"),
  cancel: tr("取消", "Cancelled"),
};

export interface HoldSpec {
  intervalMs: number;
  stopOnRelease: boolean;
  /** Sends one jog; the next is sent only after ok (refused = the device is held by someone else, see holdRepeat). */
  send: () => Promise<HoldSendResult>;
  stop?: () => Promise<unknown>;
  /** Asked right before a trailing stop is sent (who holds the token now). */
  stopGuard?: () => Promise<HoldStopGuard>;
}

export function HoldJogButton({
  children,
  title,
  disabled,
  hold,
  onSingle,
  onNote,
}: {
  children: ReactNode;
  title?: string;
  disabled?: boolean;
  /** Non-null = holding repeats the jog (otherwise single jogs only). */
  hold: HoldSpec | null;
  /** Single jog (and the click when there is no hold mode). */
  onSingle: () => void;
  /** Notice shown when a hold ends; null when a new hold starts. */
  onNote?: (note: string | null) => void;
}) {
  const ctl = useRef<HoldController | null>(null);
  const holdRef = useRef(hold);
  holdRef.current = hold;
  const noteRef = useRef(onNote);
  noteRef.current = onNote;
  const [active, setActive] = useState(false);

  // Window blur or page hidden: stop at once; unmounting (page change, card removed) also stops
  useEffect(() => {
    const onBlur = () => ctl.current?.release("blur");
    const onVis = () => {
      if (document.visibilityState !== "visible") ctl.current?.release("hidden");
    };
    window.addEventListener("blur", onBlur);
    document.addEventListener("visibilitychange", onVis);
    return () => {
      window.removeEventListener("blur", onBlur);
      document.removeEventListener("visibilitychange", onVis);
      ctl.current?.release("cancel");
    };
  }, []);

  /**
   * Start time of a previous hold that has not finished winding down (the last jog is still in
   * flight, or it is undecided whether to send a stop). No new hold starts meanwhile, so that late
   * stop cannot cut the new one short. A request stuck for more than 10 s is no longer waited on.
   */
  const finishingSince = useRef<number | null>(null);

  const start = () => {
    const h = holdRef.current;
    if (!h || disabled) return;
    if (finishingSince.current !== null && Date.now() - finishingSince.current < 10_000) return;
    ctl.current?.release("cancel");
    finishingSince.current = Date.now();
    const c = createHoldRepeat({
      intervalMs: h.intervalMs,
      stopOnRelease: h.stopOnRelease,
      send: () => holdRef.current?.send() ?? Promise.resolve("skipped"),
      stop: h.stop,
      stopGuard: h.stopGuard,
      onEnd: () => setActive(false),
      onDone: (reason, info) => {
        // An old hold stuck for over 10 s while a new one has started: its wind-down must not overwrite the new hold's state
        if (ctl.current !== c) return;
        finishingSince.current = null;
        const stopText = !h.stopOnRelease
          ? ""
          : info.stopped
            ? tr("，已补发停止", ", stop sent")
            : info.blocked
              ? tr(`，${info.blocked}，不发停止，免得截断它的运动`, `, ${info.blocked}; no stop sent, so it is not cut short`)
              : info.last === "refused"
              ? tr("，最后一下被拒（设备可能在别人手里），不发停止，免得截断别人的运动", ", the last jog was refused (the device may be held by someone else); no stop sent, so their motion is not cut short")
              : tr("，一下都没执行，不发停止", ", no jog ran; no stop sent");
        noteRef.current?.(tr(`${END_LABEL[reason]}，停发（共 ${info.sent} 次${stopText}）`, `${END_LABEL[reason]}, repeating stopped (${info.sent} sent${stopText})`));
      },
    });
    ctl.current = c;
    noteRef.current?.(null);
    setActive(true);
    c.press();
  };

  const cls = clsx(
    "inline-flex min-w-9 select-none items-center justify-center rounded-mast-ctl border px-2.5 py-1.5 text-sm font-medium",
    "touch-none transition-colors disabled:cursor-not-allowed disabled:opacity-50",
    active
      ? "border-mast-accent bg-mast-accent-soft text-mast-accent"
      : "border-mast-border-strong bg-mast-panel text-mast-text hover:bg-mast-panel-2",
  );

  if (!hold) {
    return (
      <button type="button" className={cls} title={title} disabled={disabled} onClick={onSingle}>
        {children}
      </button>
    );
  }
  return (
    <button
      type="button"
      className={cls}
      title={title ?? tr("点一下走一步；按住连续走（松手即停）", "Click for one step; hold to keep stepping (stops on release)")}
      disabled={disabled}
      // No setPointerCapture: once captured, the pointer leaving the button no longer fires pointerleave, so stop-on-leave would break
      onPointerDown={(e) => {
        if (e.button !== 0) return;
        start();
      }}
      onPointerUp={() => ctl.current?.release("release")}
      onPointerCancel={() => ctl.current?.release("cancel")}
      onPointerLeave={() => ctl.current?.release("leave")}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          if (!e.repeat) onSingle();
        }
      }}
    >
      {children}
    </button>
  );
}
