"""Station profile: which kind of instrument station this MAST process serves.

MAST was built around one machine, a Nanonis STM. The optical station (a WITec
confocal Raman/SHG computer with no STM at all, design
``docs/v2/design/optical_station_witec.md``) runs the same code, the same install
package and the same jobs / tokens / e-stop / records, but the *surface* the AI sees
and the subsystems the process starts differ. That difference is one setting:

``stm``      (default) Every behaviour is exactly what it was before profiles
             existed. Nothing in this module changes anything on an STM station.
``optical``  No Nanonis is dialled and none of its threads start (state polling,
             the tip-crash watchdog, the current monitor); serial auto-detection is
             forced off; STM-only skills are withheld from the tool face (and refused
             at the external door); the briefing, ``observe`` and the home page show
             the optical station instead of a tip and a bias.

Where the value comes from, strongest first:

1. the environment variable ``MAST_STATION_PROFILE`` (tests, frozen deployments);
2. the persisted setting ``station_profile`` (``config/ui_settings.json``);
3. the default, ``stm``.

An unrecognised value is **not** an error and not a guess: it is treated as ``stm``
(the profile every existing machine runs) and a WARNING says so once. A typo must not
quietly turn a working STM station into something else.

Configured versus active
------------------------
Several consequences of the profile are decided once, when the runtime starts (which
threads exist, whether Nanonis is dialled). The setting can be edited at any time
afterwards. So there are two answers to "what profile is this":

* :func:`resolve` / :func:`configured_profile` - what the environment and the
  settings say *now* (what a restart would give);
* :func:`current_profile` - what the running process is. After
  :func:`note_boot` (called once by ``CoreRuntime.setup``) it is the profile the
  process started under, and **editing the setting does not change it** until the
  service is restarted. Everything that shapes the AI surface (tool face, briefing,
  ``observe``, auto-detection) and the UI reads this one, so the surface never
  disagrees with the subsystems actually running. Before any boot (unit tests,
  headless tools) it is simply the configured profile.

:func:`restart_pending` is the difference between the two.

The boot latch is only valid for the settings store that was published when it was
taken (``settings_store_for_runtime()``): the test suite publishes a fresh store for
every test, which retires a latch left behind by an earlier test without anyone having
to remember to reset it. In production the runtime publishes its store once and keeps
it, so the latch holds for the life of the process. The environment variable is never
latched: it is process-level by nature and tests set it per test.

Identity strings: ``"stm"`` and ``"optical"`` are compared, stored and sent over the
API. They are never wrapped in ``tr()``.
"""
from __future__ import annotations

import logging
import os
import threading
import weakref
from dataclasses import dataclass

logger = logging.getLogger(__name__)

PROFILE_STM = "stm"
PROFILE_OPTICAL = "optical"
PROFILES: tuple[str, ...] = (PROFILE_STM, PROFILE_OPTICAL)
DEFAULT_PROFILE = PROFILE_STM

#: the ``SettingsStore`` key (it must also be in ``KNOWN_KEYS``: an unlisted key is
#: silently dropped by ``update()``)
SETTINGS_KEY = "station_profile"
#: environment override: tests and frozen deployments
ENV_VAR = "MAST_STATION_PROFILE"

SOURCE_ENV = "env"
SOURCE_SETTINGS = "settings"
SOURCE_DEFAULT = "default"


@dataclass(frozen=True)
class ProfileInfo:
    """A resolved profile and where it came from."""

    profile: str                      # always one of PROFILES
    source: str                       # env | settings | default
    raw: str | None = None            # the unrecognised text, when the value was bad

    @property
    def valid(self) -> bool:
        """False when a value was present but unrecognised (the profile is then ``stm``)."""
        return self.raw is None


_lock = threading.Lock()
_warned: set[tuple[str, str]] = set()
#: (weak ref to the settings store published at boot, profile the process booted under)
_boot: tuple["weakref.ref", str] | None = None


def normalize(value: object) -> str | None:
    """``'Optical '`` → ``'optical'``; anything that is not a profile → ``None``."""
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    return v if v in PROFILES else None


def _warn_once(source: str, raw: object) -> None:
    key = (source, repr(raw))
    with _lock:
        if key in _warned:
            return
        _warned.add(key)
    logger.warning(
        "station profile: %s value %r is not one of %s - treating it as %r",
        source, raw, "/".join(PROFILES), DEFAULT_PROFILE)


def _store() -> object | None:
    try:
        from mast.webui.settings_store import settings_store_for_runtime

        return settings_store_for_runtime()
    except Exception as exc:  # noqa: BLE001 - an unreadable store is "not set"
        logger.debug("station profile: settings unreadable (%s)", exc)
        return None


def _stored_value() -> object:
    store = _store()
    try:
        return store.get(SETTINGS_KEY) if store is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("station profile: settings unreadable (%s)", exc)
        return None


def resolve() -> ProfileInfo:
    """What the environment and the settings say right now. Never raises."""
    env = os.environ.get(ENV_VAR)
    if env is not None and env.strip():
        got = normalize(env)
        if got is not None:
            return ProfileInfo(got, SOURCE_ENV)
        _warn_once(SOURCE_ENV, env)
        return ProfileInfo(DEFAULT_PROFILE, SOURCE_ENV, raw=env)
    stored = _stored_value()
    if stored is None or (isinstance(stored, str) and not stored.strip()):
        return ProfileInfo(DEFAULT_PROFILE, SOURCE_DEFAULT)
    got = normalize(stored)
    if got is not None:
        return ProfileInfo(got, SOURCE_SETTINGS)
    _warn_once(SOURCE_SETTINGS, stored)
    return ProfileInfo(DEFAULT_PROFILE, SOURCE_SETTINGS, raw=str(stored))


def configured_profile() -> str:
    """The profile the environment / settings say now (what a restart would give)."""
    return resolve().profile


def _booted_profile() -> str | None:
    """The boot latch, if it is still valid for the store in force."""
    with _lock:
        boot = _boot
    if boot is None:
        return None
    ref, profile = boot
    return profile if ref() is not None and ref() is _store() else None


def current_profile() -> str:
    """``'stm'`` or ``'optical'``: the profile this process runs as."""
    env = os.environ.get(ENV_VAR)
    if env is not None and env.strip():
        return resolve().profile            # process-level and never latched
    booted = _booted_profile()
    return booted if booted is not None else resolve().profile


def is_optical() -> bool:
    return current_profile() == PROFILE_OPTICAL


def is_stm() -> bool:
    return current_profile() == PROFILE_STM


def runtime_is_optical(rt: object) -> bool:
    """Whether a runtime-like object is an optical station: the profile its ``setup()``
    started under (``rt._station_profile``), else the process profile.

    Takes any object on purpose: several ``CoreRuntime`` methods are exercised with a stand-in
    for ``self`` (a ``SimpleNamespace``), and a method that calls ``self._is_optical()`` is
    an ``AttributeError`` on those."""
    prof = getattr(rt, "_station_profile", None)
    if isinstance(prof, str):
        return prof == PROFILE_OPTICAL
    return is_optical()


# ── the profile the running process was started under ────────────────────────────

def note_boot() -> str:
    """Latch (and log) the profile this process starts under. Called once by
    ``CoreRuntime.setup``, after it has published its settings store; the runtime
    decides what to start from the value this returns."""
    global _boot
    info = resolve()
    store = _store()
    with _lock:
        try:
            _boot = (weakref.ref(store), info.profile) if store is not None else None
        except TypeError:          # a store that cannot be weak-referenced: no latch
            _boot = None
    logger.info("station profile: %s (source: %s)%s", info.profile, info.source,
                "" if info.valid else f" - unrecognised value {info.raw!r} ignored")
    return info.profile


def boot_profile() -> str | None:
    """The profile the process was started under (``None`` before the runtime ran)."""
    return _booted_profile()


def restart_pending() -> bool:
    """True when the environment / settings now say something different from what the
    running process is, i.e. the operator changed the setting and has not restarted
    the service yet. Never true while the environment variable pins the profile."""
    return resolve().profile != current_profile()


def describe() -> dict:
    """What the health endpoint and the briefing say about the profile."""
    info = resolve()
    active = current_profile()
    return {"profile": active, "configured": info.profile, "source": info.source,
            "boot_profile": boot_profile(), "restart_pending": info.profile != active}


def reset() -> None:
    """Tests: drop the boot latch and the warned-once memory."""
    global _boot
    with _lock:
        _boot = None
        _warned.clear()


__all__ = [
    "DEFAULT_PROFILE", "ENV_VAR", "PROFILES", "PROFILE_OPTICAL", "PROFILE_STM", "ProfileInfo",
    "SETTINGS_KEY", "SOURCE_DEFAULT", "SOURCE_ENV", "SOURCE_SETTINGS", "boot_profile",
    "configured_profile", "current_profile", "describe", "is_optical", "is_stm", "normalize",
    "note_boot", "reset", "resolve", "restart_pending", "runtime_is_optical",
]
