# Quickstart

This file gets you from nothing to a finished first session: install MAST, connect a client, read
the briefing, pick a sample, look at the instrument, take one action, measure what it produced, and
hand over. Read [Concepts](02-concepts.md) next for the ideas behind what you just did, and
[Operating rules](03-operating-rules.md) for the rules with reasons.

## Install MAST 7.0

MAST 7.0 ships as one full Windows installer; it does not arrive as an over-the-air update.
Install it by hand on the PC that drives the instrument (or the simulator), and keep the previous
installer at hand in case you need to go back. Besides MAST itself, the installation folder holds
everything a client needs:

| In `<install dir>` | What it is |
|---|---|
| `MASTv2\pyruntime\python.exe` | MAST's bundled Python; it runs the MCP server, with nothing else to install |
| `integrations\claude-code\` | The Claude Code plugin: the `mast` MCP server, the `mast-operator` and `mast-skill-author` skills, and this guide |
| `.claude-plugin\marketplace.json` | Makes the installation folder a Claude Code plugin marketplace |
| `integrations\codex\AGENTS.md` | The operating rules that go into a Codex workspace |
| `integrations\setup_clients.py` | Connects Codex, and points the Claude Code plugin at the bundled Python |
| `docs\external\` | This guide |

The installer already points the plugin's `python` option at the bundled Python. Start MAST before
you connect a client, and check that its main service runs: an open MAST launcher window is not
enough — click 「启动主服务」 (Start main service) in it. By default the service then answers at
`http://127.0.0.1:7862`, on the same machine; with LAN access ticked in the launcher it answers
`https://` with a login instead, on this machine too (see [Remote access](#remote-access)).

## Ways to connect

Codex and Claude Code start the same MCP server, which talks to the same API, `/api/ext/v1`; plain
HTTP reaches that API directly. Every route is bound by the same rules. The recommended client is
Claude Code with Claude Opus 5.5; Codex is also supported.

### A. Claude Code

The plugin gives Claude Code the same `mast_*` tools, plus two skills: `mast-operator` (the working
procedure for driving MAST) and `mast-skill-author` (writing composite skills and Python proposals,
and contributing them upstream). The installation folder is a plugin marketplace; install from it:

```text
/plugin marketplace add <install dir>
/plugin install mast@mast
```

A checkout of the MAST repository works the same way (`/plugin marketplace add
<path-to-your-MAST-checkout>`), and so does the public repository (`/plugin marketplace add
HamsterPark/MAST`). Prefer the copy that comes with the MAST you run, so the tools match the
server.

Claude Code then asks for these options:

| Option | Default | Meaning |
|---|---|---|
| `python` | `python`; the installer replaces it with `<install dir>\MASTv2\pyruntime\python.exe` | Interpreter that runs the server (3.10+); avoid a bare `python` on Windows if it opens the Microsoft Store — point it at a real interpreter |
| `mast_url` | `http://127.0.0.1:7862` | MAST's address; LAN mode uses `https://` |
| `mast_user` | empty | HTTP Basic user, LAN mode only |
| `mast_password` | empty | HTTP Basic password, LAN mode only |
| `verify_tls` | `true` | `false` accepts a self-signed certificate |
| `allow_remote` | `false` | Allow an address that is not this machine |
| `actor` | `claude-code` | Name your actions are attributed to in MAST's records |

The full option list and its exact defaults live in the plugin's own package (its `README.md`,
right next to the plugin source); this table is a summary. `mast_wait` can block for up to 840 s:
if your Claude Code applies a shorter MCP tool timeout, start it with `MCP_TOOL_TIMEOUT` set to at
least `900000` (milliseconds). Once connected, ask the agent to read the briefing — that is the
first rule in [Operating rules](03-operating-rules.md).

### B. Codex

Run this once per Windows user (quote the two paths if the folder name contains spaces):

```text
<install dir>\MASTv2\pyruntime\python.exe <install dir>\integrations\setup_clients.py codex
codex mcp list
```

`setup_clients.py codex` does two things and changes nothing else:

1. It adds a `[mcp_servers.mast]` entry to Codex's `config.toml` (in `.codex` in your user folder,
   or in the folder `CODEX_HOME` names). Running it again replaces only that entry, and the file is
   backed up next to itself before every change.
2. It creates an agent workspace, the folder `Documents\MAST-agent` in your user folder, and puts
   the operating rules into its `AGENTS.md`.

The entry it writes looks like this (its comment lines left out):

```toml
[mcp_servers.mast]
command = '<install dir>\MASTv2\pyruntime\python.exe'
args = ["-I", "-X", "utf8", '<install dir>\integrations\claude-code\server\run_server.py']
cwd = '<workspace>'
startup_timeout_sec = 30
tool_timeout_sec = 900
default_tools_approval_mode = "approve"

[mcp_servers.mast.env]
MAST_URL = "http://127.0.0.1:7862"
MAST_ACTOR = "codex"
PYTHONUTF8 = "1"
PYTHONIOENCODING = "utf-8"
```

| Setting | Why |
|---|---|
| `tool_timeout_sec = 900` | `mast_wait` waits on the server for up to 840 s; Codex's default tool timeout is shorter and would cut it off |
| `supports_parallel_tool_calls`, left unset | Codex then runs MAST's write tools one at a time; read-only tools may still run in parallel |
| `default_tools_approval_mode = "approve"` | Every MAST tool runs without a Codex prompt. The protection is on MAST's side: its safety gates check every action, and an act is refused without the token of the latest observation. With `--approve read_only`, only the read-only tools are pre-approved and Codex asks before any other |
| `MAST_ACTOR = "codex"` | The name your actions are recorded under |

| Option of `setup_clients.py codex` | Meaning |
|---|---|
| `--workspace DIR` | Another workspace folder |
| `--mast-url URL` | MAST's address (default `http://127.0.0.1:7862`) |
| `--actor NAME` | The name in MAST's records (default `codex`; lower-case letters, digits, `.`, `-` and `_`) |
| `--approve read_only` | Pre-approve only the read-only MAST tools |
| `--codex-home DIR` | Codex's folder, when it is neither `CODEX_HOME` nor `.codex` in your user folder |
| `--dry-run` | Show what would change, and write nothing |
| `--uninstall` | Remove the `[mcp_servers.mast]` entry again; the workspace stays |

Then:

1. **Check** that Codex sees the server: `codex mcp list` lists `mast`.
2. **Sign in with ChatGPT**, not with an API key: `codex login`. Make sure none of
   `OPENAI_API_KEY`, `CODEX_API_KEY` and `CODEX_ACCESS_TOKEN` is set in your environment: when one
   is, Codex signs in with that key and bills the API account instead of your ChatGPT plan.
   `forced_login_method = "chatgpt"` in `config.toml` makes Codex refuse any other sign-in.
3. **Open the workspace folder** in Codex: in the Codex app, open the folder; in a terminal,
   `codex -C "<workspace>"`. Codex reads the rules in that folder's `AGENTS.md`.

Run `setup_clients.py codex` again after every MAST upgrade: it refreshes the entry and the
workspace's `AGENTS.md` (a copy you edited is backed up first).

### C. Another MCP client

Any MCP-compatible client can run the same server directly, without a plugin marketplace: point
its MCP configuration at `server/run_server.py` inside the plugin's directory
(`<install dir>\integrations\claude-code\server\run_server.py` in an installation). Run this way,
the server takes its settings from environment variables instead of the plugin's options:

| Variable | Meaning |
|---|---|
| `MAST_URL` | MAST's address, same as `mast_url` above |
| `MAST_USER` / `MAST_PASSWORD` | HTTP Basic login, LAN mode only |
| `MAST_VERIFY_TLS` | `false` accepts a self-signed certificate |
| `MAST_ALLOW_REMOTE` | Allow an address that is not this machine |
| `MAST_ACTOR` | Name your actions are attributed to |
| `MAST_SESSION` | Session name; a random one is used when this is unset |
| `MAST_FETCH_DIR` | Where a fetched file is saved; defaults to `.mast-fetch/` in the working directory |
| `MAST_MCP_LOG` | The server's log level (the log goes to stderr) |

```json
{
  "mcpServers": {
    "mast": {
      "command": "<install dir>\\MASTv2\\pyruntime\\python.exe",
      "args": ["-I", "-X", "utf8", "<install dir>\\integrations\\claude-code\\server\\run_server.py"],
      "env": {
        "MAST_URL": "http://127.0.0.1:7862",
        "MAST_ACTOR": "my-agent"
      }
    }
  }
}
```

Give the client an MCP tool timeout of at least 900 s, for `mast_wait`. The client should show
images from tool results; a client that cannot still works, but then reads only the text half of
every observation.

### D. Plain HTTP

The API needs no client at all: any HTTP library, or `curl`, can call it directly. This is the
lowest-level way in, and useful for a first look at what the API returns; the last section of this
file uses it. The visual harness lives under `/api/ext/v1/vista/` and tells clients apart by the
`X-MAST-Session` header. Once a session has used it, that session's instrument-driving skills must
go through `/vista/act` with the token; `POST /jobs` refuses them with `use_act`. A caller that
never touches the harness works exactly as in 6.5.0.

## Remote access

By default, everything above talks to MAST on the same machine. There are two ways to work with an
instrument from another computer; both keep MAST off the open internet, over a private VPN such as
[Tailscale](https://tailscale.com/download) (free for personal use, no router settings).

- **SSH over Tailscale (simplest).** The MAST launcher's "Set up AI remote access" button turns on
  Windows' built-in SSH on the instrument PC and authorises a key, reachable only over your
  Tailscale network. Claude Code or Codex on the other computer connects with plain `ssh` and works
  on the instrument PC — nothing to install there. Use MAST on that PC as if you were sitting at it.
- **The plugin over a VPN.** The other computer runs the `mast` MCP server itself, which calls
  MAST's API across the VPN in LAN mode (https with a login). Use this when you want MAST's `mast_*`
  tools inside an MCP client on the other computer.

### SSH over Tailscale, step by step

1. **Install Tailscale on both computers** and sign in to the same Tailscale account.
2. **On the instrument PC, click "Set up AI remote access" in the MAST launcher** and follow the
   wizard (it needs administrator rights and asks once before it changes anything). It turns on the
   built-in OpenSSH server — which then starts automatically, and that is what keeps the PC
   reachable — makes a dedicated key, limits SSH in the firewall to Tailscale addresses
   (`100.64.0.0/10`), and prints an `ssh` line. The private key is saved to
   `%USERPROFILE%\.ssh\ai-remote`.
3. **Carry the private key file and the `ssh` line to the other computer** and tell Claude Code or
   Codex to connect with it, for example `ssh -i <key> -p 22 <user>@<Tailscale IP>`. It runs `ssh`
   with its own shell; nothing is installed there and there is no extra login.
4. **Work on the instrument PC over that shell.** A connected AI can do anything the login account
   can do on that computer. To drive the instrument through MAST, use MAST on that PC as you would
   locally (the `mast` command line); for the full `mast_*` MCP tools in a remote client, use the
   plugin over a VPN, below.

What else to know:

- **Revoking access:** delete that key's line from the `authorized_keys` file the script names, or
  turn SSH off entirely (`Stop-Service sshd; Set-Service sshd -StartupType Manual`).
- **Who can connect:** only devices in your tailnet that hold the key. SSH does not elevate on its
  own; there is no telemetry and nothing connects back to anyone.
- **The setup script is standalone** (`setup-ai-ssh.ps1`, shipped with MAST but independent of it);
  the launcher button only runs it for you.

### Shared instrument PC: handing over (not simultaneous)

When several people take turns on the same instrument PC, switch the Tailscale account per
operator. They do not need to be online at the same time.

1. **Previous operator finishes:** leave the instrument idle, save `WORKING.md`, then
   `mast_handover`.
2. **Sign the instrument PC into the next person's Tailscale account** (or sign the previous one
   out).
3. **Wait for the launcher to refresh:** the "Remote - Tailscale" URL and the AI remote-access
   `ssh` paste line follow the new `100.x` (about a minute; or click "Refresh connection info").
4. **If you use the LAN/MCP plugin path:** restart the main service once so the certificate SAN
   picks up the new address.
5. **The next person copies the updated `ssh` line** (and the existing private-key path) to their
   work computer.

**You do not need to revoke the old SSH key:** once the instrument PC leaves the previous
tailnet, the previous operator can no longer reach it. Revoke (uninstall AI remote access or
delete the `authorized_keys` line) only when you want to cut access inside the *same* tailnet.

### The plugin over a VPN

Reaching MAST's API from another machine is supported only over a VPN, and only with LAN mode
turned on: MAST then serves `https://` (a self-signed certificate) with an HTTP Basic login. There
is no other authentication layer.

The MCP server enforces the loopback default itself: an address other than this machine is refused
until `allow_remote` is turned on. It has no way to verify that the address you then use is
actually inside a VPN — keeping it there is your own responsibility; never point it at an address
reachable from the open internet. A self-signed certificate needs `verify_tls` set to `false`, or
the certificate installed locally. For Codex, the same switches are environment variables that you
add by hand to `[mcp_servers.mast.env]`: `MAST_ALLOW_REMOTE = "true"`, `MAST_USER`, `MAST_PASSWORD`
and, for a self-signed certificate, `MAST_VERIFY_TLS = "false"`. One more trap worth knowing about
in advance: if you type a **hostname** that happens to sit on a browser's built-in HSTS-preload
list, the browser will refuse to offer a "proceed anyway" option for the self-signed certificate at
all — use the numeric IP address instead in that case.

LAN access also changes how a client on the same machine connects: `127.0.0.1` then answers
`https://` too, with the self-signed certificate and the login. If nobody needs remote access,
untick LAN access in the launcher and restart the main service; the default address then works.
Otherwise set `mast_url` (`https://127.0.0.1:7862`), `mast_user`, `mast_password` and
`verify_tls` (`false`) together, then restart the MCP server — in Claude Code, `/mcp` and
reconnect `mast`. Set them in one go: every one left out costs another reconnect.

### The plugin over Tailscale, step by step

1. **Install Tailscale on both machines** (the instrument PC and the one you work from) and sign in
   to the same Tailscale account. On the instrument PC, `tailscale ip -4` prints its Tailscale
   address (it starts with `100.`). MAST's Settings page, "Remote access across networks
   (Tailscale)", shows the same address.
2. **Turn on remote access on the instrument PC.** In the MAST launcher, tick the box in
   "LAN / remote access" (*Enable: other computers can reach MAST via the LAN IP or Tailscale*),
   choose a username and password, then click "Restart main service". Without the restart nothing
   changes.
3. **Check it in a browser** on the other machine: open `https://<Tailscale IP>:7862`, accept the
   certificate warning the first time (Advanced, then Proceed) and sign in with the username and
   password. Use the numeric address, **not** the `*.ts.net` name: browsers refuse a self-signed
   certificate on that domain with no way past the warning.
4. **Point Claude Code at it.** Install the plugin on the other machine as in
   [A. Claude Code](#a-claude-code) (`/plugin marketplace add HamsterPark/MAST`, then
   `/plugin install mast@mast`), and set the plugin options:

```text
mast_url       https://<Tailscale IP>:7862
mast_user      <the username from step 2>
mast_password  <the password from step 2>
verify_tls     false
allow_remote   true
```

Then reconnect the server (`/mcp`, then `mast`) and ask Claude to read the briefing. The tools,
the safety gates and the one-act-one-observation rule behave exactly as on the instrument PC. Two
things differ: the `mast` plugin needs Python 3.10 or newer on that machine (set the `python`
option to a real interpreter), and the detector endpoints stay local to the instrument PC (see
[Troubleshooting](08-troubleshooting.md)). If something does not connect, the same page lists the
usual causes (not signed in to the same Tailscale account, a firewall, a proxy).

Keep MAST inside the tailnet: do not use Tailscale Funnel or any port forwarding to publish it.

## First session

Codex and Claude Code call the same tools. Every step below is one call; run them in order the
first time you connect. On a real instrument, agree on the first actions with the operator: the
first sessions there are supervised.

1. **Briefing.** `mast_briefing` — read it before anything else. It carries the tip, live
   readings, the operator's preferences, recent actions by anyone, and your own open requests.
2. **Scope.** `mast_scope` with `experiment_name`, `goal` and `sample_name` — choose or create the
   experiment and sample. Without a sample, scans and spectroscopy are refused at the door; without
   an experiment, nothing you do is recorded. The experiment also selects the workspace: its frame
   archive, its `WORKING.md` and its history.
3. **Notes.** `mast_read_guide` and `mast_read_working` — what earlier sessions learnt about this
   instrument, and where this experiment stands.
4. **Look.** `mast_observe` — the newest frame as an image, the instrument state, and a
   `next_token`. Look at the image.
5. **Act once.** `mast_act` with one action and `after` set to that `next_token`, for example a
   small scan (the arguments are shown below). When the act ends within `wait_s` (default 45 s),
   the answer is its observation: the new frame and a fresh `next_token`. When it is still running,
   the answer says `state: running` with a `job_id`: call `mast_wait` with
   `{"type": "job", "job_id": ...}` for its observation, and do not act meanwhile.
6. **Look at the result.** Look at the new image before deciding anything (in Codex Code Mode, call
   `image()` on the result).
7. **Measure.** Take numbers from the archived values, not from the picture: `mast_read_values` for
   exact values, `mast_line_profile` for distances and step heights, `mast_fft_peaks` for periods,
   `mast_detect_blobs` to count and locate features, `mast_stats` for roughness and noise,
   `mast_diff` to compare two frames, and `mast_inspect` to zoom in.
8. **Write it down.** `mast_write_working`: the plan, what you expect next, the frame ids you still
   need. Lessons that will hold for later experiments go into `mast_write_guide`.
9. **Next act**, with the token of the newest observation you received.
10. **Hand over.** Leave the instrument idle (no act running, scan stopped), make sure `WORKING.md`
    is saved, and call `mast_handover` with a summary, even for a short session; it teaches the
    next agent, human or not, what happened.

The arguments of the act in step 5:

```json
{"action": "scan", "params": {"center": {"x_nm": 0, "y_nm": 0}, "size_nm": 50}, "after": "K7QM-29XA", "note": "expect terraces separated by monatomic steps"}
```

The token is a placeholder: use the `next_token` your own observation returned. Actions take nm, V,
pA, s and nm/s; a position is `{"x_nm": .., "y_nm": ..}` in scan-frame nm, or
`{"frame": "t0007.s0", "x": .., "y": ..}` in display pixels of an archived frame. Unset optional
values of a scan come from the instrument's scan policy for that size. Skills run with `mast_run`
keep their own units, SI base units (see [Concepts](02-concepts.md)).

## The same, over plain HTTP

The 6.5.0 walkthrough drives the job API directly: health check, briefing, scope, finding a skill,
its card, one harmless read as a job, polling it, and the handover.

```bash
BASE=http://127.0.0.1:7862/api/ext/v1
curl -s "$BASE/health"
curl -s "$BASE/briefing" -H "X-MAST-Actor: claude-code"
curl -s -X POST "$BASE/scope" -H "Content-Type: application/json" -H "X-MAST-Actor: claude-code" \
  -d '{"experiment": {"name": "Quickstart"}, "sample": {"name": "Bench sample"}}'
curl -s "$BASE/skills/search?q=bias" -H "X-MAST-Actor: claude-code"
curl -s "$BASE/skills/GetBias" -H "X-MAST-Actor: claude-code"
curl -s -X POST "$BASE/jobs" -H "Content-Type: application/json" -H "X-MAST-Actor: claude-code" \
  -d '{"skill": "GetBias", "params": {}, "request_id": "quickstart-001"}'
curl -s "$BASE/jobs/j_0123456789ab?wait_s=30" -H "X-MAST-Actor: claude-code"
curl -s -X POST "$BASE/handover" -H "Content-Type: application/json" -H "X-MAST-Actor: claude-code" \
  -d '{"summary": "Quickstart walkthrough: read the bias once."}'
```

The visual harness over HTTP: an observation; then, with that observation's token, one act that
only lets time pass (`wait`, five seconds) and returns at once instead of waiting for itself
(`wait_s` 0); then a server-side wait for the act's job, whose answer carries the act's
observation.

```bash
BASE=http://127.0.0.1:7862/api/ext/v1
H1="Content-Type: application/json"; H2="X-MAST-Actor: my-script"; H3="X-MAST-Session: quickstart"
curl -s -X POST "$BASE/vista/observe" -H "$H1" -H "$H2" -H "$H3" -d '{}'
curl -s -X POST "$BASE/vista/act" -H "$H1" -H "$H2" -H "$H3" \
  -d '{"action": "wait", "params": {"seconds": 5}, "after": "K7QM-29XA", "wait_s": 0, "request_id": "quickstart-act-001"}'
curl -s -X POST "$BASE/wait" -H "$H1" -H "$H2" -H "$H3" \
  -d '{"conditions": [{"type": "job", "job_id": "j_0123456789ab"}], "timeout_s": 600}'
```

The token and the job ids are placeholders: use the ones your own answers returned. See
[the API reference](07-api-reference.md) for what every field in these answers means, and
[Records and context](04-records-and-context.md) for what just got written down because you set a
scope first.
