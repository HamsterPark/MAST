// The browser side effects of an operator alert: a short beep and a desktop
// notification. No audio file is shipped: the beep is synthesised with Web Audio.
//
// Browsers only start audio after a user gesture (the autoplay policy), so a beep for a
// prompt that arrives before anyone has touched the page is silently refused. `playBeeps`
// says so (returns false) and the alert host shows a one-line hint until the first click
// or key press unlocks the context.

import { notificationText, type BeepStep, type OperatorPrompt } from "@/lib/operatorPrompts";

let ctx: AudioContext | null = null;

function audioContext(): AudioContext | null {
  try {
    if (!ctx) {
      const Ctor = window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
      if (!Ctor) return null;
      ctx = new Ctor();
    }
    return ctx;
  } catch {
    return null;
  }
}

/** Call from a user gesture: lets later beeps through. */
export function unlockAudio(): boolean {
  const c = audioContext();
  if (!c) return false;
  if (c.state === "suspended") void c.resume().catch(() => undefined);
  return true;
}

/** Whether audio is available and not blocked right now (without trying to start it). */
export function audioRunning(): boolean {
  return ctx?.state === "running";
}

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

/** Play the plan. False when the browser refused (autoplay policy) or has no Web Audio. */
export async function playBeeps(plan: readonly BeepStep[]): Promise<boolean> {
  const c = audioContext();
  if (!c) return false;
  if (c.state === "suspended") {
    // Without a user gesture resume() stays pending; do not wait for it and do not play late.
    await Promise.race([c.resume().catch(() => undefined), sleep(150)]);
  }
  if (c.state !== "running") return false;
  let t = c.currentTime + 0.02;
  for (const step of plan) {
    const osc = c.createOscillator();
    const gain = c.createGain();
    const end = t + step.ms / 1000;
    osc.type = "sine";
    osc.frequency.value = step.freq;
    gain.gain.setValueAtTime(0.0001, t);
    gain.gain.exponentialRampToValueAtTime(0.28, t + 0.012);
    gain.gain.exponentialRampToValueAtTime(0.0001, end);
    osc.connect(gain).connect(c.destination);
    osc.start(t);
    osc.stop(end + 0.02);
    t = end + step.gapMs / 1000;
  }
  return true;
}

export function notificationsSupported(): boolean {
  return typeof window !== "undefined" && "Notification" in window;
}

export function notificationPermission(): NotificationPermission | "unsupported" {
  return notificationsSupported() ? Notification.permission : "unsupported";
}

/** Ask the browser for permission. Must be called from a click. */
export async function requestNotificationPermission(): Promise<NotificationPermission | "unsupported"> {
  if (!notificationsSupported()) return "unsupported";
  try {
    return await Notification.requestPermission();
  } catch {
    return Notification.permission;
  }
}

export function showDesktopNotification(p: OperatorPrompt, onClick: () => void): void {
  if (!notificationsSupported() || Notification.permission !== "granted") return;
  const { title, body } = notificationText(p);
  try {
    const n = new Notification(title, {
      body,
      tag: p.id, // one notification per prompt, even if it is raised twice
      requireInteraction: p.severity !== "info",
    });
    n.onclick = () => {
      window.focus();
      onClick();
      n.close();
    };
  } catch {
    /* some browsers refuse `new Notification` outside a service worker: the toast still shows */
  }
}
