import { useEffect, useRef } from "react";

import {
  isAtBottom,
  nearestScrollable,
  type ScrollMetrics,
} from "@/lib/stickToBottom";

/**
 * Follows new content to the bottom, **but only when the user was already at the bottom**.
 *
 * The decision and the container lookup live in `@/lib/stickToBottom` (pure functions with unit
 * tests); this hook only wires them to React. They are split because each can break silently,
 * and the symptom (the scrollbar being yanked away while scrolling) is invisible in a screenshot
 * and not caught by the type checker.
 *
 * ## Difference from `scrollIntoView()`
 *
 * `scrollIntoView()` scrolls **every scrollable ancestor** until the target is visible: the chat
 * panel and the whole page both jump back. Here `scrollTop` is set only on the nearest container
 * and ancestors above it are never touched.
 *
 * ## After the user scrolls up
 *
 * `stickRef` records whether the container was at the bottom at the last scroll event. Scrolling
 * up makes it false and following stops; scrolling back to the bottom makes it true and following
 * resumes automatically, with no button needed.
 */
export function useStickToBottom(
  anchor: React.RefObject<HTMLElement | null>,
  deps: unknown[],
  enabled = true,
) {
  // Initially true: on the first render the user has not scrolled yet, so follow the latest content.
  const stick = useRef(true);
  const box = useRef<Element | null>(null);

  // Listen to the container's scroll and update a single boolean: at the bottom or not.
  useEffect(() => {
    if (!enabled) return;
    const el = nearestScrollable(anchor.current);
    box.current = el;
    if (!el) return;
    const onScroll = () => {
      stick.current = isAtBottom(el as unknown as ScrollMetrics);
    };
    onScroll();
    // passive: this callback is one assignment and must never be able to slow scrolling itself.
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, [anchor, enabled]);

  useEffect(() => {
    if (!enabled || !stick.current) return;
    const el = box.current ?? nearestScrollable(anchor.current);
    if (!el) return;
    // Set scrollTop directly rather than animating: while content keeps appending, an unfinished
    // smooth scroll fights the next message and the view jitters.
    el.scrollTop = el.scrollHeight;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, enabled]);
}
