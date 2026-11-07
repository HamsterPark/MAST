# Records and context

MAST keeps a permanent experiment record, independent of whatever your own harness remembers
between sessions. This file explains what lands in it when you act, what does not, what the visual
harness keeps for each experiment, and where to find the context MAST already holds before you
start.

## What gets recorded, and where

Every job you submit is written into the experiment record under your signature, so a later reader
— human or agent — can tell your actions apart from anyone else's. From 7.0 on, so is every step of
every act: an act's steps run through the same recording path as an external job.

| Record | Field | Value |
|---|---|---|
| v1 `actions` table | `context` | `ext:<actor>` (or `ext:<actor>/<session>` when you sent a session name) |
| v2 action record | `agent_id` | `ext:<actor>` |
| v2 action record | `thread_id` | `ext:<actor>` (or `ext:<actor>/<session>`) |
| Data files the action produced | — | registered, and copied into the current experiment's folder |

Each job's own view carries a `recorded` field with the same facts:

```json
{"v1": true, "v2_action_id": "...", "experiment_id": "...", "sample_id": "..."}
```

## Without an experiment, nothing is recorded

`v1: false` means there was no active experiment when the action ran, so it left no trace in the
record at all — not a failure, just an absence. Both the briefing's `recording` section and every
job's `recorded` field say this plainly; set a sample and an experiment first if the work needs to
be found again later. Frames are still archived without one, in the `no-experiment` workspace, but
no experiment record points to them.

## The workspace (new in 7.0)

Each experiment has a workspace of the visual harness (see [Concepts](02-concepts.md)). Next to the
frame archive and `WORKING.md`, it keeps `events.jsonl`, an append-only log with one record per
event:

| Record | Written when | Holds |
|---|---|---|
| `act` | An act ends | Turn, action or skill, parameters as given and as resolved, the controller's result, the steps, new frame ids, the instrument state afterwards, time used, your note, and the client: the Codex code-cell id and turn metadata, the Claude Code tool-use id, the client's name and version |
| `observe` | An observation is handed out | Its id, the turn, the frame it shows, and why (look, recovery, wait) |
| `refusal` | The observation gate refuses an act | The code, the action, the client |
| `external_frame` | A frame from outside your acts is archived | The frame id and `origin: external` |
| `checkpoint` | A checkpoint is requested, or `WORKING.md` is saved for it | The reason |
| `detector_hit` | A detector records a hit | The detector, the frame, its message, and whether the hit was `suppressed` |
| `detector_disabled` | A detector is switched off automatically | The detector and the reason |

No record holds a token. `mast_history` reads the log without changing anything: `view: "events"`
returns acts, outside frames, detector hits and switch-offs, and checkpoints by turn
(`start_turn`, `end_turn`, `limit`), and `view: "attempts"` summarises the experiment so far:
actions by type and how many failed, frames and spectra.

The frame archive, `archive\`, holds one `index.jsonl` line per frame and, per frame, an `.npz`
with every channel and both directions as SI values (NaN where nothing was acquired) and a `.png`
with the default rendering, the one the agent saw first. A spectrum also keeps its original `.dat`
file, and a derived image keeps the values it was drawn from when it has any. Nothing is ever
evicted. The original data files stay where the controller saved them and are registered in the
experiment as before.

## Finding data (new in 7.0.1)

`mast_list_data` (`GET /data/files`) lists the files MAST can give you, newest first; to find
files by what is in them, use `mast_search_data` (`GET /data/search`). It searches the index the
data gallery has built (`/records/gallery` in the user interface), plus the newest files that index
does not have yet, whose headers it reads on the spot (at most 200 per search):

| Filter | Meaning |
|---|---|
| `ext` | `sxm` (frames), `dat` (spectra), `3ds` (grid spectra) |
| `experiment`, `sample` | An id (or a prefix of at least 6 hex characters), or part of the name |
| `since`, `until` | Start time, in the instrument PC's local time; a date alone in `until` includes that whole day |
| `size_min_nm`, `size_max_nm` | Frame or grid width |
| `bias_min_v`, `bias_max_v`, `setpoint_min_pa`, `setpoint_max_pa` | Bias, setpoint |
| `text`, `tag`, `min_rating` | Words in the file name, path or operator note; an operator tag; the rating (2 key, 1 usable, -1 excluded) |

Filters combine freely; the newest come first (`order: "oldest"` reverses that), and `n` with
`offset` pages through the hits (`total`, `has_more`). Each hit gives:

- `path`, for `mast_fetch`. `fetchable: false` means it lies outside the folders MAST's data
  egress allows (the gallery's folders are set by the operator and can be wider); ask the operator
  to copy the file in;
- `experiment` and `sample`, worked out from the experiment folders, or `null` when MAST cannot tell;
- `frame`, the frame id when the active experiment's frame archive holds the file, ready for
  `mast_inspect` and the measure tools;
- size, pixels, bias, setpoint, channels, a spectrum's sweep range, points and position, and the
  operator's rating, tags and note;
- `time` (start) and `saved`, in local time.

`index` says when the index was written and which folders it covers; before the first index is
built, only the files read on the spot are searched. `live.truncated: true` means the on-the-spot
read reached its limit, so older files outside the index were not searched this time. In
`mast_list_data` results, a file the active frame archive holds carries its `frame` too.

## The briefing's sections

`GET /briefing` is sectioned; ask for all of it, or name the sections you want:

| Section | Contents |
|---|---|
| `status` | Mode, abort flag, instrument lock, connection, live readings, job counts, as text. |
| `scope` | The current experiment and sample, and what happens if there is neither. |
| `resume` | How far this experiment has come — the same resume block MAST's own agents read. |
| `tip` | The registered facts about the current tip. |
| `instrument` | The instrument profile and any learned calibrations. |
| `live` | Live readings, from the cached snapshot. |
| `prefs` | The operator's default-parameter preferences. |
| `safety` | The safety envelope in force, and any latched stops with how to release them. |
| `recent_actions` | Recent actions in this experiment, with who did each one. |
| `recent_files` | Recent data files. |
| `alarms` | The environment monitor's overall status and its most recent alarms. |
| `notes` | Recent notes in this experiment, from any author. |
| `recording` | Your own signature, and whether your actions are being recorded at all. |
| `jobs` | Your own recent jobs. |
| `operator_requests` | Your requests to the operator, and any answers. |

See [the API reference](07-api-reference.md) for the exact shape of each section's answer.

## Notes and MAST's memory

A note you write lands in the same memory store MAST's own internal agents read from, and they
recall it automatically in later turns — you do not need to tell anyone it exists. The reverse is
also true: you can search notes MAST's own agents wrote, which is often the fastest way to learn
what an earlier session already found out.

`GUIDE.md` and `WORKING.md` are a different thing: files the agent keeps for itself, read and
written whole, that a fresh session starts from. Write a finding that the operator and MAST's own
agents should know as a note; keep your plan and your working understanding in the two files.

## Asking the operator

A question or request to the operator is answered **asynchronously**. Do not wait on it: keep
working on whatever does not depend on the answer, and check back later (or read the
`operator_requests` briefing section) rather than blocking your whole session on a reply that may
take a while to arrive. A `mast_wait` that includes `{"type": "operator_reply"}` among its
conditions wakes you when the answer comes, along with whatever else you are waiting for.

## Handover

A handover report is written into MAST's document library, where the operator can find it on the
reports page — it is not a private log only you can see. Write one whenever you finish a session,
even a short one: the jobs you ran, the actions recorded under your name, and your own notes are
gathered into it automatically, alongside the summary and next steps you provide. Save `WORKING.md`
before it, so that the next session starts where you stopped.

## The runner's records (new in 7.0)

Each unattended run keeps its own folder, `experiments\vista\runs\<run id>\` under MAST's project
folder: `status.json` (the run, every session it started, usage totals, and any tool call left
without a result), the system prompt, and for every session its command, settings, input and output
streams (image data replaced by a hash and a size), error output and hook log. `GET /runner` lists
the runs, earlier ones included; a run that was active when MAST stopped shows as `interrupted`.

## After a restart

A job that was still running when MAST's process restarted is marked `lost_on_restart` in the
record and is never replayed — nobody, including MAST itself, knows how far it actually got on the
instrument. Read the briefing again before deciding what to do next; do not resubmit blind. The
workspace comes back with its archive, notes, history and turn counter; the observation token does
not, so call `mast_observe` before the next act.
