"""The process's runner manager: at most one unattended run at a time (contract §4, §8).

README
------
``get_runner_manager()`` is the process singleton the HTTP routes (``mast.api.ext.runner``) use.
:class:`RunnerManager`:

* ``start(request, ctx=...)`` validates the request (task, model, effort, limits, a loopback
  ``base_url``), resolves the executables (:func:`.paths.resolve_runtime`), gets the VISTA service
  (``mast.vista.service.get_service(ctx)``, imported lazily), creates
  ``<runs_root>/<run_id>/`` and starts :class:`~.claude.ClaudeRunner` on its own thread. Refusals
  are :class:`RunnerRefused` (status, code, detail): 409 ``run_active`` when a run is active,
  400 ``invalid_request``, 503 ``runner_unavailable`` (executables, service). The run id is
  ``YYYYMMDD-HHMMSS-<6 hex>``; the run's client session at the service is ``run-<run_id>``.
* ``list()`` / ``get(run_id)``: this process's runs (live views) and earlier runs read from their
  ``status.json`` (a run that was active when MAST stopped is shown as ``state: "interrupted"``).
* ``stop(run_id, reason)``: operator stop. The service closes the session at once (it refuses that
  session's acts from now on), then the runner interrupts Claude Code and ends the run with
  ``stopped_by_operator``.
* ``hook(run_id, secret=..., event=..., payload=..., client_host=...)``: the Claude Code hook relay.
  Only from loopback, only with the run's secret (``hmac.compare_digest``), only while the run is
  active; answers ``{"output": <decision>}`` (:meth:`~.claude.ClaudeRunner.on_hook`).
* ``shutdown()``: MAST is stopping: urgent stop of the active run (registered as a runtime
  shutdown hook on the first start, and with ``atexit``). On Windows every ``claude`` process
  is also in a kill-on-close job object, so it dies with MAST even after a hard kill.

The per-run secret lives only in memory and in the ``claude`` process environment; it is never
written to the run records.
"""
from __future__ import annotations

import atexit
import hmac
import ipaddress
import json
import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from .claude import DEFAULT_BASE_URL, EFFORT_LEVELS, ClaudeRunner, RunnerConfig, process_job
from .paths import RuntimePaths, RuntimeSetupError, resolve_runtime, runs_root

logger = logging.getLogger(__name__)

MAX_TASK_CHARS = 20_000
LIST_LIMIT = 50
_RUN_ID = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-\[\]]{0,79}$")
#: Request fields that map onto :class:`RunnerConfig` (bounds checked here, the rest there).
LIMIT_FIELDS: dict[str, tuple[type, float, float]] = {
    "max_segments": (int, 1, 100),
    "turn_timeout_s": (float, 60, 86_400),
    "stall_turns": (int, 1, 100),
    "image_limit_per_session": (int, 2, 2000),
    "image_reserve": (int, 0, 1000),
    "max_budget_usd": (float, 0.01, 1000),
    "max_recoveries": (int, 0, 50),
    "compact_window_tokens": (int, 100_000, 1_000_000),
    "compact_pct": (int, 1, 100),
}


class RunnerRefused(Exception):
    """A refused runner request → HTTP ``status`` with ``{"error": code, "detail": ...}``."""

    def __init__(self, status: int, code: str, detail: str = "", **extra: Any) -> None:
        super().__init__(detail or code)
        self.status = int(status)
        self.code = code
        self.detail = detail
        self.extra = extra


@dataclass
class _Run:
    run_id: str
    runner: ClaudeRunner
    secret: str
    thread: threading.Thread | None = None
    result: Any = None
    done: threading.Event = field(default_factory=threading.Event)

    @property
    def active(self) -> bool:
        return not self.done.is_set()


def is_loopback(host: str | None) -> bool:
    h = str(host or "").strip().strip("[]").lower()
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def new_run_id() -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"


class RunnerManager:
    """See the module docstring. Everything but the run thread is safe to call from any thread."""

    def __init__(self, *, root: str | Path | None = None, service: Any = None,
                 runtime: RuntimePaths | Callable[[], RuntimePaths] | None = None,
                 config_defaults: Mapping[str, Any] | None = None,
                 runner_options: Mapping[str, Any] | None = None,
                 environ: Mapping[str, str] | None = None, use_job: bool = True,
                 runner_cls: type[ClaudeRunner] = ClaudeRunner) -> None:
        self._root = Path(root) if root is not None else None
        self._runner_cls = runner_cls
        self._service = service
        self._runtime = runtime
        self._defaults = dict(config_defaults or {})
        self._runner_options = dict(runner_options or {})
        self._environ = dict(environ) if environ is not None else None
        self._use_job = use_job
        self._runs: dict[str, _Run] = {}
        self._lock = threading.RLock()
        self._hooked_runtime: Any = None

    # ── paths and collaborators ──────────────────────────────────────────────
    @property
    def root(self) -> Path:
        return self._root if self._root is not None else runs_root()

    def _service_for(self, ctx: Any) -> Any:
        if self._service is not None:
            return self._service
        try:
            from mast.vista.service import get_service
        except ImportError as exc:
            raise RunnerRefused(503, "runner_unavailable", f"the VISTA service is not available: {exc}") from exc
        return get_service(ctx)

    def _runtime_paths(self) -> RuntimePaths:
        try:
            if isinstance(self._runtime, RuntimePaths):
                return self._runtime
            if callable(self._runtime):
                return self._runtime()
            return resolve_runtime(self._environ)
        except RuntimeSetupError as exc:
            raise RunnerRefused(503, "runner_unavailable", str(exc)) from exc

    def _register_shutdown(self, ctx: Any) -> None:
        try:
            from mast.api import direct_exec

            rt = direct_exec.live_runtime(ctx)
        except Exception:  # noqa: BLE001
            rt = None
        hook = getattr(rt, "add_shutdown_hook", None)
        if callable(hook) and rt is not self._hooked_runtime:
            try:
                hook(self.shutdown, name="vista runner")
                self._hooked_runtime = rt
            except Exception as exc:  # noqa: BLE001
                logger.warning("vista runner: shutdown hook not registered (%s)", exc)

    # ── validation ───────────────────────────────────────────────────────────
    def _config(self, request: Mapping[str, Any]) -> RunnerConfig:
        values = dict(self._defaults)
        model = request.get("model")
        if model is not None:
            if not isinstance(model, str) or not _MODEL.match(model):
                raise RunnerRefused(400, "invalid_request", f"model {model!r} is not a model name")
            values["model"] = model
        effort = request.get("effort")
        if effort is not None:
            if effort not in EFFORT_LEVELS:
                raise RunnerRefused(400, "invalid_request", f"effort must be one of {list(EFFORT_LEVELS)}")
            values["effort"] = effort
        if request.get("preflight") is not None:
            values["preflight"] = bool(request["preflight"])
        for key, (kind, lo, hi) in LIMIT_FIELDS.items():
            raw = request.get(key)
            if raw is None:
                continue
            try:
                value = kind(raw)
            except (TypeError, ValueError):
                raise RunnerRefused(400, "invalid_request", f"{key} must be a number") from None
            if isinstance(raw, bool) or not lo <= value <= hi:
                raise RunnerRefused(400, "invalid_request", f"{key} must be in [{lo:g}, {hi:g}]")
            values[key] = value
        known = {f.name for f in fields(RunnerConfig)}
        try:
            return RunnerConfig(**{k: v for k, v in values.items() if k in known})
        except (TypeError, ValueError) as exc:
            raise RunnerRefused(400, "invalid_request", str(exc)) from exc

    @staticmethod
    def _base_url(request: Mapping[str, Any]) -> str:
        raw = str(request.get("base_url") or DEFAULT_BASE_URL).strip().rstrip("/")
        try:
            parts = urlsplit(raw)
            port = parts.port
        except ValueError:
            raise RunnerRefused(400, "invalid_request", f"base_url {raw!r} is not a URL") from None
        if parts.scheme != "http" or not is_loopback(parts.hostname) or port is None \
                or parts.path not in ("", "/") or parts.query:
            raise RunnerRefused(400, "invalid_request",
                                f"base_url {raw!r} must be this MAST on loopback, like {DEFAULT_BASE_URL}")
        return raw

    # ── public API ───────────────────────────────────────────────────────────
    def start(self, request: Mapping[str, Any], *, ctx: Any = None, started_by: str = "") -> dict:
        """Start a run; returns its view. Raises :class:`RunnerRefused`."""
        client = request.get("client", "claude")
        if client != "claude":
            raise RunnerRefused(400, "invalid_request", f"client {client!r} is not supported (only 'claude')")
        task = request.get("task")
        if not isinstance(task, str) or not task.strip():
            raise RunnerRefused(400, "invalid_request", "task must be a non-empty string")
        if len(task) > MAX_TASK_CHARS:
            raise RunnerRefused(400, "invalid_request", f"task is longer than {MAX_TASK_CHARS} characters")
        cfg = self._config(request)
        base_url = self._base_url(request)
        with self._lock:
            active = self._active()
            if active is not None:
                raise RunnerRefused(409, "run_active", f"run {active.run_id} is still active; stop it first",
                                    run_id=active.run_id)
            paths = self._runtime_paths()
            service = self._service_for(ctx)
            run_id = new_run_id()
            while (self.root / run_id).exists() or run_id in self._runs:
                run_id = new_run_id()
            secret = secrets.token_urlsafe(32)
            try:
                runner = self._runner_cls(run_id=run_id, task=task, service=service, run_dir=self.root / run_id,
                                          paths=paths, cfg=cfg, base_url=base_url, hook_secret=secret,
                                          base_env=self._environ,
                                          job=process_job() if self._use_job else None,
                                          meta={"started_by": started_by or None}, **self._runner_options)
            except OSError as exc:
                raise RunnerRefused(503, "runner_unavailable",
                                    f"cannot create the run directory under {self.root}: {exc}") from exc
            run = _Run(run_id=run_id, runner=runner, secret=secret)
            run.thread = threading.Thread(target=self._drive, args=(run,), name=f"vista-runner-{run_id}",
                                          daemon=True)
            self._runs[run_id] = run
            self._register_shutdown(ctx)
            run.thread.start()
        logger.info("vista runner: run %s started (model %s, effort %s)", run_id, cfg.model, cfg.effort)
        return runner.snapshot()

    def _drive(self, run: _Run) -> None:
        try:
            run.result = run.runner.run()
        except Exception:  # noqa: BLE001 — ClaudeRunner.run never raises; this is the backstop
            logger.exception("vista runner: run %s crashed", run.run_id)
        finally:
            run.done.set()
            logger.info("vista runner: run %s finished (%s)", run.run_id, run.runner.outcome)

    def _active(self) -> _Run | None:
        with self._lock:
            return next((r for r in self._runs.values() if r.active), None)

    def wait(self, run_id: str, timeout: float | None = None) -> bool:
        """Block until the run has finished (tests, shutdown). True if it has."""
        run = self._runs.get(run_id)
        return True if run is None else run.done.wait(timeout)

    def runner(self, run_id: str) -> ClaudeRunner | None:
        run = self._runs.get(run_id)
        return run.runner if run is not None else None

    def get(self, run_id: str) -> dict | None:
        run = self._runs.get(run_id)
        if run is not None:
            return run.runner.snapshot()
        if not _RUN_ID.match(str(run_id or "")):
            return None
        return self._from_disk(self.root / run_id)

    def list(self) -> list[dict]:
        views: dict[str, dict] = {}
        try:
            dirs = sorted((p for p in self.root.iterdir() if p.is_dir() and _RUN_ID.match(p.name)),
                          key=lambda p: p.name, reverse=True)[:LIST_LIMIT]
        except OSError:
            dirs = []
        for path in dirs:
            if path.name not in self._runs:
                view = self._from_disk(path)
                if view is not None:
                    views[path.name] = view
        for run_id, run in list(self._runs.items()):
            views[run_id] = run.runner.snapshot()
        rows = sorted(views.values(), key=lambda v: str(v.get("run_id")), reverse=True)[:LIST_LIMIT]
        return [_summary(v) for v in rows]

    def _from_disk(self, path: Path) -> dict | None:
        try:
            doc = json.loads((path / "status.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(doc, dict):
            return None
        if doc.get("state") != "finished":
            doc["state"] = "interrupted"         # it was active when its MAST process ended
        for key in ("config", "runtime", "status_version"):
            doc.pop(key, None)
        doc["segments"] = [_compact_disk_segment(s) for s in doc.get("segments") or [] if isinstance(s, dict)]
        doc["current_segment"] = None
        return doc

    def stop(self, run_id: str, reason: str = "", *, urgent: bool = False) -> dict:
        """Operator stop. Raises :class:`RunnerRefused` 404 for an unknown run; a finished run is
        returned as it is."""
        run = self._runs.get(run_id)
        if run is None:
            view = self.get(run_id)
            if view is None:
                raise RunnerRefused(404, "unknown_run", f"no run {run_id!r}")
            return view
        reason = str(reason or "operator stop").strip()[:300]
        if run.active:
            runner = run.runner
            try:
                runner.svc.close_session(runner.session, f"stopped by the operator: {reason}")
            except Exception as exc:  # noqa: BLE001 — the interrupt below still stops the run
                logger.warning("vista runner: close_session failed for %s: %s", run_id, exc)
            runner.request_stop(reason, urgent=urgent)
            logger.info("vista runner: stop requested for %s (%s)", run_id, reason)
        return run.runner.snapshot()

    def hook(self, run_id: str, *, secret: str | None, event: str | None, payload: Mapping[str, Any],
             client_host: str | None) -> dict:
        """The hook relay (see the module docstring). Raises :class:`RunnerRefused`."""
        if not is_loopback(client_host):
            raise RunnerRefused(403, "loopback_only", "the hook endpoint answers loopback clients only")
        run = self._runs.get(run_id)
        if run is None or not secret or not hmac.compare_digest(str(secret).encode("utf-8"),
                                                                run.secret.encode("utf-8")):
            # one answer for "no such run" and "wrong secret": the endpoint confirms nothing
            raise RunnerRefused(403, "unknown_run_or_secret", "no active run with this id and secret")
        if not run.active:
            raise RunnerRefused(409, "run_finished", f"run {run_id} has finished")
        return {"output": run.runner.on_hook(event, payload)}

    def shutdown(self, timeout_s: float = 15.0) -> None:
        """MAST is stopping: urgent stop of the active run, bounded wait."""
        run = self._active()
        if run is None:
            return
        try:
            self.stop(run.run_id, "MAST is shutting down", urgent=True)
        except RunnerRefused:
            return
        run.done.wait(timeout_s)


def _compact_disk_segment(record: Mapping[str, Any]) -> dict:
    from .claude import compact_record

    return compact_record(record)


def _summary(view: Mapping[str, Any]) -> dict:
    """One row of ``GET /runner``."""
    keys = ("run_id", "session", "client", "state", "outcome", "error", "stop", "model", "effort",
            "created_at", "ended_at", "started_by", "usage", "task")
    row = {k: view.get(k) for k in keys}
    row["task"] = str(view.get("task") or "")[:200]
    row["segments"] = len(view.get("segments") or []) + (1 if view.get("current_segment") else 0)
    row["unmatched_tool_calls"] = len(view.get("unmatched_tool_calls") or [])
    return row


_MANAGER: RunnerManager | None = None
_MANAGER_LOCK = threading.Lock()


def get_runner_manager() -> RunnerManager:
    """The process's :class:`RunnerManager` (created on first use)."""
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            _MANAGER = RunnerManager()
            atexit.register(_MANAGER.shutdown, 5.0)
        return _MANAGER


__all__ = ["RunnerManager", "RunnerRefused", "get_runner_manager", "is_loopback", "new_run_id"]
