import { useState } from "react";
import { useOperatorSound } from "@/hooks/useOperatorPrefs";
import { tr } from "@/lib/i18n";
import { beepPlan } from "@/lib/operatorPrompts";
import { notificationPermission, playBeeps, requestNotificationPermission, unlockAudio } from "./alertEffects";

const BTN = "rounded border border-mast-border px-3 py-1 text-xs text-mast-muted hover:text-mast-text";

/**
 * How this browser tells the operator about a new prompt: the beep (on/off, with a test
 * button) and desktop notifications (asks for the browser's permission on click, as
 * browsers require). The toast and the tab-title count need no switch.
 */
export function AlertControls() {
  const [sound, setSound] = useOperatorSound();
  const [perm, setPerm] = useState(notificationPermission());
  const [soundNote, setSoundNote] = useState<string | null>(null);

  const testSound = async () => {
    unlockAudio(); // this click is the gesture the browser wants
    const ok = await playBeeps(beepPlan("action"));
    setSoundNote(ok ? null : tr("浏览器拦截了声音：再点一次试试。", "The browser blocked the sound: click again."));
  };

  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-mast-muted">
      <label className="flex items-center gap-1.5 text-mast-text">
        <input type="checkbox" checked={sound} onChange={(e) => setSound(e.target.checked)} />
        {tr("新提示响铃", "Beep on a new prompt")}
      </label>
      <button type="button" onClick={() => void testSound()} className={BTN}>
        {tr("试听提示音", "Test the sound")}
      </button>
      {soundNote && <span className="text-mast-warn">{soundNote}</span>}

      {perm === "default" && (
        <button
          type="button"
          onClick={() => void requestNotificationPermission().then(setPerm)}
          className={BTN}
          title={tr("这个页面在后台时，新提示会弹出系统通知", "While this page is in the background, a new prompt raises a system notification")}
        >
          {tr("开启桌面通知", "Enable desktop notifications")}
        </button>
      )}
      {perm === "granted" && <span className="text-mast-auto">{tr("桌面通知：已开启", "Desktop notifications: on")}</span>}
      {perm === "denied" && (
        <span className="text-mast-warn">
          {tr("桌面通知被浏览器禁止：到地址栏旁的站点设置里允许后刷新。", "Desktop notifications are blocked: allow them in the site settings next to the address bar, then reload.")}
        </span>
      )}
    </div>
  );
}
