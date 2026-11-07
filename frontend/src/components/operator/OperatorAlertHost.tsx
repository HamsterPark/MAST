import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import clsx from "clsx";
import { Badge } from "@/components/ui";
import { useOperatorSound } from "@/hooks/useOperatorPrefs";
import { tr } from "@/lib/i18n";
import {
  beepPlan,
  freshPrompts,
  rememberSeen,
  severityLabel,
  shouldDesktopNotify,
  titleWithCount,
  toastLifetimeMs,
  type OpenPromptFeed,
  type OperatorPrompt,
  type Severity,
} from "@/lib/operatorPrompts";
import {
  notificationPermission,
  notificationsSupported,
  playBeeps,
  showDesktopNotification,
  unlockAudio,
} from "./alertEffects";

// The page-wide alert for a new operator prompt: a toast at the top right, a short
// beep, a desktop notification when the operator has allowed them and is not looking at
// this tab, and a "(N)" in the tab title. Mounted once in the app shell, which owns the
// polling and hands the open prompts in (`feed`). The shell mounts it on an optical station
// only (`feed.enabled`): an STM station gets no alert, no listener, no title change.
//
// What is open when the page loads is the starting point, not news: someone who has just
// opened the page is looking at it, and the nav badge already counts those. Only an id
// that appears afterwards raises an alert, once.

const MAX_TOASTS = 4;
const WISHLIST_PATH = "/wishlist";

const TONE: Record<Severity, { badge: string; edge: string }> = {
  safety: { badge: "DANGEROUS", edge: "border-mast-danger-border border-l-mast-danger" },
  action: { badge: "WARN", edge: "border-mast-warn-border border-l-mast-warn" },
  info: { badge: "INFO", edge: "border-mast-info-border border-l-mast-info" },
};

interface Toast {
  prompt: OperatorPrompt;
  shownAt: number;
}

function expired(t: Toast, now: number): boolean {
  const life = toastLifetimeMs(t.prompt.severity);
  return life !== null && now - t.shownAt > life;
}

export function OperatorAlertHost({ feed }: { feed: OpenPromptFeed }) {
  const navigate = useNavigate();
  const [soundOn] = useOperatorSound();
  const { prompts, loaded, openCount } = feed;
  const seen = useRef<Set<string> | null>(null);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [dismissed, setDismissed] = useState<ReadonlySet<string>>(() => new Set());
  const [now, setNow] = useState(() => Date.now());
  const [audioBlocked, setAudioBlocked] = useState(false);

  // A new prompt: toast, sound, desktop notification.
  useEffect(() => {
    if (!loaded) return;
    if (seen.current === null) {
      seen.current = rememberSeen(new Set(), prompts); // the starting point, not news
      return;
    }
    const fresh = freshPrompts(seen.current, prompts);
    if (fresh.length === 0) return;
    seen.current = rememberSeen(seen.current, prompts);
    const stamp = Date.now();
    setNow(stamp);
    setToasts((prev) => [...fresh.map((p) => ({ prompt: p, shownAt: stamp })), ...prev].slice(0, 20));
    if (soundOn) {
      // one beep for the batch, in the most severe one's pattern (fresh is in answering order)
      void playBeeps(beepPlan(fresh[0]!.severity)).then((ok) => setAudioBlocked(!ok));
    }
    if (
      shouldDesktopNotify({
        supported: notificationsSupported(),
        permission: notificationPermission(),
        hidden: document.hidden,
        hasFocus: document.hasFocus(),
      })
    ) {
      for (const p of fresh) showDesktopNotification(p, () => navigate(WISHLIST_PATH));
    }
  }, [prompts, loaded, soundOn, navigate]);

  // Forget toasts whose prompt is no longer open (answered here, elsewhere, or cancelled).
  const openIds = useMemo(() => new Set(prompts.map((p) => p.id)), [prompts]);
  useEffect(() => {
    setToasts((prev) => (prev.every((t) => openIds.has(t.prompt.id)) ? prev : prev.filter((t) => openIds.has(t.prompt.id))));
  }, [openIds]);

  // A clock for the toasts that expire; idle when there are none.
  useEffect(() => {
    if (toasts.length === 0) return;
    const h = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(h);
  }, [toasts.length]);

  // The browser lets audio start only after a gesture: take the first click or key press.
  useEffect(() => {
    const onGesture = () => {
      if (unlockAudio()) setAudioBlocked(false);
    };
    window.addEventListener("pointerdown", onGesture, { passive: true });
    window.addEventListener("keydown", onGesture);
    return () => {
      window.removeEventListener("pointerdown", onGesture);
      window.removeEventListener("keydown", onGesture);
    };
  }, []);

  // "(2) MAST — …" in the tab title while something waits.
  const baseTitle = useRef<string | null>(null);
  useEffect(() => {
    if (baseTitle.current === null) baseTitle.current = document.title.replace(/^\(\d+\+?\)\s*/, "");
    document.title = titleWithCount(baseTitle.current, openCount);
  }, [openCount]);
  useEffect(
    () => () => {
      if (baseTitle.current !== null) document.title = baseTitle.current;
    },
    [],
  );

  const live = toasts.filter((t) => openIds.has(t.prompt.id) && !dismissed.has(t.prompt.id) && !expired(t, now));
  const visible = live.slice(0, MAX_TOASTS);
  const hidden = live.length - visible.length;
  const dismiss = (id: string) => setDismissed((d) => new Set(d).add(id));

  return (
    <div
      className="pointer-events-none fixed right-4 top-14 z-[10000] flex w-[22rem] max-w-[calc(100vw-2rem)] flex-col gap-2"
      role="region"
      aria-label={tr("操作员提醒", "Operator alerts")}
    >
      {visible.map((t) => {
        const p = t.prompt;
        const tone = TONE[p.severity];
        return (
          <div
            key={p.id}
            role={p.severity === "safety" ? "alert" : "status"}
            className={clsx("pointer-events-auto rounded-mast-card border border-l-4 bg-mast-panel p-3 shadow-lg", tone.edge)}
          >
            <div className="flex items-start gap-2">
              <Badge tone={tone.badge}>{severityLabel(p.severity)}</Badge>
              <b className="min-w-0 flex-1 break-words text-sm text-mast-text">{p.title}</b>
              <button
                type="button"
                onClick={() => dismiss(p.id)}
                aria-label={tr("关闭这条提醒", "Dismiss this alert")}
                title={tr("关闭这条提醒（提示本身仍待处理）", "Dismiss this alert (the prompt itself stays open)")}
                className="rounded px-1.5 text-mast-muted hover:text-mast-text"
              >
                ×
              </button>
            </div>
            {p.message && <p className="mt-1 line-clamp-2 whitespace-pre-wrap text-xs text-mast-muted">{p.message}</p>}
            <div className="mt-2 flex items-center justify-between gap-2">
              <span className="text-[11px] text-mast-faint">{tr("等你处理", "Waiting for you")}</span>
              <Link
                to={WISHLIST_PATH}
                onClick={() => dismiss(p.id)}
                className="rounded bg-mast-accent px-3 py-1 text-xs font-semibold text-mast-accent-ink hover:opacity-90"
              >
                {tr("去处理", "Answer now")}
              </Link>
            </div>
          </div>
        );
      })}
      {hidden > 0 && (
        <Link
          to={WISHLIST_PATH}
          className="pointer-events-auto rounded-mast-card border border-mast-border bg-mast-panel px-3 py-1.5 text-center text-xs text-mast-muted shadow-lg hover:text-mast-text"
        >
          {tr(`还有 ${hidden} 条提醒，查看全部`, `${hidden} more, see all`)}
        </Link>
      )}
      {audioBlocked && soundOn && visible.length > 0 && (
        <p className="pointer-events-auto rounded-mast-card border border-mast-border bg-mast-panel px-3 py-1.5 text-xs text-mast-muted shadow-lg">
          {tr("浏览器拦截了提示音：在页面上点一下即可启用。", "The browser blocked the sound: click anywhere on this page once to enable it.")}
        </p>
      )}
    </div>
  );
}
