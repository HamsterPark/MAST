"""VistaService: the VISTA visual harness inside the MAST server (one per process).

README
------
What VISTA-SPM's ``VistaHost`` did for one simulator episode, this service does for the
instrument MAST controls, behind ``/api/ext/v1/vista/*`` (design contract
``docs/v2/architecture/mast7_vista.md``):

* **Workspaces** (:mod:`mast.vista.workspace`): archive, notes and events per experiment.
* **One act, one observation** (:mod:`mast.vista.gate`): every observation carries a token,
  the next act must quote it, at most one act per Codex code cell, frames from outside
  invalidate the token.
* **Acts are jobs.** An accepted act runs as a ``kind="act"`` job of the ext
  ``JobManager`` (idempotency, cancel, journal, busy refusal, shutdown all apply). The job
  runs VISTA's :class:`~mast.vista.actions.ActionRunner` (or one raw skill) with every step
  through ``direct_exec.run_and_record`` (recorded like any ext job, and the act job reports
  what its steps recorded), then ingests the new files from the controller's session
  directory, reads the state back and mints the token of the resulting observation. A scan
  that stopped before its frame was complete leaves its rows as a partial frame
  (:meth:`VistaService._salvage_partial`).
* **Frames from outside** (UI, operator, built-in agents, other clients) are archived by a
  watcher thread with ``origin="external"`` and invalidate the live token.
* **Waits and detectors** are published to :mod:`mast.vista.wait` and
  :mod:`mast.vista.detectors`; the runner uses the checkpoint methods at the bottom.

The service never imports :mod:`mast.api.ext` (the ext layer binds it with
:meth:`VistaService.bind`); it talks to the instrument only through ``direct_exec``.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .actions import ACTIONS, ActionError, ActionOutcome, ActionRunner, validate_params
from .gate import ObservationGate, Refusal
from .protocol import ToolReply, compact_json, error_reply
from .state import CURRENT_SETTLE_BUDGET_S, optical_echo, state_echo
from .workspace import GUIDE_SHARED_NAME, Workspace, public_event, workspace_id_for

log = logging.getLogger(__name__)

DIR_ENV = "MAST_VISTA_DIR"
STRICT_ENV = "MAST_VISTA_STRICT"
#: fallback when the controller cannot tell where it saves (tests, simulators)
SESSION_DIR_ENV = "MAST_VISTA_SESSION_DIR"
#: the watcher runs while a gated client was active this recently
ACTIVE_CLIENT_S = 15 * 60
#: client sessions remembered at most; idle ones are dropped first (the header is caller-chosen)
MAX_CLIENTS = 256
#: an observe is logged in events.jsonl at most this often per session (it is not throttled)
OBSERVE_LOG_EVERY_S = 5.0
OBSERVATIONS_KEPT = 256
RESULT_CAP_CHARS = 8000
ACT_WAIT_MAX_S = 50.0
FRAME_SUFFIXES = (".sxm", ".dat")
NOTES = ("guide", "working")
#: ``Scan_Save`` budget when a stopped scan's rows are saved; below the controller socket's
#: receive timeout, so a slow save comes back as "timed out" instead of breaking the connection
SALVAGE_SAVE_TIMEOUT_MS = 4000


class VistaError(Exception):
    """A refusal or bad request; the ext layer turns it into ``{"error", "detail", ...}``."""

    def __init__(self, status: int, code: str, detail: str = "", **extra: Any) -> None:
        super().__init__(detail or code)
        self.status = int(status)
        self.code = code
        self.detail = detail
        self.extra = extra

    def body(self) -> dict:
        return {"error": self.code, "detail": self.detail, **self.extra}


@dataclass
class ClientSession:
    session: str
    actor: str
    client_meta: dict = field(default_factory=dict)
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    closed: str | None = None
    checkpoint: str = "none"              # none | requested | ready
    checkpoint_reason: str | None = None
    acts: int = 0
    observations: int = 0
    refusals: int = 0
    last_observe_logged: float = 0.0

    def view(self) -> dict:
        return {"session": self.session, "actor": self.actor, "client": self.client_meta.get("client"),
                "model": (self.client_meta.get("turn") or {}).get("model")
                if isinstance(self.client_meta.get("turn"), dict) else None,
                "first_seen": round(self.first_seen, 1), "last_seen": round(self.last_seen, 1),
                "closed": self.closed, "checkpoint": self.checkpoint, "acts": self.acts,
                "observations": self.observations, "refusals": self.refusals}


@dataclass
class Observation:
    obs_id: str
    session: str
    workspace: str
    kind: str                       # act | observe | recovery | wait
    turn: int
    payload: dict
    frames: list[str]
    token: str | None = None
    created: float = field(default_factory=time.time)
    #: ``(label, png)`` pictures that are not archive frames: the optical station's newest
    #: spectrum (``mast.vista.optical``). Empty on an STM station.
    pictures: list = field(default_factory=list)


@dataclass(frozen=True)
class _Caller:
    """Minimal caller for internal use (runner recovery); same attributes as the ext Caller."""

    actor: str
    session: str = ""

    @property
    def agent_id(self) -> str:
        return f"ext:{self.actor}"

    @property
    def owner(self) -> str:
        return f"外部 agent {self.agent_id}"

    @property
    def thread_id(self) -> str:
        return f"{self.agent_id}/{self.session}" if self.session else self.agent_id


class _StepContext:
    """What :class:`ActionRunner` calls ``ctx``: every step through ``run_and_record``.

    It also keeps, per step, what a plain skill job reports for its one skill (the record
    ids, the Nanonis call count, the parameters as executed), so an act job reports them too
    (:meth:`recorded`, :meth:`nanonis_calls`, :meth:`step_params`)."""

    def __init__(self, ec: Any, *, runtime: Any, caller: Any, job_id: str,
                 disabled: frozenset[str] = frozenset()) -> None:
        self.ec = ec
        self.runtime = runtime
        self.caller = caller
        self.job_id = job_id
        self.disabled = disabled
        self.refused_by: str | None = None
        self.busy_holder: Any = None
        self.n = 0
        self.records: list[dict] = []

    def run(self, name: str, params: dict | None = None):
        from mast.api import direct_exec

        self.n += 1
        if name in self.disabled:
            self.refused_by = self.refused_by or "skill_disabled"
            return _Failed(f"{name} is switched off on this instrument (hardware module absent "
                           "or the capability is not granted); the operator decides that.")
        run = direct_exec.run_and_record(
            self.ec, name, dict(params or {}), runtime=self.runtime,
            agent_id=self.caller.agent_id, thread_id=self.caller.thread_id,
            tool_call_id=f"{self.job_id}.{self.n}", context=self.caller.thread_id,
            approval_source=direct_exec.APPROVAL_LLM)
        self.note(name, run)
        if run.refused_by and not self.refused_by:
            self.refused_by = run.refused_by
            if run.refused_by == "busy":
                self.busy_holder = direct_exec.jsonable(getattr(run, "busy_holder", None))
        return run

    def note(self, name: str, run: Any) -> None:
        """Keep one executed step's accounting (``run``: a ``direct_exec.DirectRun``)."""
        from mast.api import direct_exec

        self.records.append({
            "skill": name, "success": bool(getattr(run, "success", False)),
            "recorded": dict(direct_exec.jsonable(getattr(run, "recorded", None) or {})),
            "nanonis_calls": int(getattr(run, "nanonis_calls", 0) or 0),
            "params": direct_exec.jsonable(getattr(run, "params", None) or {}),
            "elapsed_s": round(float(getattr(run, "elapsed_s", 0.0) or 0.0), 3)})

    def recorded(self) -> dict:
        """A plain skill job's ``recorded`` shape, over every step: ``v1`` true only if every
        recorded step was written to the legacy record; the last step's ``v2_action_id`` (all
        of them in ``v2_action_ids``); the scope ids; per step in ``steps``. Empty when nothing
        was recorded (no runtime, or no step ran)."""
        rows = [r for r in self.records if r["recorded"]]
        if not rows:
            return {}
        ids = [r["recorded"].get("v2_action_id") for r in rows if r["recorded"].get("v2_action_id")]
        # A step whose record is still being written by the background writer
        # (``pending``) is neither written nor failed: ``v1`` is then
        # None ("not known yet") unless some finished step already says False.
        pending = sum(1 for r in rows if r["recorded"].get("pending"))
        done = [r for r in rows if not r["recorded"].get("pending")]
        v1: "bool | None" = all(bool(r["recorded"].get("v1")) for r in done)
        if pending and v1:
            v1 = None
        out: dict = {"v1": v1, "v2_action_id": ids[-1] if ids else None, "v2_action_ids": ids}
        if pending:
            out["pending"] = pending
        for key in ("experiment_id", "sample_id"):
            vals = [r["recorded"].get(key) for r in rows if r["recorded"].get(key)]
            if vals:
                out[key] = vals[-1]
        errors = [str(r["recorded"]["error"]) for r in rows if r["recorded"].get("error")]
        if errors:
            out["error"] = errors[-1]
        out["steps"] = [{"skill": r["skill"], "v1": r["recorded"].get("v1"),
                         "v2_action_id": r["recorded"].get("v2_action_id")} for r in rows]
        return out

    def nanonis_calls(self) -> int:
        return sum(r["nanonis_calls"] for r in self.records)

    def step_params(self) -> list[dict]:
        """Each executed step and its parameters as executed (SI, after string coercion)."""
        return [{"skill": r["skill"], "params": r["params"]} for r in self.records]


@dataclass
class _Failed:
    error: str
    success: bool = False
    data: dict = field(default_factory=dict)
    summary: str = ""
    refused_by: str | None = "skill_disabled"


def _listing(d: Path | None) -> dict[str, tuple[int, float]]:
    out: dict[str, tuple[int, float]] = {}
    if d is None:
        return out
    try:
        with os.scandir(d) as it:
            for e in it:
                if os.path.splitext(e.name)[1].lower() in FRAME_SUFFIXES and e.is_file():
                    st = e.stat()
                    out[e.name] = (int(st.st_size), float(st.st_mtime))
    except OSError:
        pass
    return out


def _cap(data: Any, limit: int = RESULT_CAP_CHARS) -> Any:
    try:
        text = json.dumps(data, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return {"_unserialisable": True}
    if len(text) <= limit:
        return json.loads(text)
    keys = list(data)[:60] if isinstance(data, dict) else []
    return {"_truncated": True, "chars": len(text), "keys": keys}


def _clean_meta(meta: Any) -> dict:
    """``client_meta`` from the bridge: small, JSON-safe, nothing executable."""
    if not isinstance(meta, dict):
        return {}
    out: dict = {}
    for key in ("item_id", "call_id", "tool_use_id"):
        v = meta.get(key)
        if isinstance(v, (str, int, float)) and str(v).strip():
            out[key] = str(v).strip()[:200]
    client = meta.get("client")
    if isinstance(client, dict):          # the bridge forwards initialize.clientInfo
        parts = [str(client.get(k)).strip()[:80] for k in ("name", "version")
                 if isinstance(client.get(k), (str, int, float)) and str(client.get(k)).strip()]
        if parts:
            out["client"] = " ".join(parts)
    elif isinstance(client, (str, int, float)) and str(client).strip():
        out["client"] = str(client).strip()[:200]
    turn = meta.get("turn")
    if isinstance(turn, dict):
        out["turn"] = {str(k)[:40]: (str(v)[:120] if not isinstance(v, (int, float, bool)) else v)
                       for k, v in list(turn.items())[:12]}
    return out


def _fingerprint(label: str, params: Any) -> str:
    """Idempotency fingerprint, same recipe as the ext job manager's."""
    import hashlib
    raw = json.dumps({"skill": label, "params": params}, sort_keys=True, ensure_ascii=False,
                     default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


class VistaService:
    """See the module docstring. Thread-safe; one instance per process (:func:`get_service`)."""

    def __init__(self, *, root: str | Path | None = None, strict: bool | None = None,
                 watch_interval_s: float = 2.0, start_watcher: bool = True) -> None:
        self._root_override = Path(root) if root else None
        self._strict_override = strict
        self.watch_interval_s = float(watch_interval_s)
        self._start_watcher = bool(start_watcher)
        self._lock = threading.RLock()
        self._watch_lock = threading.Lock()
        self._ctx: Any = None
        self._jobs: Any = None
        self._disabled: Callable[[], frozenset[str]] | None = None
        self._reset_state()
        self._stop = threading.Event()
        self._watcher: threading.Thread | None = None

    def _reset_state(self) -> None:
        self.gate = ObservationGate()
        self._root: Path | None = None
        self._workspaces: dict[str, Workspace] = {}
        self._detectors: dict[str, Any] = {}
        self._clients: dict[str, ClientSession] = {}
        self._observations: OrderedDict[str, Observation] = OrderedDict()
        self._hub: Any = None
        self._known: set[str] = set()
        self._pending: dict[str, tuple[int, float]] = {}
        self._watch_dir: Path | None = None
        self.watch_errors = 0

    # ── binding ─────────────────────────────────────────────────────────────
    def bind(self, ctx: Any, *, jobs: Any = None,
             disabled: Callable[[], frozenset[str]] | None = None) -> "VistaService":
        """Attach the live MAST context and the ext job manager. A different context means a
        different instrument (or a test): all state is reset."""
        with self._lock:
            if ctx is not self._ctx and self._ctx is not None:
                self._close_detectors()
                self._reset_state()
            self._ctx = ctx
            if jobs is not None:
                self._jobs = jobs
            if disabled is not None:
                self._disabled = disabled
            self._root = None
        if self._start_watcher:
            self._ensure_watcher()
        return self

    @property
    def ctx(self) -> Any:
        return self._ctx

    @property
    def jobs(self) -> Any:
        return self._jobs

    @property
    def strict(self) -> bool:
        if self._strict_override is not None:
            return bool(self._strict_override)
        return _truthy_env(STRICT_ENV)

    @property
    def root(self) -> Path:
        with self._lock:
            if self._root is None:
                if self._root_override is not None:
                    self._root = self._root_override
                elif os.environ.get(DIR_ENV, "").strip():
                    self._root = Path(os.environ[DIR_ENV].strip()).expanduser()
                else:
                    from mast._runtime_paths import project_root
                    self._root = project_root() / "experiments" / "vista"
                self._root.mkdir(parents=True, exist_ok=True)
            return self._root

    def _runtime(self) -> Any:
        from mast.api import direct_exec
        return direct_exec.live_runtime(self._ctx)

    @staticmethod
    def _optical() -> bool:
        """Optical station profile (``mast.station``): ``observe`` shows spectra and the
        optical state instead of scans and the Nanonis readings. Never true on an STM
        station, and read live from the process profile, so a unit test can flip it."""
        try:
            from mast import station
            return station.is_optical()
        except Exception:  # noqa: BLE001 - no profile information: STM, as before
            return False

    def _disabled_names(self) -> frozenset[str]:
        if self._disabled is None:
            return frozenset()
        try:
            return frozenset(self._disabled())
        except Exception:  # noqa: BLE001
            return frozenset()

    # ── wait hub / detectors (optional modules) ─────────────────────────────
    @property
    def hub(self) -> Any:
        with self._lock:
            if self._hub is None:
                try:
                    from .wait import WaitHub, default_wishlist_lookup
                    # the operator answers on the UI's board: without the lookup an
                    # operator_reply wait would only fire on an explicit publish
                    self._hub = WaitHub(job_lookup=self._job_lookup,
                                        wishlist_lookup=default_wishlist_lookup)
                    try:
                        self._hub.attach_event_bus()
                    except Exception as exc:  # noqa: BLE001
                        log.warning("vista: wait hub could not subscribe to the event bus (%s)", exc)
                except Exception as exc:  # noqa: BLE001
                    log.warning("vista: wait hub unavailable (%s)", exc)
                    return None
            return self._hub

    def _job_lookup(self, job_id: str) -> Any:
        jm = self._jobs
        return jm.get(job_id) if jm is not None else None

    def _publish(self, kind: str, event: dict) -> None:
        hub = self.hub
        if hub is None:
            return
        try:
            hub.publish(kind, event)
        except Exception as exc:  # noqa: BLE001
            log.warning("vista: publish %s failed (%s)", kind, exc)

    def detectors(self, ws: Workspace | None = None) -> Any:
        """The detector manager of a workspace (default: the active one)."""
        ws = ws or self.workspace()
        with self._lock:
            mgr = self._detectors.get(ws.id)
            if mgr is None:
                from .detectors import DetectorManager

                def log_event(ev: dict, ws: Workspace = ws) -> None:
                    hit = ev.get("kind") == "hit"
                    ws.append_event({"kind": "detector_hit" if hit else "detector_disabled",
                                     "turn": ws.turn, "detector": ev.get("name"),
                                     "detector_id": ev.get("detector_id"), "frame": ev.get("fid"),
                                     "message": ev.get("message") if hit else ev.get("reason"),
                                     "suppressed": bool(ev.get("suppressed"))})

                mgr = DetectorManager(ws.dir / "detectors.json", hub=self.hub,
                                      workspace_id=ws.id, on_event=log_event)
                self._detectors[ws.id] = mgr
            return mgr

    def _close_detectors(self) -> None:
        for mgr in list(self._detectors.values()):
            try:
                mgr.close()
            except Exception:  # noqa: BLE001
                pass
        self._detectors.clear()

    # ── workspaces ──────────────────────────────────────────────────────────
    def _experiment_id(self) -> Any:
        try:
            from mast.logging.experiment_log import get_active_log
            lg = get_active_log()
            return getattr(lg, "current_experiment_id", None) if lg is not None else None
        except Exception:  # noqa: BLE001
            return None

    def workspace(self) -> Workspace:
        wid = workspace_id_for(self._experiment_id())
        with self._lock:
            ws = self._workspaces.get(wid)
            if ws is None:
                ws = Workspace(self.root, wid, guide_path=self.root / GUIDE_SHARED_NAME)
                self._workspaces[wid] = ws
            return ws

    def _workspace_by_id(self, wid: str) -> Workspace:
        with self._lock:
            return self._workspaces.get(wid) or self.workspace()

    # ── clients ─────────────────────────────────────────────────────────────
    def register(self, caller: Any, client_meta: Any = None) -> ClientSession:
        meta = _clean_meta(client_meta)
        with self._lock:
            cs = self._clients.get(caller.session)
            if cs is None:
                if len(self._clients) >= MAX_CLIENTS:
                    self._evict_idle_clients_locked()
                cs = ClientSession(session=caller.session, actor=caller.actor, client_meta=meta)
                self._clients[caller.session] = cs
            cs.last_seen = time.time()
            if meta.get("client"):
                cs.client_meta["client"] = meta["client"]
            if meta.get("turn"):
                cs.client_meta["turn"] = meta["turn"]
        self._ensure_watcher()
        return cs

    def _evict_idle_clients_locked(self) -> None:
        """Drop the longest-idle sessions (never a closed one: its refusals must stick)."""
        now = time.time()
        idle = sorted((c for c in self._clients.values()
                       if not c.closed and now - c.last_seen >= ACTIVE_CLIENT_S),
                      key=lambda c: c.last_seen)
        for c in idle:
            if len(self._clients) < MAX_CLIENTS:
                break
            self._clients.pop(c.session, None)

    def is_gated(self, session: str) -> bool:
        with self._lock:
            return bool(session) and session in self._clients

    def _clients_active(self) -> bool:
        now = time.time()
        with self._lock:
            return any(now - c.last_seen < ACTIVE_CLIENT_S and not c.closed
                       for c in self._clients.values())

    def hello(self, caller: Any, client_meta: Any = None) -> dict:
        cs = self.register(caller, client_meta)
        ws = self.workspace()
        notes = ws.notes.snapshot()
        return {"session": cs.session, "actor": cs.actor, "workspace": ws.id, "turn": ws.turn,
                "strict": self.strict, "gate": self.gate.status(),
                "frames": len(ws.archive.frames(kind="s")), "spectra": len(ws.archive.frames(kind="d")),
                "notes": {"guide_chars": len(notes.get("guide") or ""),
                          "working_exists": notes.get("working") is not None}}

    def _check_session(self, caller: Any, *, notes_ok: bool = False) -> ClientSession:
        cs = self.register(caller)
        if cs.checkpoint == "requested" and not notes_ok:
            from .gate import CHECKPOINT_REQUESTED, REFUSAL_TEXT
            cs.refusals += 1
            raise VistaError(409, CHECKPOINT_REQUESTED, REFUSAL_TEXT[CHECKPOINT_REQUESTED])
        return cs

    # ── skills ──────────────────────────────────────────────────────────────
    def _registry(self) -> Any:
        ctx = self._ctx
        return getattr(ctx, "skill_registry", None) or getattr(ctx, "registry", None)

    def is_instrument_driving(self, skill: str) -> bool | None:
        """True if the skill takes the instrument token; None if the skill is unknown."""
        reg = self._registry()
        if reg is None:
            return None
        try:
            cls = reg.get(skill)
        except Exception:  # noqa: BLE001
            # MAST's instrument-agent tools (mast.api.ext.agent_tools) are not registry skills
            # and never drive the instrument: they run as plain jobs, like reads
            try:
                from mast.api.ext.agent_tools import is_offered

                if is_offered(skill):
                    return False
            except Exception:  # noqa: BLE001
                pass
            return None
        try:
            meta = reg._get_metadata(cls)       # effective metadata, admin overrides included
        except Exception:  # noqa: BLE001
            meta = None                         # needs_token: unknown metadata → driving
        from mast.core.instrument_lock import BYPASS_TAGS, needs_token
        tags = {str(t).lower() for t in (getattr(meta, "tags", None) or ())}
        if {"composite", "spec"} <= tags and tags & BYPASS_TAGS:
            # a declarative composite never earns the remedy status (no lock, no token) from
            # tags its author chose; only built-in stop / retract skills have it
            return True
        return bool(needs_token(meta, skill))

    # ── acts ────────────────────────────────────────────────────────────────
    def submit_act(self, caller: Any, body: dict) -> tuple[Any, dict]:
        """Validate, check the gate, submit. Returns ``(job, {"kind", "replay"})``; raises
        :class:`VistaError` (refusals carry a recovery observation where the contract says)."""
        jm = self._jobs
        if jm is None:
            raise VistaError(503, "not_wired", "job manager not wired")
        action = str(body.get("action") or "").strip() or None
        skill = str(body.get("skill") or "").strip() or None
        if bool(action) == bool(skill):
            raise VistaError(422, "invalid_request", "give exactly one of `action` or `skill`")
        if action and self._optical():
            # The named actions (scan, withdraw, set_bias, ...) are the STM's vocabulary. An
            # optical station has no such instrument: say so and point at the right door,
            # before the observation token is touched (this refusal costs nothing).
            raise VistaError(
                422, "not_available_on_this_station",
                f"Action {action!r} belongs to the STM's vocabulary; this is an optical station "
                "(no STM / Nanonis). Run optical skills with mast_run (find them with "
                "mast_find_skills, read mast_skill_card first); an instrument-driving skill still "
                "needs the `after` token of your latest observation.",
                station="optical")
        meta = _clean_meta(body.get("client_meta"))
        cs = self.register(caller, meta)
        request_id = str(body.get("request_id") or "").strip()[:120] or None
        note = str(body.get("note") or "")[:1000]
        skill_params = body.get("skill_params") if skill else None
        if skill and skill_params is not None and not isinstance(skill_params, dict):
            raise VistaError(422, "invalid_request", "skill_params must be an object")

        if skill:
            driving = self.is_instrument_driving(skill)
            if driving is False:
                # reads, analysis and stop/retract remedies are never gated
                job, replay = jm.submit(self._ctx, caller, skill, dict(skill_params or {}),
                                        request_id=request_id, note=note)
                return job, {"kind": "job", "replay": replay}

        fp = _fingerprint(f"act:{action or skill}",
                          {"action": action, "params": body.get("params"), "skill": skill,
                           "skill_params": skill_params})
        if request_id:
            prior = jm.find_request(caller.actor, request_id)
            if prior is not None:
                if prior.fp and prior.fp != fp:
                    raise VistaError(409, "request_id_conflict",
                                     f"request_id {request_id!r} was used for a different submission.",
                                     existing_job_id=prior.job_id)
                return prior, {"kind": "act" if prior.kind == "act" else "job", "replay": True}

        spec = None
        params: dict = {}
        if action:
            spec = ACTIONS.get(action)
            if spec is None:
                raise VistaError(422, "unknown_action", f"unknown action {action!r}",
                                 actions=sorted(ACTIONS))
            try:
                params = validate_params(spec, body.get("params"))
            except ActionError as exc:
                raise VistaError(422, "invalid_params", str(exc), action=action) from None
        else:
            jm.door(self._ctx, skill)        # unknown / disabled skill → ExtError from the door

        ws = self.workspace()
        res = self.gate.acquire(session=caller.session, after=body.get("after"),
                                item_id=meta.get("item_id"),
                                # "ready": WORKING.md is saved and the runner is about to start
                                # a fresh session; an act now would run in the old one
                                checkpoint=cs.checkpoint in ("requested", "ready"),
                                closed=bool(cs.closed))
        if isinstance(res, Refusal):
            cs.refusals += 1
            ws.append_event({"kind": "refusal", "code": res.code, "session": caller.session,
                             "turn": ws.turn, "action": action, "skill": skill, "client": meta})
            extra: dict = {}
            if res.job_id:
                extra["job_id"] = res.job_id
            if res.recover:
                extra["observation"] = self._observe(caller, kind="recovery")
            raise VistaError(409, res.code, res.detail, **extra)

        turn = ws.next_turn()
        label = f"act:{action}" if action else skill
        job_params = params if action else dict(skill_params or {})

        def run_body(job: Any, ec: Any) -> dict:
            return self._act_body(job, ec, ws=ws, turn=turn, grant=res, caller=caller,
                                  spec=spec, params=job_params, skill=skill, note=note, meta=meta)

        try:
            job, replay = jm.submit_act(self._ctx, caller, label=label, params=job_params,
                                        body=run_body, request_id=request_id, note=note, fp=fp,
                                        extra={"turn": turn, "workspace": ws.id, "action": action,
                                               "skill": skill})
        except BaseException:
            self.gate.abort(res)
            raise
        self.gate.bind_job(res.ticket, job.job_id)
        cs.acts += 1
        return job, {"kind": "act", "replay": replay}

    def _act_body(self, job: Any, ec: Any, *, ws: Workspace, turn: int, grant: Any, caller: Any,
                  spec: Any, params: dict, skill: str | None, note: str, meta: dict) -> dict:
        from mast.api import direct_exec

        t0 = time.monotonic()
        t_wall0 = time.time()
        optical = self._optical()
        try:
            # Optical station: no controller save folder to watch. What an act produces is
            # spectra, found afterwards by comparing the experiment folder with this list.
            sdir = None if optical else self._session_dir(fresh=True)
            before = _listing(sdir)
            spectra_before = None
            if optical:
                from . import optical as optical_view
                spectra_before = optical_view.listing_keys()
            steps = _StepContext(ec, runtime=self._runtime(), caller=caller, job_id=job.job_id,
                                 disabled=self._disabled_names())
            if spec is not None:
                def on_plan(plan: dict, _job: Any = job, _action: str = spec.name) -> None:
                    self._note_plan(_job, _action, params, plan)

                # ``scan`` saves explicitly only when the controller did not autosave: ask for
                # the folder afresh each time, it may only just have been created
                runner = ActionRunner(steps, resolve_point=ws.resolve_point, time_scale=1.0,
                                      is_qplus=True,
                                      list_files=lambda: set(_listing(self._session_dir(fresh=True))),
                                      on_plan=on_plan)
                outcome = runner.run(spec, params)
            else:
                run = steps.run(skill, params)
                ok = bool(getattr(run, "success", False))
                err = str(getattr(run, "error", "") or "")
                outcome = ActionOutcome(ok, None if ok else (err or "failed"),
                                        facts=_cap(direct_exec.jsonable(getattr(run, "data", {}) or {})))
                outcome.steps = [{"skill": skill, "success": ok, "error": err}]
            sdir_after = (None if optical else self._session_dir(fresh=True)) or sdir
            if sdir_after is not None and sdir_after != sdir:
                # the save folder changed (or first appeared) during the act: what was written
                # there since the act started is the act's
                before = {n: st for n, st in _listing(sdir_after).items()
                          if st[1] < t_wall0 - 1.0}
                sdir = sdir_after
            new_entries = self._ingest(ws, turn, sdir, before, origin="act") if sdir else []
            partial_note = None
            if outcome.salvage and sdir is not None and not any(e.kind == "s" for e in new_entries):
                salvaged, partial_note, step = self._salvage_partial(ws, turn, sdir, caller=caller,
                                                                     steps=steps)
                new_entries += salvaged
                outcome.steps.append(step)
            state = self._read_state(caller, settle=True)
            elapsed = round(time.monotonic() - t0, 2)
            payload: dict = {"turn": turn, "action": spec.name if spec is not None else "skill"}
            if skill:
                payload["skill"] = skill
            payload["ok"] = bool(outcome.ok)
            if outcome.error:
                payload["error"] = outcome.error
            if steps.refused_by == "busy":
                payload["busy"] = ("someone else holds the instrument; the refused step did not "
                                   "execute")
                if steps.busy_holder:
                    payload["busy_holder"] = steps.busy_holder
            if outcome.resolved:
                payload["resolved"] = outcome.resolved
            if outcome.facts:
                payload["result"] = outcome.facts
            if partial_note:
                payload["partial_frame"] = partial_note
            pictures: list = []
            if optical:
                new_spectra, latest, pictures = self._optical_act_view(spectra_before)
                payload["new_spectra"] = new_spectra
                if not new_spectra:
                    payload["latest_frame"] = latest
            else:
                payload["new_frames"] = [e.summary() for e in new_entries]
                if not new_entries:
                    # what observe calls latest_frame: the newest scan or spectrum, as a summary
                    newest = self._newest_frame(ws)
                    payload["latest_frame"] = newest.summary() if newest is not None else None
                if sdir is None:
                    payload["frames_unavailable"] = ("the controller did not report its save "
                                                     "directory; new files could not be archived")
            payload["instrument"] = state
            payload["elapsed_s"] = elapsed
            ws.append_event({"kind": "act", "turn": turn, "session": caller.session,
                             "actor": caller.actor, "job_id": job.job_id,
                             "action": payload["action"], "skill": skill, "params": params,
                             "note": note, "ok": bool(outcome.ok), "error": outcome.error,
                             "facts": outcome.facts, "resolved": outcome.resolved,
                             "steps": outcome.steps, "new_frames": [e.fid for e in new_entries],
                             "state": state, "elapsed_s": elapsed, "client": meta})
            obs = self._remember(Observation(obs_id=f"o{secrets.token_hex(5)}", session=caller.session,
                                             workspace=ws.id, kind="act", turn=turn, payload=payload,
                                             frames=self._shown(new_entries), pictures=pictures))
            obs.token = self.gate.complete(grant.ticket, caller.session, obs.obs_id)
            with self._lock:
                cs = self._clients.get(caller.session)
                if cs is not None:
                    cs.observations += 1
            for e in new_entries:
                self._frame_archived(ws, e, origin="act")
            return {"success": bool(outcome.ok),
                    "summary": f"turn {turn}: {payload['action']}{'' if outcome.ok else ' failed'}",
                    "error": outcome.error or "",
                    "data": {"vista": {"obs_id": obs.obs_id, "turn": turn,
                                       "new_frames": [e.fid for e in new_entries]}},
                    "refused_by": steps.refused_by, "busy_holder": steps.busy_holder,
                    "recorded": steps.recorded(), "nanonis_calls": steps.nanonis_calls(),
                    "params_used": self._params_used(spec, skill, params, outcome, steps)}
        finally:
            # an act that died before its observation leaves no token: the next act is refused
            # with the newest observation (observe_first), which is the safe direction
            self.gate.release(grant.ticket)

    @staticmethod
    def _note_plan(job: Any, action: str, params: dict, plan: dict) -> None:
        """A running act's resolved plan, visible on its job while it runs: ``params_used``,
        and for a scan the estimated duration the job view turns into ``eta_s``."""
        from mast.api import direct_exec

        job.params_used = {"action": action, "params": direct_exec.jsonable(params),
                           "plan": direct_exec.jsonable(plan)}
        est = (plan.get("scan") or {}).get("estimated_scan_s") if isinstance(plan, dict) else None
        if isinstance(est, (int, float)) and not isinstance(est, bool) and est > 0:
            job.estimate_s = float(est)

    @staticmethod
    def _params_used(spec: Any, skill: str | None, params: dict, outcome: ActionOutcome,
                     steps: _StepContext) -> dict | None:
        """What an act job reports as ``params_used``: a raw skill's parameters as executed
        (as a plain skill job reports them); for an action, the validated parameters, what they
        resolved to, the action's plan, and every step's parameters as executed."""
        from mast.api import direct_exec

        executed = steps.step_params()
        if spec is None:
            return executed[0]["params"] if executed else None
        out: dict = {"action": spec.name, "params": direct_exec.jsonable(params)}
        if outcome.resolved:
            out["resolved"] = direct_exec.jsonable(outcome.resolved)
        if outcome.plan:
            out["plan"] = direct_exec.jsonable(outcome.plan)
        out["steps"] = executed
        return out

    def _salvage_partial(self, ws: Workspace, turn: int, sdir: Path, *, caller: Any,
                         steps: _StepContext) -> tuple[list, str, dict]:
        """Save the rows a stopped scan acquired and archive them as a partial frame.

        A scan act that is cancelled, times out or is halted from outside leaves the rows it
        acquired in the controller's buffer; without this they were lost (``new_frames: []``).
        ``SaveScan`` (Nanonis ``Scan_Save``) writes the buffer to a file, which is archived as
        ``tNNNN.p0`` through the same loader as every saved scan. The runner asks for this only
        when ScanAt's own record shows ``start_scan`` ran, so the buffer is this frame's, not the
        previous one's.

        Its own execution context, on purpose: a cancelled act's context refuses every further
        step, and saving what was already acquired does not continue the act (it moves nothing).
        The process-wide stop still applies (``build_context`` always includes it): after an
        emergency stop or an operator abort nothing is saved. The step is recorded like the
        act's others but cannot turn the act into ``refused_busy``.

        Returns ``(archived entries, note for the observation, step for the events log)``."""
        from mast.api import direct_exec

        params = {"timeout_ms": SALVAGE_SAVE_TIMEOUT_MS}
        step: dict = {"skill": "SaveScan", "params": dict(params), "purpose": "save the partial frame"}
        before = _listing(sdir)
        ec, missing = direct_exec.build_context(self._ctx, owner=caller.owner,
                                                run_id=direct_exec.new_run_id("vista-salvage"))
        if ec is None:
            step.update(success=False, error=f"execution context unavailable: {missing}")
            return [], f"not saved: the execution context is unavailable ({', '.join(missing)})", step
        run = direct_exec.run_and_record(
            ec, "SaveScan", params, runtime=self._runtime(), agent_id=caller.agent_id,
            thread_id=caller.thread_id, tool_call_id=f"{steps.job_id}.salvage",
            context=caller.thread_id, approval_source=direct_exec.APPROVAL_LLM)
        steps.note("SaveScan", run)
        ok, err = bool(run.success), str(run.error or "")
        step.update(success=ok, error=err)
        if not ok:
            return [], f"not saved: SaveScan failed: {err or 'failed'}", step
        entries = self._ingest(ws, turn, sdir, before, origin="act", partial=True)
        partial = [e for e in entries if e.kind == "p"]
        if partial:
            return entries, partial[-1].fid, step
        late = " (the save had not finished in time; the file may still arrive as an external frame)" \
            if (run.data or {}).get("timed_out") else ""
        return entries, f"not saved: the controller wrote no new scan file{late}", step

    def act_response(self, job: Any, session: str) -> dict:
        """The ``/vista/act`` answer for a job (running or done)."""
        if getattr(job, "kind", "skill") != "act":
            return {"kind": "job", "job": job.view()}
        extra = dict(getattr(job, "extra", {}) or {})
        base = {"kind": "act", "job_id": job.job_id, "turn": extra.get("turn")}
        if not job.terminal:
            return {**base, "state": "running",
                    "hint": ("the action is executing; call mast_wait with condition "
                             f"{{\"type\": \"job\", \"job_id\": \"{job.job_id}\"}} to get its observation")}
        data = ((job.result or {}).get("data") or {}) if isinstance(job.result, dict) else {}
        obs_id = ((data.get("vista") or {}) if isinstance(data, dict) else {}).get("obs_id")
        obs = self._observations.get(obs_id) if obs_id else None
        out = {**base, "state": "done", "job_state": job.state,
               "ok": job.state == "succeeded"}
        if obs is None:
            out["observation"] = None
            out["error"] = ((job.result or {}).get("error") if isinstance(job.result, dict) else None) \
                or job.state
            out["hint"] = "no observation was recorded for this action; call mast_observe"
            return out
        out["observation"] = self._observation_payload(obs)
        return out

    # ── observe ─────────────────────────────────────────────────────────────
    def observe(self, caller: Any, client_meta: Any = None) -> dict:
        self.register(caller, client_meta)
        self._check_session(caller)
        return self._observe(caller, kind="observe")

    def _observe(self, caller: Any, *, kind: str) -> dict:
        ws = self.workspace()
        pictures: list = []
        if self._optical():
            # Optical station: the newest spectrum (summary + one picture) and the optical
            # state echo. No scan archive, no Nanonis readings.
            newest = None
            latest, pictures = self._optical_latest()
            payload: dict = {"observe": True, "station": "optical", "workspace": ws.id,
                             "turn": ws.turn, "latest_frame": latest,
                             "instrument": self._read_state(caller), "last_act": ws.last_act()}
        else:
            newest = self._newest_frame(ws)
            state = self._read_state(caller)
            payload = {"observe": True, "workspace": ws.id, "turn": ws.turn,
                       "latest_frame": newest.summary() if newest is not None else None,
                       "instrument": state, "last_act": ws.last_act()}
        if kind == "recovery":
            payload["recovery"] = True
        obs = self._remember(Observation(obs_id=f"o{secrets.token_hex(5)}", session=caller.session,
                                         workspace=ws.id, kind=kind, turn=ws.turn, payload=payload,
                                         frames=[newest.fid] if newest is not None else [],
                                         pictures=pictures))
        now = time.time()
        with self._lock:
            cs = self._clients.get(caller.session)
            closed = bool(cs and cs.closed)
            log_it = cs is None or now - cs.last_observe_logged >= OBSERVE_LOG_EVERY_S
            if cs is not None:
                cs.observations += 1
                if log_it:
                    cs.last_observe_logged = now
        if not closed:
            obs.token = self.gate.mint(caller.session, obs.obs_id)
        if log_it or kind != "observe":
            ws.append_event({"kind": "observe", "session": caller.session, "turn": ws.turn,
                             "obs": obs.obs_id, "frame": newest.fid if newest is not None else None,
                             "reason": kind})
        return self._observation_payload(obs)

    @staticmethod
    def _newest_frame(ws: Workspace) -> Any:
        frames = [e for e in ws.archive.frames() if e.kind in ("s", "d")]
        return frames[-1] if frames else None

    def _read_state(self, caller: Any, *, settle: bool = False) -> dict:
        """The state echo. ``settle`` (after an act): a withdrawn tip's current is re-read until
        it has decayed, for a bounded time (:mod:`mast.vista.state`); an observation reads once
        and only flags a reading that has not decayed."""
        if self._optical():
            # No Nanonis readbacks to run; the echo reads the devices this process already
            # has open (never opens one) and the signed optical state. ``settle`` is the
            # STM's withdrawn-tip current decay and means nothing here.
            return optical_echo()
        from mast.api import direct_exec

        ec, missing = direct_exec.build_context(self._ctx, owner=caller.owner,
                                                run_id=direct_exec.new_run_id("vista-state"))
        if ec is None:
            return {"unavailable": missing}
        return state_echo(ec.run, settle_budget_s=CURRENT_SETTLE_BUDGET_S if settle else 0.0)

    def _optical_latest(self) -> tuple[dict | None, list]:
        """``(latest_frame summary, pictures)`` of an optical observation: the newest
        spectrum in the experiment folder, rendered. ``(None, [])`` when there is none."""
        from . import optical as optical_view

        summary, png = optical_view.latest_view()
        if summary is None:
            return None, []
        pics = [(f'<visual kind="spectrum" file="{summary.get("file", "")}"/>', png)] if png else []
        return summary, pics

    def _optical_act_view(self, before: Any) -> tuple[list, dict | None, list]:
        """After an optical act: ``(summaries of the spectra it produced, newest spectrum
        summary, pictures)``. The picture is the newest spectrum that is new (or the newest
        one when the act produced none)."""
        from . import optical as optical_view

        fresh = optical_view.new_since(before or set(), limit=3)
        summaries: list[dict] = []
        pics: list = []
        for i, sf in enumerate(fresh):
            try:
                loaded = optical_view.load(sf)
                s = optical_view.summarize(sf, loaded=loaded)
                summaries.append(s)
                if i == 0:
                    pics.append((optical_view.label_for(sf),
                                 optical_view.render_png(sf, s, loaded=loaded)))
            except Exception as exc:  # noqa: BLE001 - a broken file is reported, not fatal
                log.warning("vista: cannot show the spectrum %s (%s: %s)", sf.path.name,
                            type(exc).__name__, exc)
                summaries.append({"kind": "spectrum", "file": sf.path.name,
                                  "error": f"{type(exc).__name__}: {exc}"[:300]})
        if summaries:
            return summaries, None, pics
        latest, pics = self._optical_latest()
        return [], latest, pics

    # ── observations ────────────────────────────────────────────────────────
    def _remember(self, obs: Observation) -> Observation:
        with self._lock:
            self._observations[obs.obs_id] = obs
            while len(self._observations) > OBSERVATIONS_KEPT:
                self._observations.popitem(last=False)
        return obs

    @staticmethod
    def _shown(entries: list) -> list[str]:
        """At most one image per observation: the newest scan, else the newest partial scan (the
        rows of a scan that stopped early), else the newest spectrum."""
        for kind in ("s", "p", "d"):
            hits = [e for e in entries if e.kind == kind]
            if hits:
                return [hits[-1].fid]
        return []

    def _observation_payload(self, obs: Observation) -> dict:
        ws = self._workspace_by_id(obs.workspace)
        live = obs.token is not None and self.gate.is_live(obs.token)
        strict = self.strict
        payload = dict(obs.payload)
        if live and not strict:
            payload["next_token"] = obs.token
        elif live and strict:
            payload["next_token"] = None
            payload["token_on_image"] = True
            payload["token_hint"] = "the token for your next action is printed in the band under the image"
        else:
            payload["next_token"] = None
            payload["token_note"] = ("no live token: a newer observation superseded this one, or this "
                                     "session is closed; call mast_observe for the newest observation")
        images = self._images_for(ws, obs.frames, stamp=obs.token if (live and strict) else None)
        for label, png in obs.pictures:
            # pictures that are not archive frames (the optical station's spectrum) get the
            # same token band in strict mode as archive frames do
            shown = _stamp_token(png, obs.token) if (live and strict) else png
            images.append({"label": label, "mime": "image/png",
                           "data_b64": base64.b64encode(shown).decode("ascii")})
        if live and strict and not images:
            images = [_card_image(payload, obs.token)]
        return {"obs_id": obs.obs_id, "turn": obs.turn, "frames": list(obs.frames),
                "text": compact_json(payload), "images": images,
                "next_token": obs.token if (live and not strict) else None,
                "token_on_image": bool(live and strict)}

    @staticmethod
    def _images_for(ws: Workspace, fids: list[str], *, stamp: str | None) -> list[dict]:
        out: list[dict] = []
        for fid in fids:
            try:
                blocks = ws.archive.observation(fid)
            except Exception as exc:  # noqa: BLE001 — a missing file is reported, not fatal
                log.warning("vista: observation image of %s unavailable (%s)", fid, exc)
                continue
            for b in blocks:
                png = _stamp_token(b.png, stamp) if stamp else b.png
                out.append({"label": b.label, "mime": "image/png",
                            "data_b64": base64.b64encode(png).decode("ascii")})
        return out

    def observation_after_wait(self, session: str, fired: list[dict]) -> dict | None:
        """The observation a ``wait`` answer carries: a finished act's, or a fresh one when a
        new frame fired. None otherwise."""
        for f in fired or []:
            if f.get("type") != "job":
                continue
            ev = f.get("event") or {}
            job_id = ev.get("job_id") or (ev.get("job") or {}).get("job_id")
            job = self._job_lookup(job_id) if job_id else None
            if job is not None and getattr(job, "kind", "") == "act" and job.terminal:
                resp = self.act_response(job, session)
                if resp.get("observation"):
                    return resp["observation"]
        if any(f.get("type") == "frame" for f in fired or []):
            with self._lock:
                cs = self._clients.get(session)
            caller = _Caller(actor=cs.actor if cs else "anonymous", session=session)
            return self._observe(caller, kind="wait")
        return None

    # ── archive tools ───────────────────────────────────────────────────────
    def _tools(self, ws: Workspace) -> dict:
        from .viewtools import view_tools
        from . import measure as M

        grid = bool(getattr(ws.archive, "grid", False))
        tools = dict(view_tools(ws.archive.display_max, grid=grid))
        mt = getattr(M, "measure_tools", None)
        tools.update(mt(ws.archive.display_max) if callable(mt) else M.MEASURE_TOOLS)
        return tools

    def tool(self, caller: Any, name: str, args: Any) -> dict:
        """inspect / read_values / line_profile / fft_peaks / detect_blobs / diff / stats."""
        self._check_session(caller)
        ws = self.workspace()
        tools = self._tools(ws)
        if name not in tools:
            return reply_dict(error_reply(f"unknown tool {name!r}", available=sorted(tools)))
        clean = {k: v for k, v in dict(args or {}).items() if k != "client_meta"}
        try:
            reply = tools[name][1](ws.archive, clean)
        except Exception as exc:  # noqa: BLE001 — a tool never raises into the API
            reply = error_reply(f"internal error in {name}: {type(exc).__name__}: {exc}")
        return reply_dict(reply)

    def notes_get(self, caller: Any, which: str) -> dict:
        self._check_session(caller, notes_ok=True)
        if which not in NOTES:
            return reply_dict(error_reply("which must be 'guide' or 'working'"))
        return reply_dict(self.workspace().notes.call(f"read_{which}", {}))

    def notes_put(self, caller: Any, which: str, content: Any) -> dict:
        cs = self._check_session(caller, notes_ok=True)
        if which not in NOTES:
            return reply_dict(error_reply("which must be 'guide' or 'working'"))
        ws = self.workspace()
        reply = ws.notes.call(f"write_{which}", {"content": content})
        if which == "working" and not reply.is_error and cs.checkpoint == "requested":
            with self._lock:
                cs.checkpoint = "ready"
            ws.append_event({"kind": "checkpoint", "session": caller.session, "turn": ws.turn,
                             "reason": "WORKING.md saved"})
            reply = ToolReply(text=reply.text + "\nWORKING.md updated; checkpoint ready.",
                              images=reply.images, is_error=False, effect="notes", meta=reply.meta)
        return reply_dict(reply)

    def history(self, caller: Any, args: dict) -> dict:
        self._check_session(caller)
        return reply_dict(self.workspace().history(args))

    def status(self) -> dict:
        ws = self.workspace()
        with self._lock:
            clients = [c.view() for c in self._clients.values()]
            watcher = {"running": bool(self._watcher and self._watcher.is_alive()),
                       "dir_known": self._watch_dir is not None, "errors": self.watch_errors}
        return {"workspace": ws.id, "turn": ws.turn, "frames": len(ws.archive.frames(kind="s")),
                "spectra": len(ws.archive.frames(kind="d")), "strict": self.strict,
                "gate": self.gate.status(), "clients": clients, "watcher": watcher}

    # ── frames ──────────────────────────────────────────────────────────────
    def _session_dir(self, *, fresh: bool = False) -> Path | None:
        """The controller's save directory. ``fresh`` bypasses the runtime's 15 s cache: an act
        asks at its start and at its end, because the answer can change in between (the
        operator switches session folders; a controller creates the folder on the first save,
        and until then MAST's resolver falls back to the parent directory)."""
        rt = self._runtime()
        fn = getattr(rt, "_resolve_session_dir", None)
        if callable(fn):
            if fresh:
                try:
                    rt._session_dir_cache = None
                except Exception:  # noqa: BLE001
                    pass
            try:
                d = fn()
            except Exception:  # noqa: BLE001
                d = None
            if d and Path(d).is_dir():
                return Path(d)
        env = os.environ.get(SESSION_DIR_ENV, "").strip()
        if env and Path(env).is_dir():
            return Path(env)
        return None

    def _ingest(self, ws: Workspace, turn: int, sdir: Path, before: dict, *, origin: str,
                names: list[str] | None = None, partial: bool = False) -> list:
        """Archive the files in ``sdir`` that are not in ``before`` (or ``names``). ``partial``:
        scans are the rows of a scan that stopped early (:meth:`_salvage_partial`)."""
        after = _listing(sdir)
        new = list(names) if names is not None else [n for n in after if n not in before]
        new.sort(key=lambda n: (after.get(n, (0, 0.0))[1], n))
        out = []
        for n in new:
            with self._watch_lock:
                self._known.add(n)
                self._pending.pop(n, None)
            p = sdir / n
            try:
                if p.suffix.lower() == ".sxm" and partial:
                    out.append(ws.archive.add_partial_scan(turn, p, origin=origin))
                elif p.suffix.lower() == ".sxm":
                    out.append(ws.archive.add_scan(turn, p, origin=origin))
                else:
                    out.append(ws.archive.add_spectrum(turn, p, origin=origin))
            except Exception as exc:  # noqa: BLE001 — a broken file is recorded, not fatal
                ws.append_event({"kind": "archive_error", "turn": turn, "file": n,
                                 "error": f"{type(exc).__name__}: {exc}"[:300]})
        return out

    def _frame_archived(self, ws: Workspace, entry: Any, *, origin: str) -> None:
        self._publish("frame", {"frame": entry.fid, "kind": entry.kind_name, "origin": origin,
                                "workspace": ws.id, "turn": entry.turn})
        if entry.kind in ("s", "d"):
            try:
                self.detectors(ws).on_frame(ws.archive, entry)
            except Exception as exc:  # noqa: BLE001 — detectors are optional
                log.debug("vista: detectors skipped for %s (%s)", entry.fid, exc)

    def _ensure_watcher(self) -> None:
        if not self._start_watcher:
            return
        with self._lock:
            if self._watcher is not None and self._watcher.is_alive():
                return
            self._stop.clear()
            t = threading.Thread(target=self._watch_loop, name="vista-frame-watcher", daemon=True)
            self._watcher = t
            t.start()

    def stop_watcher(self) -> None:
        """Stop the background poll for good (tests drive :meth:`watch_once` themselves)."""
        self._start_watcher = False
        self._stop.set()
        t = self._watcher
        if t is not None and t.is_alive():
            t.join(timeout=3.0)

    def _watch_loop(self) -> None:
        while not self._stop.wait(self.watch_interval_s):
            try:
                self.watch_once()
            except Exception as exc:  # noqa: BLE001 — the watcher never dies on one bad poll
                self.watch_errors += 1
                log.warning("vista: frame watcher poll failed (%s)", exc)

    def watch_once(self, *, now: float | None = None) -> list:
        """One poll of the controller's save directory (the watcher thread calls this).
        Returns the entries archived as external frames."""
        if self._ctx is None or not self._clients_active() or self.gate.in_flight():
            return []
        if self._optical():
            return []             # no controller save folder to poll (see `_optical_latest`)
        sdir = self._session_dir()
        if sdir is None:
            return []
        listing = _listing(sdir)
        now = time.time() if now is None else now
        ready: list[str] = []
        with self._watch_lock:
            if self._watch_dir != sdir:
                # first look at this directory: what is there already is history, not news
                self._watch_dir = sdir
                self._known = set(listing)
                self._pending = {}
                return []
            for name, st in listing.items():
                if name in self._known:
                    continue
                prev = self._pending.get(name)
                if prev == st and now - st[1] >= 1.0:
                    ready.append(name)
                else:
                    self._pending[name] = st
        if not ready or self.gate.in_flight():
            return []
        ws = self.workspace()
        entries = self._ingest(ws, ws.turn, sdir, {}, origin="external", names=ready)
        for e in entries:
            ws.append_event({"kind": "external_frame", "turn": ws.turn, "frame": e.fid,
                             "origin": "external"})
            self.gate.invalidate(f"external frame {e.fid}")
            self._frame_archived(ws, e, origin="external")
        return entries

    # ── runner support ──────────────────────────────────────────────────────
    def request_checkpoint(self, session: str, reason: str) -> None:
        with self._lock:
            cs = self._clients.get(session) or ClientSession(session=session, actor="runner")
            self._clients[session] = cs
            if cs.checkpoint == "none":
                cs.checkpoint = "requested"
                cs.checkpoint_reason = str(reason)[:200]
        self.workspace().append_event({"kind": "checkpoint", "session": session,
                                       "turn": self.workspace().turn,
                                       "reason": f"requested: {reason}"[:200]})

    def checkpoint_state(self, session: str) -> str:
        with self._lock:
            cs = self._clients.get(session)
            return cs.checkpoint if cs is not None else "none"

    def clear_checkpoint(self, session: str) -> None:
        with self._lock:
            cs = self._clients.get(session)
            if cs is not None:
                cs.checkpoint = "none"
                cs.checkpoint_reason = None

    def close_session(self, session: str, reason: str) -> None:
        with self._lock:
            cs = self._clients.get(session) or ClientSession(session=session, actor="runner")
            self._clients[session] = cs
            cs.closed = str(reason or "closed")[:200]

    def recovery_observation(self, session: str) -> dict:
        """First message of a fresh runner segment: environment record, notes, newest
        observation (with a fresh token bound to ``session``)."""
        with self._lock:
            cs = self._clients.get(session)
        caller = _Caller(actor=cs.actor if cs else "runner", session=session)
        obs = self._observe(caller, kind="recovery")
        ws = self.workspace()
        notes = ws.notes.snapshot()
        record = {"workspace_so_far": ws.attempts(), "last_action_result": ws.last_act()}
        text = ("Environment record:\n" + compact_json(record)
                + "\n\nAgent-authored notes:\n"
                + compact_json({"guide": notes.get("guide") or "",
                                "working_memory": notes.get("working") or ""})
                + "\n\nNewest observation:\n" + obs["text"])
        return {"text": text, "images": obs["images"], "next_token": obs.get("next_token")}

    # ── shutdown ────────────────────────────────────────────────────────────
    def shutdown(self) -> None:
        self._stop.set()
        t = self._watcher
        if t is not None and t.is_alive():
            t.join(timeout=3.0)
        self._close_detectors()
        hub = self._hub
        if hub is not None:
            try:
                hub.detach_event_bus()
            except Exception:  # noqa: BLE001
                pass


def reply_dict(reply: ToolReply) -> dict:
    """ToolReply → the HTTP shape of contract §3."""
    return {"text": reply.text,
            "images": [{"label": im.label, "mime": "image/png", "data_b64": im.b64()}
                       for im in reply.images],
            "is_error": bool(reply.is_error)}


# ── strict mode rendering ─────────────────────────────────────────────────────
def _font(size: int):
    from PIL import ImageFont
    try:
        return ImageFont.load_default(size=size)
    except TypeError:                         # Pillow < 10.1: bitmap default only
        return ImageFont.load_default()


def _stamp_token(png: bytes, token: str | None) -> bytes:
    """Append a band with the next-act token under an observation image."""
    if not token:
        return png
    from PIL import Image, ImageDraw
    im = Image.open(io.BytesIO(png)).convert("RGB")
    band = max(48, im.height // 12)
    out = Image.new("RGB", (im.width, im.height + band), (16, 16, 16))
    out.paste(im, (0, 0))
    draw = ImageDraw.Draw(out)
    draw.text((12, im.height + band // 6), f"next action token: {token}", fill=(255, 255, 255),
              font=_font(max(20, band * 2 // 3)))
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


def _card_image(payload: dict, token: str | None) -> dict:
    """An observation without a frame in strict mode: the readbacks and the token as an image."""
    from PIL import Image, ImageDraw
    lines = ["observation (no new frame)"]
    inst = payload.get("instrument") if isinstance(payload.get("instrument"), dict) else {}
    for k, v in list(inst.items())[:10]:
        lines.append(f"{k}: {v}")
    lines.append(f"next action token: {token}")
    im = Image.new("RGB", (720, 40 + 36 * len(lines)), (16, 16, 16))
    draw = ImageDraw.Draw(im)
    for i, line in enumerate(lines):
        draw.text((16, 16 + 36 * i), line[:60], fill=(255, 255, 255), font=_font(26))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return {"label": '<visual kind="card"/>', "mime": "image/png",
            "data_b64": base64.b64encode(buf.getvalue()).decode("ascii")}


# ── process singleton ─────────────────────────────────────────────────────────
_SERVICE: VistaService | None = None
_SERVICE_LOCK = threading.Lock()


def get_service(ctx: Any = None) -> VistaService:
    """The process's service, bound to ``ctx`` when given and not yet bound."""
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None:
            _SERVICE = VistaService()
        svc = _SERVICE
    if ctx is not None and svc.ctx is None:
        svc.bind(ctx)
    return svc


def peek_service() -> VistaService | None:
    """The service if one exists (never creates one)."""
    return _SERVICE


def reset_service() -> None:
    """Tests: drop the singleton (stops its watcher)."""
    global _SERVICE
    with _SERVICE_LOCK:
        svc, _SERVICE = _SERVICE, None
    if svc is not None:
        svc.shutdown()


__all__ = ["ClientSession", "Observation", "VistaError", "VistaService", "get_service",
           "peek_service", "public_event", "reply_dict", "reset_service"]
