# MAST for Codex

[中文](README.zh.md)

Codex (the desktop app or the CLI) drives MAST through the same MCP server as the Claude Code
plugin, `integrations/claude-code/server`, with the same `mast_*` tools and the same rules. Codex
expands neither `${CLAUDE_PLUGIN_ROOT}` nor `${user_config.*}`, so the plugin's `.mcp.json` cannot
be reused: Codex gets its own entry in its `config.toml`, with absolute paths, written by
`setup_clients.py`.

> **Status:** Codex is a supported client (Claude Code with Claude Opus 5.5 is the recommended one). The connection and the visual harness have been
> tested end to end against the STM-Bench simulator, not yet on a real instrument: treat the first
> sessions on real hardware as supervised.

## Connect

On the PC where MAST is installed, in a terminal (MAST installs to `C:\MAST` by default; use your
own installation folder if you chose another one):

```text
C:\MAST\MASTv2\pyruntime\python.exe C:\MAST\integrations\setup_clients.py codex
```

This

- adds the `[mcp_servers.mast]` tables to `%USERPROFILE%\.codex\config.toml` (`CODEX_HOME` if
  that is set). The file is backed up first (`config.toml.bak-<time>`), and nothing outside these
  tables changes. Running the command again replaces the tables it wrote.
- creates the agent workspace `%USERPROFILE%\Documents\MAST-agent` with an `AGENTS.md` that holds
  the operating rules. An `AGENTS.md` you edited is backed up before it is replaced.

Then:

1. Start MAST.
2. Run `codex mcp list`: `mast` should be listed.
3. Open the workspace in Codex: in the app, open the folder `MAST-agent`; on the command line,
   `codex -C "%USERPROFILE%\Documents\MAST-agent"`.

Sign in to Codex with your ChatGPT account (`codex login`), not with an API key. Remove
`OPENAI_API_KEY`, `CODEX_API_KEY` and `CODEX_ACCESS_TOKEN` from your environment variables:
when one of them is set, Codex uses it instead, and the API account is billed. `setup_clients.py`
warns if it finds one; `forced_login_method = "chatgpt"` in `config.toml` makes Codex refuse any
other sign-in.

Run the command again after every MAST upgrade: it refreshes the entry and the workspace's
`AGENTS.md`.

## Options

| Option | Meaning |
|---|---|
| `--mast-url URL` | MAST's address, default `http://127.0.0.1:7862` |
| `--actor NAME` | the name your actions are recorded under, default `codex` |
| `--workspace DIR` | the agent workspace, default `%USERPROFILE%\Documents\MAST-agent` |
| `--approve all` | default: every MAST tool runs without a Codex prompt |
| `--approve read_only` | only MAST's read-only tools are pre-approved; Codex asks before the others |
| `--codex-home DIR` | Codex's folder, default `CODEX_HOME`, else `%USERPROFILE%\.codex` |
| `--dry-run` | print what would change and write nothing |
| `--uninstall` | remove the `[mcp_servers.mast]` tables again (after a backup) |

## Why the entry looks the way it does

- **`--approve all` is the default.** Without pre-approval, `codex exec` refuses MAST's write
  tools outright (there is nobody to ask), and in the app a prompt per action gets in the way of
  a long session. The protection is on MAST's side: its safety gates check every action, and an
  act is refused unless it carries the token of the latest observation, so an agent cannot act
  without having looked first. With `--approve read_only`, run the command again after upgrading
  MAST: new tools may have been added.
- **`tool_timeout_sec = 900`.** `mast_wait` blocks inside MAST for up to 840 s, and Codex does not
  tell the server when it gives up on a call.
- **No `supports_parallel_tool_calls`.** With it, Codex would run MAST's write tools in parallel;
  without it, they are queued.
- **`cwd` is the workspace.** Files that `mast_fetch` downloads land in `.mast-fetch` there.
- **A MAST on another machine** is refused by the bridge unless `MAST_ALLOW_REMOTE = "true"` is in
  `[mcp_servers.mast.env]`; LAN mode also needs `MAST_USER` and `MAST_PASSWORD` there. Add them by
  hand, and again after each run of `setup_clients.py codex`, which rewrites that table.
- **LAN mode on this machine.** With LAN access ticked in the MAST launcher, MAST answers `https://`
  with a self-signed certificate and a login on `127.0.0.1` too. Either untick LAN access and
  restart the main service, or put `MAST_URL = "https://127.0.0.1:7862"`, `MAST_USER`,
  `MAST_PASSWORD` and `MAST_VERIFY_TLS = "false"` into `[mcp_servers.mast.env]` together (by hand,
  as above) and start a new session.

## Working with MAST from Codex

The workspace's `AGENTS.md` holds the operating rules, the same ones as the Claude Code plugin's
`mast-operator` skill, and the `mast` server serves the full guide as MCP resources
(`mast://guide/en/README.md`, `mast://guide/zh/README.md`). A session runs: `mast_briefing`, then
`mast_scope`, `mast_read_guide` and `mast_read_working`, then `mast_observe`; one `mast_act` with
`after` set to the observation's `next_token`; a look at the new image; numbers from
`mast_read_values` and the measure tools; `mast_write_working`; the next act with the new token;
and finally `mast_handover`. In Code Mode, call `image()` on every observation, since an image
reaches the model only that way, and send at most one act per code cell: MAST refuses a second one
(`same_code_cell`). The [quickstart](../../docs/external/en/01-quickstart.md) walks through a first
session.

## If something does not work

| Symptom | Fix |
|---|---|
| `codex mcp list` does not list `mast` | Run `setup_clients.py codex` again as this Windows user; add `--codex-home` if Codex uses another folder |
| Codex bills an API account | Remove the three API-key variables and sign in with ChatGPT (`codex login`) |
| `mast_wait` is cut off after a few minutes | `tool_timeout_sec = 900` must be in `[mcp_servers.mast]`: run the command again |
| The model acts without having looked | Code Mode: `image()` on every observation, one act per code cell |
| No images reach the model at all | Connect Codex to MAST's server directly: a wrapper that adds `structuredContent` to results makes Codex drop their images |
| `observe_first` again and again | Another observation replaced the token: act with the token of the newest observation, and keep other clients and MAST's interface from scanning meanwhile |
| `Cannot connect … (connection refused)`, although a MAST window is open | That window is the launcher: click 「启动主服务」 (Start main service) in it, then call the tool again |

The guide's [troubleshooting](../../docs/external/en/08-troubleshooting.md) covers the rest.

[`mast_mcp.example.toml`](mast_mcp.example.toml) is the same entry with placeholder paths, for
writing it by hand.
