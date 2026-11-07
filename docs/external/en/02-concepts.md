# Concepts

The ideas below explain what the endpoints in [the API reference](07-api-reference.md) are
actually doing. Read this once; the [Operating rules](03-operating-rules.md) that follow will make
more sense with these names in hand.

## Skills and skill metadata

A **skill** is one named, registered action: a read (get the bias), a write (set a scan frame), a
composite of several skills, or a pure analysis with no hardware involved. Every skill carries
metadata that the skill card (`GET /skills/{name}`) exposes in full:

| Field | What it tells you |
|---|---|
| `category` | `read`, `write`, `composite` or `analysis` — its shape, not its risk |
| `safety_level` | `auto`, `confirm` or `dangerous` — see [Safety levels](#safety-levels) below |
| `parameters` | Each parameter's type, unit, required flag, default, and `min`/`max` bounds or an allowed-values list |
| `preconditions` | Instrument-state conditions checked before it runs (for example, that a scan is not already running) |
| `capabilities` | Tags such as electrical-pulse or tip-shaping, read by the operating-mode gate — see below |

A dimensioned parameter's bounds are in SI base units (volts, amperes, metres, seconds); some also
accept an SI-prefixed string such as `"5n"` instead of a bare float — the skill card's `si_params`
says which, and whether the prefix is required. Do not guess a skill's name from what it does:
search for it by the action, as [Operating rules](03-operating-rules.md) explains.

## Composite skills

A **composite skill** is a graph of existing skills expressed as a JSON `CompositeSpec` — data, not
code. It runs under exactly the same gates as any other skill. Its effective safety level is the
**stricter** of what the spec itself declares and the strictest of its steps, and its capability
tags are the **union** of its steps' tags — so a declaration can only tighten what its steps
already are, never loosen it: a spec declared `dangerous` stays `dangerous` even if every step is
`auto`; a spec declared `auto` that contains one `confirm` step runs as `confirm`; and a spec
containing a tip-processing step carries that capability tag, so SAFE refuses the whole composite
exactly as it would refuse that one step alone. [Authoring skills](05-authoring-skills.md) covers
writing one.

## MAST's instrument-agent tools (new in 7.0.1)

Besides skills, MAST's own instrument agent works with a set of tools that are not skills: they
read and write MAST's records, not the instrument. From 7.0.1 an external agent has the same set,
with no role distinction (the two kinds not offered are listed at the end of this section). `GET /skills/search` lists them among the skills (origin
`仪器 agent 工具`, origin code `ic_agent_tool`), `GET /skills/{name}` gives a card of the same shape
plus its `family`, and `POST /jobs` — or `mast_run` without a token — runs one as a job of kind
`tool`, recorded like a skill call.

| Family | Tools |
|---|---|
| Experiments and samples | `list_experiments`, `list_samples`, `start_experiment`, `switch_experiment`, `start_sample`, `switch_sample`, `clear_sample`, `end_experiment`, `end_sample`, `rename_experiment`, `rename_sample` |
| Tip registry | `get_current_tip`, `list_tips` (read only; the operator registers, edits and removes tips in the UI) |
| Scan map and coarse map | `get_map_analysis`, `get_next_scan_position`, `get_markers_near`, `get_coarse_map`, `mark_area_used` |
| Plans | `plan_scan_batch`, `create_plan`, `approve_plan`, `list_plans`, `get_plan_progress`, `advance_plan`, `pause_plan`, `resume_plan`, `show_plan_on_map`, `clear_plan_on_map` |
| Knowledge and guidance | `query_knowledge`, `get_workflow_advice`, `get_skill_guidance`, `get_literature_parameters`, `get_fault_diagnosis`, `get_noise_reference`, `get_measurement_template`, `search_deep_reference`, `read_reference_section` |
| Files and documents | `get_latest_scan_info`, `load_scan_file`, `list_documents`, `load_document`, `survey_environment` |
| Skill market | `search_skill_market`, `recommend_skill_subscription` |
| Conduct | `conduct_status`, `conduct_list`, `conduct_compile_from_plan`, `conduct_post_op` (`pause`, `abort` or `takeover` only) |

Their footprint is `mast-read` or `mast-write`. None of them sends a controller command itself
(the `abort` op of `conduct_post_op` makes the conduct director retract the tip; the card lists
`SafeRetract` under `sub_skills`); the
experiment, tip, scan-map, plan, knowledge and scan-file tools refresh Nanonis's save directory once
per call (`Util_SessionPathGet`, cached for 15 s, a read; the card lists it under `verbs`), exactly
as they do for MAST's own agent. Three rules apply on the external
side: a tool that changes the active experiment or sample is refused while the instrument is busy,
as `POST /scope` is; a file path must lie inside the data search directories or the experiment
root, as for `/data/*`; and `conduct_post_op` takes only the ops that stop or take back a run.

Two kinds of tool are not offered; their card and a submission answer 403 `not_offered` with the
reason:

- Tools that would hand work to MAST's internal agents: `spawn_background_task`,
  `request_activation`, `conduct_create`, `conduct_approve`, and the `start` and `resume` ops of
  `conduct_post_op`. The external agent does the work itself, and every capability those tools
  would use is reachable directly.
- Tools that state, on the operator's behalf, that something happened at the instrument:
  `register_tip`, `update_tip`, `remove_current_tip` (a tip change) and `record_coarse_move` (a
  coarse move made by hand in Nanonis). Recording them clears safety interlocks or learned values:
  backfilling a coarse move clears the block on a spot where the tip crashed twice, registering or
  removing a tip clears the qPlus amplitude baseline of the crash check, and the default
  conditioning doses follow the registered tip. A remote agent cannot see whether the event
  happened. Ask the operator to do it in MAST's UI (the tip card, the scan-map page) with
  `mast_ask_operator`.

## The execution choke point

However a skill call arrives — a direct job, a step inside a composite, MAST's own internal agents
— it passes through the same sequence of checks before anything reaches the instrument:

1. **Abort check** — refused outright if the abort flag is set.
2. **Sample gate** — skills that produce data need an active sample (see below).
3. **Operating-mode gate** — SAFE or SEMI may refuse tip-processing calls (see below).
4. **Five hard gates** — physical protections that are refused unconditionally on this path, with
   no approval to wait for: open-loop coarse Z approach toward the sample; disabling a hardware
   protection (SafeTip, Z soft limits); rewriting the global bias/current calibration; setting the
   coarse motor's drive voltage or frequency; and a raw lateral coarse move that bypasses the
   guarded relocation skill. Nothing you do changes these five; they are not addressed to you.
5. **Global safety envelope** — every dimensioned parameter is checked against the administrator's
   configured bounds, regardless of what the skill's own metadata says.
6. **Parameter validation** — the skill's own `validate_params`.
7. **Preconditions** — the skill's declared preconditions, checked against current instrument
   state. When one is not met, the state is read from the instrument once more and the check
   repeated; a failed re-read never turns a refusal into a pass.
8. **Instrument arbitration** — the instrument lock (see below).

Only after all eight does the skill actually touch the instrument. A refusal at any step is not an
HTTP error: it is a normal, structured answer — read [Troubleshooting](08-troubleshooting.md) for
what each one looks like.

## Safety levels

`safety_level` is informational on this path, not a built-in brake — read it as "how much care this
deserves", not "MAST will pause for a human here":

| Level | What happens on the external-agent path |
|---|---|
| `auto` | Executes immediately. Reads and small, repeatable writes. |
| `confirm` | Also executes immediately — this path has no human in the loop to confirm to. Scan parameters, spectroscopy, tip conditioning and guarded lateral coarse motion are typically this level. |
| `dangerous` | Also executes — recorded, and the operator is notified afterward. It is not blocked and no approval is awaited; this level is rare (only a couple of skills in the whole catalog). |

The five hard gates above are the only calls that are unconditionally refused regardless of
`safety_level`. Everything else executing means the judgment about whether an action is wise is
yours to make, not something the level enforces for you.

## Operating modes

The **operating mode** is the operator's global setting, reported by `GET /status` as `mode` (or
`unknown` when nothing has bound one):

| Mode | What it allows |
|---|---|
| `safe` | No tip processing at all: every electrical pulse and mechanical tip-shaping call is refused, including one buried inside a composite. Scans, spectroscopy and all other measurement continue normally. |
| `semi` | Shallow, purely mechanical tip shaping is allowed; a plunge that is too deep is refused. An electrical pulse runs and is logged — it is not silently blocked here. |
| `auto` | Everything allowed (subject to every other gate above). |

You cannot change the mode from the external-agent API, and should not try to route around it: if a
task needs more than the current mode allows, ask the operator (see
[Operating rules](03-operating-rules.md)).

## Sample gate

Skills that produce data (scans, spectroscopy) require an active sample; skills that only read
state or that are safety remedies (a stop, a retract, a withdraw) never need one. When a skill's
classification is ambiguous, this gate **fails open** — on purpose: an unlabelled recording is a
bookkeeping loss, while refusing a safety remedy in the wrong moment is not an acceptable trade.
Set a sample with `POST /scope` before running anything that produces data.

## Instrument lock

Only one caller drives the physical instrument at a time. The lock is taken per skill call, is
re-entrant (so a composite does not deadlock on its own sub-steps), and reads, analysis and stop or
retract skills never take it at all. A caller that cannot get the lock is **refused, never
queued** — `refused_busy` names the current holder. This is deliberate: a long scan must never grow
an invisible backlog of callers waiting behind it, and an agent told that someone else is driving
should wait and check back, not spin.

## Abort and cancel

Two different stop mechanisms exist at two different scopes. The process-wide **abort flag** — the
emergency latch — is set by E-STOP (the operator's own button, or your own `POST /estop`), an
environment alarm, or a hardware safety signal; the operator simply stopping one of MAST's own
agent runs does not set it. While the latch is set, every skill call is refused before it starts —
reads and stop skills included, with no exception — because the E-STOP that set it has already
stopped motion and withdrawn the tip, so there is nothing left for another stop skill to do; only
the operator can release it. (Status and briefing calls still answer while it is set: they report
cached state rather than running a skill.) `GET /status` reports the latch as
`abort: {set, emergency, why}`.

A per-job **cancel** (`POST /jobs/{job_id}/cancel`) is a narrower, cooperative mechanism scoped to
one job: the skill stops at its next check, which means a skill blocked inside a single Nanonis
call only stops once that call returns. To stop physical motion immediately, run a stop skill
(never blocked by the instrument lock) rather than waiting on a cancel.

## Jobs and their states

Submitting a skill through `POST /jobs` returns immediately with a `job_id`; the work happens in
its own thread and you poll for the result:

| State | Meaning |
|---|---|
| `queued` / `running` | Not finished yet — keep polling. |
| `succeeded` | The skill itself reported success. |
| `failed` | The skill reported failure, or was refused at one of the gates above (`refused_by` names which). |
| `refused_busy` | The instrument lock was held by someone else; nothing ran and nothing was queued. |
| `cancelled` | Stopped by `POST /jobs/{job_id}/cancel` before it finished successfully. |
| `crashed` | The job's own thread failed unexpectedly; treat the outcome as unknown. |
| `lost_on_restart` | MAST restarted while the job was in flight. It is never replayed, and nobody knows how far it got on the instrument. |

A client-side timeout while waiting is not one of these states: the job keeps running on MAST
regardless of whether you are still watching it, which is the whole reason jobs exist instead of a
single long HTTP call. [Operating rules](03-operating-rules.md) covers how to act on each state.
From 7.0 on, every act of the visual harness is a job too (kind `act`), with the same states, and
`mast_wait` waits for any job on the server instead of polling (see below).

## The visual harness (new in 7.0)

VISTA is a visual harness for multimodal models. Instead of a text summary that an image
classifier wrote, the model receives the instrument's own images; it can bring back any earlier
frame without loss and magnify part of it, read exact values, measure with tools, and keep two
notes files of its own. In 7.0 the harness runs inside the MAST server, so the archive, the
measurements and the one-act-one-observation rule are the same for every client.

### Workspaces

Each MAST experiment has one workspace. The active experiment, chosen with `mast_scope`, decides
which workspace every VISTA tool reads and writes: `exp-<experiment id>`, or `no-experiment` while
none is active. Switching the experiment switches the archive, `WORKING.md`, the history and the
turn counter with it. A workspace lives in `experiments\vista\<workspace id>\` under MAST's project
folder (the installation folder, for an installed MAST) and is reopened after a restart.

| In a workspace | What it holds |
|---|---|
| `archive\` | Every frame of the experiment, lossless (see below) |
| `notes\WORKING.md` | The agent's working memory for this experiment |
| `events.jsonl` | One record per act, observation, refusal, outside frame, checkpoint, detector hit and detector switch-off |
| `detectors.json` | The experiment's analysis detectors, with their recent hits |

`GUIDE.md`, the durable lessons, is one file shared by every experiment:
`experiments\vista\GUIDE.md`.

### Frames and frame ids

Every new data file the controller saves (`.sxm` scans, `.dat` spectra) is archived with all its
channels and both scan directions, as SI values, so it can be re-rendered, magnified and read again
at any time. A frame id names the turn and the kind: `t0007.s0` is the first saved scan of turn 7;
`p` marks a partial snapshot of a scan in progress, `d` a spectrum, and `m` a derived image that a
measurement tool produced (a profile plot, a Fourier spectrum, a difference map). The turn counts
the accepted acts of the workspace. Every frame records its `origin`: `act` for a frame your acts
produced, `external` for a frame that came from anywhere else — MAST's user interface, the
operator, MAST's built-in agents, or another client.

An observation shows at most one image: the newest new scan, or else the newest new spectrum. The
other new frames are listed in its text, and `mast_inspect` shows any of them.

### Display pixels, scan-frame nanometres and the grid

Two coordinate systems are in play:

- **Display pixels:** positions in the frame's full image as rendered. The origin is the top-left
  corner, x runs to the right and y down. A small frame is enlarged by a whole number of image
  pixels per scan pixel, without smoothing; every image says how its pixels map to scan pixels and
  to nanometres.
- **Scan-frame nm:** the instrument's own lateral coordinates, with the frame's centre and rotation
  taken into account. This is what the tip moves in. Its y axis points up, the opposite of display
  pixels.

Acts and measure tools accept either kind of position: `{"x_nm": .., "y_nm": ..}`, or
`{"frame": "t0007.s0", "x": .., "y": ..}` in display pixels of an archived frame, converted with
that frame's own geometry. You never convert by hand. Scan images carry a faint amber grid, a
square every `grid_nm` nanometres counted from the frame's top-left corner; the spacing is in the
image's label and gets finer when you zoom in. The grid is graph paper for estimating positions and
sizes by eye; exact numbers come from the read and measure tools. Positions taken from an older
frame may be stale: the sample drifts and the piezo creeps.

### GUIDE.md and WORKING.md

Two Markdown files the agent writes for itself. They live on the MAST server, not in the client's
context:

| File | Holds | Shared by | Size limit |
|---|---|---|---|
| `GUIDE.md` | Durable, revisable understanding of the instrument, the samples and the tip | Every experiment and every later session | 65,536 characters |
| `WORKING.md` | The current plan, the next expected observation, open contradictions, the frame ids still needed | The active experiment | 16,384 characters |

Read both at the start of a session (`mast_read_guide`, `mast_read_working`), and rewrite them
whole with `mast_write_guide` and `mast_write_working`. They are what survives a change of context:
a fresh session — after the client compacts its context, after a crash, or as the runner's next
session — starts from `GUIDE.md`, `WORKING.md` and the newest observation. They are separate from
MAST's notes (`mast_note_write`), which go into MAST's memory for MAST's own agents and the
operator.

## One act, one observation (new in 7.0)

An action may run only after the result of the previous action has reached the agent. An MCP server
cannot see what a model has looked at, so MAST enforces the rule with an **observation token**:

- Every observation MAST hands out carries a fresh token, `next_token`, such as `K7QM-29XA`: the
  result of an act, `mast_observe`, a `mast_wait` that brings a finished act or a new frame, and the
  recovery observation attached to an `observe_first` refusal.
- The next instrument action must quote it as `after`. An accepted act consumes it: a token works
  once.
- There is one live token per MAST process, because there is one instrument. It is bound to the
  client session it was delivered to (one MCP connection). It dies when a newer observation is
  handed out, to you or to any other client, and when a frame arrives that your acts did not
  cause.

What is gated: `mast_act`, and `mast_run` of a skill that drives the instrument. Never gated:
reads, analyses, the archive and measure tools, and the remedies — stop and retract skills such as
`StopScan` and `WithdrawTip`, `mast_cancel` and `mast_emergency_stop`. Once a session has used the
visual harness, `POST /jobs` refuses its instrument-driving skills with `use_act`; they go through
`mast_run` with `after`, or `mast_act`. A caller that never touches the harness, such as a 6.5.0
script, works exactly as before.

### Refusals

A refusal executes nothing and does not advance the turn. The checks run in this order, and the
first one that fails answers (HTTP 409):

| Code | Meaning | What to do |
|---|---|---|
| `session_closed` | The operator stopped this session (a runner stop) | Do not act again; finish with a short summary |
| `act_in_flight` | Another act is still running, in any session; the answer names its `job_id` | `mast_wait` for that job, look at its observation, then act |
| `same_code_cell` | This Codex code cell already ran an act | End the cell, look at that act's observation (`image()`), and act from a new cell |
| `checkpoint_requested` | MAST asked for a checkpoint | Save `WORKING.md` with `mast_write_working`; until then only the notes tools run |
| `observe_first` | `after` is missing, already used, older than the newest observation, or was issued to another session | The refusal carries the newest observation and a fresh token: look at it, then act with that token |

`observe_first` is also the way back for a client that lost its token — after its context was
compacted, or after its MCP server or MAST restarted: call `mast_observe`, or read the observation
the refusal brings, look at it, and act with its token.

### Codex Code Mode

In Code Mode, Codex models call tools from JavaScript. A result lands in a variable, and its image
reaches the model only when the code calls `image()` on it. Every call made by one code cell
carries the same item id, so the server accepts at most one act per code cell. The pattern that
works: one act in a cell, `image()` on its observation, end the cell, decide, and act from the next
cell with the new token.

### Strict mode

With strict mode on (the operator starts MAST with `MAST_VISTA_STRICT=1`), the next token is not in
the text at all: it is printed in a band under the observation image, and the text says
`"next_token": null, "token_on_image": true`. An observation without a frame image then comes with
a small rendered card that shows the instrument state and the token. Strict mode is off by default.
The token's alphabet leaves out look-alike characters (no 0 and O, 1 and I, 2 and Z, 5 and S, 8 and
B), and case, spaces and the dash do not matter when you quote it.

The token is a protocol against acting without looking, **not** a security boundary: any process on
the machine can call MAST's HTTP API. Safety stays with MAST's safety gates.

## Acts and waiting (new in 7.0)

An act runs as a MAST job of kind `act`, through the same execution layer as every skill:
idempotency by `request_id`, cancelling, the job journal and the busy refusal all apply, and each
step is recorded like an external job. `mast_act` waits up to `wait_s` seconds for it (default 45,
at most 50); an act that is still running then comes back with `state: running` and its `job_id`.
Get its observation with `mast_wait` on that job, and do not act meanwhile: a second act is refused
with `act_in_flight` until the first one has ended.

`mast_wait` waits **on the server** until one of its conditions fires, for up to `timeout_s` seconds
(default 600, at most 840):

| Condition | Fields | Fires when |
|---|---|---|
| `job` | `job_id` | That job, an act or a plain job, has ended |
| `frame` | — | A new frame was archived |
| `alert` | `min_level`: `info`, `warning` or `critical` | An anomaly, current-monitor or conduct alert at or above that level |
| `operator_reply` | `request_id` (optional) | The operator answered that request, or any request |
| `detector` | `detector_id` (optional) | A detector recorded a hit |
| `timer` | `seconds` | That much time has passed |

The answer lists what fired, whether the wait timed out, how long it waited, and a `cursor`. When a
finished act or a new frame fired, the answer also carries that observation, with its image and a
fresh token. The next `mast_wait` on the same connection continues from the last cursor (or from the
one you pass as `since`), so no event between two waits is lost. A wait can last 14 minutes, so the
client's MCP tool timeout must be at least 900 s (Codex: `tool_timeout_sec = 900`). Interrupting a
wait changes nothing on MAST.

## Detectors (new in 7.0)

An **analysis detector** is a short Python function you write, `def detect(frame)`, that MAST runs
on every newly archived scan or spectrum of the kinds you choose (`applies_to`). It runs in a
separate interpreter: no access to the instrument, the raw data protected, a time limit and a
memory cap. `frame` provides `kind` (`scan` or `spectrum`), `fid`, `channels` (channel name to the
forward pass as a 2-D array in SI units, row 0 at the top edge, NaN where nothing was acquired),
`backward`, and `meta` (geometry, bias, setpoint, units); a spectrum also has `columns` and `units`.
Return a falsy value for no hit, `True`, or `{"hit": bool, "message": str, "value": ...}`. A hit is
recorded and ends a `mast_wait` on `{"type": "detector"}`.

| Limit | Value |
|---|---|
| Enabled detectors per experiment | 8 |
| Time per frame, `max_runtime_s` | 20 s by default, at most 120 s |
| Automatic switch-off | After 5 errors in a row, or more than 30 hits within an hour |
| Wake-ups | At most 60 an hour across all detectors; a further hit is recorded as `suppressed` and wakes no one |

Detectors never act. To respond to a hit, you act yourself: with `mast_act`, or with a composite
skill run through `mast_run`. An **action detector** is exactly that: a composite skill
(`mast_composite_save`) that you run when a hit calls for it, through the same safety gates as any
skill. Test a new analysis detector on an archived frame before relying on it:
`mast_detectors` with `op` `test`, the `detector_id` and a `frame` such as `t0007.s0`.

## The runner (new in 7.0)

The runner lets MAST drive an agent unattended. It starts Claude Code sessions itself
(`claude -p`), with MAST's MCP server as their only tool source, and keeps them going for the length
of a task. It is the operator's tool, started over HTTP (`POST /api/ext/v1/runner/start` with the
task), one run at a time; no MCP tool starts it.

- **Subscription only.** The runner removes `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` from the
  session's environment and ends a session that reports any API-key source: Claude Code must be
  signed in with a Claude subscription on that PC.
- **Checkpoints and fresh sessions.** When a session has used up its image budget, or Claude Code
  is about to compact its context, the runner asks for a checkpoint: only the notes tools run until
  `WORKING.md` is written. Then a fresh session starts from `GUIDE.md`, `WORKING.md`, the task and
  the newest observation.
- **Operator stop.** `POST /api/ext/v1/runner/{run_id}/stop` closes the session at once, so its acts
  are refused with `session_closed`; then the runner interrupts Claude Code and ends the run with
  `stopped_by_operator`.
- **Every call answered.** The runner checks that every tool call got its result. A call left open
  is recorded (its job keeps running on MAST), and the next session is told to check `mast_jobs` and
  `mast_wait`.

A runner for Codex follows in a later 7.0.x release; until then Codex is used interactively.

## Literature search (new in 7.0)

`mast_literature_search` searches MAST's literature knowledge base — the operator's library of
papers — by meaning, without touching the instrument. Each hit gives the title, year, journal, DOI,
a relevance score and an excerpt of the abstract. Use it for published values and procedures
(lattice constants, adsorption sites, spectroscopic features, tip preparation), and cite the DOI.
`degraded: true` means keyword matching only, or no library on this machine.
