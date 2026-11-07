"""Nanonis motor groups — ``<data root>/config/nanonis_motor_groups.json``.

A Nanonis PMD4 drives up to six motor **groups** of three axes each ("group 1
could move the scan table, group 2 the mirror", Nanonis help). On a TERS rig
one group is the STM coarse approach and others move mirrors, a beam deflection
and lenses. MAST used to hard-code group 0 for every coarse move; this module makes
the STM group a configured value and describes the optical groups.

Schema::

    {
      "stm_coarse_group": 0,          // value passed as Motor_StartMove's Group
      "groups": [
        {"group": 1,                  // 0-based: 0 = "Group 1" in the Nanonis UI
         "name": "Mirror X/Y",
         "role": "laser_mirror",      // anything except an STM role
         "axes": {"X": "mirror_x", "Y": "mirror_y"},   // Nanonis axis → logical name
         "max_steps_per_cmd": 200,    // one NanonisMotorGroupMove may not exceed it
         "chunk_steps": 50,           // abort is checked between chunks (default ≤ 50)
         "requires": "tip_withdrawn", // tip_withdrawn | z_feedback_on | none
         "freq_hz": 442.0,            // the drive this group must run with
         "amp_v": 200.0,
         "drive_policy": "verify_only",  // verify_only (default) | write_and_restore
         "enabled": true, "note": ""}
      ],
      "reserved_digital_ports": [     // Nanonis DigLines ports no skill may write
        {"port": 0, "reason": "PMD4 粗动控制器的控制口"}
      ]
    }

Behaviour contract (backed by a measurement on the Nanonis simulator, contract §9):

* **No file ⇒ STM group 0 and no optical groups** — every coarse skill behaves
  exactly as before this module existed.
* **A file that exists but cannot be read is a refusal, not a fallback.** If the
  operator wrote ``"stm_coarse_group": 2`` and the JSON is broken, falling back
  to 0 would step whatever group 0 moves. :func:`stm_coarse_group` raises
  :class:`MotorGroupConfigError`; the STM skills turn that into a clear failure.
* Group numbers are validated here (0–5). The simulator accepted groups 6 and 7
  without an error, so Nanonis cannot be relied on to reject a bad index.
* **Whose drive does ``Motor_FreqAmpSet/Get`` touch?** It has no group argument.
  On the Mimea simulator it is module-global (a value set "on" group 0 read back
  unchanged after moving group 1; an axis-1 write showed on axes 0–3). But a
  real session ini can store amplitude/frequency **per group**, so on the PMD4 a write most likely
  lands on whichever group the Motor Control panel has selected — possibly the
  STM group. Hence ``drive_policy``:

  - ``verify_only`` (default): never write. Read the drive back and step only if
    it equals the group's configured value; otherwise refuse and tell the
    operator to select the group / check its values in the Nanonis panel.
  - ``write_and_restore``: write the configured drive, verify, step, restore what
    was read, verify again. Only valid where the drive has been shown to be
    global on that rig. An unverified restore **latches**
    (:func:`latch_drive_not_restored`): every coarse move, STM or optical, is
    refused until a readback shows the drive is back. The latch is persisted
    (:func:`latch_path`, beside the config) because a restart does not put the
    drive back.

  See ``skills/builtins/optics_motor_groups.py``.

An entry that fails validation is dropped from :attr:`MotorGroupsConfig.groups`
and reported in :attr:`MotorGroupsConfig.errors` — never "repaired".
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mast._runtime_paths import project_root
from mast.i18n import tr

logger = logging.getLogger(__name__)

__all__ = [
    "CONFIG_FILENAME",
    "GROUP_MIN",
    "GROUP_MAX",
    "REQUIRES",
    "DRIVE_POLICIES",
    "DIRECTION_CODES",
    "MotorGroupConfigError",
    "MotorGroup",
    "MotorGroupsConfig",
    "config_path",
    "parse_motor_groups",
    "load_motor_groups",
    "stm_coarse_group",
    "optical_group",
    "digital_port_refusal",
    "port_label",
    "LATCH_FILENAME",
    "latch_path",
    "latch_drive_not_restored",
    "drive_latch",
    "clear_drive_latch",
    "note_steps",
    "odometer",
    "reset_for_tests",
]

CONFIG_FILENAME = "nanonis_motor_groups.json"

#: Motor_StartMove's documented Group range (0 = "Group 1" … 5 = "Group 6").
GROUP_MIN, GROUP_MAX = 0, 5

#: The tip-state policy an optical group can ask for before it steps.
REQUIRES = ("tip_withdrawn", "z_feedback_on", "none")

#: How a move treats the controller's (group-less) frequency/amplitude.
DRIVE_POLICIES = ("verify_only", "write_and_restore")

#: Roles that mean "this is the STM coarse drive" — never an optical group.
_STM_ROLES = frozenset({"stm", "stm_coarse", "coarse", "stm_coarse_group", "approach"})

#: Nanonis Motor_StartMove direction codes.
DIRECTION_CODES = {("X", "+"): 0, ("X", "-"): 1, ("Y", "+"): 2, ("Y", "-"): 3,
                   ("Z", "+"): 4, ("Z", "-"): 5}

#: Hard ceilings no config can widen (same character as coarse_drive's).
_MAX_STEPS_HARD = 10_000          # Motor_StartMove's step count is a uint16; stay far below
_MAX_AMP_V = 400.0                # mirrors coarse_drive.ABSOLUTE_MAX_AMPLITUDE_V
_MAX_FREQ_HZ = 20_000.0           # mirrors coarse_drive.ABSOLUTE_MAX_FREQUENCY_HZ
_DEFAULT_CHUNK = 50


class MotorGroupConfigError(RuntimeError):
    """The motor-group file exists but cannot be used for the requested decision."""


@dataclass(frozen=True)
class MotorGroup:
    """One optical motor group, validated."""

    group: int
    name: str
    role: str
    axes: dict[str, str]
    max_steps_per_cmd: int
    chunk_steps: int
    requires: str
    freq_hz: float
    amp_v: float
    enabled: bool = True
    note: str = ""
    drive_policy: str = "verify_only"

    def axis_letter(self, axis: str) -> str:
        """Nanonis axis letter (X/Y/Z) for a logical name or a letter."""
        key = str(axis or "").strip()
        if key.upper() in self.axes:
            return key.upper()
        for letter, logical in self.axes.items():
            if logical == key or logical.lower() == key.lower():
                return letter
        raise MotorGroupConfigError(
            f"motor group {self.group} ({self.name}) has no axis {axis!r}; "
            f"configured: {self.axes}")

    def as_dict(self) -> dict:
        return {"group": self.group, "name": self.name, "role": self.role,
                "axes": dict(self.axes), "max_steps_per_cmd": self.max_steps_per_cmd,
                "chunk_steps": self.chunk_steps, "requires": self.requires,
                "freq_hz": self.freq_hz, "amp_v": self.amp_v,
                "drive_policy": self.drive_policy,
                "enabled": self.enabled, "note": self.note}


@dataclass(frozen=True)
class MotorGroupsConfig:
    stm_coarse_group: int = 0
    groups: tuple[MotorGroup, ...] = ()
    errors: tuple[str, ...] = ()
    source: str = "default"
    #: why the whole file is unusable (then stm_coarse_group must not be used)
    fatal: str = ""
    #: ``((port, reason), …)`` — Nanonis digital ports no skill may write
    #: (``reserved_digital_ports``); a PMD4 rig's Port A is its control link
    reserved_digital_ports: tuple[tuple[int, str], ...] = ()
    #: why ``reserved_digital_ports`` cannot be read (then no port may be written)
    digital_ports_error: str = ""

    def as_dict(self) -> dict:
        return {"stm_coarse_group": self.stm_coarse_group,
                "groups": [g.as_dict() for g in self.groups],
                "reserved_digital_ports": [{"port": p, "reason": r}
                                           for p, r in self.reserved_digital_ports],
                "digital_ports_error": self.digital_ports_error,
                "errors": list(self.errors), "source": self.source, "fatal": self.fatal}


def config_path() -> Path:
    return project_root() / "config" / CONFIG_FILENAME


# ── parsing ───────────────────────────────────────────────────────────────


def _int_in(value: Any, lo: int, hi: int, what: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{what} must be an integer, not {value!r}")
    f = float(value)
    if not math.isfinite(f) or f != int(f) or not (lo <= int(f) <= hi):
        raise ValueError(f"{what}={value!r} must be an integer in [{lo}, {hi}]")
    return int(f)


def _pos_float(value: Any, hi: float, what: str) -> float:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{what} is required (no default: the drive is the operator's value)")
    f = float(value)
    if not (math.isfinite(f) and 0.0 < f <= hi):
        raise ValueError(f"{what}={value!r} must be in (0, {hi:g}]")
    return f


def _parse_group(raw: Any, stm_group: int) -> MotorGroup:
    if not isinstance(raw, dict):
        raise ValueError("entry is not an object")
    g = _int_in(raw.get("group"), GROUP_MIN, GROUP_MAX, "group")
    label = f"group {g}"
    if g == stm_group:
        raise ValueError(f"{label} is the STM coarse group (stm_coarse_group={stm_group}); "
                         "it cannot also be an optical group")
    role = str(raw.get("role") or "").strip()
    if not role:
        raise ValueError(f"{label}: role is required (e.g. laser_mirror, lens)")
    if role.lower() in _STM_ROLES:
        raise ValueError(f"{label}: role {role!r} is an STM role")
    axes_raw = raw.get("axes")
    if not isinstance(axes_raw, dict) or not axes_raw:
        raise ValueError(f"{label}: axes must map Nanonis axes (X/Y/Z) to names")
    axes: dict[str, str] = {}
    for k, v in axes_raw.items():
        letter = str(k).strip().upper()
        if letter not in ("X", "Y", "Z"):
            raise ValueError(f"{label}: axis key {k!r} is not X, Y or Z")
        name = str(v or "").strip()
        if not name or name in axes.values():
            raise ValueError(f"{label}: axis {letter} needs a unique non-empty name")
        axes[letter] = name
    max_steps = _int_in(raw.get("max_steps_per_cmd"), 1, _MAX_STEPS_HARD,
                        f"{label}: max_steps_per_cmd")
    chunk = _int_in(raw.get("chunk_steps", min(max_steps, _DEFAULT_CHUNK)), 1, max_steps,
                    f"{label}: chunk_steps")
    requires = str(raw.get("requires") or "tip_withdrawn").strip()
    if requires not in REQUIRES:
        raise ValueError(f"{label}: requires={requires!r} not in {REQUIRES}")
    policy = str(raw.get("drive_policy") or "verify_only").strip()
    if policy not in DRIVE_POLICIES:
        raise ValueError(f"{label}: drive_policy={policy!r} not in {DRIVE_POLICIES}")
    return MotorGroup(
        group=g, name=str(raw.get("name") or label), role=role, axes=axes,
        max_steps_per_cmd=max_steps, chunk_steps=chunk, requires=requires,
        freq_hz=_pos_float(raw.get("freq_hz"), _MAX_FREQ_HZ, f"{label}: freq_hz"),
        amp_v=_pos_float(raw.get("amp_v"), _MAX_AMP_V, f"{label}: amp_v"),
        enabled=bool(raw.get("enabled", True)), note=str(raw.get("note") or ""),
        drive_policy=policy)


def parse_motor_groups(raw: Any, *, source: str = "inline") -> MotorGroupsConfig:
    """Validate a decoded config. Never raises; problems land in ``fatal``/``errors``."""
    if not isinstance(raw, dict):
        return MotorGroupsConfig(source=source, fatal="top level is not a JSON object")
    try:
        stm = _int_in(raw.get("stm_coarse_group", 0), GROUP_MIN, GROUP_MAX,
                      "stm_coarse_group")
    except (TypeError, ValueError) as exc:
        return MotorGroupsConfig(source=source, fatal=str(exc))
    groups: list[MotorGroup] = []
    errors: list[str] = []
    seen: set[int] = set()
    entries = raw.get("groups") or []
    if not isinstance(entries, list):
        errors.append("groups is not a list — ignored")
        entries = []
    for i, entry in enumerate(entries):
        try:
            mg = _parse_group(entry, stm)
        except (TypeError, ValueError) as exc:
            errors.append(f"groups[{i}]: {exc}")
            continue
        if mg.group in seen:
            errors.append(f"groups[{i}]: group {mg.group} defined twice — both dropped")
            groups = [g for g in groups if g.group != mg.group]
            continue
        seen.add(mg.group)
        groups.append(mg)
    reserved, ports_err = _parse_reserved_ports(raw.get("reserved_digital_ports"))
    return MotorGroupsConfig(stm_coarse_group=stm, groups=tuple(groups),
                             errors=tuple(errors), source=source,
                             reserved_digital_ports=reserved, digital_ports_error=ports_err)


#: Nanonis DigLines ports: 0=A, 1=B, 2=C, 3=D, 4+ = expanded DIO.
_PORT_LETTERS = {0: "A", 1: "B", 2: "C", 3: "D"}
_PORT_MAX = 15


def _parse_reserved_ports(raw: Any) -> tuple[tuple[tuple[int, str], ...], str]:
    """``reserved_digital_ports: [{"port": 0, "reason": "…"}, …]``. Absent ⇒ none.
    Anything malformed is NOT dropped entry by entry — a dropped reservation is an
    unprotected port — the whole list is reported unusable instead, and then no
    digital port may be written (:func:`digital_port_refusal`)."""
    if raw is None:
        return (), ""
    if not isinstance(raw, list):
        return (), "reserved_digital_ports is not a list"
    out: list[tuple[int, str]] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            return (), f"reserved_digital_ports[{i}] is not an object"
        try:
            port = _int_in(entry.get("port"), 0, _PORT_MAX, f"reserved_digital_ports[{i}].port")
        except (TypeError, ValueError) as exc:
            return (), str(exc)
        out.append((port, str(entry.get("reason") or "").strip() or tr("本机配置的保留口", "reserved by this machine's config")))
    return tuple(out), ""


def port_label(port: int) -> str:
    return f"Port {_PORT_LETTERS[port]}" if port in _PORT_LETTERS else f"DIO port {port}"


def digital_port_refusal(port: Any, path: Path | None = None) -> str:
    """``""`` when *port* may be written; otherwise why not. For every skill that
    writes a Nanonis digital port (direction, level, pulse) — reads are never
    gated. No config file ⇒ nothing reserved (behaviour as before). An
    unreadable file or reservation list refuses every port: not knowing which
    port is reserved is not knowing that this one is free."""
    cfg = load_motor_groups(path)
    if cfg.fatal:
        return tr(f"机器配置 {CONFIG_FILENAME} 读不出来（{cfg.fatal}）—— 不知道哪些数字口是保留口，"
                  "拒绝写数字口。修好或删掉该文件后重试。",
                  f"Machine config {CONFIG_FILENAME} cannot be read ({cfg.fatal}) - it is unknown which digital ports are reserved, "
                  "so writing digital ports is refused. Fix or delete the file and retry.")
    if cfg.digital_ports_error:
        return tr(f"{CONFIG_FILENAME} 的 reserved_digital_ports 读不懂（{cfg.digital_ports_error}）"
                  "—— 不知道哪些数字口是保留口，拒绝写数字口。",
                  f"reserved_digital_ports in {CONFIG_FILENAME} cannot be parsed ({cfg.digital_ports_error}) "
                  "- it is unknown which digital ports are reserved, so writing digital ports is refused.")
    try:
        p = int(port)
    except (TypeError, ValueError):
        return tr(f"数字口 {port!r} 不是整数", f"Digital port {port!r} is not an integer")
    for rp, reason in cfg.reserved_digital_ports:
        if rp == p:
            return tr(f"拒绝写 {port_label(p)}（port={p}）：本机配置把它列为保留数字口 —— {reason}。"
                      "误写它的方向位、电平或脉冲可能让接在上面的设备误动作；"
                      "不会截断，也不会改用别的口。",
                      f"Refusing to write {port_label(p)} (port={p}): this machine's config lists it as a reserved digital port - {reason}. "
                      "Writing its direction bit, level or pulse by mistake could make the device attached to it misbehave; "
                      "the value will not be truncated and no other port will be used instead.")
    return ""


# ── cached loader ──────────────────────────────────────────────────────────

_cache_lock = threading.Lock()
_cache: dict[str, tuple[float | None, MotorGroupsConfig]] = {}


def load_motor_groups(path: Path | None = None) -> MotorGroupsConfig:
    """Current config. Re-read whenever the file's mtime changes. Never raises."""
    p = Path(path) if path is not None else config_path()
    key = str(p)
    try:
        mtime: float | None = p.stat().st_mtime
    except FileNotFoundError:
        mtime = None
    except OSError as exc:
        return MotorGroupsConfig(source=key, fatal=f"cannot stat {p}: {exc}")
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and hit[0] == mtime:
            return hit[1]
    if mtime is None:
        cfg = MotorGroupsConfig(source="default (no file)")
    else:
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            cfg = MotorGroupsConfig(source=key, fatal=f"{p.name} unreadable: {exc}")
        else:
            cfg = parse_motor_groups(raw, source=key)
    if cfg.fatal:
        logger.error("nanonis motor groups: %s", cfg.fatal)
    for e in cfg.errors:
        logger.warning("nanonis motor groups: %s", e)
    with _cache_lock:
        _cache[key] = (mtime, cfg)
    return cfg


def stm_coarse_group(path: Path | None = None) -> int:
    """The Group argument every STM coarse move must use (0 when unconfigured).

    Raises :class:`MotorGroupConfigError` when the file exists but is unusable —
    a broken file must not silently send the STM move to group 0."""
    cfg = load_motor_groups(path)
    if cfg.fatal:
        raise MotorGroupConfigError(
            tr(f"{CONFIG_FILENAME} 无法使用（{cfg.fatal}）—— 不知道 STM 粗动是哪一组，"
               "拒绝粗动。修好或删掉该文件（删掉 = 回到 group 0）后重试。",
               f"{CONFIG_FILENAME} is unusable ({cfg.fatal}) - it is unknown which group is the STM coarse motion, "
               "so coarse motion is refused. Fix or delete the file (deleting it falls back to group 0) and retry."))
    return cfg.stm_coarse_group


def optical_group(key: Any, path: Path | None = None) -> MotorGroup:
    """An enabled optical group by number or name; refuses STM / unknown / disabled."""
    cfg = load_motor_groups(path)
    if cfg.fatal:
        raise MotorGroupConfigError(tr(f"{CONFIG_FILENAME} 无法使用：{cfg.fatal}", f"{CONFIG_FILENAME} is unusable: {cfg.fatal}"))
    text = str(key).strip()
    num: int | None = None
    try:
        num = int(text)
    except ValueError:
        num = None
    if num is not None and num == cfg.stm_coarse_group:
        raise MotorGroupConfigError(
            tr(f"group {num} 是 STM 粗动组（stm_coarse_group）—— 光学电机组技能一律拒绝；"
               "STM 粗动请用 RelocateCoarseXY / MotorMove。",
               f"group {num} is the STM coarse-motion group (stm_coarse_group) - optical motor-group skills always refuse it; "
               "use RelocateCoarseXY / MotorMove for STM coarse motion."))
    for g in cfg.groups:
        if (num is not None and g.group == num) or g.name == text or g.name.lower() == text.lower():
            if not g.enabled:
                raise MotorGroupConfigError(f"motor group {g.group} ({g.name}) is disabled in "
                                            f"{CONFIG_FILENAME}")
            return g
    known = [f"{g.group}:{g.name}" for g in cfg.groups]
    raise MotorGroupConfigError(
        f"no optical motor group {key!r} in {CONFIG_FILENAME} (configured: {known or 'none'}"
        + (f"; dropped entries: {list(cfg.errors)}" if cfg.errors else "") + ")")


# ── drive-restore latch (persisted) & odometer (process state) ─────────────

LATCH_FILENAME = "nanonis_drive_latch.json"

_state_lock = threading.Lock()
_latch: dict | None = None
_latch_loaded = False
_odo: dict[tuple[int, str], int] = {}


def latch_path() -> Path:
    """The latch is persisted next to the group config: a restart must not forget
    that the coarse drive may not be the STM's (the drive stays wrong across it)."""
    return config_path().with_name(LATCH_FILENAME)


def _load_latch_locked() -> None:
    """First access after start: pick up a latch an earlier process left. An
    unreadable file stays latched — only a readback can clear a latch, and an
    unreadable one says nothing about what to read back."""
    global _latch, _latch_loaded
    if _latch_loaded:
        return
    _latch_loaded = True
    p = latch_path()
    try:
        if not p.exists():
            return
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("not a JSON object")
        float(raw["expected_freq_hz"]), float(raw["expected_amp_v"])
        _latch = dict(raw)
        logger.error("nanonis motor drive latch carried over from %s: %s", p, raw.get("reason"))
    except Exception as exc:  # noqa: BLE001
        _latch = {"expected_freq_hz": None, "expected_amp_v": None, "last_read": None,
                  "reason": f"{p.name} unreadable ({type(exc).__name__}: {exc})",
                  "since": time.time(), "unreadable": True, "path": str(p)}
        logger.error("nanonis motor drive latch file %s unreadable: %s — staying latched",
                     p, exc)


def _write_latch_locked() -> None:
    p = latch_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(_latch, ensure_ascii=False, default=str), encoding="utf-8")
        tmp.replace(p)
    except Exception as exc:  # noqa: BLE001 - the in-process latch still holds
        logger.error("nanonis motor drive latch not persisted to %s: %s — it will not "
                     "survive a restart", p, exc)


def latch_drive_not_restored(*, expected: tuple[float, float], last_read: Any,
                             reason: str) -> None:
    """Record that the module-global drive may not be what the STM group expects."""
    global _latch, _latch_loaded
    with _state_lock:
        _latch = {"expected_freq_hz": float(expected[0]), "expected_amp_v": float(expected[1]),
                  "last_read": last_read, "reason": str(reason)[:300], "since": time.time()}
        _latch_loaded = True
        _write_latch_locked()
    logger.error("nanonis motor drive NOT restored: %s", reason)


def drive_latch() -> dict | None:
    with _state_lock:
        _load_latch_locked()
        return dict(_latch) if _latch else None


def clear_drive_latch() -> None:
    global _latch, _latch_loaded
    with _state_lock:
        _latch = None
        _latch_loaded = True
        p = latch_path()
        try:
            p.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.error("nanonis motor drive latch file %s not removed: %s — the next "
                         "start will be latched again", p, exc)


def note_steps(group: int, axis_letter: str, signed_steps: int) -> int:
    """Add commanded steps to the per-(group, axis) odometer; returns the total."""
    with _state_lock:
        k = (int(group), str(axis_letter).upper())
        _odo[k] = _odo.get(k, 0) + int(signed_steps)
        return _odo[k]


def odometer() -> dict[str, int]:
    """Commanded steps since process start, ``{"<group>/<axis>": steps}`` (no feedback)."""
    with _state_lock:
        return {f"{g}/{a}": n for (g, a), n in sorted(_odo.items())}


def reset_for_tests() -> None:
    """Forget the in-process state (as a restart would); the next access re-reads
    the persisted latch from :func:`latch_path`."""
    global _latch, _latch_loaded
    with _state_lock:
        _latch = None
        _latch_loaded = False
        _odo.clear()
    with _cache_lock:
        _cache.clear()
