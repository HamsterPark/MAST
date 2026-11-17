"""The comms breaker's HALF-OPEN probe must always come back.

After the cooldown the breaker lets exactly one probe through and refuses everyone else
until that probe reports (``record_success`` / ``record_failure``). The probe used to be
handed to whoever asked first, and several kinds of caller never report:

* ``count_health=False`` calls (current pump, aux / env / Z-burst pollers, scan-live);
* exits of ``safe_call`` that return without reporting: role lock busy, method not
  found, any non-socket exception.

Once such a caller held the probe, every later call on every role was refused, the
emergency withdraw (``urgent_call("ZCtrl_Withdraw", role="emergency")``) included,
while ``comms_snapshot()`` said ``half_open`` and ``comms_healthy()`` said True. Only a
full ``connect_all`` or a restart undid it.

Harness: the REAL ``ConnectionPool`` and ``CommsCircuitBreaker`` (only the breaker's
clock is fake), the REAL ``nanonis_spm`` client class loaded from disk with MAST's wire
patches (``tests/conftest.py`` replaces the importable module with a MagicMock, which
would pass anything), and one socketpair peer per role that speaks the Nanonis framing.
Every port in the config is 0, so a reconnect can never dial anything.

Run from repo root:
    .venv-v2-py313/Scripts/python.exe -m pytest tests/v2/unit/core/test_comms_breaker_probe.py -v
"""
from __future__ import annotations

# ── path setup BEFORE any mast.* imports ──
import sys
from pathlib import Path

_MASTV2_ROOT = str(Path(__file__).resolve().parents[4] / "MASTv2")
if sys.path[0] != _MASTV2_ROOT:
    while _MASTV2_ROOT in sys.path:
        sys.path.remove(_MASTV2_ROOT)
    sys.path.insert(0, _MASTV2_ROOT)
for _name in list(sys.modules):
    if _name == "mast" or _name.startswith("mast."):
        _f = getattr(sys.modules[_name], "__file__", "") or ""
        if "MASTv2" not in _f.replace("\\", "/"):
            del sys.modules[_name]

import functools
import socket
import struct
import threading
import time

import pytest

from mast.core.comms_health import CLOSED, HALF_OPEN, OPEN


# ─────────────────────────────────────────────────────────────────────
# The genuine client class, with MAST's wire patches
# ─────────────────────────────────────────────────────────────────────

@functools.lru_cache(maxsize=1)
def _real_nanonis_class():
    """``nanonis_spm.Nanonis`` from disk, patched the way ``core/nanonis_patch`` patches
    the production class (send framing + reply parsing). Same loader as
    ``test_nanonis_osci2t_wire.py::_real_nanonis_class``."""
    import importlib.util
    import sysconfig

    candidates = [Path(sysconfig.get_paths()["purelib"])]
    candidates += [Path(b) for b in sys.path if "_internal" not in b]
    candidates.append(Path(sys.executable).resolve().parents[1])
    for base in candidates:
        cand = base / "nanonis_spm" / "NanonisClass.py"
        if cand.exists():
            break
    else:
        pytest.skip("nanonis_spm package not installed on disk")

    spec = importlib.util.spec_from_file_location("_real_nanonis_breaker_probe", cand)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    from mast.core import nanonis_patch

    mod.Nanonis.send = nanonis_patch._patched_send
    mod.Nanonis.parseGeneralResponse = nanonis_patch._patched_parseGeneralResponse
    mod.Nanonis.decodeArray = nanonis_patch._patched_decodeArray
    mod.Nanonis.decodeArrayPrepended = nanonis_patch._patched_decodeArrayPrepended
    return mod.Nanonis


# ─────────────────────────────────────────────────────────────────────
# A Nanonis peer on the far end of a socketpair
# ─────────────────────────────────────────────────────────────────────

_ERR_OK = struct.pack(">Ii", 0, 0)           # error section: status 0, no description
_F32 = struct.pack(">f", 1.5e-10)
#: Reply bodies (before the error section) for the verbs these tests send.
_REPLIES = {"Bias.Get": _F32, "Current.Get": _F32, "Signals.ValGet": _F32}


def _frame(cmd: str, body: bytes) -> bytes:
    return (cmd.ljust(32, "\0").encode() + len(body).to_bytes(4, "big")
            + (0).to_bytes(2, "big") + bytes(2) + body)


def _read_exact(s: socket.socket, n: int) -> bytes | None:
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


class _Peer:
    """Answers Nanonis requests on one end of a socketpair; ``silent`` never answers
    (the far end of a link that stopped responding without closing)."""

    def __init__(self, *, silent: bool = False, timeout_s: float = 2.0) -> None:
        self.silent = silent
        self.calls: list[str] = []
        self.client, self._server = socket.socketpair()
        self.client.settimeout(timeout_s)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        s = self._server
        try:
            while True:
                hdr = _read_exact(s, 40)
                if hdr is None:
                    return
                cmd = hdr[:32].replace(b"\0", b"").decode()
                size = int.from_bytes(hdr[32:36], "big")
                if size and _read_exact(s, size) is None:
                    return
                self.calls.append(cmd)
                if not self.silent:
                    s.sendall(_frame(cmd, _REPLIES.get(cmd, b"") + _ERR_OK))
        except OSError:
            return

    def close(self) -> None:
        for x in (self.client, self._server):
            try:
                x.close()
            except OSError:
                pass


# ─────────────────────────────────────────────────────────────────────
# The rig: one real pool, four peers, a breaker on a fake clock
# ─────────────────────────────────────────────────────────────────────

class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class _Rig:
    ROLES = ("main", "monitor", "data", "emergency")

    def __init__(self) -> None:
        from mast.config import NanonisConfig
        from mast.core.comms_health import CommsCircuitBreaker
        from mast.core.connection import ConnectionPool

        self.clock = _Clock()
        self.pool = ConnectionPool(NanonisConfig(
            host="127.0.0.1", port_main=0, port_monitor=0, port_data=0,
            port_emergency=0, timeout_s=5.0, connect_timeout_s=0.3))
        self.breaker = CommsCircuitBreaker(clock=self.clock)
        self.pool._breaker = self.breaker
        self.peers: dict[str, _Peer] = {}
        for role in self.ROLES:
            self.plug(role)

    def plug(self, role: str, *, silent: bool = False, timeout_s: float = 2.0) -> _Peer:
        """Give *role* a fresh peer — the link on that role (re)appears."""
        old = self.peers.get(role)
        peer = _Peer(silent=silent, timeout_s=timeout_s)
        with self.pool._struct_lock:
            self.pool._connections[role] = (peer.client, _real_nanonis_class()(peer.client))
        self.peers[role] = peer
        if old is not None:
            old.close()
        return peer

    def trip(self, role: str = "monitor") -> None:
        """Three real TCP timeouts on *role* open the breaker; then Nanonis answers on
        *role* again (a fresh peer), so only the breaker stands in the way."""
        from mast.core.connection import is_transport_failure

        self.plug(role, silent=True, timeout_s=0.15)
        for _ in range(3):
            rec = self.pool.safe_call("Bias_Get", role=role)
            assert rec.error and is_transport_failure(rec), rec.error
        assert self.breaker.state() == OPEN
        self.plug(role)

    def elapse_cooldown(self) -> None:
        self.clock.t += self.breaker.cooldown_remaining_s() + 1.0
        assert self.breaker.state() == HALF_OPEN

    def close(self) -> None:
        for peer in self.peers.values():
            peer.close()


@pytest.fixture
def rig():
    r = _Rig()
    try:
        yield r
    finally:
        r.close()


def test_the_rig_speaks_the_real_wire(rig):
    """The harness itself: a counted call crosses the socketpair and parses."""
    rec = rig.pool.safe_call("Bias_Get", role="main")
    assert not rec.error, rec.error
    assert rec.return_value[2][0] == pytest.approx(1.5e-10)
    assert rig.peers["main"].calls == ["Bias.Get"]


# ══ 1. Only a call that reports back may take the probe ══════════════════

def test_an_uncounted_call_first_after_the_cooldown_does_not_wedge_the_pool(rig):
    """On the real pool.

    Before: the background poller's call (count_health=False) took the probe, succeeded,
    never reported — and main, monitor and the emergency withdraw were refused from
    then on, an hour later too, with ``comms_healthy()`` saying True."""
    rig.trip("monitor")
    rig.elapse_cooldown()

    rec = rig.pool.safe_call("Bias_Get", role="data", count_health=False)
    assert "comms_circuit_open" in rec.error, rec.error
    assert rig.peers["data"].calls == [], (
        "an uncounted call reached the socket while the breaker was not CLOSED")

    for dt in (0.0, 3600.0):
        rig.clock.t += dt
        for role in ("main", "monitor"):
            rec = rig.pool.safe_call("Bias_Get", role=role)
            assert not rec.error, f"{role} refused at +{dt:.0f} s: {rec.error}"
        rec = rig.pool.urgent_call("ZCtrl_Withdraw", 1, -1, role="emergency")
        assert not rec.error, f"emergency withdraw refused at +{dt:.0f} s: {rec.error}"
    assert rig.breaker.state() == CLOSED


def test_an_uncounted_call_never_touches_the_socket_unless_closed(rig):
    rig.trip("monitor")
    rec = rig.pool.safe_call("Bias_Get", role="data", count_health=False)
    assert "comms_circuit_open" in rec.error                     # OPEN
    rig.elapse_cooldown()
    rec = rig.pool.safe_call("Bias_Get", role="data", count_health=False)
    assert "comms_circuit_open" in rec.error                     # HALF_OPEN
    assert rig.peers["data"].calls == []
    assert rig.breaker.state() == HALF_OPEN, "the uncounted call moved the breaker"


def test_an_uncounted_call_flows_while_closed_and_reports_nothing(rig):
    rec = rig.pool.safe_call("Bias_Get", role="data", count_health=False)
    assert not rec.error, rec.error
    assert rig.peers["data"].calls == ["Bias.Get"]
    assert rig.breaker.snapshot()["streak"] == 0


# ══ 2. Every exit that took the probe gives it back ══════════════════════

class _Escape(BaseException):
    """Not an ``Exception``: leaves safe_call past its handlers, the way
    KeyboardInterrupt / SystemExit would."""


def _free_from_another_thread(lock) -> bool:
    """RLocks are re-entrant: only ANOTHER thread can tell whether one was left held."""
    out: list[bool] = []

    def _try():
        ok = lock.acquire(timeout=0.5)
        out.append(ok)
        if ok:
            lock.release()

    t = threading.Thread(target=_try, daemon=True)
    t.start()
    t.join(2.0)
    return bool(out and out[0])


def _probe_is_back(rig) -> None:
    """The probe is available again: a counted call on a free role takes it, succeeds
    and closes the breaker."""
    assert rig.breaker.snapshot()["probe_in_flight"] is False
    rec = rig.pool.safe_call("Bias_Get", role="monitor")
    assert not rec.error, f"the next counted call was refused: {rec.error}"
    assert rig.breaker.state() == CLOSED


def test_a_call_queued_on_a_busy_role_lock_never_holds_the_probe(rig):
    """Before: the probe was taken before the lock wait (up to 30 s, every other role
    refused meanwhile) and the RoleBusy exit left with it."""
    from mast.core.connection import is_lock_busy

    rig.trip("monitor")
    rig.elapse_cooldown()
    lock = rig.pool._role_lock("main")
    held, release = threading.Event(), threading.Event()

    def _occupy():
        with lock:
            held.set()
            release.wait(5.0)

    th = threading.Thread(target=_occupy, daemon=True)
    th.start()
    assert held.wait(2.0)
    try:
        rec = rig.pool.safe_call("Bias_Get", role="main", lock_timeout_s=0.2)
        assert is_lock_busy(rec), rec.error
        assert rig.peers["main"].calls == []
        _probe_is_back(rig)
    finally:
        release.set()
        th.join(5.0)


def test_a_method_that_does_not_exist_returns_the_probe(rig):
    rig.trip("monitor")
    rig.elapse_cooldown()
    rec = rig.pool.safe_call("Nope_NoSuchVerb", role="main")
    assert "not found" in rec.error, rec.error
    _probe_is_back(rig)


def test_an_exception_inside_the_call_returns_the_probe(rig):
    """A non-socket exception (the real client failing to frame a bad argument) becomes
    an error record, and never reaches record_success / record_failure."""
    from mast.core.connection import is_transport_failure

    rig.trip("monitor")
    rig.elapse_cooldown()
    rec = rig.pool.safe_call("Bias_Set", "not a number", role="main")
    assert rec.error and not is_transport_failure(rec), rec.error
    assert rig.peers["main"].calls == [], "the request should have failed before the wire"
    _probe_is_back(rig)


def test_an_exception_escaping_safe_call_returns_the_probe_and_the_lock(rig):
    rig.trip("monitor")
    rig.elapse_cooldown()

    def _boom(*_a):
        raise _Escape("interpreter going down")

    rig.pool.get("main").Boom_Get = _boom
    with pytest.raises(_Escape):
        rig.pool.safe_call("Boom_Get", role="main")
    assert _free_from_another_thread(rig.pool._role_lock("main"))
    _probe_is_back(rig)


def test_a_probe_that_reported_failure_is_not_handed_back(rig):
    """The return in ``finally`` must not undo a verdict: a failed probe re-opens the
    breaker with a fresh cooldown, and it stays open."""
    from mast.core.connection import is_transport_failure

    rig.trip("monitor")
    rig.elapse_cooldown()
    rig.plug("main", silent=True, timeout_s=0.15)
    rec = rig.pool.safe_call("Bias_Get", role="main")
    assert is_transport_failure(rec), rec.error
    assert rig.breaker.state() == OPEN
    assert rig.breaker.cooldown_remaining_s() > 19.0
    assert rig.breaker.snapshot()["probes_abandoned"] == 0


def test_a_probe_that_reported_success_is_not_handed_back(rig):
    rig.trip("monitor")
    rig.elapse_cooldown()
    rec = rig.pool.safe_call("Bias_Get", role="main")
    assert not rec.error, rec.error
    snap = rig.breaker.snapshot()
    assert snap["state"] == CLOSED
    assert snap["probes_issued"] == 1 and snap["probes_abandoned"] == 0


def test_abandon_frees_only_the_probe_it_names():
    from mast.core.comms_health import CommsCircuitBreaker

    clk = _Clock()
    b = CommsCircuitBreaker(clock=clk)
    for _ in range(3):
        b.record_failure("timeout")
    clk.t += 21.0
    ok, first = b.admit()
    assert ok and first > 0
    assert b.would_admit() is False and b.admit() == (False, 0)
    assert b.abandon_probe(first, "test") is True
    ok, second = b.admit()
    assert ok and second not in (0, first)
    assert b.abandon_probe(first) is False, "a stale number freed the newer probe"
    assert b.would_admit() is False
    assert b.abandon_probe(0) is False


def test_a_caller_that_will_not_report_is_never_given_the_probe():
    from mast.core.comms_health import CommsCircuitBreaker

    clk = _Clock()
    b = CommsCircuitBreaker(clock=clk)
    assert b.admit(may_probe=False) == (True, 0)            # CLOSED: it flows
    for _ in range(3):
        b.record_failure("timeout")
    clk.t += 21.0
    assert b.would_admit(may_probe=False) is False
    assert b.admit(may_probe=False) == (False, 0)
    ok, pid = b.admit()                                      # still there for a counted caller
    assert ok and pid > 0


# ══ 3. A probe that never reports expires after its own wire budget ══════

def _tripped_breaker():
    from mast.core.comms_health import CommsCircuitBreaker

    clk = _Clock()
    b = CommsCircuitBreaker(clock=clk)
    for _ in range(3):
        b.record_failure("timeout")
    clk.t += 21.0
    assert b.state() == HALF_OPEN
    return b, clk


def test_a_probe_that_never_reports_expires_after_its_lease():
    b, clk = _tripped_breaker()
    ok, stuck = b.admit(lease_s=30.0)
    assert ok and stuck
    clk.t += 29.0
    assert b.would_admit() is False and b.admit() == (False, 0)
    assert b.state() == HALF_OPEN
    clk.t += 2.0                                     # 31 s: past the 30 s lease
    assert b.would_admit() is True
    assert b.snapshot()["probe_in_flight"] is False
    ok, fresh = b.admit()
    assert ok and fresh not in (0, stuck)
    snap = b.snapshot()
    assert snap["probes_expired"] == 1 and snap["probe_in_flight"] is True
    assert b.abandon_probe(stuck) is False, "the expired holder freed its successor"


def test_the_default_lease_applies_when_the_caller_names_none():
    from mast.core.comms_health import _PROBE_LEASE_S

    b, clk = _tripped_breaker()
    ok, _pid = b.admit()
    assert ok
    assert b.snapshot()["probe_lease_s"] == pytest.approx(_PROBE_LEASE_S)
    clk.t += _PROBE_LEASE_S - 1.0
    assert b.would_admit() is False
    clk.t += 2.0
    assert b.would_admit() is True


def test_the_pool_recovers_from_a_leaked_probe_once_its_lease_runs_out(rig):
    """Defence in depth: a probe taken by a path that never reports (simulated by
    taking one straight from the breaker) blocks for one lease, not forever."""
    rig.trip("monitor")
    rig.elapse_cooldown()
    ok, leaked = rig.breaker.admit(lease_s=40.0)
    assert ok and leaked
    rec = rig.pool.safe_call("Bias_Get", role="main")
    assert "comms_circuit_open" in rec.error and rig.peers["main"].calls == []
    rig.clock.t += 41.0
    rec = rig.pool.safe_call("Bias_Get", role="main")
    assert not rec.error, rec.error
    assert rig.breaker.state() == CLOSED
    assert rig.breaker.snapshot()["probes_expired"] == 1


def test_safe_call_leases_the_probe_for_its_own_wire_budget(rig):
    """A long spectroscopy call must not lose the probe mid-sweep; an ordinary call that
    leaked must not block everyone for a long call's worth."""
    rig.trip("monitor")
    rig.elapse_cooldown()
    nn = rig.pool.get("main")
    seen: dict = {}

    def _spy(*_a):
        seen.update(rig.breaker.snapshot())        # the breaker as the probe sees it
        return nn.Bias_Get()

    nn.Spy_Get = _spy
    rec = rig.pool.safe_call("Spy_Get", role="main", recv_timeout_s=600.0)
    assert not rec.error, rec.error
    assert seen["probe_in_flight"] is True
    assert seen["probe_lease_s"] == pytest.approx(rig.pool._probe_lease_s(600.0), abs=0.01)
    assert seen["probe_lease_s"] >= 1200.0


def test_the_lease_budget_is_two_tries_plus_one_reconnect(rig):
    from mast.core import connection as C
    from mast.core.comms_health import _PROBE_LEASE_S

    pool = rig.pool
    ordinary = 2 * 5.0 + 0.3 + C._STALE_DRAIN_TOTAL_S + C._PROBE_LEASE_SLACK_S
    assert pool._probe_lease_s(None) == pytest.approx(ordinary)
    longest = pool._probe_lease_s(C._MAX_CALL_RECV_TIMEOUT_S)
    assert pool._probe_lease_s(1e9) == pytest.approx(longest)
    assert pool._probe_lease_s(float("nan")) == pytest.approx(longest)
    # The breaker's default (for callers that name no budget) covers an ordinary call
    # given the module-open grace, which is what its comment claims.
    assert _PROBE_LEASE_S >= pool._probe_lease_s(C._MODULE_OPEN_RECV_S)


# ══ 4. comms_healthy() means CLOSED; "recovering" is not "healthy" ═══════

def test_comms_healthy_means_verified_not_merely_not_open(rig):
    assert rig.pool.comms_healthy() is True
    rig.trip("monitor")
    assert rig.pool.comms_healthy() is False                    # OPEN
    rig.elapse_cooldown()
    assert rig.pool.comms_snapshot()["state"] == HALF_OPEN
    assert rig.pool.comms_healthy() is False, "a breaker nobody has verified read as healthy"
    rec = rig.pool.safe_call("Bias_Get", role="monitor")        # the counted probe
    assert not rec.error, rec.error
    assert rig.pool.comms_healthy() is True


def test_an_env_sensor_parks_with_zero_tcp_while_the_breaker_recovers(rig):
    """NanonisSignalSensor gates its uncounted read on comms_healthy(). With HALF_OPEN
    read as healthy it went on to send that read (which could take the
    probe); now it reports unavailable and stays off the wire."""
    from mast.environment.nanonis_env import NanonisSignalSensor

    sensor = NanonisSignalSensor(lambda: rig.pool, name="field", signal_index=3)
    assert sensor.read().status == "ok"                         # CLOSED: reads on "data"
    assert rig.peers["data"].calls == ["Signals.ValGet"]
    rig.trip("monitor")
    rig.elapse_cooldown()
    reading = sensor.read()
    assert reading.status == "unavailable", reading
    assert rig.peers["data"].calls == ["Signals.ValGet"], "it touched the socket while not CLOSED"


# ══ 5. An operator reconnect re-arms one probe; it does not declare health ══

def test_an_operator_reconnect_ends_the_cooldown_with_a_probe_not_a_close(rig):
    rig.trip("monitor")
    assert rig.breaker.state() == OPEN and rig.breaker.cooldown_remaining_s() > 19.0
    rig.pool.reconnect_role("main", "operator")       # ports are 0: the reconnect itself fails
    assert rig.breaker.state() == HALF_OPEN, "the reconnect declared the link healthy"
    assert rig.breaker.cooldown_remaining_s() == 0.0
    assert rig.breaker.snapshot()["streak"] >= 3
    rec = rig.pool.safe_call("Bias_Get", role="monitor")          # the next counted call probes
    assert not rec.error, rec.error
    assert rig.breaker.state() == CLOSED


def test_after_a_reconnect_a_failed_probe_reopens_at_once(rig):
    from mast.core.connection import is_transport_failure

    rig.trip("monitor")
    rig.pool.reconnect_role("main", "operator")
    rig.plug("main", silent=True, timeout_s=0.15)
    rec = rig.pool.safe_call("Bias_Get", role="main")
    assert is_transport_failure(rec), rec.error
    assert rig.breaker.state() == OPEN
    assert rig.breaker.cooldown_remaining_s() > 19.0


def test_a_reconnect_stops_waiting_for_a_probe_that_is_not_coming_back(rig):
    rig.trip("monitor")
    rig.elapse_cooldown()
    ok, stuck = rig.breaker.admit()
    assert ok and stuck
    assert "comms_circuit_open" in rig.pool.safe_call("Bias_Get", role="main").error
    rig.pool.reconnect_role("main", "operator")
    rec = rig.pool.safe_call("Bias_Get", role="main")
    assert not rec.error, rec.error
    assert rig.breaker.state() == CLOSED


def test_a_reset_while_a_real_probe_is_in_flight_lets_the_next_counted_call_through(rig):
    """A probe that is genuinely out: a counted call sits in recv
    on a peer that never answers when the operator resets. The next counted call must go
    through at once instead of waiting for that probe, and the stuck probe's eventual
    failure is an ordinary failure, not a fresh wedge."""
    from mast.core.connection import is_transport_failure

    rig.trip("monitor")
    rig.elapse_cooldown()
    rig.plug("main", silent=True, timeout_s=1.5)
    out: dict = {}
    th = threading.Thread(
        target=lambda: out.setdefault("rec", rig.pool.safe_call("Bias_Get", role="main")),
        daemon=True)
    th.start()
    deadline = time.monotonic() + 1.0
    while not rig.breaker.snapshot()["probe_in_flight"]:
        assert time.monotonic() < deadline, "the probe never went out"
        time.sleep(0.01)
    assert "comms_circuit_open" in rig.pool.safe_call("Bias_Get", role="monitor").error
    rig.pool.reconnect_role("main", "operator")
    rec = rig.pool.safe_call("Bias_Get", role="monitor")          # the next counted call
    assert not rec.error, f"held up by the probe the reset should have cleared: {rec.error}"
    assert rig.breaker.state() == CLOSED
    th.join(5.0)
    assert is_transport_failure(out["rec"]), out["rec"].error
    assert rig.breaker.state() == CLOSED, "the old probe's late failure re-opened the breaker"


def test_a_reconnect_leaves_a_closed_breaker_alone(rig):
    rig.pool.reconnect_role("main", "operator")
    snap = rig.breaker.snapshot()
    assert snap["state"] == CLOSED
    assert snap["tripped_total"] == 0 and snap["probes_issued"] == 0


def test_the_safety_page_reconnect_rearms_the_real_breaker(rig):
    """POST /api/safety/reconnect-role with the real pool behind it: the button the
    operator actually presses."""
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from mast.api.context import AppContext
    from mast.api.routes.safety import router

    api = FastAPI()
    ctx = AppContext()
    ctx.live_app = SimpleNamespace(_pool=rig.pool)
    api.state.ctx = ctx
    api.include_router(router, prefix="/api")

    rig.trip("monitor")
    r = TestClient(api).post("/api/safety/reconnect-role",
                             json={"role": "main", "reason": "test"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert rig.breaker.state() == HALF_OPEN
    rec = rig.pool.safe_call("Bias_Get", role="main")
    assert not rec.error, rec.error
    assert rig.breaker.state() == CLOSED


# ══ 6. Emergency traffic is never refused by the breaker (behaviour change) ══

def test_the_emergency_port_is_never_refused_by_an_open_breaker(rig):
    rig.trip("monitor")
    rec = rig.pool.safe_call("ZCtrl_Withdraw", 1, -1, role="emergency")
    assert not rec.error, rec.error
    assert rig.peers["emergency"].calls == ["ZCtrl.Withdraw"]
    assert rig.breaker.state() == CLOSED, "it is still reported: the link answered"


def test_the_urgent_withdraw_fallback_on_main_is_never_refused(rig):
    rig.trip("monitor")
    rec = rig.pool.urgent_call("ZCtrl_Withdraw", 1, -1, role="main")
    assert not rec.error, rec.error
    assert rig.peers["main"].calls == ["ZCtrl.Withdraw"]


def test_routine_traffic_is_still_refused_while_open(rig):
    rig.trip("monitor")
    rec = rig.pool.safe_call("Bias_Get", role="main")
    assert "comms_circuit_open" in rec.error, rec.error
    rec = rig.pool.safe_call("ZCtrl_Withdraw", 1, -1, role="main")     # not via urgent_call
    assert "comms_circuit_open" in rec.error, rec.error
    assert rig.peers["main"].calls == []


def test_a_failed_emergency_call_is_still_reported(rig):
    from mast.core.connection import is_transport_failure

    rig.trip("monitor")
    streak = rig.breaker.snapshot()["streak"]
    rig.plug("emergency", silent=True, timeout_s=0.15)
    rec = rig.pool.urgent_call("ZCtrl_Withdraw", 1, -1, role="emergency")
    assert is_transport_failure(rec), rec.error
    assert rig.breaker.snapshot()["streak"] == streak + 1
    assert rig.breaker.state() == OPEN


def test_an_emergency_call_goes_out_while_a_probe_is_in_flight(rig):
    rig.trip("monitor")
    rig.elapse_cooldown()
    ok, probe = rig.breaker.admit()
    assert ok and probe
    rec = rig.pool.urgent_call("ZCtrl_Withdraw", 1, -1, role="emergency")
    assert not rec.error, rec.error
    assert rig.peers["emergency"].calls == ["ZCtrl.Withdraw"]


def _occupy(lock, hold_s: float):
    """Hold *lock* from another thread: an emergency call stuck on a dead link."""
    held, release = threading.Event(), threading.Event()

    def _run():
        with lock:
            held.set()
            release.wait(hold_s)

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    assert held.wait(2.0)
    return release, th


def test_an_emergency_call_let_past_the_breaker_does_not_queue(rig):
    """What the gate used to buy, made concrete: a stuck emergency call holds the role
    lock. When the gate was written that lock was unbounded and the socket wait could be
    ~1000 s, so later emergency calls queued behind it. Let past the breaker, an
    emergency call waits at most EMERGENCY_LOCK_TIMEOUT_S for the lock - also a plain
    safe_call asking for the 30 s default, or for "forever" (0)."""
    from mast.core.connection import EMERGENCY_LOCK_TIMEOUT_S, is_lock_busy

    rig.trip("monitor")
    release, th = _occupy(rig.pool._role_lock("emergency"), 10.0)
    try:
        for kw in ({}, {"lock_timeout_s": 0}):
            t0 = time.perf_counter()
            rec = rig.pool.safe_call("ZCtrl_Withdraw", 1, -1, role="emergency", **kw)
            waited = time.perf_counter() - t0
            assert is_lock_busy(rec), rec.error
            assert waited < EMERGENCY_LOCK_TIMEOUT_S + 1.5, f"queued {waited:.1f} s with {kw}"
        assert rig.peers["emergency"].calls == []
    finally:
        release.set()
        th.join(5.0)


def test_while_closed_an_emergency_call_keeps_its_own_lock_wait(rig):
    """The cap applies only where the bypass changes the outcome: nothing changes while
    the breaker is CLOSED."""
    from mast.core.connection import EMERGENCY_LOCK_TIMEOUT_S, is_lock_busy

    release, th = _occupy(rig.pool._role_lock("emergency"), 10.0)
    try:
        asked = EMERGENCY_LOCK_TIMEOUT_S + 0.8
        t0 = time.perf_counter()
        rec = rig.pool.safe_call("ZCtrl_Withdraw", 1, -1, role="emergency",
                                 lock_timeout_s=asked)
        waited = time.perf_counter() - t0
        assert is_lock_busy(rec), rec.error
        assert waited >= asked - 0.2, f"capped while CLOSED: waited {waited:.2f} s of {asked} s"
    finally:
        release.set()
        th.join(5.0)


def test_the_emergency_lock_wait_cap():
    from mast.core.connection import EMERGENCY_LOCK_TIMEOUT_S as cap
    from mast.core.connection import _emergency_lock_wait as wait

    assert wait(None) == cap and wait(0) == cap and wait(-1.0) == cap
    assert wait(float("nan")) == cap and wait(30.0) == cap
    assert wait(0.5) == 0.5
