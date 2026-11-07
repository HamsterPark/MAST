# Troubleshooting

Symptom, cause, fix — read top to bottom for whatever you are seeing. For the mechanisms these
refer to, see [Concepts](02-concepts.md) and [Operating rules](03-operating-rules.md); for the
exact shape of an error body, see [the API reference](07-api-reference.md).

## Connecting

| Symptom | Cause | Fix |
|---|---|---|
| `Cannot connect to MAST … (connection refused)`, although a MAST window is open | That window is the MAST launcher, and its main service — the API, on port 7862 — has not been started. When the MCP server finds the launcher's own port answering, its message says so | Click 「启动主服务」 (Start main service) in the launcher; once the service is up, call the tool again — the MCP server needs no restart |
| A client on the same machine cannot connect, and LAN access is ticked in the launcher | With LAN access on, MAST serves `https://` with a self-signed certificate and an HTTP Basic login — on `127.0.0.1` too | Either untick LAN access in the launcher and restart the main service (the default `http://127.0.0.1:7862` then works without reconnecting), or set all four together — `mast_url` `https://127.0.0.1:7862`, `mast_user`, `mast_password`, `verify_tls` `false` (as environment variables `MAST_URL`, `MAST_USER`, `MAST_PASSWORD`, `MAST_VERIFY_TLS`) — and restart the MCP server (Claude Code: `/mcp`, reconnect `mast`). The MCP server's message names every setting still missing |
| `curl` reports `HTTP=000`, or a connection error, against a MAST you know is running in LAN mode | LAN mode serves TLS at the transport layer | Use `https://`, not `http://` |
| `401 Unauthorized` | LAN mode uses HTTP Basic, and either no credentials were sent or they are wrong | Set the login (`mast_user`/`mast_password` in the plugin, `MAST_USER`/`MAST_PASSWORD` as environment variables) |
| `403`, `cross_origin_refused` | The request looked like it came from a browser (it carried an `Origin` or a `Sec-Fetch-*` header) and either crossed origins or reached the service through a hostname instead of a loopback name or a literal IP address | Call the API directly, not from a web page; use `127.0.0.1` or the numeric address instead of a hostname — programmatic clients such as curl and the MCP server are unaffected, since they send neither header |
| `415`, `unsupported_media_type` | A write request — including `POST /estop` and `POST /jobs/{job_id}/cancel` — was sent without `Content-Type: application/json` and a JSON body | Send `Content-Type: application/json` with a JSON body; send `{}` when there is nothing else to say |
| An HTML page instead of JSON | The wrong path prefix, or a MAST build without the external-agent API | Check the base path is `/api/ext/v1` — the external surface answers JSON for every request, including a 404 |
| A TLS certificate error | The instrument PC's certificate is self-signed | Set `verify_tls` to false, or install that certificate locally |
| A remote address is refused | An address other than this machine is refused unless remote access is explicitly allowed | Turn it on only over a VPN — never expose MAST to the open internet |
| The MCP server does not start, or shows "Failed to connect" | The configured Python interpreter is a placeholder (on Windows, often the Microsoft Store's) rather than a real one | Point the interpreter option at a real Python 3.10+ |
| Non-ASCII text prints as garbled characters | The terminal or client is not reading the response as UTF-8 | Set your client or terminal to UTF-8; the API always answers UTF-8-encoded JSON |

## Running skills

| Symptom | Cause | Fix |
|---|---|---|
| Every job fails at once, with an error saying `aborted by operator — refusing to start ...` | The emergency latch is set — by E-STOP, `POST /estop`, an environment alarm, or a hardware safety signal; `GET /status` shows it as `abort.emergency: true` with `abort.why` | Do not retry: every job is refused before it starts while the latch is set, stop skills included, because the E-STOP that set it has already stopped motion and withdrawn the tip; tell the operator, who releases it from MAST's own interface |
| `503`, a `degraded` list, or a `missing` list | A subsystem is not wired, often because MAST is still starting — this is not the same thing as an empty answer | Retry the health or status check after a moment; tell the operator if it persists |
| `refused_busy` | Another driver already holds the instrument | Wait, or ask the operator; retrying at once will not make it finish sooner |
| `failed`, `refused_by: sample_gate` | The skill produces data and needs an active sample | Set one with `POST /scope` first |
| `failed`, `refused_by: si_parse` | A dimensioned parameter's value could not be parsed — usually a string that dropped a required SI prefix | Check `si_params` on the skill card; write a value such as `"5n"`, or a plain number in SI base units — the prefix rule applies only to strings |
| A running job with `waiting_operator` | The job reached a step only a person may decide (or ran `AskOperator`); MAST asked the operator, prompt id in `waiting_operator.prompt_id` | Nothing is wrong: wait with `mast_wait` or keep working on something else; only the operator can answer. Cancel the job to withdraw the prompt |
| `failed`, error `HumanNodeFailed` | The operator did not decide: no answer in time (30 minutes by default), the prompt was dismissed or cancelled, or a confirmation was answered no | Read the message (it names the prompt); ask the operator with `mast_ask_operator` if the step is still needed, then submit again |
| `failed`, `refused_by: needs_human_node` | A pause MAST could not turn into a question for the operator (a `human` node inside a thread the skill started itself, or another kind of pause) | Run it from MAST's own interface, or ask the operator instead |
| `422`, `skill_disabled` | The skill, or a sub-step it uses, needs hardware or a capability switched off on this installation | Pick a different skill, or ask the operator to enable it |
| `429` | Too many jobs are running at once | Wait for one to finish, or cancel one you no longer need — a stop or retract skill (such as `StopScan`) is exempt from this limit and can always be submitted |
| `503`, `shutting_down` | The service is shutting down and refuses new jobs | Wait for it to come back, then reconnect |
| `lost_on_restart` | The job was still running when MAST's process restarted; it is never replayed | Read the briefing before deciding whether to submit it again |
| A refusal naming `safe_mode_tip_processing_blocked` | The operator has set SAFE, which refuses tip processing everywhere, including inside a composite | Do not retry; ask the operator if the task genuinely needs it |
| A refusal describing one of the five hard gates, saying no approval will come | You called, or a composite step called, one of the physically dangerous actions refused unconditionally on this path | Stop retrying — nothing changes this outcome; if it is genuinely needed, a person has to do it from MAST's own interface |

## Connecting a client (new in 7.0)

| Symptom | Cause | Fix |
|---|---|---|
| `codex mcp list` does not list `mast` | `setup_clients.py codex` has not run for this Windows user, or Codex reads another folder (`CODEX_HOME`) | Run it again (`--codex-home` names the folder); `--dry-run` shows what it would write |
| Codex reports that the `mast` server failed to start | The interpreter or the bridge path in `[mcp_servers.mast]` no longer exists, typically after MAST moved or was reinstalled elsewhere | Run `setup_clients.py codex` again from the current installation |
| Codex bills an API account, or asks for an API key | `OPENAI_API_KEY`, `CODEX_API_KEY` or `CODEX_ACCESS_TOKEN` is set, so Codex signs in with that key | Remove them from your environment and sign in with ChatGPT (`codex login`); `forced_login_method = "chatgpt"` in `config.toml` refuses any other sign-in |
| A `mast_wait` call fails after a few minutes with a client-side timeout | The client's MCP tool timeout is shorter than the wait (up to 840 s) | Codex: `tool_timeout_sec = 900` in `[mcp_servers.mast]` (the setup tool writes it); Claude Code: `MCP_TOOL_TIMEOUT` of at least `900000`; other clients: their tool timeout, at least 900 s |
| The plugin asks for `python`, or its server shows "Failed to connect" | The plugin was installed from somewhere other than the installation folder, or before the installer set its `python` | Point the `python` option at `<install dir>\MASTv2\pyruntime\python.exe`, or run `setup_clients.py patch-plugin` |
| `mast_act` answers "This MAST has no /vista/act" | The server is older than 7.0 | Install MAST 7.0; until then, run skills with `mast_run`, which falls back to the 6.5.0 job API |

## AI remote access (SSH over Tailscale)

| Symptom | Cause | Fix |
|---|---|---|
| The other computer cannot reach the instrument PC over SSH | The two are not in the same tailnet, Tailscale is off on one of them, a proxy (Clash and the like) catches `100.x` addresses, the SSH service is not running, or the firewall does not allow the tailnet | Sign both into the same Tailscale account and let the proxy bypass `100.64.0.0/10`; on the instrument PC check that `sshd` is running (`Get-Service sshd`). The setup script's firewall rule already allows SSH from `100.64.0.0/10` |
| SSH refuses the key for an administrator account | An admin's key must sit in `%ProgramData%\ssh\administrators_authorized_keys` with a strict ACL, not the per-user file | Re-run "Set up AI remote access" / `setup-ai-ssh.ps1`; it places the key correctly |
| The setup window closes before you can read the `ssh` line | An older build did not pause at the end | Re-run it from a current build; it now waits for Enter before closing |

## Images (new in 7.0)

| Symptom | Cause | Fix |
|---|---|---|
| Codex Code Mode: the model works on results without having seen their images | In Code Mode an image reaches the model only when the code calls `image()` on it | Call `image()` on every observation, one act per code cell |
| Codex: no images at all, in any mode | A result that carries `structuredContent` loses its images in Codex. MAST's server never sends one; a proxy or wrapper between Codex and the server may add it | Connect Codex to MAST's server directly, as `setup_clients.py codex` does |
| Numbers read off an image disagree with the tools | Models misjudge positions and sizes in pictures, and misread more often when several images arrive at once | Take numbers from `mast_read_values` and the measure tools; an observation carries one image on purpose |
| Claude Code: an act came back `FAILED`, and its observation shows no image | Claude Code does not show the images of an error result | Call `mast_observe` for the current observation. A refusal that brings the newest observation (`observe_first`) is answered as a normal result, so its image is shown |
| Claude Code: a reply is cut short, or saved to a file instead of shown | The reply is larger than Claude Code's limit for an MCP result. The plugin raises the limit for its image tools, but very large replies still exceed it | Ask for fewer views, a smaller region or fewer samples; or start Claude Code with a larger `MAX_MCP_OUTPUT_TOKENS` |
| An observation says "No frame image yet" | Nothing has been archived in this workspace yet (a new experiment, for example) | Act — a scan gives the first frame |
| `next_token` is `null` and `token_on_image` is `true` | Strict mode is on | Read the token from the band under the image and pass it as `after` |

## The observation gate (new in 7.0)

| Symptom | Cause | Fix |
|---|---|---|
| `observe_first`, again and again | You act with an older token. A newer observation replaced yours: your own `mast_observe` or `mast_wait`, a call made in parallel, an observation handed to another connected client, or a frame from outside your acts (the user interface, the operator, MAST's built-in agents, another client). A restart of your MCP server or of MAST also ends the old token | Use the token of the observation the refusal brought, and call nothing else that observes between reading it and acting. `mast_history` lists outside frames; if someone else keeps scanning or observing, ask the operator |
| `same_code_cell`, again and again | A Codex code cell sends more than one act | One act per cell: `image()` on its result, end the cell, act from the next one |
| `act_in_flight` | An earlier act, yours or another session's, is still running | `mast_wait` with `{"type": "job", "job_id": ...}` from the refusal, look at its observation, then act |
| `checkpoint_requested`, and every tool except the notes tools refuses | The runner asked for a checkpoint | Write `WORKING.md` with `mast_write_working`; the session then continues, or a fresh one takes over |
| `session_closed` | The operator stopped this session | Do not act again; finish with a short summary |
| `409`, `use_act`, from `POST /jobs` | This session has used the visual harness, so its instrument-driving skills must carry the token | Use `mast_run` with `after`, or `mast_act`; reads, analyses and stop skills still go through `POST /jobs` |
| `409`, `request_id_conflict` | That `request_id` was already used for a different action or different parameters | Reuse a `request_id` only to resend the same action after a timeout; a new action needs a new one |
| `next_token` is `null`, with a `token_note` | That observation was superseded by a newer one, or the session is closed | Call `mast_observe` for the newest observation |

## Frames, waits and detectors (new in 7.0)

| Symptom | Cause | Fix |
|---|---|---|
| An act's text says `frames_unavailable`: "the controller did not report its save directory" | MAST could not find the folder the controller saves new files to, so new scans and spectra were not archived | Tell the operator: the controller's save directory has to be known to MAST. On a simulator or a test setup, start MAST with `MAST_VISTA_SESSION_DIR` set to the folder the files appear in |
| Frames you did not take appear, with `origin: external` | Someone else saved data: the user interface, the operator, MAST's built-in agents or another client. Each such frame also ends the current token | Expected; look at them (`mast_inspect`) and act with the token of the newest observation |
| `mast_wait` times out with nothing fired | Nothing happened within `timeout_s` | Call it again: it continues from the last cursor, so nothing is missed |
| `mast_wait` returns at once, every time | A condition is already true: the job has ended, a timer is short, or events have piled up since the cursor | Read what fired and handle it before waiting again |
| A detector shows as disabled, with a reason | It failed 5 times in a row, or hit more than 30 times within an hour | `mast_detectors` with `op` `get` shows its code and the reason; fix it, test it on an archived frame, and enable it again |
| A detector's hits are marked `suppressed` | Detectors woke agents more than 60 times within an hour | The hits are recorded but wake no one; make the detector more selective |
| Creating or enabling a detector is refused with `too_many_enabled` | 8 detectors are already enabled in this experiment | Disable or delete one you no longer need |
| Creating or test-running a detector is refused with `loopback_only` | Detector code runs as the MAST user on the instrument PC, so by default only requests from that machine are accepted, and you reached MAST over the LAN | Create it on the instrument PC itself; if it really must come over the LAN, the operator sets the environment variable `MAST_DETECTORS_ALLOW_REMOTE=1` and restarts MAST (this makes the LAN password a permission to run code on that PC) |
| On the STM-Bench simulator, acts fail with controller timeouts | The simulator renders a frame more slowly than the controller timeout of 5 s allows | Start MAST with `MAST_NANONIS_TIMEOUT_S` set higher (0.5 to 600 s; an out-of-range value is ignored); keep the default on a real instrument |

## The runner (new in 7.0)

| Symptom | Cause | Fix |
|---|---|---|
| `POST /runner/start` answers `409`, `run_active` | One run at a time, and another run is active | Wait for it to end, or stop it (`POST /runner/{run_id}/stop`) |
| `POST /runner/start` answers `503`, `runner_unavailable` | Claude Code, the MCP bridge, its Python or the visual harness is missing on this PC | Install Claude Code for the account MAST runs under, and check the installation folder |
| A run ends right after it starts, with `init_invalid` in its records | The session reported an API-key source (the runner runs on a Claude subscription only), or its tools or MCP server were not the expected ones | Sign Claude Code in with the subscription on this PC, and remove any API key from its configuration |
| A run shows as `interrupted` | MAST stopped while the run was active | Read the briefing and the run's records before starting a new run |
