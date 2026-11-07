# MAST plugin for Claude Code

[中文](README.zh.md)

Drive a MAST-controlled scanning tunneling microscope (Nanonis) from Claude Code, through MAST's
external agent API (`/api/ext/v1`). Install this plugin from a local checkout of this public snapshot.

> **Status:** the twenty tools of 6.5.0 were tested in unit tests and end to end on one machine with
> a mock instrument; the visual-harness tools of 7.0 were tested end to end against the STM-Bench
> simulator, and the changes of 7.0.1 against the Nanonis simulator. None of it has run on a real
> instrument yet: treat the first sessions on real hardware as supervised.

## What it adds

- **The `mast` MCP server**: thirty-nine `mast_*` tools. The twenty from 6.5.0 cover the session
  briefing, skill search and skill cards, jobs with polling and cancellation, emergency stop,
  experiment and sample scope, raw data download, notes, operator requests, composite skills and
  the handover report. The eighteen added in 7.0 are the visual harness (VISTA): looking at the
  instrument and acting with the observation token, re-rendering and magnifying archived frames,
  exact readings and measurements, the agent's `GUIDE.md` and `WORKING.md`, the experiment's
  history, server-side waits, detectors and literature search. 7.0.1 adds `mast_search_data`
  (finding data files by metadata) and opens the tools of MAST's own instrument agent to
  `mast_run`: experiment records, map analysis, documents, environment and the rest, for every
  external agent alike. Handing work to MAST's internal agents is not part of the external API,
  and recording what happened at the instrument (a tip change, a coarse move made by hand) stays
  with the operator.
- **Two skills**: [mast-operator](skills/mast-operator/SKILL.md) (working procedure and rules)
  and [mast-skill-author](skills/mast-skill-author/SKILL.md) (adding a capability the right way).
- **Guide resources**: the bilingual operator guide as `mast://guide/{en|zh}/{file}`.

The server uses the Python standard library only: there is nothing to install, and MAST's
bundled Python runs it. Codex uses the same server (see [Using it from Codex](#using-it-from-codex)).

## Requirements

- MAST 7.0, running on this machine, or on the instrument PC and reachable over a VPN. A MAST
  6.5.0 serves the twenty older tools (`mast_run` then falls back to its job API), but none of the
  visual-harness tools. `mast_search_data` and the instrument agent's tools need MAST 7.0.1.
- Python 3.10 or newer for the server. MAST's bundled runtime is enough:
  `MASTv2/pyruntime/python.exe` inside the MAST installation folder.

## Install

The MAST installation folder is a plugin marketplace. In Claude Code:

```text
/plugin marketplace add <install dir>
/plugin install mast@mast
```

A checkout of the MAST repository works the same way, and so does
`/plugin marketplace add <path-to-your-MAST-checkout>`; prefer the copy that comes with the MAST you run, so the
tools match the server. Claude Code asks for the options below when the plugin is enabled.

## From another computer: SSH over Tailscale

For Claude Code or Codex on another computer, this plugin is not needed. On the instrument PC, the
MAST launcher's "Set up AI remote access" button turns on Windows' built-in SSH and authorises a
key, reachable only over your Tailscale network (it runs the standalone `setup-ai-ssh.ps1`). The AI
on the other computer connects with plain `ssh` and works on the instrument PC — nothing to install
there, no extra login. The private key is saved to `%USERPROFILE%\.ssh\ai-remote`; revoke access by
deleting that key's line from the `authorized_keys` file, or by stopping the SSH service. Step by
step: [Remote access](skills/mast-operator/references/en/01-quickstart.md#remote-access).

To use MAST's `mast_*` tools inside an MCP client on the other computer instead, run this plugin
there against MAST's API over a VPN in LAN mode (see Configuration below).

## Configuration

| Option | Default | Meaning |
|---|---|---|
| `python` | `python`; the MAST installer sets the bundled runtime | Interpreter for the server (3.10+): MAST's `MASTv2/pyruntime/python.exe` or an installed Python |
| `mast_url` | `http://127.0.0.1:7862` | MAST root address; LAN mode uses `https://` |
| `mast_user` | empty | HTTP Basic user, LAN mode only |
| `mast_password` | empty | HTTP Basic password, LAN mode only; kept in secure storage |
| `verify_tls` | `true` | `false` accepts the instrument PC's self-signed certificate |
| `allow_remote` | `false` | Allow a MAST address that is not this machine (VPN only) |
| `actor` | `claude-code` | Name your actions are attributed to in MAST's records |

### Environment variables

Run by hand, the server reads the same settings from `MAST_URL`, `MAST_USER`, `MAST_PASSWORD`,
`MAST_VERIFY_TLS`, `MAST_ALLOW_REMOTE` and `MAST_ACTOR`. `MAST_SESSION` names the session (one per
connection; random when unset). `MAST_FETCH_DIR` sets where `mast_fetch` saves files; the default
is `.mast-fetch/` in the project, which gets its own `.gitignore` so fetched data stays out of git.
`MAST_MCP_LOG` sets the log level (logs go to stderr).

### Checking the connection

Run `/mcp` in Claude Code: `mast` should be listed as connected. If it shows "Failed to connect"
with "Connection closed", the `python` option does not point at a working interpreter (see the
Windows notes). Then ask Claude to call `mast_status`. If the answer is an error, it says what to
check: whether MAST is running, http or https, the login, or an address that is not this machine.
`mast_observe` should answer with an observation and, once the experiment has a frame, its image.

Two situations come first, and the error names both. An open MAST launcher window whose main
service was never started: click 「启动主服务」 (Start main service) in the launcher, then call the
tool again, with no reconnect. LAN access ticked in the launcher: MAST then answers `https://` with a
self-signed certificate and a login, on `127.0.0.1` too; either untick it and restart the main
service, or set `mast_url`, `mast_user`, `mast_password` and `verify_tls` together and reconnect
(`/mcp`). The error lists every setting still missing, so that one reconnect is enough.

## Windows notes

- **Python path.** The bare `python` on Windows is often the Microsoft Store placeholder, which
  cannot run the server. The MAST installer points the `python` option at its bundled runtime; if
  you installed the plugin from somewhere else, point it at a real interpreter, for example MAST's
  bundled one in the default installation folder:

```text
C:\MAST\MASTv2\pyruntime\python.exe
```

- **https and self-signed certificates.** In LAN mode MAST serves `https://` with the instrument
  PC's own certificate. Use `https://` in `mast_url` and set `verify_tls` to `false` unless that
  certificate is installed on your machine. Set `mast_user` and `mast_password` at the same time:
  LAN mode always asks for the login.
- **This machine only, by default.** Addresses other than `127.0.0.1`, `localhost` and `::1` are
  refused until `allow_remote` is on, because instrument control belongs on this machine or
  inside a VPN (for example `https://192.0.2.10:7862` over a VPN). Never expose MAST to the open
  internet.
- **No proxies.** The server never sends MAST traffic through a system proxy.

## Tools

All endpoints are under `/api/ext/v1`.

| Tool | Endpoint | Purpose |
|---|---|---|
| `mast_briefing` | `GET /briefing` | Whole state in one call; read it first |
| `mast_status` | `GET /status` | Quick state check |
| `mast_find_skills` | `GET /skills/search` | Find skills by action |
| `mast_skill_card` | `GET /skills/{name}` | Parameters, units, safety, duration |
| `mast_run` | `POST /vista/act`, `GET /jobs/{id}` | Run a skill, or one of the instrument agent's tools (7.0.1), as a job and wait for it; a skill that drives the instrument needs `after` (a MAST older than 7.0: `POST /jobs`) |
| `mast_job` | `GET /jobs/{id}` | Poll one job |
| `mast_jobs` | `GET /jobs` | List jobs |
| `mast_cancel` | `POST /jobs/{id}/cancel` | Cooperative cancel |
| `mast_emergency_stop` | `POST /estop` | E-STOP and cancel every external job |
| `mast_scope` | `GET /scope`, `POST /scope` | Experiment and sample |
| `mast_list_data` | `GET /data/files` | Recent data files |
| `mast_search_data` | `GET /data/search` | Find data files by metadata (gallery index + newest files; 7.0.1) |
| `mast_fetch` | `GET /data/file`, `GET /data/frame` | Save a raw file or a frame locally |
| `mast_note_write` | `POST /notes` | Write a note into MAST's memory |
| `mast_note_search` | `GET /notes` | Search notes |
| `mast_ask_operator` | `POST /requests` | Ask the operator |
| `mast_operator_reply` | `GET /requests`, `GET /requests/{id}` | Read the answers |
| `mast_composite_draft` | `POST /composites/draft` | Check a composite spec |
| `mast_composite_save` | `POST /composites` | Save and register a composite |
| `mast_propose_skill` | `POST /skills/proposals` | Propose Python code for review |
| `mast_handover` | `POST /handover` | Handover report |

The visual harness, new in 7.0:

| Tool | Endpoint | Purpose |
|---|---|---|
| `mast_observe` | `POST /vista/observe` | The newest frame as an image, the instrument state and a fresh observation token |
| `mast_act` | `POST /vista/act` | One instrument action with `after` = the newest token; answers with its observation, or `running` |
| `mast_inspect` | `POST /vista/inspect` | Re-render and magnify archived frames |
| `mast_read_values` | `POST /vista/read_values` | Exact archived values |
| `mast_line_profile` | `POST /vista/measure/line_profile` | Values along a line, distances, step heights |
| `mast_fft_peaks` | `POST /vista/measure/fft_peaks` | Periods and directions from the 2-D Fourier spectrum |
| `mast_detect_blobs` | `POST /vista/measure/detect_blobs` | Count and locate compact features |
| `mast_diff` | `POST /vista/measure/diff` | Register two frames and map what changed |
| `mast_stats` | `POST /vista/measure/stats` | Statistics, roughness, line noise |
| `mast_read_guide` | `GET /vista/notes/guide` | Read `GUIDE.md`, shared by every experiment |
| `mast_write_guide` | `PUT /vista/notes/guide` | Rewrite `GUIDE.md` |
| `mast_read_working` | `GET /vista/notes/working` | Read `WORKING.md` of the active experiment |
| `mast_write_working` | `PUT /vista/notes/working` | Rewrite `WORKING.md` |
| `mast_history` | `GET /vista/history` | The experiment's acts, outside frames, detector events and checkpoints |
| `mast_wait` | `POST /wait` | Wait on the server for a job, a frame, an alert, an operator reply, a detector hit or a timer |
| `mast_detector_create` | `POST /detectors` | Create an analysis detector |
| `mast_detectors` | `GET /detectors`, `POST /detectors/{id}/test` and others | List, inspect, enable, disable, delete and test detectors |
| `mast_literature_search` | `POST /literature/search` | Search the operator's literature library |

Every tool call returns within 55 seconds, except `mast_wait`, which waits on the server for up to
840 s, and `mast_detectors` with `op` `test`, which lasts as long as the detector may run. If your
Claude Code applies a shorter MCP tool timeout, start it with `MCP_TOOL_TIMEOUT` set to at least
`900000` (milliseconds). Longer work keeps running on MAST as a job: wait for it with `mast_wait`,
or poll it with `mast_job`. Interrupting a tool call does not cancel the job; `mast_cancel` does.

## Using it from Codex

Codex expands neither `${CLAUDE_PLUGIN_ROOT}` nor `${user_config.*}`, so it cannot use this
plugin's `.mcp.json`. The MAST installation sets Codex up with the same server instead:

```text
<install dir>\MASTv2\pyruntime\python.exe <install dir>\integrations\setup_clients.py codex
```

It adds `[mcp_servers.mast]` to Codex's `config.toml` (with `tool_timeout_sec = 900`) and creates
the workspace `Documents\MAST-agent` in your user folder, with the operating rules in its
`AGENTS.md`. Then check `codex mcp list`, sign Codex in with ChatGPT rather than an API key, and
open the workspace in Codex. The [quickstart](skills/mast-operator/references/en/01-quickstart.md)
has every step.

## Guide

The full operator guide is in the [mast-operator references](skills/mast-operator/references/en/README.md),
also served as MCP resources `mast://guide/en/...`.

## Development

From the repository root:

```text
python -m pytest tests/v2/unit/integrations tests/v2/unit/docs -q
python scripts/sync_external_docs.py --check
```

The guide files under `skills/mast-operator/references/` are copies of `docs/external/`; edit
the originals and run `scripts/sync_external_docs.py`.

## License

MIT, like MAST.
