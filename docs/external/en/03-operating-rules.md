# Operating rules

The twelve rules below are also returned by `GET /guide` as plain text, for an agent that never
reads this file at all; their `id` is worth keeping in mind, since a refusal from the API sometimes
names one directly. 7.0 adds the rules of the visual harness in a second table; those are not in
`GET /guide` but in the tool descriptions, the `mast-operator` skill, the `AGENTS.md` of a Codex
workspace, and this page. The sections after the two tables are operational rules that do not fit
in a short list, each with the reason behind it — see [Concepts](02-concepts.md) for the mechanisms
they refer to.

## The twelve rules

| ID | Rule | Why |
|---|---|---|
| `briefing-first` | Read the briefing first | `GET /briefing` carries what MAST's own agents see every turn: tip, instrument profile, live readings, operator preferences, the resume block, recent actions by anyone, and your open requests. |
| `scope-before-data` | Set the scope before producing data | Choose or create the experiment and sample first. Without a sample, scans and spectroscopy are refused at the door; without an experiment, nothing you do is recorded. |
| `find-by-action` | Find skills by what they do | Search using the action you need, not a guessed name; skills are named after the problem they solve. Read the skill card before running: units, bounds, preconditions, measured duration on this instrument. |
| `jobs-not-timeouts` | Run skills as jobs | Submit and poll. A client-side timeout is not a failure: the skill keeps running and keeps the instrument. Retry a lost submission with the same `request_id`, never a new one. |
| `busy-means-wait` | Busy means someone else is driving | `refused_busy` means another driver holds the instrument. Retrying immediately will not make it finish sooner: wait for it, or ask the operator. |
| `how-to-stop` | How to stop | Cancel stops your job at its next check (cooperative). To stop motion physically right now, run a stop skill (never blocked by the instrument lock) or the emergency stop. Clearing an emergency latch is the operator's call. |
| `three-states` | ok, success and degraded are different facts | A failed skill is not an endpoint error, and a degraded answer (a subsystem not wired) is not an empty answer. Read all three before concluding anything. |
| `operating-mode` | The operating mode is the operator's | SAFE refuses tip processing on every path, including inside composite skills. Do not route around it and do not try to change it; ask the operator if you need tip processing. |
| `no-proxy-approval` | Never decide for the operator | Do not approve or resolve human-in-the-loop prompts on the operator's behalf. Ask and wait for the answer. |
| `after-restart` | After a restart, look before you act | Jobs running when the service restarted are marked `lost_on_restart` and are never replayed. Read the briefing first to learn what state the instrument is in. |
| `raw-data` | Use the server's frames | Fetch scan images through the data endpoint: the server applies the orientation conventions (backward-direction mirror, scan-direction flip) once and correctly. |
| `leave-a-trail` | Leave a trail | Write conclusions as notes; MAST's own agents recall them. End with a handover, and leave the instrument idle: no running jobs, scan stopped. |

## Rules of the visual harness (new in 7.0)

| Rule | Why |
|---|---|
| **Look before you act.** Start with `mast_observe`, and look at every observation's image before the next act. | An action chosen without seeing the last result repeats what the image would have shown; the server refuses an act without the newest token anyway. |
| **Always act with the newest token.** Quote the `next_token` of the latest observation you received, never an older one. | A token works once, and every newer observation replaces it: your own `mast_observe` or `mast_wait`, a refusal's recovery observation, or a frame from outside your acts. An older token is refused with `observe_first`. |
| **One act per code cell.** In Codex Code Mode, call `image()` on the act's result, end the cell, and act again only from a new cell. | Calls from one cell share an item id, and the server accepts one act per cell (`same_code_cell`): acts chained inside one cell would be taken blind. |
| **Wait for a running act; never resubmit it.** On `state: running`, call `mast_wait` with `{"type": "job", "job_id": ...}`, and act only after its observation. | The act keeps running on MAST. Another act is refused with `act_in_flight` until it ends, and the same act sent again later under a new `request_id` runs a second time. |
| **Take numbers from the read tools.** Positions, heights, periods, counts and changes come from `mast_read_values` and the measure tools; images and their grid are for seeing. | Models misjudge exact positions, sizes and counts in pictures, and misread more often when several images arrive at once; the tools read the archived values. |
| **Keep GUIDE.md and WORKING.md current.** Read both at the start; rewrite `WORKING.md` after every step that changes the plan and before you stop; put what will still hold later into `GUIDE.md`. | A fresh session — after compaction, a crash or a checkpoint — knows only these two files and the newest observation. |
| **Do not spam waits.** Use one `mast_wait` with every condition you care about and a long timeout, not a loop of short ones; after a timeout, call it again. | Every call costs a turn of the conversation, and the cursor already guarantees that nothing between two waits is lost. |
| **Detectors never act.** A hit wakes you; you decide and act yourself. Test a detector on an archived frame before relying on it. | Every action goes through the gate and the safety layer with your judgement behind it; a detector only watches. |
| **Observe again before precise positioning.** | Positions read from older frames go stale: the sample drifts and the piezo creeps between frames. |
| **A refusal ran nothing.** Read its code and do what it says; on `observe_first`, look at the attached observation, then act with its token. | Refusals never execute and never advance the turn; the same call sent again unchanged gets the same refusal. |
| **Finish idle, with notes and a handover.** No act or job running, scan stopped, `WORKING.md` saved, then `mast_handover`. | The next session, human or agent, starts from what you leave behind. |

## MAST's instrument-agent tools (new in 7.0.1)

The tools MAST's own instrument agent uses besides skills are open to an external agent too, with no
role distinction ([Concepts](02-concepts.md#masts-instrument-agent-tools-new-in-701)); you run them with
`mast_run` like a skill, without a token.

| Rule | Why |
|---|---|
| **Ask the scan map, not the image, where you have been.** Before choosing where to scan, run `get_map_analysis` or `get_next_scan_position`, and `get_markers_near` before working at one spot. | The scan map is MAST's record of every frame, spectrum, pulse, crash and approach. It computes coverage, damaged areas and the next position; a picture cannot. |
| **Have the operator record what happened at the instrument.** After a tip change, ask the operator to register the tip in the tip card; after a coarse move made by hand in Nanonis, to backfill it on the scan-map page. Ask with `mast_ask_operator`; read the current tip with `get_current_tip`. | Conditioning recipes and calibrations are chosen by the registered tip. Registering a tip clears the old tip's learned calibrations and backfilling a coarse move clears the crash block; only someone at the instrument knows whether the event happened (`register_tip`, `record_coarse_move` and the like are not offered). |
| **Changing the scope through a tool follows the scope rule.** `switch_experiment`, `start_sample` and the other scope tools are refused while the instrument is busy, as `mast_scope` is. | An action still running would be recorded under the new experiment or sample. |
| **Do the work yourself.** Tools that would hand work to MAST's internal agents are not offered (`not_offered`). | Every capability they would use is reachable directly. |

## Stopping, precisely

Stop any running scan, and wait until it has actually stopped, before starting a coarse motion —
the two must never overlap. To stop physically, right now: run a stop skill such as `StopScan`,
which the instrument lock never blocks, or call the emergency stop for a full E-STOP; then cancel
the job itself so its own record reflects that you asked it to stop. The stop skill or the
emergency call is what actually moves the instrument; a cancel by itself only takes effect at the
job's next cooperative check, and does nothing to a call already blocked inside a single Nanonis
command.

Stop and retract skills never need an observation token: run `StopScan` or `WithdrawTip` with
`mast_run` and no `after`, even while an act is in flight. A stop or retract skill is also exempt
from the concurrent-job limit, so submitting one is never refused with `429` just because other
jobs are running. But once the emergency latch itself is set, every job — a stop skill included — is
refused before it starts, because the E-STOP that set the latch has already stopped motion and
withdrawn the tip: at that point, do not retry a stop; ask the operator, who releases the latch from
MAST's own interface.

## Long-running tasks in an agent harness

When your own harness spawns a task that will drive the instrument for a while, start the
interpreter or executable directly rather than through a shell wrapper script: stopping the wrapper
does not stop the grandchild process that is actually holding the instrument, and a wrapper sitting
in a wait loop can go on to its next command regardless of what you intended to stop. Do not launch
it with `nohup` either, for the same underlying reason — you need the ability to actually stop what
you started, not just to stop watching it.

## After an interruption

If a long task is interrupted — your harness is killed, the connection drops, the process driving
it is stopped from outside — do not assume the instrument is where you left it. An error handler
may have withdrawn the tip, or the last command may have left it mid-motion; an interrupted job is
never replayed, and nobody replays what you were doing either. Read the briefing again before doing
anything else, and treat what it reports as the truth, not your last known state. Your token is
probably gone too: call `mast_observe` and look before the next act, and read `mast_history` for
what the last acts did.

## After a bias change

Wait for piezo creep to settle before imaging or running spectroscopy after you change the bias.
The settle time is specific to this instrument: ask the operator, or check the operator
preferences section of the briefing. Do not write a number of your own into a skill call — a
plausible-looking constant that is wrong for this instrument is worse than asking.

## Human-in-the-loop points

A composite skill can contain a step that only a person may resolve (a `human` node). On the
**optical station**, on the external-agent path, MAST asks the operator for you: the job keeps its
place but waits, `GET /jobs/{id}` (and `mast_job`) shows `waiting_operator` with the `prompt_id`, and the operator
answers in MAST's web UI or the launcher popup, which lights up and sounds. The wait is bounded —
30 minutes unless the skill says otherwise, never more than 4 hours — and cancelling the job
withdraws the prompt. If nobody answers in time, the operator dismisses the prompt, or the prompt
is cancelled, the job fails with an explicit message; it never stalls and never picks a branch for
you. While it waits, do whatever else does not need that job, wait for it with `mast_wait`, or
cancel it. The job **keeps the instrument** while it waits, like a measurement protocol: a write
from anybody else is refused as `refused_busy` (naming the job) until the operator has answered or
the job has ended, because the composite's next step expects the instrument as its first steps
left it; the stop skills (`StopWITec`, `StopSpectrometer`, ...) still run. A plain
`needs_human_node` refusal is rare: a pause MAST cannot turn into a question for the operator (a
`human` node inside a thread the skill started itself, or another kind of pause).

**On an STM station nothing waits for a person.** A `human` node on the external path ends the job
at once with an error saying that path has no human-decision channel - nothing is asked, no popup,
no instrument token is held for somebody who may not be there - exactly as it always did. Run such
a composite from the MAST UI, or use a version without human nodes.

For your own questions use `mast_ask_operator`. On the optical station the structured kinds —
`confirm`, `choice`, `number`, `text`, `attest` — appear as a card with a popup and a sound; read
the answer with `mast_operator_reply` or wait for it with `mast_wait` (`operator_reply`). On an STM
station every request is a text request: the kind is stored as you wrote it, the structured fields
are ignored without an error, and the operator replies in words. `attest` asks the
operator to sign off a manual optical setup (which waveplate is installed, whether the laser area
is safe); the answer sets those registry slots, and only the operator can give it.

What the operator is shown to sign comes from the registry's slot definitions, not from you. A
yes/no slot such as `laser_area_safe` takes no `options` of yours (422 `invalid_prompt`: an option
you word is an option the operator may click, and its id is what gets recorded); a choice slot
takes only options from its own list and shows them under its own names; a number slot has its
own unit and range; one prompt names slots of one kind only. An attestation the operator
recorded as `False` is an answer, not an approval: the answer's `value` is `false` and `ok` is
false, and `AskOperator` returns `attested` (`{slot: recorded value}`) and says so in its
summary. A prompt you post without `timeout_s` closes as a timeout after 24 hours; the
operator's list holds at most 100 open prompts, at most 75 of them from external agents
together (the next one is a 429 `too_many_open_prompts`, whatever name you use).

Never approve, dismiss or otherwise resolve a human-in-the-loop prompt for the operator —
including one that belongs to MAST's own internal agents. The endpoints that answer are not part
of this API, and no tool here will answer for you. Be honest about what that is: a rule, not a
lock. A program on the machine that runs MAST can reach the operator's own endpoints, and
nothing technical stops it from calling them; that is exactly why you must not. What MAST does
is keep a record: for every answer and every optical-state entry it notes where it came from —
the peer address and the client's identification (user agent, whether a web page sent it) —
next to the name the operator typed, so the operator can check it afterwards. Those details are
what the server saw, and a program can send headers that look like a browser's, so the record is
evidence for a person to weigh, not proof. That decision belongs to a person, not to you.

## SAFE mode is not a fence against you

SAFE refuses tip processing at the execution layer, and it is the operator's setting: you cannot
change it through this API, and you should not try to route around it. Be honest with yourself
about what it actually is. It is a directive that a well-behaved agent follows, not a wall that
would stop a differently-behaved one: the same login that reaches this API typically reaches the
rest of MAST's application too, since authentication wraps the whole service rather than this
surface alone. The boundary that matters here is your own conduct, not a technical barrier between
you and the operator's settings — treat SAFE, and every rule in this file, as something you follow
because it is right, not because nothing else was possible. The observation token is the same kind
of thing: it keeps a well-behaved agent from acting blind, and it locks nobody out.
