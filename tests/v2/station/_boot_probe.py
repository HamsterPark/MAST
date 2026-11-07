"""Boot a real ``CoreRuntime`` in a child process and report what it started.

Used by ``test_station_runtime.py``. It is a child process, and not a function called in
the test process, because ``CoreRuntime.setup()`` starts daemons, publishes process-wide
holders and registers ``atexit`` hooks: run inside the pytest process it would leave all of
that behind for the next test.

    python _boot_probe.py <stm|optical> <project_root>

Prints one JSON line on stdout. **Nothing here dials a socket**: ``ConnectionPool.connect_all``
is replaced by a recorder (a test must never reach a real Nanonis on the machine it runs on).
The other Nanonis-bound starts are wrapped, not replaced: they are recorded and still run.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading

profile, root = sys.argv[1], sys.argv[2]
os.environ["MAST2_PROJECT_ROOT"] = root
os.environ["MAST_STATION_PROFILE"] = profile
os.environ.setdefault("MAST_LANG", "zh")

try:
    import nanonis_spm  # noqa: F401
except Exception:  # the real vendor package is not needed
    from unittest.mock import MagicMock

    sys.modules["nanonis_spm"] = MagicMock()

logging.disable(logging.CRITICAL)
calls: list[str] = []

import mast.core.connection as conn  # noqa: E402
import mast.core.state as state_mod  # noqa: E402
import mast.core.watchdog as watchdog_mod  # noqa: E402
import mast.monitoring.service as monitor_service  # noqa: E402


def _no_dial(self):
    calls.append("connect_all")
    return {role: False for role in ("main", "monitor", "data", "emergency")}


conn.ConnectionPool.connect_all = _no_dial

_orig_refresh = state_mod.InstrumentState.start_background_refresh


def _refresh(self, *a, **k):
    calls.append("start_background_refresh")
    return _orig_refresh(self, *a, **k)


state_mod.InstrumentState.start_background_refresh = _refresh

_orig_wd = watchdog_mod.SafetyWatchdog.start


def _wd(self, *a, **k):
    calls.append("watchdog.start")
    return _orig_wd(self, *a, **k)


watchdog_mod.SafetyWatchdog.start = _wd

_orig_cm = monitor_service.start_service


def _cm(*a, **k):
    calls.append("current_monitor.start_service")
    return _orig_cm(*a, **k)


monitor_service.start_service = _cm

from mast.config import MASTConfig  # noqa: E402
from mast.core.runtime import CoreRuntime  # noqa: E402

rt = CoreRuntime(MASTConfig())
rt.setup()
from mast import station  # noqa: E402

out = {
    "calls": calls,
    "profile": rt._station_profile,
    "station": station.describe(),
    "ports": rt.config.nanonis.all_ports,
    "pool": type(rt._pool).__name__ if rt._pool is not None else None,
    "state": type(rt._state).__name__ if rt._state is not None else None,
    "executor": type(rt._executor).__name__ if rt._executor is not None else None,
    "threads": sorted(t.name for t in threading.enumerate()),
}
try:
    rt.shutdown()
except Exception as exc:  # noqa: BLE001
    out["shutdown_error"] = str(exc)
sys.stdout.write("PROBE_JSON " + json.dumps(out) + "\n")
sys.stdout.flush()
os._exit(0)          # no atexit replay of the hooks setup() registered
