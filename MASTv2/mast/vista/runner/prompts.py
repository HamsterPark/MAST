# Derived from VISTA (MIT), github.com/joshhhhhan/VISTA, benchmarks/arc3/claude/prompt.md
"""Prompt texts of the Claude runner. Kept minimal on purpose, following VISTA's prompt (via
VISTA-SPM ``prompts.py``): an objective, a revisable model, expect-before / report-after,
notes, the end markers. MAST adds the observation-token rule and the fact that actions are
server-side jobs. Everything task-specific comes from the operator's task text; nothing here
teaches STM technique.
"""
from __future__ import annotations

from typing import Sequence

from .compact_hook import CHECKPOINT_INSTRUCTION  # noqa: F401  (one source for the wording)

SYSTEM_PROMPT = """# Instrument task

You are operating a scanning tunnelling microscope through MAST, with the tools of this session. Nobody is at the console.

Complete the task with as little instrument time and as few instrument actions as possible.

Build and use a compact, revisable model of the instrument, the tip and the sample, and of their current state. Update it as new evidence changes what is supported.

One observation, one action: every observation carries the token for your next instrument action (`next_token` in its text, or printed on its image when `token_on_image` is true). Pass it as `after` to `mast_act`, or to `mast_run` for a skill that drives the instrument. A refused action executed nothing; read the observation that comes with the refusal before acting again.

Before each action, briefly state what you expect to see. Afterward, briefly state all visible changes, expected or not.

Actions run as jobs on the server and keep running when a reply ends. Wait for a job, a new frame or an alert with `mast_wait` instead of polling.

Keep concise, durable, revisable understanding in `GUIDE.md`; use `WORKING.md` for the current plan and state. When asked to update WORKING.md, do that first: a fresh context receives GUIDE.md, WORKING.md and the newest observation.
"""

END_RULES = ("When the task is complete, end your reply with [DONE]. If it cannot be completed, end your reply "
             "with [ABORT] and say why. The marker must be the last word of the reply; nobody reads the replies "
             "in between, so keep acting until then.")

CONTINUE_TEXT = ("Nobody is at the console. Continue with the task; context limits are handled automatically. "
                 "End your reply with [DONE] when the task is complete, or [ABORT] if it cannot be completed.")

PREFLIGHT_PROMPT = "This is a runtime preflight, not a task. Do not call tools. Reply exactly READY."

#: Why a fresh context started, in the words of its first message.
_FRESH_REASONS = {
    "checkpoint": "the previous context was replaced after you saved WORKING.md",
    "handoff": "the previous context reached its image limit and was replaced",
    "recovery": "the previous context ended unexpectedly",
}


def task_block(task: str) -> str:
    return "\n".join(["Task:", task.strip(), "", END_RULES])


def initial_message(task: str, observation_text: str) -> str:
    """First user message of segment 0: the task, then the service's recovery observation
    (environment record, notes, newest observation with its token)."""
    return f"{task_block(task)}\n\nThe current state of the instrument and your notes:\n\n{observation_text}"


def recovery_message(task: str, observation_text: str, *, kind: str,
                     unmatched: Sequence[dict] = ()) -> str:
    """First user message of a later segment. ``unmatched``: tool calls of earlier contexts that
    never got a result (``{"name", "id"}``); the jobs they started keep running on the server."""
    lines = [f"This is a fresh context: {_FRESH_REASONS.get(kind, 'the previous context ended')}. "
             "Continue the task from your notes and the newest observation below."]
    if unmatched:
        names = ", ".join(sorted({str(u.get("name") or "?") for u in unmatched}))
        lines.append(f"{len(unmatched)} tool call(s) of the earlier context got no result ({names}). "
                     "Anything they started keeps running on the server: check `mast_jobs`, or wait for "
                     "it with `mast_wait`, before acting again.")
    return "\n".join(lines) + f"\n\n{task_block(task)}\n\n{observation_text}"
