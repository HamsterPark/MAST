---
name: mast-operator
description: "Operate a MAST-controlled instrument station - a scanning tunneling microscope (STM, Nanonis) or an optical Raman / SHG station - through the mast_* MCP tools - read the briefing, set the experiment and sample, look at the instrument (mast_observe), take one action per observation with its token (mast_act, mast_run), measure from the archived frames, wait on the server (mast_wait), keep GUIDE.md and WORKING.md, ask the operator, and hand over."
when_to_use: "Use for any request that touches the STM or MAST - scanning and imaging, approach and withdraw, bias and setpoint, tip conditioning, spectroscopy (STS, dI/dV, grids), drift, looking at or measuring frames and spectra (profiles, FFT, blobs, differences), detectors, waiting for scans, experiment records, notes, handover. Trigger words - STM, scanning tunneling microscope, Nanonis, MAST, scan, tip, bias, setpoint, spectroscopy, dI/dV, approach, withdraw, frame, observe, 扫描隧道显微镜, 针尖, 扫描, 谱, 偏压, 设定点, 进针, 退针, 帧, 观测, 测量, 实验记录, 交接. On an optical station (no STM; WITec confocal Raman / SHG, lasers, power meter, temperature controller) it also applies to - acquiring spectra, Raman, SHG, excitation wavelength or power series, polarization or temperature series, laser, WITec, 拉曼, 光谱, 激光, 偏振, 光学站."
---

# Operating MAST from Claude Code

MAST is the control system of a real instrument station: a scanning tunneling microscope
(Nanonis) or an optical Raman / SHG station with no STM. `mast_briefing` says which; on an optical
station read [the optical station page](references/en/09-optical-station.md) first (the rest of this
page describes the STM station). The `mast` MCP
server of this plugin talks to MAST's external agent API, and you act through its `mast_*`
tools. Treat every action as physical: a wrong parameter can crash the tip or damage the sample.

From 7.0 on, MAST runs the VISTA visual harness on the server: you look at the instrument's own
images, every frame of the experiment stays archived and can be brought back and magnified, exact
numbers come from measurement tools, and an action runs only with the token of the newest
observation you received (one act, one observation). The harness has been tested against the
STM-Bench simulator, not yet on a real instrument.

## Workflow

1. **Briefing first.** `mast_briefing` returns the whole state in one call: mode and abort flag,
   who holds the instrument, scope and recording, resume notes, tip, instrument profile, live
   readings, the operator's parameter preferences, safety envelope, recent actions and files,
   alarms, notes, your jobs and requests. `mast_status` is the cheap re-check.
2. **Scope.** `mast_scope` shows or sets the active experiment and sample. Without an active
   experiment nothing you do is recorded, so set one before acting
   (`experiment_name`, `goal`, `sample_name`). The experiment also selects the workspace: frame
   archive, `WORKING.md`, history.
3. **Notes.** `mast_read_guide` (durable lessons, shared by every experiment) and
   `mast_read_working` (the plan and state of this experiment).
4. **Look.** `mast_observe`: the newest frame as an image, the instrument state and
   `next_token`. Look at the image.
5. **Act once, with the token.** `mast_act(action, params, after=<next_token>, note=...)` takes
   exactly one instrument action. For one raw skill that drives the instrument, use
   `mast_run(skill, params, after=<next_token>)`; reads, analyses and stop or retract skills need
   no token. Pass a `request_id` whenever you might resend after a timeout: the same id returns the
   original act or job instead of running it twice.
6. **Look at the result.** A finished act answers with its observation: the new image (one per
   observation), the controller's result, the instrument state and a fresh `next_token`. Look at
   the image before you decide anything.
7. **Measure.** Numbers come from the archived values, not from pixels: `mast_read_values`,
   `mast_line_profile` (distances, step heights), `mast_fft_peaks` (periods, directions),
   `mast_detect_blobs` (count and locate features), `mast_diff` (what changed between two
   frames), `mast_stats` (roughness, noise). `mast_inspect` re-renders and magnifies any archived
   frame; frame ids look like `t0007.s0`.
8. **Wait on the server, do not poll.** An act still running after `wait_s` (default 45 s) answers
   `state: running` with a `job_id`: call `mast_wait` with `{"type": "job", "job_id": ...}` for its
   observation, and do not act meanwhile. `mast_wait` also waits for new frames, alerts, operator
   replies, detector hits and timers; one call with all the conditions you care about and a long
   `timeout_s` (up to 840 s). A plain skill job can still be polled with `mast_job(job_id,
   wait_s=30)`.
9. **Find skills by action.** For `mast_run`, call `mast_find_skills` with the verb or Nanonis
   command you need ("withdraw", "set bias", "z controller", "scan frame", "bias spectroscopy").
   Many skills are named after the problem they solve, so guessing names fails. Prefer hits marked
   `official`, and read `mast_skill_card` before a skill's first use: parameters with units and
   limits, `si_params`, preconditions, safety level, footprint, whether it takes the instrument
   lock or needs a sample, typical duration, and whether it is switched off on this machine.
   The same search finds MAST's instrument-agent tools (origin `仪器 agent 工具`): experiment and
   sample lists (`list_experiments`), the scan map and next position (`get_map_analysis`,
   `get_next_scan_position`, `get_markers_near`), the coarse map, the current tip
   (`get_current_tip`), plans (`plan_scan_batch`, `create_plan`), the knowledge base
   (`query_knowledge`, `get_fault_diagnosis`), documents and conduct status. They read or write
   MAST's own records, never the instrument, and run with `mast_run` without a token.
10. **Keep the notes current.** `mast_write_working` after every step that changes the plan: the
    plan, the next expected observation, open contradictions, the frame ids you still need. A
    fresh session starts from it. Lessons that will hold later go into `mast_write_guide`;
    findings the operator and MAST's own agents should know go into `mast_note_write`.
11. **Ask, do not assume.** `mast_ask_operator` for anything that needs a person. It does not
    block; read the answer later with `mast_operator_reply`, or include
    `{"type": "operator_reply"}` in a `mast_wait`.
12. **Handover.** When you are done, leave the instrument idle (no act or job running, scan
    stopped), save `WORKING.md` and call `mast_handover(summary, next_steps)`.

## Rules

- Read the briefing at the start, and again after a restart, a `lost_on_restart` job or any
  surprise.
- **Look before you act, and always act with the newest token.** A token works once; every newer
  observation (your own `mast_observe` or `mast_wait`, a refusal's recovery observation, a frame
  from outside your acts) replaces it. A refusal ran nothing: `observe_first` carries the newest
  observation and a fresh token, so look at it and act with that token. `act_in_flight`: wait for
  the running act. `checkpoint_requested`: write `WORKING.md` first; only the notes tools run until
  then. `session_closed`: the operator stopped this session; do not act again.
- When an observation says `"token_on_image": true` (strict mode), the token is printed in the band
  under the image: read it there.
- Take numbers from the read and measure tools; images and their amber grid (a square every
  `grid_nm`) are for seeing. Positions from older frames may be stale: the sample drifts and the
  piezo creeps.
- Ask the scan map, not the image, where you have been and where to go next:
  `mast_run("get_map_analysis")` / `get_next_scan_position`. Tools that change the active
  experiment or sample are refused while the instrument is busy, like `mast_scope`.
- What happened at the instrument is the operator's to record: a new tip (the tip card) and a
  coarse move made by hand in Nanonis (the scan-map page). Ask with `mast_ask_operator`;
  `register_tip`, `update_tip`, `remove_current_tip` and `record_coarse_move` are not offered,
  because recording them clears safety interlocks (the repeated-crash block, the qPlus amplitude
  baseline) and you cannot see whether the event happened.
- Do the work yourself: tools that would hand work to MAST's internal agents
  (`spawn_background_task`, `request_activation`, starting a conduct) are not offered.
- Detectors (`mast_detector_create`, `mast_detectors`) only watch new frames; they never act. Test
  one on an archived frame (`op` `test`) before relying on it.
- `refused_busy` means someone else holds the instrument: your action did not run and was not
  queued. Do not retry at once; check `mast_status` and wait until the holder is done.
- `lost_on_restart` means MAST restarted while the job was in flight and never replays it. Its
  outcome is unknown: read the briefing before deciding to submit again.
- `mast_cancel` is cooperative. It acts between skills, between sub-steps and before write
  commands, never inside one blocking Nanonis call. To stop motion now, run a stop skill such
  as `StopScan` with `mast_run` and no token (stop skills are never blocked by the instrument lock
  or the observation gate). For real danger use `mast_emergency_stop`; afterwards the abort flag
  refuses every write until the operator clears it.
- Never approve human-in-the-loop requests on the operator's behalf.
- SAFE is the handover mode. The operator sets it before handing you the instrument; it refuses
  tip shaping and pulses on every path, composites included, and these tools cannot change it or
  any other operating mode. If a task needs more, ask.
- Stay inside the operator's parameter preferences and the safety envelope from the briefing.
  A refusal from a safety gate is an answer, not an obstacle to route around.
- A failed skill is not an API error, and a degraded answer (a subsystem not wired) is not an
  empty one. Read the job state, the result and `degraded` before concluding anything.
- Use `mast_fetch(mode="frame")` when you need a scan as arrays on disk: MAST applies the
  orientation conventions (backward mirror, scan-direction flip) once and correctly.
- To find earlier data by size, bias, setpoint, experiment, sample or date, use
  `mast_search_data`; a hit with `frame` goes straight to `mast_inspect` and the measure tools.
- Interrupting a tool call does not stop the job on MAST; look it up with `mast_jobs` or
  `mast_wait`.

## Parameters and units

Actions of `mast_act` take nm, V, pA, s and nm/s. A position is `{"x_nm": .., "y_nm": ..}` in
scan-frame nm, or `{"frame": "t0007.s0", "x": .., "y": ..}` in display pixels of an archived frame
(origin top-left, y down). The action list and its ranges are in the `mast_act` tool description.

Skills run with `mast_run` take SI base units: volts, amperes, metres, seconds. Parameters listed
under `si_params` in the skill card also accept prefixed strings: `"5n"` is 5e-9, `"200p"` is
2e-10; the card also says where the prefix is required. Check `min`, `max` and the allowed values in
the card before the first run.

## Building new skills

Use an official skill when one fits; otherwise build a composite from existing skills, and
propose Python code only as the last resort. The `mast-skill-author` skill walks through it.

## When something goes wrong

| What you see | Meaning | What to do |
|---|---|---|
| `observe_first` | Your token is missing, used, or older than the newest observation | Look at the observation the refusal brought (it is a normal result, so its image shows); act with its token |
| `act_in_flight` | Another act is still running | `mast_wait` on its `job_id`, look, then act |
| `same_code_cell` | (Codex) a second act from one code cell | One act per cell |
| `checkpoint_requested` | MAST asked for a checkpoint | `mast_write_working` first |
| `session_closed` | The operator stopped this session | Stop acting; summarise |
| `use_act` from `POST /jobs` | This session uses the observation gate | `mast_run` with `after`, or `mast_act` |
| `frames_unavailable` in an act | MAST does not know where the controller saves files | Tell the operator; new frames are not archived |
| `refused_busy` | Another agent or the operator holds the instrument | Wait; check `mast_status`; resubmit after they finish |
| `failed`, `refused_by: needs_human_node` | The composite has a step for a person | Ask the operator to run it in MAST |
| `failed`, refused by a sample gate | The skill produces data and needs an active sample | `mast_scope(sample_name=...)` |
| `failed` with a parameter error | A value could not be read or is out of range | Re-read the card; use SI numbers or `si_params` strings |
| Abort flag set (status or job) | E-STOP or a stop is latched; writes are refused | Tell the operator; only they clear it |
| `recorded.v1: false` | No active experiment; the action is not in the records | `mast_scope` before the next action |
| `lost_on_restart` / `crashed` | MAST restarted, or the job died inside MAST | `mast_briefing` first, then decide |
| `mast_wait` timed out | Nothing fired within `timeout_s` | Call it again; it continues from its cursor |
| A reply cut short or saved to a file | The reply is larger than Claude Code's MCP output limit | Ask for fewer views, a smaller region or fewer samples |
| An error result (e.g. a `FAILED` act) shows no image | Claude Code does not show the images of an error result | `mast_observe` for the current observation |
| Cannot connect (connection refused) | MAST's main service is not running — an open launcher window is not enough — or the port is wrong | Ask the user to click 「启动主服务」 (Start main service) in the MAST launcher; the error says when it found the launcher. No reconnect is needed afterwards |
| `answers with TLS`, an untrusted certificate, or a login asked for | LAN mode: https, a self-signed certificate and a login, on `127.0.0.1` too | The error names every setting still missing; ask the user either to untick LAN access in the launcher and restart the main service, or to set `mast_url` (https), `mast_user`, `mast_password` and `verify_tls` (false) together and reconnect (`/mcp`) |
| HTML page instead of JSON | That MAST has no external agent API | Update MAST or fix `mast_url` |
| Login rejected | Wrong user name or password for MAST's LAN access | Ask the user for the login shown in the MAST launcher |
| Remote address refused | Only this machine or a VPN is allowed | Turn on `allow_remote`, over a VPN only |
| Too many jobs (429) | The concurrent job limit is reached | Wait for running jobs (`mast_jobs`) |
| `skill_disabled` | That hardware or capability is off on this machine | Pick another skill or ask the operator |
| `not_offered` | The tool would hand work to MAST's internal agents | Do the work yourself with the skills and tools it would use |
| `instrument_busy` from a scope tool | Changing the experiment or sample while the instrument is busy | Wait for the holder, or `mast_scope(force=true)` |

## Reference

Full guide (also served as MCP resources `mast://guide/en/...` and `mast://guide/zh/...`):

- Overview: [English](references/en/README.md) · [中文](references/zh/README.md)
- Quickstart: [English](references/en/01-quickstart.md) · [中文](references/zh/01-quickstart.md)
- Concepts: [English](references/en/02-concepts.md) · [中文](references/zh/02-concepts.md)
- Operating rules: [English](references/en/03-operating-rules.md) · [中文](references/zh/03-operating-rules.md)
- Records and context: [English](references/en/04-records-and-context.md) · [中文](references/zh/04-records-and-context.md)
- Authoring skills: [English](references/en/05-authoring-skills.md) · [中文](references/zh/05-authoring-skills.md)
- Contributing skills: [English](references/en/06-contributing-skills.md) · [中文](references/zh/06-contributing-skills.md)
- API reference: [English](references/en/07-api-reference.md) · [中文](references/zh/07-api-reference.md)
- Troubleshooting: [English](references/en/08-troubleshooting.md) · [中文](references/zh/08-troubleshooting.md)
- Optical station: [English](references/en/09-optical-station.md) · [中文](references/zh/09-optical-station.md)
- Optical layout (read the operator's drawing of the table, comment on it): [English](references/en/10-optical-layout.md) · [中文](references/zh/10-optical-layout.md)
