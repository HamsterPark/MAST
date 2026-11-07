# MAST external-agent guide

MAST drives a real scanning tunnelling microscope (STM) through a Nanonis controller, using a
catalog of registered instrument and analysis actions ("skills") behind a shared safety layer.
This guide is for an outside AI agent (Codex, Claude Code or another coding agent) or a human
operator who drives MAST through its **external-agent API**, `/api/ext/v1`, rather than through
MAST's own built-in agents. It assumes no prior knowledge of MAST's internals: everything you need
to act safely is either in this guide or returned by the API itself.

> **Status:** the external-agent API and the Claude Code plugin arrived in 6.5.0. They have been
> verified in unit tests and in end-to-end tests on one machine (real HTTP, a real MCP server
> process, a mock instrument). 7.0 adds the visual harness described below and connects Codex;
> those parts have been tested end to end against the STM-Bench simulator. **None of it has run on
> a real instrument yet.** Every skill they run goes through the same execution layer that MAST's
> own agents use on the instrument, but treat the first sessions on real hardware as supervised.

We recommend driving MAST from Claude Code with Claude Opus 5.5.

## New in 7.0

7.0 is an external agent plus VISTA, a visual harness for multimodal models. The agent looks at the
instrument's own images instead of a text summary of them, can look again at any earlier frame and
zoom in, takes exact numbers from measurement tools, and keeps notes of its own. The harness runs
inside the MAST server, so every client gets the same archive, the same tools and the same rules:

| What | Tools | Read |
|---|---|---|
| Claude Code (recommended) or Codex as the client | `setup_clients.py codex`, the `mast` plugin | [Quickstart](01-quickstart.md) |
| A lossless archive of every frame, per experiment, re-rendered and magnified on demand | `mast_observe`, `mast_inspect` | [Concepts](02-concepts.md) |
| Exact values and measurements from the archived data | `mast_read_values`, `mast_line_profile`, `mast_fft_peaks`, `mast_detect_blobs`, `mast_diff`, `mast_stats` | [Concepts](02-concepts.md) |
| One act, one observation, enforced by the server | `mast_act`, and `mast_run` with `after` | [Operating rules](03-operating-rules.md) |
| Acts that run as jobs, and waits that happen on the server | `mast_wait` | [Concepts](02-concepts.md) |
| The agent's own notes, and the experiment's history | `mast_read_guide`, `mast_write_guide`, `mast_read_working`, `mast_write_working`, `mast_history` | [Records and context](04-records-and-context.md) |
| Detectors that look at every new frame | `mast_detector_create`, `mast_detectors` | [Concepts](02-concepts.md) |
| Unattended Claude Code runs, started by the operator | `POST /runner/start` | [Concepts](02-concepts.md) |
| The operator's literature library | `mast_literature_search` | [Concepts](02-concepts.md) |

Everything 6.5.0 offered is still there and still works the same way.

## Who this is for

Anyone driving MAST from outside the application: an AI coding agent connected over MCP (Codex,
the Claude Code plugin, or another MCP client), a script talking plain HTTP, or a person typing
`curl` commands by hand. The rules in this guide bind all of them equally — the API does not know
or care what is on the other end of the connection.

## How it works

Every capability MAST exposes — reading a value, running a scan, shaping a tip, saving data — is a
named **skill** with declared parameters, units and a safety level. Every skill call, no matter
which door it came through, passes through the same execution choke point: an abort check, the
sample gate, the operating-mode gate, five hard-coded physical safety gates, the global parameter
envelope, precondition checks and instrument arbitration, in that order. The external-agent API
lets you search that catalog, read a skill's full card before running it, submit a run as an
asynchronous **job** you poll to completion, pull raw data files, leave notes and questions for the
operator, and write a handover report when you are done. A **briefing** endpoint gives you, in one
call, the same context MAST's own agents see every turn: the tip, live readings, the operator's
preferences, recent actions and your own open requests.

From 7.0 on, an agent's instrument actions also pass through the visual harness. Every observation
— the result of an act, or a plain look with `mast_observe` — carries the newest image and a
one-time **observation token**, and the next action must quote that token: no action runs before
the agent has received the result of the previous one. Every frame is archived losslessly for the
experiment, so the agent can bring back any earlier frame, magnify part of it, read exact values,
and measure distances, periods, step heights and changes. An action that takes a while runs as a
job, and `mast_wait` waits on the server for the job, a new frame, an alert, an operator reply, a
detector hit or a timer.

## Reading order

| File | Read this for |
|---|---|
| [01-quickstart.md](01-quickstart.md) | Installing 7.0, connecting Codex or Claude Code (or another MCP client, or plain HTTP), remote access over Tailscale, and a first session from briefing to handover |
| [02-concepts.md](02-concepts.md) | Skills, the execution choke point, safety levels, operating modes, jobs; and the visual harness: workspaces, frames, the observation token, waits, detectors, the runner |
| [03-operating-rules.md](03-operating-rules.md) | The rules that keep a session safe, with the reasons behind each one |
| [04-records-and-context.md](04-records-and-context.md) | What MAST records about your actions, the workspace records, and where all of it goes |
| [05-authoring-skills.md](05-authoring-skills.md) | Building a composite skill, or proposing a new one |
| [06-contributing-skills.md](06-contributing-skills.md) | Submitting a skill to the community contribution tree |
| [07-api-reference.md](07-api-reference.md) | The generated endpoint-by-endpoint reference |
| [08-troubleshooting.md](08-troubleshooting.md) | Symptom, cause and fix, for when something goes wrong |
| [09-optical-station.md](09-optical-station.md) | Running MAST on an optical (Raman / SHG) station: what differs from the STM, what needs the operator, what `mast_observe` returns |
| [10-optical-layout.md](10-optical-layout.md) | The operator's optical layout: reading it (`mast_optical_layout`), what its trace and description say, commenting on it, and why you cannot change it |
