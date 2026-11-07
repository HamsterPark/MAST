// Pure "follow the bottom" logic — no DOM access, so it runs under node --test.
//
// ## Background
//
// The chat view used to lock the scroll position: both the conversation scrollbar
// and the whole tab's scrollbar were pulled back to the bottom whenever the user
// tried to scroll down (for example to record the screen). Two separate defects,
// matching the two halves of this file:
//
// **1. Unconditional following.** The original was
//    `useEffect(() => endRef.scrollIntoView(), [messages])` — every new message
//    pulled the view to the bottom, **without asking where the user was
//    looking**. A user who scrolled up to read history was thrown back by the
//    next message. The decision now lives in `isAtBottom()`.
//
// **2. Scrolling the ancestors too.** `scrollIntoView()` scrolls **every
//    scrollable ancestor** until the target is visible — so the chat panel's
//    scrollbar and the whole page's scrollbar moved together. The fix is to move
//    only the nearest scroll container (`nearestScrollable()` finds it) by
//    setting its `scrollTop`, never using `scrollIntoView`.
//
// The threshold is not 0: content reflow, finished image loads and sub-pixel
// rounding can leave `scrollTop` a few pixels off, and testing for 0 would read
// "actually at the bottom" as "the user scrolled up", so following would never
// trigger.

/** One scroll measurement. Field names match the DOM so they can be read straight off an element. */
export interface ScrollMetrics {
  scrollTop: number;
  scrollHeight: number;
  clientHeight: number;
}

/**
 * How many pixels from the bottom **still count as at the bottom**.
 *
 * 48 px ≈ the height of one message bubble: a user who scrolled up by less than
 * a line still intends to be looking at the latest content.
 */
export const STICK_THRESHOLD_PX = 48;

/** Whether the view is currently stuck to the bottom (following only happens in this state). */
export function isAtBottom(m: ScrollMetrics, threshold = STICK_THRESHOLD_PX): boolean {
  const gap = m.scrollHeight - m.scrollTop - m.clientHeight;
  // Content shorter than the container ⇒ no scrollbar ⇒ always at the bottom
  // (otherwise the first message would never be followed).
  if (m.scrollHeight <= m.clientHeight) return true;
  // A negative gap appears during rubber-band bounce (macOS / touch) — that is
  // clearly at the bottom, not "scrolled up".
  return gap <= Math.max(0, threshold);
}

/**
 * Find the **nearest** scrollable container: check `el` itself first, then walk up
 * through its ancestors; returns null when there is none.
 *
 * It finds only one and stops — that is the difference from `scrollIntoView()`,
 * which scrolls every container it finds, whereas only the one holding this
 * conversation should move.
 *
 * Checking `el` itself lets two call styles share one function: the chat passes
 * an empty anchor at the end of the transcript (not scrollable itself, so an
 * ancestor is needed) while the task panel passes the scroll container directly.
 * Two APIs would only add a place where passing the wrong one raises no error
 * and merely never follows.
 *
 * `getStyle` is injectable so the function can run in tests without a DOM.
 */
export function nearestScrollable(
  el: Element | null,
  getStyle: (e: Element) => { overflowY?: string } = (e) =>
    (typeof globalThis !== "undefined" && (globalThis as any).getComputedStyle
      ? (globalThis as any).getComputedStyle(e)
      : {}),
): Element | null {
  let cur: Element | null = el ?? null;
  while (cur) {
    const oy = String(getStyle(cur).overflowY || "");
    // `overlay` is the old WebKit spelling and behaves like auto — missing it
    // would walk all the way up to <body>, which scrolls the whole page again.
    if (oy === "auto" || oy === "scroll" || oy === "overlay") return cur;
    cur = cur.parentElement;
  }
  return null;
}
