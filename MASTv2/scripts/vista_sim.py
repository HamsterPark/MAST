"""Run the real MAST service on the STM-Bench simulator, end to end over HTTP.

Why this exists
---------------
MAST 7.0 is tested the way an external agent drives it: a real MAST HTTP service (the FastAPI
app around a real ``CoreRuntime``) connected to ``stmsim``'s controller wire server, driven
over ``/api/ext/v1`` and the MCP bridge on top of it. Two existing entry points come close and
both fall short:

* STM-Bench's ``RuntimeHost`` hosts a real ``CoreRuntime`` on the simulator, but in-process
  and with no HTTP server;
* ``mast2_launcher.py --service-mode`` serves HTTP, but on the hard-coded port 7862, against
  the controller ports 6501-6504, in the operator's data root.

This script builds the service the launcher builds (``build_live_context`` + ``create_app``
under ``uvicorn.Server``, ``app.state.uvicorn_server`` wired so ``/api/admin/shutdown`` works)
on ephemeral loopback ports, in an isolated data root, against a simulated instrument, and then
syncs the operator facts the way ``RuntimeHost.sync_facts`` does (that method is reused, not
copied): tip registration, instrument profile, tilt calibration, coarse-drive ceiling, STS
condition (scenario worlds), experiment + sample, vacuum attestation. Without those facts the
skills refuse their preconditions (no sample scope, no tilt matrix, no coarse ceiling ...).

Usage
-----
One simulator + MAST for manual MCP tests (Ctrl+C stops it gracefully; point the bridge at the
printed ``MAST_URL``)::

    set PYTHONUTF8=1
    set PYTHONPATH=<repo>\\MASTv2;<STM-Bench checkout>
    .venv-v2-py313\\Scripts\\python.exe MASTv2\\scripts\\vista_sim.py serve --seed 0 --material "Au(111)" --port 7862

From Python: :class:`SimService` runs everything in the calling process (one per process: MAST
keeps process-level singletons, and the environment variables below are process-wide).
:class:`SimServiceProcess` runs ``serve`` in a child process; tests use that, because
``tests/conftest.py`` replaces ``nanonis_spm`` with a ``MagicMock`` in the test process, so an
in-process runtime there would talk to the mock instead of the simulator.

How-to and pitfalls: ``docs/v2/architecture/mast7_sim_bringup.md``.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Sequence

log = logging.getLogger("mast.vista_sim")

SCRIPT = Path(__file__).resolve()
REPO_ROOT = SCRIPT.parents[2]
MASTV2_ROOT = REPO_ROOT / "MASTv2"

#: MAST's controller reply timeout on the simulator. stmsim renders a whole frame inside
#: ``Scan_Action`` (2-15 s for large Au(111) frames; a real controller answers at once), past
#: MAST's 5 s default, which then reads as a dropped link. VISTA-SPM episodes used the same 60 s
#: (``vista_spm/episode.py`` ``SIM_REPLY_TIMEOUT_S``).
SIM_REPLY_TIMEOUT_S = 60.0

#: ui_settings.json seed. ``RuntimeHost.DEFAULT_SETTINGS`` writes dotted keys
#: (``current_monitor.cm_enabled``); ``SettingsStore`` keeps only ``KNOWN_KEYS`` and drops them,
#: so of that seed only ``tool_refine_enabled`` ever reached MAST. This is that effective seed.
SEED_SETTINGS: dict[str, Any] = {"tool_refine_enabled": 0}

#: Provider credentials a simulator run never needs (the external agent is the driver).
#: :class:`SimServiceProcess` removes them from the child environment so a test cannot spend.
LLM_KEY_VARS = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "MOONSHOT_API_KEY", "KIMI_API_KEY",
    "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "ALIYUN_BAILIAN_API_KEY", "MINIMAX_API_KEY",
    "ZHIPU_API_KEY", "GLM_API_KEY", "OPENAI_API_KEY",
)

WIRE_ROLES = ("main", "monitor", "data", "emergency")
READY_FILE = "ready.json"
LOG_FILE = "service.log"


class SimServiceError(RuntimeError):
    """The simulator-backed service did not come up (or did not go down)."""


# ── environment ────────────────────────────────────────────────────────────


def package_parent(name: str) -> Path | None:
    """The directory that makes package ``name`` importable (``None``: not importable)."""
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ValueError):
        return None
    origin = getattr(spec, "origin", None) if spec is not None else None
    return Path(origin).resolve().parent.parent if origin else None


def isolation_env(root: Path) -> dict[str, str]:
    """Every data location MAST resolves, pointed into ``root``.

    ``MAST2_PROJECT_ROOT`` alone does not isolate a run: the experiment folders follow
    ``MAST_EXPERIMENT_ROOT`` (default ``<drive>:\\MAST-Data\\experiments``, the operator's real
    data; ``mast.core.experiment_paths._default_root``), and the literature, gallery and trace
    stores have their own variables. ``tests/v2/conftest.py`` guards the same set, one fixture
    each. ``logging.v2.storage.open_store`` falls back to the working directory unless
    ``MAST_DATA_DIR`` is set.
    """
    mast_root = root / "mast_root"
    lit = root / "literature"
    return {
        "MAST2_PROJECT_ROOT": str(mast_root),
        "MAST2_USER_ROOT": str(mast_root),
        "MAST_DATA_DIR": str(mast_root),
        "MAST_EXPERIMENT_ROOT": str(root / "experiments"),
        "MAST_EXPERIMENT_DB": str(mast_root / "experiments" / "mast_experiments.db"),
        "MAST_EXT_JOURNAL_DIR": str(mast_root / "experiments" / "ext_gateway"),
        "MAST_TRACES_DIR": str(mast_root / "experiments" / "traces"),
        "MAST_GALLERY_DIR": str(root / "gallery"),
        "MAST_LITERATURE_INDEX_DIR": str(lit / "literature_index"),
        "MAST_LITERATURE_LIBS_DIR": str(lit / "literature_libs"),
        "MAST_PAPERS_DIR": str(lit / "papers"),
        "MAST_PAPER_CORPUS": str(lit / "papers"),
        # the vision stack must never reach the network from a simulator run
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
    }


def _prepare_mast_root(mast_root: Path, settings: dict[str, Any], seed_composites: bool) -> None:
    """The launcher's first-run directories, the settings seed, and a copy of the repo's
    declarative composite skills (a copy, so nothing the service versions reaches
    ``config/composite_skills``; the ``seeded_composite_store`` test fixture does the same)."""
    for sub in ("experiments", "working-sessions", "api key", "models"):  # launcher _ensure_user_dirs
        (mast_root / sub).mkdir(parents=True, exist_ok=True)
    cfg_dir = mast_root / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    path = cfg_dir / "ui_settings.json"
    try:
        current = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except ValueError:
        current = {}
    current.update(settings)
    path.write_text(json.dumps(current, ensure_ascii=False, indent=1), encoding="utf-8")
    src, dst = REPO_ROOT / "config" / "composite_skills", cfg_dir / "composite_skills"
    if seed_composites and src.is_dir() and not dst.exists():
        shutil.copytree(src, dst)


def _apply_controller_timeout(cfg: Any, seconds: float) -> None:
    """Raise the controller recv timeout on every ``NanonisConfig`` of ``cfg`` (the same loop as
    ``RuntimeHost.start``, stmbench/harness/runtime_host.py:74-77). Also exported as
    ``MAST_NANONIS_TIMEOUT_S`` for MAST versions that read it; this covers the ones that do not."""
    from mast.config import NanonisConfig

    for value in vars(cfg).values():
        if isinstance(value, NanonisConfig):
            value.timeout_s = float(seconds)


def _ensure_mast_importable() -> None:
    if str(MASTV2_ROOT) not in sys.path:
        sys.path.insert(0, str(MASTV2_ROOT))


# ── the simulated instrument ───────────────────────────────────────────────


def resolve_scenario(spec: str) -> Path:
    """A scenario file path, or the id of one of STM-Bench's Track-B scenarios (``B6_sts_clean``)."""
    p = Path(spec)
    if p.is_file():
        return p
    parent = package_parent("stmbench")
    if parent is not None:
        cand = parent / "stmbench" / "trackB" / "scenarios" / f"{spec}.yaml"
        if cand.is_file():
            return cand
    raise SimServiceError(f"no scenario file or STM-Bench scenario id {spec!r}")


def build_world(*, seed: int, material: str, profile: str, scenario: str | None,
                time_scale: float | None, approached: bool, session_dir: Path):
    """The stmsim ``World``: from a scenario (``Scenario.build_world``, as VISTA-SPM episodes do),
    or bare like ``python -m stmsim serve``. Returns ``(world, scenario or None)``."""
    if scenario:
        from stmsim.scenario import Scenario

        sc = Scenario.load(resolve_scenario(scenario))
        if time_scale is not None:
            sc.time_scale = float(time_scale)
        return sc.build_world(seed, session_dir=session_dir), sc
    from stmsim.physics.rig import RigProfile
    from stmsim.physics.world import World

    world = World(rig=RigProfile.load(profile), seed=seed, material=material,
                  time_scale=1.0 if time_scale is None else float(time_scale),
                  session_dir=session_dir)
    if approached:
        # tunnelling with the feedback on and settled: what Scenario.build_world does for
        # ``initial.approached`` (stmsim/scenario.py:301-306); ScanAt needs ``z_controller_on``
        world.coarse.coarse_gap_m = world.surface_height_here() + 0.6e-9
        world.withdrawn = False
        world.zctrl_set(True)
        world.transients.clear()
        settle = getattr(world, "achievable_z_tip", None)
        if callable(settle):
            settle()
    return world, None


# ── the service, in this process ───────────────────────────────────────────


class SimService:
    """Simulator wire server + the real MAST service, in THIS process.

    One per process. ``start()`` sets process-wide environment variables (data root, controller
    ports and timeout, vision backend) before MAST is imported, and by default changes the working
    directory to the MAST data root, as ``mast2_launcher.run_service`` does.
    """

    def __init__(self, root: str | Path | None = None, *, seed: int = 0, material: str = "Au(111)",
                 profile: str = "reference-stm", scenario: str | None = None,
                 time_scale: float | None = None, approached: bool = True,
                 host: str = "127.0.0.1", port: int = 0,
                 wire_ports: Sequence[int] = (0, 0, 0, 0),
                 controller_timeout_s: float = SIM_REPLY_TIMEOUT_S,
                 vision_backend: str | None = "mock", settings: dict[str, Any] | None = None,
                 seed_composites: bool = True, chdir: bool = True, http_log_level: str = "warning"):
        # absolute: start() changes the working directory, and the World keeps root / "session"
        self.root = (Path(root) if root is not None
                     else Path(tempfile.mkdtemp(prefix="mast-sim-"))).resolve()
        self.seed = int(seed)
        self.material = material
        self.profile = profile
        self.scenario = scenario
        self.time_scale = time_scale
        self.approached = approached
        self.bind_host = host
        self.port = int(port)
        self.wire_ports = [int(p) for p in wire_ports]
        if len(self.wire_ports) != len(WIRE_ROLES):
            raise ValueError(f"wire_ports needs {len(WIRE_ROLES)} ports (0 = ephemeral)")
        self.controller_timeout_s = float(controller_timeout_s)
        self.vision_backend = vision_backend
        self.settings = {**SEED_SETTINGS, **(settings or {})}
        self.seed_composites = seed_composites
        self.chdir = chdir
        self.http_log_level = http_log_level
        self.world: Any = None
        self.wire: Any = None
        self.runtime_host: Any = None      # stmbench RuntimeHost: its dispatcher + sync_facts only
        self.ctx: Any = None
        self.runtime: Any = None
        self.app: Any = None
        self.server: Any = None            # uvicorn.Server
        self.facts: dict = {}
        self.bound_port: int | None = None
        self.startup_s: float | None = None
        self._thread: threading.Thread | None = None
        self._stopped = False

    @property
    def mast_root(self) -> Path:
        return self.root / "mast_root"

    @property
    def base_url(self) -> str:
        if self.bound_port is None:
            raise SimServiceError("service not started")
        return f"http://{self.bind_host}:{self.bound_port}"

    @property
    def ext_url(self) -> str:
        return self.base_url + "/api/ext/v1"

    @property
    def session_dir(self) -> Path:
        """Where the simulated controller saves .sxm / .dat (``Util_SessionPathGet``)."""
        return Path(self.world.session_dir)

    @property
    def serving(self) -> bool:
        return (self._thread is not None and self._thread.is_alive()
                and self.server is not None and not self.server.should_exit)

    def start(self) -> "SimService":
        if self.world is not None:
            raise SimServiceError("SimService.start() called twice")
        t0 = time.perf_counter()
        _ensure_mast_importable()
        self.root.mkdir(parents=True, exist_ok=True)
        # A real controller's session folder exists before the first save; stmsim creates it
        # lazily, and until then MAST's resolver (Util_SessionPathGet) falls back to the parent.
        (self.root / "session").mkdir(parents=True, exist_ok=True)
        self.world, _ = build_world(seed=self.seed, material=self.material, profile=self.profile,
                                    scenario=self.scenario, time_scale=self.time_scale,
                                    approached=self.approached, session_dir=self.root / "session")
        from stmbench.harness.runtime_host import RuntimeHost
        from stmsim.wire.server import WireServer

        # RuntimeHost builds the dispatcher and owns sync_facts(); its start() is NOT used, it
        # would build a second CoreRuntime with no HTTP service around it.
        self.runtime_host = RuntimeHost(self.world, self.root)
        self.wire = WireServer(self.runtime_host.dispatcher, ports=self.wire_ports).start()
        self.runtime_host.server = self.wire
        env = isolation_env(self.root)
        env.update({f"MAST_NANONIS_PORT_{role.upper()}": str(port)
                    for role, port in zip(WIRE_ROLES, self.wire.bound_ports, strict=True)})
        env["MAST_NANONIS_TIMEOUT_S"] = f"{self.controller_timeout_s:g}"
        if self.vision_backend:
            env["MAST_VISION_BACKEND"] = self.vision_backend
        os.environ.update(env)
        _prepare_mast_root(self.mast_root, self.settings, self.seed_composites)
        if self.chdir:
            os.chdir(self.mast_root)
        log.info("stmsim serving seed=%s material=%s wire ports %s, session dir %s",
                 self.seed, self.world.surface.material.name, self.wire.bound_ports, self.session_dir)

        # mast2_launcher.run_service boot order: platform patch before the heavy imports
        import mast.core.platform_patch  # noqa: F401
        from mast.api.app import create_app
        from mast.api.bootstrap import build_live_context
        from mast.config import MASTConfig

        cfg = MASTConfig()
        _apply_controller_timeout(cfg, self.controller_timeout_s)
        self.ctx = build_live_context(cfg)
        self.runtime = getattr(self.ctx, "runtime", None)
        if self.runtime is None:
            raise SimServiceError("build_live_context() returned no runtime (CoreRuntime import "
                                  "failed; the log above says why)")
        self.runtime_host.app = self.runtime
        self.facts = self.runtime_host.sync_facts()
        bad = sorted(k for k, v in self.facts.items() if isinstance(v, dict) and v.get("ok") is False)
        if bad:
            log.warning("operator facts NOT synced: %s", {k: self.facts[k] for k in bad})
        self.app = create_app(context=self.ctx, dev_cors=False)
        self._serve()
        self.startup_s = round(time.perf_counter() - t0, 1)
        log.info("MAST on stmsim serving at %s (startup %.1f s)", self.base_url, self.startup_s)
        return self

    def _serve(self) -> None:
        import uvicorn

        server = uvicorn.Server(uvicorn.Config(self.app, host=self.bind_host, port=self.port,
                                               log_level=self.http_log_level, proxy_headers=False))
        # what /api/admin/shutdown asks to exit (mast2_launcher.run_service does the same)
        self.app.state.uvicorn_server = server
        self.server = server
        self._thread = threading.Thread(target=server.run, name="mast-sim-uvicorn", daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 30.0
        while not server.started:
            if not self._thread.is_alive():
                raise SimServiceError(f"uvicorn did not start on {self.bind_host}:{self.port} "
                                      "(port in use?)")
            if time.monotonic() > deadline:
                raise SimServiceError("uvicorn did not report started within 30 s")
            time.sleep(0.05)
        self.bound_port = int(server.servers[0].sockets[0].getsockname()[1])

    def stop(self, timeout_s: float = 30.0) -> None:
        """Graceful and idempotent: ``CoreRuntime.shutdown()`` (what ``/api/admin/shutdown`` runs,
        without its 20 s ``os._exit`` backstop), then uvicorn ``should_exit``, then the wire server."""
        if self._stopped:
            return
        self._stopped = True
        # should_exit already set = /api/admin/shutdown ran, and it shut the runtime down first.
        # Not calling it again matters: the runtime's atexit hook calls it once more, and a third
        # call waits out QueuedIngestSink.flush(timeout=5) on the stop sentinel the second
        # call's close() left in the queue.
        already = self.server is not None and self.server.should_exit
        if self.runtime is not None and not already:
            try:
                self.runtime.shutdown()
            except Exception:  # noqa: BLE001 - keep going: uvicorn and the wire server must stop
                log.exception("CoreRuntime.shutdown() failed")
        if self.server is not None:
            self.server.should_exit = True
            if self._thread is not None:
                self._thread.join(timeout_s)
                if self._thread.is_alive():
                    log.warning("uvicorn still draining after %.0f s; forcing exit", timeout_s)
                    self.server.force_exit = True
                    self._thread.join(5.0)
        if self.wire is not None:
            self.wire.stop()
        log.info("MAST on stmsim stopped")

    def info(self) -> dict:
        """JSON-able description of the running service (the ``--ready-file`` content)."""
        import mast

        world = self.world
        return {
            "base_url": self.base_url,
            "ext_url": self.ext_url,
            "port": self.bound_port,
            "pid": os.getpid(),
            "root": str(self.root),
            "mast_root": str(self.mast_root),
            "experiment_root": os.environ.get("MAST_EXPERIMENT_ROOT", ""),
            "session_dir": str(self.session_dir),
            "wire_ports": dict(zip(WIRE_ROLES, self.wire.bound_ports, strict=True)),
            "controller_timeout_s": self.controller_timeout_s,
            "seed": self.seed,
            "material": world.surface.material.name,
            "scenario": self.scenario,
            "time_scale": float(getattr(world.clock, "time_scale", 1.0)),
            "initial_bias_v": float(world.bias_v),
            "facts": self.facts,
            "startup_s": self.startup_s,
            "stmsim": str(package_parent("stmsim") or ""),
            "mast": str(Path(mast.__file__).resolve().parent),
        }

    def __enter__(self) -> "SimService":
        try:
            return self.start()
        except BaseException:
            self.stop()
            raise

    def __exit__(self, *exc) -> None:
        self.stop()


# ── the service, in a child process ────────────────────────────────────────


def http_json(method: str, url: str, body: Any = None, *, headers: dict[str, str] | None = None,
              timeout: float = 30.0) -> tuple[int, Any]:
    """``(status, parsed JSON or text)``. Never through a proxy: the service is on loopback."""
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    text = raw.decode("utf-8", "replace")
    try:
        return status, json.loads(text) if text else None
    except ValueError:
        return status, text


class SimServiceProcess:
    """:class:`SimService` in a child process (``vista_sim.py serve``).

    For a process that cannot host the service itself: a pytest process (``tests/conftest.py``
    mocks ``nanonis_spm``), or one that already holds MAST's process-level singletons.

    The child gets the parent's environment minus provider credentials (:data:`LLM_KEY_VARS`),
    plus ``PYTHONPATH`` = MASTv2 + the directories the PARENT imports ``stmsim`` / ``stmbench``
    from (so both sides run the same simulator), ``PYTHONUTF8=1``, and a per-service
    ``MAST2_SHUTDOWN_TOKEN``. It writes ``<root>/ready.json`` once it serves and logs to
    ``<root>/service.log``. It also stops on its own when this process exits without calling
    :meth:`stop` (``--parent-pid``).

    The child's stdin is ``DEVNULL``, never a pipe: on Windows, while a thread is blocked
    reading a synchronous pipe, every process started from that process inherits the pipe and
    stalls in its own startup until the read returns (an earlier stdin-EOF watcher hung the
    service's startup that way until the pipe was closed).
    """

    def __init__(self, root: str | Path, *, seed: int = 0, material: str = "Au(111)",
                 scenario: str | None = None, time_scale: float | None = None, port: int = 0,
                 extra_args: Sequence[str] = (), python: str | None = None,
                 env: dict[str, str] | None = None, startup_timeout_s: float = 300.0,
                 scrub_llm_keys: bool = True):
        self.root = Path(root).resolve()
        self.args = ["--seed", str(int(seed)), "--material", material, "--port", str(int(port))]
        if scenario:
            self.args += ["--scenario", scenario]
        if time_scale is not None:
            self.args += ["--time-scale", repr(float(time_scale))]
        self.args += list(extra_args)
        self.python = python or sys.executable
        self.base_env = env
        self.startup_timeout_s = float(startup_timeout_s)
        self.scrub_llm_keys = scrub_llm_keys
        self.token = secrets.token_hex(16)
        self.proc: subprocess.Popen | None = None
        self.info: dict = {}
        self._log = None

    @property
    def ready_file(self) -> Path:
        return self.root / READY_FILE

    @property
    def log_file(self) -> Path:
        return self.root / LOG_FILE

    @property
    def base_url(self) -> str:
        return str(self.info.get("base_url") or "")

    @property
    def ext_url(self) -> str:
        return self.base_url + "/api/ext/v1"

    @property
    def session_dir(self) -> Path:
        return Path(self.info["session_dir"])

    def child_env(self) -> dict[str, str]:
        env = dict(os.environ if self.base_env is None else self.base_env)
        if self.scrub_llm_keys:
            for key in LLM_KEY_VARS:
                env.pop(key, None)
        paths = [str(MASTV2_ROOT)]
        for name in ("stmsim", "stmbench"):
            parent = package_parent(name)
            if parent is not None and str(parent) not in paths:
                paths.append(str(parent))
        if env.get("PYTHONPATH"):
            paths.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(paths)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["MAST2_SHUTDOWN_TOKEN"] = self.token
        return env

    def log_tail(self, n: int = 60) -> str:
        try:
            lines = self.log_file.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return "(no service log)"
        return "\n".join(lines[-n:])

    def start(self) -> "SimServiceProcess":
        if self.proc is not None:
            raise SimServiceError("SimServiceProcess.start() called twice")
        self.root.mkdir(parents=True, exist_ok=True)
        self.ready_file.unlink(missing_ok=True)
        cmd = [self.python, "-X", "utf8", str(SCRIPT), "serve", "--root", str(self.root),
               "--ready-file", str(self.ready_file), "--parent-pid", str(os.getpid()), *self.args]
        self._log = open(self.log_file, "ab")
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)   # Ctrl+C stays with the parent
        try:
            self.proc = subprocess.Popen(cmd, cwd=str(self.root), env=self.child_env(),
                                         stdin=subprocess.DEVNULL, stdout=self._log,
                                         stderr=subprocess.STDOUT, creationflags=flags)
        except OSError:
            self._close()
            raise
        deadline = time.monotonic() + self.startup_timeout_s
        while not self.ready_file.is_file():
            if self.proc.poll() is not None:
                code = self.proc.returncode
                self._close()
                raise SimServiceError(f"vista_sim serve exited with {code} during startup; "
                                      f"log {self.log_file}:\n{self.log_tail()}")
            if time.monotonic() > deadline:
                self.stop()
                raise SimServiceError(f"vista_sim serve not ready after {self.startup_timeout_s:.0f} s; "
                                      f"log {self.log_file}:\n{self.log_tail()}")
            time.sleep(0.25)
        self.info = json.loads(self.ready_file.read_text(encoding="utf-8"))
        return self

    def stop(self, timeout_s: float = 60.0) -> int | None:
        """``POST /api/admin/shutdown`` (the launcher's graceful path) and wait for the exit;
        a kill only if the service does not go down by itself."""
        proc = self.proc
        if proc is None:
            return None
        if proc.poll() is None and self.base_url:
            try:
                status, body = http_json("POST", self.base_url + "/api/admin/shutdown", {},
                                         headers={"x-mast-shutdown-token": self.token},
                                         timeout=timeout_s)
                if status != 200 or not (isinstance(body, dict) and body.get("ok")):
                    log.warning("/api/admin/shutdown answered %s %r", status, body)
            except OSError as exc:
                log.warning("/api/admin/shutdown failed: %s", exc)
        try:
            proc.wait(timeout_s)
        except subprocess.TimeoutExpired:
            # a simulator, not a real controller: no Nanonis port to corrupt
            log.error("vista_sim serve still running %.0f s after shutdown; killing pid %s. "
                      "Log tail:\n%s", timeout_s, proc.pid, self.log_tail())
            proc.kill()
            proc.wait(10.0)
        self._close()
        return proc.returncode

    def _close(self) -> None:
        if self._log is not None:
            self._log.close()
            self._log = None

    def __enter__(self) -> "SimServiceProcess":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


# ── CLI ────────────────────────────────────────────────────────────────────


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _stop_when_process_exits(pid: int, stop: threading.Event) -> None:
    """Set ``stop`` once process ``pid`` is gone, so an abandoned service never outlives the
    test run that started it. On Windows it waits on a handle opened now, which a later reuse
    of the pid cannot fool. (The direct parent is not the one to watch: a venv's python.exe is
    a redirector that starts the real interpreter as its own child.)"""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        handle = kernel32.OpenProcess(0x00100000, False, pid)        # SYNCHRONIZE
        if not handle:
            log.warning("cannot watch parent pid %s (error %s)", pid, ctypes.get_last_error())
            return

        def alive() -> bool:
            return kernel32.WaitForSingleObject(handle, 0) == 0x102   # WAIT_TIMEOUT
    else:
        def alive() -> bool:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            except PermissionError:
                pass
            return True

    def _watch() -> None:
        while not stop.is_set():
            if not alive():
                log.warning("parent process %s is gone: stopping", pid)
                stop.set()
                return
            stop.wait(1.0)

    threading.Thread(target=_watch, name="mast-sim-parent-watch", daemon=True).start()


def _banner(info: dict) -> str:
    facts = info.get("facts") or {}
    bad = sorted(k for k, v in facts.items() if isinstance(v, dict) and v.get("ok") is False)
    exp = facts.get("experiment") or {}
    ports = ", ".join(f"{k} {v}" for k, v in (info.get("wire_ports") or {}).items())
    return "\n".join([
        f"MAST on stmsim is up (startup {info.get('startup_s')} s)",
        f"  MAST_URL={info['base_url']}",
        f"  ext API      {info['ext_url']}",
        f"  simulator    seed {info['seed']}, {info['material']}, time scale {info['time_scale']:g}"
        + (f", scenario {info['scenario']}" if info.get("scenario") else ""),
        f"  wire ports   {ports}",
        f"  session dir  {info['session_dir']}",
        f"  data root    {info['root']}",
        f"  scope        experiment {exp.get('experiment_id')}, sample {exp.get('sample_id')}",
        f"  facts        {'all synced' if not bad else 'NOT synced: ' + ', '.join(bad)}",
        "Ctrl+C (or POST /api/admin/shutdown) stops it gracefully.",
    ])


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="vista_sim", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the simulator and the MAST service until Ctrl+C")
    s.add_argument("--root", help="data root (default: a new temp dir, kept after exit)")
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--material", default="Au(111)")
    s.add_argument("--profile", default="reference-stm", help="stmsim rig profile")
    s.add_argument("--scenario", help="STM-Bench scenario file or Track-B id (e.g. "
                                      "P2_standing_waves_cu111); overrides --material/--profile")
    s.add_argument("--time-scale", type=float, default=None,
                   help="sim seconds per wall second (default 1, or the scenario's)")
    s.add_argument("--withdrawn", action="store_true", help="start withdrawn instead of tunnelling")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=0, help="HTTP port (0 = ephemeral; the MCP bridge "
                                                       "defaults to MAST_URL=http://127.0.0.1:7862)")
    s.add_argument("--wire-ports", default="0,0,0,0",
                   help="simulator controller ports main,monitor,data,emergency (0 = ephemeral)")
    s.add_argument("--controller-timeout", type=float, default=SIM_REPLY_TIMEOUT_S,
                   help="MAST's controller reply timeout in s")
    s.add_argument("--vision-backend", default="mock",
                   help="MAST_VISION_BACKEND for the service ('' = MAST's default)")
    s.add_argument("--ready-file", help="write the service description (JSON) here once serving")
    s.add_argument("--parent-pid", type=int,
                   help="stop when this process exits (SimServiceProcess passes its own pid)")
    s.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    svc = SimService(a.root, seed=a.seed, material=a.material, profile=a.profile,
                     scenario=a.scenario, time_scale=a.time_scale, approached=not a.withdrawn,
                     host=a.host, port=a.port,
                     wire_ports=[int(p) for p in a.wire_ports.split(",")],
                     controller_timeout_s=a.controller_timeout,
                     vision_backend=a.vision_backend or None)
    stop = threading.Event()
    if a.parent_pid:
        _stop_when_process_exits(a.parent_pid, stop)
    try:
        svc.start()
        info = svc.info()
        if a.ready_file:
            _write_json_atomic(Path(a.ready_file), info)
        # sys.__stdout__, not print()'s sys.stdout: ConnectionPool.safe_call wraps every controller
        # call in contextlib.redirect_stdout (core/connection.py), which swaps the process-global
        # sys.stdout from several threads at once and can leave it pointing at a discarded buffer
        print(_banner(info), file=sys.__stdout__, flush=True)
        while svc.serving and not stop.is_set():
            stop.wait(0.5)
    except KeyboardInterrupt:
        log.info("Ctrl+C: stopping")
    finally:
        svc.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
