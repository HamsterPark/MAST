"""MAST's remote AI access: one MCP endpoint on this computer's Tailscale address.

The MAST launcher runs it (section "Remote AI"). Claude Code or Codex on another
computer in the same tailnet adds the URL as an HTTP MCP server and gets:

* the ``mast_*`` tools, which reach the MAST service on this machine over
  loopback. Each calling computer has its own ``Toolbox``, so its jobs carry its
  own actor name and ``mast_wait`` keeps its own cursor;
* ``mast_service``: whether the main service runs, and starting, restarting or
  stopping it. Somebody at the instrument would click the launcher for that; an
  AI on another computer cannot;
* the computer tools (``computer.ComputerTools``): commands, files and
  screenshots, refused unless the operator ticked "Also allow commands, files
  and screenshots".

Nothing has to be installed on the AI's computer: the two connection lines carry
the URL with its key and the command that registers it. Key, port and name are
kept in a small JSON file, so the lines stay the same across restarts; a new
key makes every earlier line stop working at once.

The listener binds this computer's Tailscale address only, and ``http_rpc``
refuses senders outside the tailnet, requests from web pages and wrong keys.
Tailscale encrypts the traffic (WireGuard), so plain HTTP is enough.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
import time
import unicodedata
from dataclasses import dataclass

from . import __version__
from .client import serves_tls
from .config import load_config, sanitize_token
from .http_rpc import HttpMcpServer, Peer, check_key
from .resources import GuideResources
from .tools import TOOLS, Session, Toolbox, ToolOutput

log = logging.getLogger("mast_mcp.remote")

DEFAULT_PORT = 47890
#: Every call ends within this; HTTP clients stop waiting for an answer after ~60 s.
CALL_CEILING_S = 55.0
MAST_PORT = 7862
#: How long a probe of the MAST service's scheme (http, or https in LAN mode) is trusted.
SCHEME_PROBE_S = 5.0
PEER_NAMES_TTL_S = 30.0
SERVICE_ACTIONS = ("status", "start", "restart", "stop")

_KEY_RX = re.compile(r"^[0-9a-f]{32}$")


def runs_code(tool: str, arguments: dict) -> bool:
    """A mast_* call that runs Python on the MAST computer: creating or test-running a detector.

    MAST takes those only from this machine (``mast/api/ext/detectors.py``, loopback_only),
    because detector code can read files, start processes and use the network as the MAST
    user. Every call of this server reaches MAST from this machine, so here the boundary is
    the operator's "commands, files and screenshots" switch instead.
    """
    if tool == "mast_detector_create":
        return True
    op = arguments.get("op") if isinstance(arguments, dict) else None
    return tool == "mast_detectors" and str(op or "").strip().lower() == "test"


def new_key() -> str:
    """128 random bits as 32 lower-case hex digits."""
    return secrets.token_hex(16)


def valid_key(key) -> bool:
    return isinstance(key, str) and bool(_KEY_RX.match(key))


def sanitize_name(text: str, fallback: str = "mast") -> str:
    """Lower-case letters, digits and dashes (an MCP server name), accents dropped."""
    plain = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")
    out = re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-")[:40].strip("-")
    return out or fallback


def default_name(machine: str) -> str:
    """What the AI calls this computer: ``mast-<Tailscale machine name>``."""
    base = sanitize_name(machine, "")
    if not base:
        return "mast"
    return base if base == "mast" or base.startswith("mast-") else sanitize_name("mast-" + base)


def mcp_url(host: str, port: int, key: str) -> str:
    h = f"[{host}]" if ":" in host else host
    return f"http://{h}:{port}/mcp/{key}"


def connection_lines(name: str, url: str) -> tuple[str, str]:
    """The two lines the operator pastes into the AI on the other computer.

    Plain ASCII without quotes, so chat and mail programs do not "smarten" them on
    the way; written for the AI, which knows how to register an MCP server.
    """
    return (
        f"MAST remote AI: please connect to the MAST computer {name}, which runs a scanning "
        f"tunneling microscope. It is an MCP server (streamable HTTP; the key is inside the URL, "
        f"no other login): {url}",
        f"How: Claude Code -> claude mcp add -s user -t http {name} <URL> (if {name} already "
        f"exists, first: claude mcp remove -s user {name}) ; Codex -> codex mcp add {name} --url "
        f"<URL> ; then tell me to restart you, and use the {name} tools (start with "
        f"mast_briefing).",
    )


@dataclass
class RemoteSettings:
    """What is remembered between runs: the key, the port and an optional name."""
    key: str = ""
    port: int = DEFAULT_PORT
    name: str = ""

    @classmethod
    def load(cls, path: str) -> "RemoteSettings":
        """Never fails: a missing or damaged file gives the defaults."""
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        port = data.get("port")
        return cls(key=data.get("key") if valid_key(data.get("key")) else "",
                   port=port if isinstance(port, int) and 0 < port < 65536 else DEFAULT_PORT,
                   name=sanitize_name(data["name"], "") if isinstance(data.get("name"), str)
                   else "")

    def save(self, path: str) -> None:
        folder = os.path.dirname(os.path.abspath(path))
        os.makedirs(folder, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"key": self.key, "port": self.port, "name": self.name}, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)


class _KeepAwake:
    """Keeps Windows from sleeping while the server runs (the screen may still turn off)."""

    def __init__(self):
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if os.name != "nt" or self._thread is not None:
            return
        self._stop.clear()

        def hold():
            import ctypes
            es_continuous, es_system_required = 0x80000000, 0x00000001
            kernel32 = ctypes.windll.kernel32
            # The request belongs to this thread and ends with it, or with ES_CONTINUOUS alone.
            kernel32.SetThreadExecutionState(es_continuous | es_system_required)
            self._stop.wait()
            kernel32.SetThreadExecutionState(es_continuous)

        self._thread = threading.Thread(target=hold, name="mast-remote-ai-awake", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2.0)
            self._thread = None


class _PeerState:
    def __init__(self):
        self.client_info: dict | None = None
        self.toolbox: Toolbox | None = None
        self.toolbox_for: tuple = ()
        self.session = Session()


SERVICE_TOOL = {
    "name": "mast_service",
    "title": "MAST main service",
    "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
        "action": {"type": "string", "enum": list(SERVICE_ACTIONS), "default": "status",
                   "description": "status (default), start, restart or stop."}}},
}


class RemoteServer:
    """The backend of ``http_rpc`` for MAST, and the listener's life cycle.

    ``host``: this computer's Tailscale IPv4 address. ``settings_path``: the JSON file
    with key, port and name. ``mast_login()``: the LAN-access user and password,
    used when the MAST service answers https (LAN mode). ``computer``: a
    ``ComputerTools`` (made here when None). ``service``: the launcher's handle on the
    main service, with ``status() -> dict`` (``running``, ``starting``, ``managed``)
    and ``start()``, ``restart()``, ``stop()``, each returning a sentence; None leaves
    ``mast_service`` answering that it cannot act. ``activity(who, kind, detail)``:
    the launcher's activity log. ``peer_names()``: Tailscale address -> machine name.
    """

    def __init__(self, *, settings_path: str, host: str, name: str, fetch_dir: str,
                 guide_dir: str | None = None, mast_port: int = MAST_PORT, mast_login=None,
                 computer=None, computer_allowed=None, service=None, activity=None,
                 peer_names=None, product: str = "", tailnet_only: bool = True,
                 exact_port: bool = False):
        self.settings_path = settings_path
        self.host = host
        self.settings = RemoteSettings.load(settings_path)
        self.name = self.settings.name or sanitize_name(name)
        self.fetch_dir = fetch_dir
        self.mast_port = int(mast_port)
        self._mast_login = mast_login
        self._service = service
        self._computer_allowed = computer_allowed
        self._activity = activity
        self._peer_names = peer_names
        self._tailnet_only = tailnet_only
        self._exact_port = exact_port
        self._resources = GuideResources(guide_dir)
        if computer is None:
            from .computer import ComputerTools
            computer = ComputerTools(name=self.name, address=host, activity=self._note_tool,
                                     allowed=computer_allowed, product=product)
        self.computer = computer
        self._template = Toolbox(load_config({"MAST_URL": f"http://127.0.0.1:{self.mast_port}"}),
                                 call_ceiling_s=CALL_CEILING_S)
        self._peers: dict[str, _PeerState] = {}
        self._lock = threading.Lock()
        self._scheme = ("", 0.0)
        self._names: dict[str, str] = {}
        self._names_at = 0.0
        self._http: HttpMcpServer | None = None
        self._awake = _KeepAwake()

    # ── life cycle ───────────────────────────────────────────────────────
    @property
    def running(self) -> bool:
        return self._http is not None

    @property
    def port(self) -> int:
        return self._http.port if self._http else self.settings.port

    @property
    def url(self) -> str:
        return mcp_url(self.host, self.port, self.settings.key)

    def lines(self) -> tuple[str, str]:
        return connection_lines(self.name, self.url)

    def start(self) -> str:
        """Bind and serve; returns the URL. The key and the port that was bound are saved."""
        if self._http is not None:
            return self.url
        if not valid_key(self.settings.key):
            self.settings.key = new_key()
        http = HttpMcpServer(self, host=self.host, port=self.settings.port,
                             exact_port=self._exact_port, tailnet_only=self._tailnet_only)
        http.start()
        self._http = http
        if not self._exact_port:      # a test's fixed port is not the one to remember
            self.settings.port = http.port
        self._save()
        self._awake.start()
        log.info("remote AI server listening on %s:%s as %s", self.host, http.port, self.name)
        return self.url

    def stop(self) -> None:
        """Stop listening, end running calls and commands."""
        http, self._http = self._http, None
        if http is not None:
            http.stop()
        try:
            self.computer.close()
        finally:
            self._awake.stop()

    def renew_key(self) -> str:
        """A new key: every earlier URL stops working at once. Returns the new URL."""
        self.settings.key = new_key()
        self._save()
        return self.url

    def _save(self) -> None:
        try:
            self.settings.save(self.settings_path)
        except OSError as exc:
            log.warning("cannot save the remote AI settings to %s: %s", self.settings_path, exc)

    # ── http_rpc backend ─────────────────────────────────────────────────
    def check_key(self, key: str) -> bool:
        return check_key(self.settings.key, key)

    def peer_name(self, address: str) -> str:
        """The Tailscale machine name of ``address`` (the map is refreshed at most every 30 s)."""
        fallback = "localhost" if address in ("127.0.0.1", "::1") else ""
        if self._peer_names is None:
            return fallback
        with self._lock:
            name = self._names.get(address)
            stale = time.monotonic() - self._names_at > PEER_NAMES_TTL_S
            if name or not stale:
                return name or fallback
            self._names_at = time.monotonic()
        try:
            fresh = dict(self._peer_names() or {})
        except Exception:  # noqa: BLE001 - a name is only a nicety for the activity log
            fresh = {}
        with self._lock:
            self._names.update({str(k): str(v) for k, v in fresh.items() if v})
            return self._names.get(address, fallback)

    def note(self, peer: Peer, kind: str, detail: str = "") -> None:
        self._emit(peer.name, kind, detail)

    def initialize(self, peer: Peer, client_info: dict | None) -> dict:
        self._state(peer).client_info = client_info
        return {"serverInfo": {"name": "mast-remote", "title": f"MAST on {self.name}",
                               "version": __version__},
                "instructions": self.instructions()}

    def list_tools(self, peer: Peer) -> list[dict]:
        return (self._template.list_tools() + [self._service_tool()]
                + self.computer.list_tools())

    def has_tool(self, peer: Peer, name: str) -> bool:
        return name in TOOLS or name == "mast_service" or self.computer.has_tool(name)

    def call_tool(self, peer: Peer, name: str, arguments: dict, signal, meta=None):
        if name == "mast_service":
            return self._service_call(peer, arguments)
        if name not in TOOLS:
            return self.computer.call_tool(name, arguments, signal, who=peer.name)
        if runs_code(name, arguments) and not self._code_allowed():
            self._emit(peer.name, "refused", name)
            return ToolOutput(
                f"{name} did not run: detector code runs as Python on the MAST computer "
                f"{self.name}, which over this connection needs the same permission as "
                "run_command. The operator has not allowed commands, files and screenshots; ask "
                "them (mast_ask_operator) to tick \"Also allow commands, files and screenshots\" "
                "in the Remote AI section of the MAST launcher. Listing, enabling, disabling "
                "and deleting detectors work without it.", True)
        self._emit(peer.name, "mast", _mast_detail(name, arguments))
        stopped = self._service_down(name)
        if stopped:
            return ToolOutput(stopped, True)
        state = self._state(peer)
        return self._toolbox(peer, state).call_tool(name, arguments, signal, meta=meta,
                                                    client_info=state.client_info)

    def list_resources(self) -> list[dict]:
        return self._resources.list_resources()

    def list_templates(self) -> list[dict]:
        return self._resources.list_templates()

    def read_resource(self, uri: str) -> dict:
        return self._resources.read_resource(uri)

    # ── what the AI reads ────────────────────────────────────────────────
    def instructions(self) -> str:
        return "\n".join((
            f"This MCP server runs on the MAST computer {self.name} and is reached over Tailscale. "
            "Everything its tools do happens on THAT computer, not on the machine you run on.",
            "- The mast_* tools operate its instrument station (a scanning tunneling microscope or "
            "an optical station) through MAST; the MAST rules below apply to them.",
            "- mast_service shows whether MAST's main service runs, and starts, restarts or stops "
            "it; the mast_* tools need it running. Nobody has to click anything on that computer.",
            "- mast_fetch saves files on that computer and answers with the path there; read them "
            "with read_file or analyse them there with run_command, when computer control is "
            "allowed.",
            "",
            self._template.instructions(),
            "",
            self.computer.instructions(),
            "- The computer tools (system_info, run_command, check_command, read_file, write_file, "
            "edit_file, list_directory, take_screenshot) work only while the operator allows "
            "them in the MAST launcher; a refusal says so. The same permission covers creating "
            "and test-running detectors (their code runs as Python on that computer); the other "
            "mast_* tools do not depend on it.",
        ))

    def _service_tool(self) -> dict:
        tool = dict(SERVICE_TOOL)
        tool["description"] = (
            f"MAST's main service on the MAST computer {self.name}: its state (action status), or "
            "start, restart or stop it. The mast_* tools need it running; when one answers that "
            "the main service is not running, start it here (that takes up to a minute or two "
            "while the vision model loads), then call mast_briefing. Restarting or stopping ends "
            "running jobs (they come back as lost_on_restart) and disconnects from Nanonis "
            "gracefully: check mast_status and finish or cancel jobs first, and leave the "
            "instrument idle.")
        tool["annotations"] = {"title": tool["title"], "readOnlyHint": False,
                               "destructiveHint": True, "idempotentHint": False,
                               "openWorldHint": False}
        return tool

    # ── mast_service ─────────────────────────────────────────────────────
    def _service_call(self, peer: Peer, arguments: dict) -> ToolOutput:
        action = str(arguments.get("action") or "status").strip().lower()
        if action not in SERVICE_ACTIONS:
            return ToolOutput(f"mast_service: action must be one of {', '.join(SERVICE_ACTIONS)}, "
                              f"not {action!r}.", True)
        if self._service is None:
            return ToolOutput("mast_service is not available: this server does not run inside the "
                              "MAST launcher.", True)
        if action != "status":
            self._emit(peer.name, "service", action)
        try:
            if action == "status":
                return ToolOutput(_service_text(self._service.status()))
            text = getattr(self._service, action)()
        except Exception as exc:  # noqa: BLE001 - reported to the AI
            log.exception("mast_service %s failed", action)
            return ToolOutput(f"mast_service {action} failed: {exc}", True)
        state = self._service.status()
        return ToolOutput(f"{text}\n{_service_text(state)}")

    def _service_down(self, tool: str) -> str:
        """Why a mast_* tool cannot run now ('' when it can)."""
        if self._service is None:
            return ""
        try:
            state = self._service.status()
        except Exception:  # noqa: BLE001 - let the tool find out by itself
            return ""
        if state.get("running"):
            return ""
        if state.get("starting"):
            return (f"MAST's main service on {self.name} is still starting, so {tool} did not "
                    "run. Wait half a minute (mast_service with action status shows when it "
                    "runs), then call mast_briefing.")
        return (f"MAST's main service is not running on {self.name}, so {tool} did not run. "
                "Start it with mast_service(action=\"start\"), which takes up to a minute or "
                "two, then call mast_briefing.")

    # ── per-peer MAST tools ──────────────────────────────────────────────
    def _state(self, peer: Peer) -> _PeerState:
        with self._lock:
            state = self._peers.get(peer.address)
            if state is None:
                state = self._peers[peer.address] = _PeerState()
            return state

    def _mast_scheme(self) -> str:
        """http, or https when the service runs in LAN mode (TLS on loopback too)."""
        with self._lock:
            scheme, at = self._scheme
            if scheme and time.monotonic() - at < SCHEME_PROBE_S:
                return scheme
        scheme = "https" if serves_tls("127.0.0.1", self.mast_port, timeout=1.0) else "http"
        with self._lock:
            self._scheme = (scheme, time.monotonic())
        return scheme

    def _toolbox(self, peer: Peer, state: _PeerState) -> Toolbox:
        scheme = self._mast_scheme()
        user = password = ""
        if scheme == "https" and self._mast_login is not None:
            try:
                user, password = self._mast_login() or ("", "")
            except Exception:  # noqa: BLE001 - the tool then reports the login it lacks
                user = password = ""
        client = ((state.client_info or {}).get("name") or "remote-ai")
        key = (scheme, user, password, client, peer.name)
        with self._lock:
            if state.toolbox is not None and state.toolbox_for == key:
                return state.toolbox
            env = {"MAST_URL": f"{scheme}://127.0.0.1:{self.mast_port}",
                   "MAST_USER": user, "MAST_PASSWORD": password, "MAST_VERIFY_TLS": "false",
                   "MAST_ACTOR": sanitize_token(f"{client}-{peer.name}", 48) or "remote-ai",
                   "MAST_SESSION": sanitize_token(f"{peer.name}-{secrets.token_hex(3)}", 64),
                   "MAST_FETCH_DIR": self.fetch_dir}
            toolbox = Toolbox(load_config(env), call_ceiling_s=CALL_CEILING_S)
            toolbox.session = state.session         # the wait cursor survives a rebuild
            state.toolbox, state.toolbox_for = toolbox, key
            return toolbox

    def _code_allowed(self) -> bool:
        if self._computer_allowed is None:
            return True
        try:
            return bool(self._computer_allowed())
        except Exception:  # noqa: BLE001 - when in doubt, the switch is off
            return False

    def _note_tool(self, who: str, kind: str, detail: str) -> None:
        self._emit(who, kind, detail)

    def _emit(self, who: str, kind: str, detail: str) -> None:
        if self._activity is None:
            return
        try:
            self._activity(who, kind, detail)
        except Exception:  # noqa: BLE001 - the log must never break a call
            log.exception("activity callback failed")


def _mast_detail(name: str, arguments: dict) -> str:
    for key in ("skill", "action", "op"):
        value = arguments.get(key) if isinstance(arguments, dict) else None
        if isinstance(value, str) and value.strip():
            return f"{name} {value.strip()[:80]}"
    return name


def _service_text(state: dict) -> str:
    if state.get("running"):
        how = "started by the MAST launcher" if state.get("managed") else \
            "run by another process, not by the launcher"
        return f"MAST's main service is running ({how})."
    if state.get("starting"):
        return "MAST's main service is starting (its port is not open yet)."
    return "MAST's main service is not running."
