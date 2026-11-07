/**
 * Display logic for the private-chat "waiting for human input" panel; pure and
 * runnable under `node --test`.
 *
 * These few lines could live in the JSX of PendingInterrupts.tsx. They are extracted
 * because one of them is a property that is easy to mistake for redundancy and delete,
 * and breaking it leaves a conversation turn stuck forever with nobody noticing: no
 * crash, no error, nothing visible in a screenshot.
 *
 * The property: dismissible does not mean disappearing. The panel must not block the
 * interface behind it, so it can be collapsed. Collapsing hides ONLY the cards; the
 * "N items waiting for you" line must stay. The turn that raised the interrupt is
 * blocked, and a notice that can be dismissed completely is no notice, which is the
 * problem the old full-screen modal was meant to solve. Both needs (do not obstruct,
 * do not get missed) have to hold.
 *
 * Hence `render` and `showCards` are two separate return values. A distinction without
 * a name gets merged by the next person.
 */

import { tr } from "./i18n.ts";

export interface InterruptRowLike {
  kind?: string | null;
}

export interface InterruptDataLike {
  interrupts?: readonly InterruptRowLike[] | null;
  degraded?: boolean | null;
}

export interface InterruptPanelView {
  /** Whether the panel is drawn at all. */
  render: boolean;
  /** Whether the cards are drawn: false when collapsed while `render` stays true. */
  showCards: boolean;
  /** Number of pending items. */
  count: number;
  /** Title. See the comment in `interruptPanelView` for why both branches remain. */
  headline: string;
}

export function interruptPanelView(
  data: InterruptDataLike | null | undefined,
  collapsed: boolean,
): InterruptPanelView {
  const rows = data?.interrupts ?? [];
  const count = rows.length;
  const degraded = !!data?.degraded;
  // When degraded, show the panel even if no row could be read: "the interrupt queue
  // could not be read" and "there are no interrupts" are different statements, and
  // presenting the former as the latter makes people stop investigating.
  const render = count > 0 || degraded;
  // The approval half of the old "questions vs approvals" distinction no longer exists:
  // the confirmation dialogs of `HumanInTheLoopMiddleware`,
  // `ModeGatedPulseHITLMiddleware` and `buffer_hitl` were removed, and DANGEROUS now
  // only warns. Reasoning from that old rationale would make the else branch look dead.
  //
  // Only three kinds of interrupt are still raised:
  //   - `ask_user` from `ask_tools.ask_user`: a legitimate question when a key
  //     parameter is missing;
  //   - `ask_user` from the supervisor's `_ask_operator_node` when routing fails: same
  //     shape;
  //   - `workflow_human`: the workflow `human` node. No built-in workflow uses it; only
  //     user-built skills might.
  // The first two are the agent asking a question; the third is a workflow step that
  // needs a person, not a question. So the else branch is live and its wording is right.
  const asking = rows.some((r) => r?.kind === "ask_user");
  return {
    render,
    // Collapsed hides the cards, but `render` is unaffected by `collapsed`: see the header.
    showCards: render && !collapsed && count > 0,
    count,
    headline: asking ? tr("智能体在问你", "The agent has a question") : tr("等待人工介入", "Waiting for human input"),
  };
}
