"""Field control on the Lake Shore 625 (7.2.0): ``mast.instruments.magnet_625`` and its skills.

What is pinned here:

* the only bytes field control can build are ``SETI <A>`` (negative only within the supply's
  LIMIT, by magnitude; never NaN or inf) and ``STOP``. The read-only driver still cannot build
  either, and does not import the write module;
* every gate refuses (never clamps), and a refused change writes nothing;
* the conversion uses the configured field constant, cross-checked against FLDS?;
* a supply frozen by STOP (ramp-done bit left at 0, the worst case the manual allows) does
  not lock MAST out of the next change;
* supervision ends correctly on arrival (a residual beyond 5 mA included), abort, external
  change, quench / hardware error (nothing written), other errors, lost contact and stall;
* "stopped" is only reported for a STOP that went out; one that did not is retried, and the
  ramp stays busy meanwhile;
* the slow checks run without holding the link (no lock-order deadlock with the monitor);
* E-STOP / shutdown / the operator's STOP button reach the supply directly, and scans that
  close the port are refused while a ramp is busy;
* reverse field: a negative target is checked by |B| and |I|, needs the user's
  words to name a negative field, never crosses zero, and while the operator has not verified
  that the ramp segments act on |I| on the negative side it may not go beyond the first
  segment's upper current. The Sim625 is bipolar and can model segments that do NOT apply on
  the negative side.
"""
from __future__ import annotations

import ast
import json
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from mast.core import magnet_control
from mast.core.instrument_lock import BYPASS_NAMES, needs_token
from mast.core.types import SafetyLevel, SkillCategory
from mast.environment import lakeshore_625 as m625
from mast.environment.serial_transport import SerialUnavailable
from mast.instruments import magnet_625 as mc
from tests.v2.environment.test_magnet_and_level_meter import IDN_625, REAL_625

K = 0.3406                      # T/A, the constant the recorded supply reports
SEGMENTS = {1: (9.0, 0.010), 2: (12.0, 0.005), 3: (14.0, 0.002), 4: (17.0, 0.001),
            5: (17.0, 0.001)}


class Sim625:
    """A Model 625 behind a pinned-generation transport that actually ramps.

    ``SETI x`` moves the set point; every ``RDGI?`` moves the output one ``step_a``
    towards it. ``STOP`` freezes the output and leaves the set point and the ramp-done
    bit (0) alone: the manual does not say what they read after STOP, and this is the
    case that could lock MAST out.
    """

    supports_generations = True

    def __init__(self, *, current=0.0, setpoint=0.0, step_a=5.0, accept_writes=True):
        self.current = float(current)
        self.setpoint = float(setpoint)
        self.step_a = step_a
        self.accept_writes = accept_writes
        self.frozen = False
        self.read_offset_a = 0.0        # RDGI? reads this much above the true output
        self.flds = "0,+0.34060"
        self.limit = "+15.0000,+2.0000,+0.0280"
        self.rseg = 1
        self.xpgm = 0
        self.pshs = "0,+010,+005"
        self.qnch = "1,+0.2000"
        self.erst = [0, 0, 0]
        self.erst_text = None           # overrides the ERST? reply when set
        self.idn = IDN_625
        self.writes: list[bytes] = []
        self.queries: list[str] = []
        self.fail_queries = 0          # this many next queries raise SerialUnavailable
        self.fail_stop_writes = 0      # this many next STOP writes raise
        self.raise_on_seti = False     # the SETI write raises after its bytes are out
        self.on_query = None           # callable(sim, cmd) before answering
        self.setpoint_after_seti = None  # what SETI? reads after a write (None = honest)
        self.segments = dict(SEGMENTS)   # RSEGS? n -> (upper current, rate)
        self.base_rate_a_s = 0.010       # the rate outside the segment table
        self.rate_text = None            # overrides the RATE? reply when set
        #: False: the supply does NOT look the segment table up on the negative side (the
        #: case the manual leaves open); it then ramps at the base RATE there.
        self.negative_segments_apply = True
        self.rate_log: list[float] = []  # modelled ramp rate at each step of a moving output
        self._gen = 0
        self._open = False
        self.settings = SimpleNamespace(port="COM13")

    # transport protocol
    @property
    def open_generation(self):
        return self._gen

    @property
    def is_open(self):
        return self._open

    def close(self):
        self._open = False

    def rate_at(self, current_a: float) -> float:
        """Modelled ramp rate (A/s) at an output current: segments looked up by |I|."""
        if current_a < 0 and not self.negative_segments_apply:
            return self.base_rate_a_s
        for n in sorted(self.segments):
            upper, rate = self.segments[n]
            if abs(current_a) < upper and rate > 0:
                return rate
        return self.base_rate_a_s

    def _moving(self) -> bool:
        return not self.frozen and abs(self.current - self.setpoint) > 1e-9

    def _reply(self, cmd: str) -> str:
        if cmd == "*IDN?":
            return self.idn.decode().strip()
        if cmd == "RDGI?":
            if self._moving():
                self.rate_log.append(self.rate_at(self.current))
                d = self.setpoint - self.current
                self.current += max(-self.step_a, min(self.step_a, d))
            return f"{self.current + self.read_offset_a:+08.4f}"
        if cmd == "SETI?":
            return f"{self.setpoint:+08.4f}"
        if cmd == "RATE?" and self.rate_text is not None:
            return self.rate_text
        if cmd == "RDGF?":
            return f"{self.current * K:+.4E}"
        if cmd == "OPST?":
            done = abs(self.current - self.setpoint) <= 1e-9
            return f"{(0b010 if done else 0) | 0b100:03d}"
        if cmd == "ERST?":
            return self.erst_text or ",".join(f"{c:03d}" for c in self.erst)
        if cmd == "FLDS?":
            return self.flds
        if cmd == "LIMIT?":
            return self.limit
        if cmd == "RSEG?":
            return str(self.rseg)
        if cmd.startswith("RSEGS? "):
            upper, rate = self.segments[int(cmd.split()[1])]
            return f"{upper:+08.4f},{rate:+.4f}"
        if cmd == "XPGM?":
            return str(self.xpgm)
        if cmd == "PSHS?":
            return self.pshs
        if cmd == "QNCH?":
            return self.qnch
        raw = REAL_625.get(cmd)
        if raw is None:
            raise AssertionError(f"unexpected query {cmd!r}")
        return raw.decode().strip()

    def transact(self, payload, *, read_until=None, read_size=None, timeout=None,
                 lines=1, expect_generation=None):
        if expect_generation is not None and (not self._open or self._gen != expect_generation):
            raise SerialUnavailable("pinned: handle not identity-verified")
        if not self._open:
            self._open = True
            self._gen += 1
        cmd = payload.decode("ascii").strip()
        if "?" not in cmd:
            assert read_size == 0, "a write must not wait for a reply"
            if cmd == "STOP" and self.fail_stop_writes:
                self.fail_stop_writes -= 1
                raise SerialUnavailable("simulated write failure")
            self.writes.append(bytes(payload))
            if cmd == "STOP":
                self.frozen = True
            elif cmd.startswith("SETI ") and self.accept_writes:
                self.setpoint = float(cmd.split()[1])
                self.frozen = False
                if self.setpoint_after_seti is not None:
                    self.setpoint = self.setpoint_after_seti
                if self.raise_on_seti:
                    raise SerialUnavailable("simulated: write raised after the bytes left")
            return b""
        self.queries.append(cmd)
        if self.fail_queries:
            self.fail_queries -= 1
            raise SerialUnavailable("simulated read failure")
        if self.on_query is not None:
            self.on_query(self, cmd)
        return (self._reply(cmd) + "\r\n").encode("ascii")

    def setis(self) -> list[float]:
        return [float(w.decode().split()[1]) for w in self.writes if w.startswith(b"SETI ")]

    def stops(self) -> int:
        return sum(1 for w in self.writes if w == b"STOP\r\n")


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.now += max(0.0, float(s))


def _settings(**over):
    base = dict(max_field_t=5.0, field_constant_t_per_a=K, temperature_channel="Magnet",
                max_temperature_k=4.5)
    base.update(over)
    return mc.MagnetSettings(**base)


def _temp(value=3.9, age=1.0):
    return lambda channel: SimpleNamespace(value_k=value, age_s=age,
                                           channel=f"{channel} (COM14)",
                                           reason=None if value is not None else "unavailable")


def _parked():
    return SimpleNamespace(state="parked", reason="Z at the retracted end, feedback off")


def _auth(up_to):
    return {"user_authorization": "液面没问题，升吧", "authorized_up_to_t": up_to}


#: What core.heater_control.interlock answers on a station without a Lake Shore 335.
NO_335 = {"ok": True, "present": False, "detail": "no temperature controller (Lake Shore 335)"}


def _make(tmp_path, sim=None, *, settings=None, temperature=None, clock=None,
          heating=None, heating_now=None):
    sim = sim or Sim625()
    link = m625.Model625Link(sim, port="COM13", min_gap_s=0.0)
    clk = clock or Clock()
    ctl = mc.Magnet625Controller(
        link, settings=lambda: settings or _settings(),
        temperature=temperature or _temp(),
        ledger_path=lambda: tmp_path / "ledger.jsonl",
        clock=clk, sleep=clk.sleep,
        heating_interlock=heating or (lambda: dict(NO_335)),
        heating_now=heating_now or (lambda: False))
    return sim, ctl, clk


def _ledger(tmp_path) -> list[dict]:
    p = tmp_path / "ledger.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


# ── what may reach the wire ──────────────────────────────────────────────────
def test_build_write_makes_only_seti_and_stop():
    assert mc.build_write("STOP") == b"STOP\r\n"
    assert mc.build_write("SETI", 14.68) == b"SETI 14.6800\r\n"
    assert mc.build_write("SETI", 0) == b"SETI 0.0000\r\n"
    assert mc.build_write("SETI", -0.0) == b"SETI 0.0000\r\n"
    for bad in [("SETI", -0.001),             # a negative current needs the instrument's LIMIT
                ("SETI", float("nan")), ("SETI", float("inf")),
                ("SETI", True), ("SETI", "5"), ("SETI", None), ("SETI", 60.0001),
                ("STOP", 1.0), ("SETF", 1.0), ("RATE", 0.1), ("*RST", None),
                ("PSH", 1), ("ERCL", None), ("MODE", 1), ("SETI 5;STOP", None)]:
        with pytest.raises(ValueError):
            mc.build_write(*bad)


def test_build_write_sends_a_negative_current_only_inside_the_instrument_limit():
    assert mc.build_write("SETI", -5.0, limit_a=15.0) == b"SETI -5.0000\r\n"
    assert mc.build_write("SETI", -15.0, limit_a=15.0) == b"SETI -15.0000\r\n"     # the limit itself
    assert mc.build_write("SETI", 15.0, limit_a=15.0) == b"SETI 15.0000\r\n"
    assert mc.build_write("SETI", -0.00001, limit_a=15.0) == b"SETI 0.0000\r\n"   # never "-0.0000"
    assert mc.build_write("SETI", 0, limit_a=0.0) == b"SETI 0.0000\r\n"
    nan, inf = float("nan"), float("inf")
    for current, limit in [(-5.0, None), (-0.001, None),              # no LIMIT given, no negative
                           (-15.0001, 15.0), (15.0001, 15.0), (-16.0, 15.0), (16.0, 15.0),
                           (-60.0001, 100.0), (60.0001, 100.0),       # the 60 A command ceiling
                           (nan, 15.0), (-inf, 15.0), (inf, 15.0), (True, 15.0), ("-5", 15.0),
                           (-5.0, nan), (-5.0, inf), (-5.0, -1.0), (-5.0, True), (-5.0, "15")]:
        with pytest.raises(ValueError):
            mc.build_write("SETI", current, limit_a=limit)


def test_the_read_only_driver_still_cannot_build_a_setter():
    for cmd in ("SETI 5", "STOP", "SETI", "SETF 1"):
        with pytest.raises(m625.Model625Error):
            m625.build_query(cmd)


def _module_source(mod) -> str:
    return Path(mod.__file__).read_text(encoding="utf-8")


def test_the_read_path_does_not_import_the_write_module():
    import mast.environment as env_pkg

    env_dir = Path(env_pkg.__file__).parent
    for py in env_dir.glob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "magnet_625" not in node.module, f"{py.name} imports the write module"
            if isinstance(node, ast.Import):
                assert not any("magnet_625" in a.name for a in node.names), py.name


def test_every_transact_in_the_write_module_sends_build_write_bytes():
    """Each ``.transact(payload, ...)`` gets a name that was assigned from build_write()."""
    tree = ast.parse(_module_source(mc))
    built: set[str] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and getattr(node.value.func, "id", "") == "build_write"):
            built |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "attr", "") == "transact"]
    assert calls, "self-check: no transact call found"
    for c in calls:
        arg = c.args[0]
        assert isinstance(arg, ast.Name) and arg.id in built, ast.unparse(c)


def test_no_other_module_writes_seti_or_stop():
    import mast

    root = Path(mast.__file__).parent
    allowed = {root / "instruments" / "magnet_625.py"}
    pat = re.compile(r"""b["'](SETI |SETF |STOP)""")
    offenders = [str(p.relative_to(root)) for p in root.rglob("*.py")
                 if p not in allowed and pat.search(p.read_text(encoding="utf-8", errors="replace"))]
    assert not offenders, offenders


# ── gates ────────────────────────────────────────────────────────────────────
def _plan(ctl, target, **kw):
    kw.setdefault("tip_verdict", _parked)
    return ctl.plan(target, **kw)


def _refused(plan) -> set[str]:
    return {c.id for c in plan.refusals}


def test_a_clean_raise_passes_every_gate_and_writes_nothing(tmp_path):
    sim, ctl, _ = _make(tmp_path)
    p = _plan(ctl, 1.0, user_authorization="液面没问题，升到 1 T", authorized_up_to_t=1.0)
    assert p.ok, p.refusal_text()
    assert p.raising is True and p.target_a == round(1.0 / K, 4)
    assert p.eta_s == pytest.approx(round(1.0 / K, 4) / 0.010, rel=1e-6)
    assert sim.writes == []


def test_not_configured_refuses_before_touching_the_supply(tmp_path):
    for s in (_settings(max_field_t=None), _settings(field_constant_t_per_a=None),
              _settings(field_constant_t_per_a=0.0)):
        sim, ctl, _ = _make(tmp_path, settings=s)
        p = _plan(ctl, 0.1, **_auth(0.1))
        assert _refused(p) == {"configured"} and sim.queries == [] and sim.writes == []


@pytest.mark.parametrize("target, gate", [(-5.01, "max_field"), (5.01, "max_field"),
                                          ("abc", "target"), (float("nan"), "target"),
                                          (True, "target"), (False, "target")])
def test_bad_targets_are_refused_not_clamped(tmp_path, target, gate):
    sim, ctl, _ = _make(tmp_path)
    p = _plan(ctl, target, **_auth(9.0))
    assert gate in _refused(p)
    assert sim.writes == []


@pytest.mark.parametrize("mutate, gate", [
    (lambda s: setattr(s, "flds", "0,+0.10000"), "field_constant"),     # DFLT reset
    (lambda s: setattr(s, "flds", "1,+3.40600"), "field_constant"),     # kG/A
    (lambda s: setattr(s, "limit", "+02.0000,+2.0000,+0.0280"), "supply_limit"),
    (lambda s: setattr(s, "erst", [0, 8, 0]), "no_errors"),
    (lambda s: setattr(s, "xpgm", 1), "internal_program"),
    (lambda s: setattr(s, "pshs", "1,+010,+005"), "no_persistent_switch"),
    (lambda s: setattr(s, "rseg", 0), "segment_ramping"),
    (lambda s: setattr(s, "qnch", "0,+0.2000"), "quench_detection"),
    (lambda s: setattr(s, "erst_text", "000,000"), "readable"),         # a field missing
])
def test_supply_state_gates(tmp_path, mutate, gate):
    sim, ctl, _ = _make(tmp_path)
    mutate(sim)
    p = _plan(ctl, 1.0, **_auth(1.0))
    assert gate in _refused(p), p.refusal_text()


def test_a_negative_output_is_no_longer_refused_and_goes_back_to_zero(tmp_path):
    sim, ctl, _ = _make(tmp_path, Sim625(current=-0.5, setpoint=-0.5))
    p = _plan(ctl, 0.0)
    assert p.ok and p.raising is False, p.refusal_text()
    assert not {"output_not_negative", "no_reverse_field"} & {c.id for c in p.checks}


def test_raising_needs_the_users_words(tmp_path):
    _, ctl, _ = _make(tmp_path)
    assert "user_authorization" in _refused(_plan(ctl, 1.0, authorized_up_to_t=1.0))
    assert "user_authorization" in _refused(_plan(ctl, 1.0, user_authorization=" ",
                                                  authorized_up_to_t=1.0))
    for example in ("液面够，可以升到 1 T", "Helium is fine, go to 1 T"):
        assert "user_authorization" in _refused(_plan(ctl, 1.0, user_authorization=example,
                                                      authorized_up_to_t=1.0)), example
    assert _plan(ctl, 1.0, user_authorization="可以", authorized_up_to_t=1.0).ok


def test_an_authorization_covers_its_field_and_no_further(tmp_path):
    _, ctl, _ = _make(tmp_path)
    assert "authorized_field" in _refused(_plan(ctl, 1.0, user_authorization="可以"))
    assert "authorized_field" in _refused(_plan(ctl, 2.0, user_authorization="可以",
                                                authorized_up_to_t=1.0))
    assert "authorized_field" in _refused(_plan(ctl, 1.0, user_authorization="可以",
                                                authorized_up_to_t=True))
    # the words name 1 T, the parameter claims 3 T
    assert "authorization_consistent" in _refused(_plan(
        ctl, 2.0, user_authorization="液面够，升到 1 T", authorized_up_to_t=3.0))
    assert _plan(ctl, 0.5, user_authorization="go to 500 mT", authorized_up_to_t=0.5).ok
    assert mc.fields_named_in("升到 2特，别超过 2.5 T；5 times") == [2.0, 2.5]


def test_a_field_written_right_after_chinese_text_is_still_named(tmp_path):
    """Regression: the look-behind ``(?<![\\w.])``
    treats a CJK character as a word character, so in "升到1T" the number named nothing
    and the consistency check was skipped: "升到1T" authorized a 3 T raise."""
    assert mc.fields_named_in("升到1T") == [1.0]
    assert mc.fields_named_in("液面够，可以升到0.5T") == [0.5]
    assert mc.fields_named_in("升到500mT") == [0.5]
    assert mc.fields_named_in("Q2.5T") == [2.5]        # a digit after a letter: still a field
    assert mc.fields_named_in("1.5T") == [1.5]          # not ".5 T" on its own
    _, ctl, _ = _make(tmp_path)
    assert "authorization_consistent" in _refused(_plan(
        ctl, 2.0, user_authorization="液面够，升到1T", authorized_up_to_t=3.0))
    # the same trap on the negative side: the number glued to Chinese text still names a field
    assert mc.signed_fields_named_in("升到-1T") == [-1.0]
    assert mc.signed_fields_named_in("升到负1T") == [-1.0]
    assert mc.signed_fields_named_in("液面够，升到反向0.5T") == [-0.5]
    assert mc.signed_fields_named_in("升到−500mT") == [-0.5]
    _, ctl, _ = _make(tmp_path, settings=_settings(negative_segments_verified=True))
    for words in ("液面够，升到-1T", "液面够，升到负1T", "液面够，升到反向1T", "go to reverse 1 T"):
        assert "authorization_consistent" in _refused(_plan(
            ctl, -2.0, user_authorization=words, authorized_up_to_t=3.0)), words


@pytest.mark.parametrize("settings, temp, ok", [
    (_settings(), _temp(3.9), True),
    (_settings(), _temp(4.6), False),                   # above the operator's limit
    (_settings(), _temp(None), False),                  # unreadable
    (_settings(), _temp(0.0), False),                   # Lake Shore "invalid input"
    (_settings(), _temp(3.9, age=120.0), False),        # stale
    (_settings(), _temp(3.9, age=None), False),         # age unknown
    (_settings(temperature_channel=""), _temp(3.9), False),
    (_settings(max_temperature_k=None), _temp(3.9), False),
])
def test_raising_needs_a_fresh_valid_magnet_temperature_under_the_limit(tmp_path, settings, temp, ok):
    _, ctl, _ = _make(tmp_path, settings=settings, temperature=temp)
    p = _plan(ctl, 1.0, **_auth(1.0))
    assert ("magnet_temperature" not in _refused(p)) is ok, p.refusal_text()


def test_lowering_needs_neither_authorization_nor_temperature(tmp_path):
    def boom(_channel):
        raise AssertionError("lowering must not consult the thermometer")

    sim = Sim625(current=3.0, setpoint=3.0)
    _, ctl, _ = _make(tmp_path, sim, temperature=boom)
    p = _plan(ctl, 0.5)
    assert p.ok and p.raising is False, p.refusal_text()


def test_the_tip_must_be_withdrawn_unless_waived(tmp_path):
    _, ctl, _ = _make(tmp_path)
    in_tunnel = lambda: SimpleNamespace(state="not_parked", reason="feedback on")  # noqa: E731
    unreadable = lambda: SimpleNamespace(state="unreadable", reason="Z read failed")  # noqa: E731
    assert "tip" in _refused(ctl.plan(1.0, **_auth(1.0), tip_verdict=in_tunnel))
    assert "tip" in _refused(ctl.plan(1.0, **_auth(1.0), tip_verdict=unreadable))
    assert "tip" in _refused(ctl.plan(1.0, **_auth(1.0), tip_verdict=None))
    assert ctl.plan(1.0, **_auth(1.0), require_tip_withdrawn=False, tip_verdict=in_tunnel).ok


def test_a_moving_output_is_refused(tmp_path):
    sim = Sim625(current=1.0, setpoint=4.0, step_a=0.5)
    _, ctl, _ = _make(tmp_path, sim)
    assert "not_ramping" in _refused(_plan(ctl, 0.0))


def test_a_supply_frozen_by_stop_does_not_lock_mast_out(tmp_path):
    sim = Sim625(current=2.0, setpoint=4.0)
    sim.frozen = True                                  # ramp-done bit stays 0
    _, ctl, _ = _make(tmp_path, sim)
    p = _plan(ctl, 0.0)
    assert p.ok, p.refusal_text()


def test_already_there_is_a_noop(tmp_path):
    sim = Sim625(current=round(1.0 / K, 4), setpoint=round(1.0 / K, 4))
    _, ctl, _ = _make(tmp_path, sim)
    r = ctl.start(1.0, origin="t", tip_verdict=_parked)
    assert r.get("noop") and not r["started"] and sim.writes == []


def test_unreadable_supply_is_not_an_answer(tmp_path):
    sim, ctl, _ = _make(tmp_path)
    sim.fail_queries = 1000
    p = _plan(ctl, 1.0, **_auth(1.0))
    assert "readable" in _refused(p) and sim.writes == []


def test_a_different_instrument_on_the_port_is_refused(tmp_path):
    sim = Sim625()
    sim.idn = b"LSCI,MODEL335,LSA0335,2.1\r\n"
    _, ctl, _ = _make(tmp_path, sim)
    assert "identity" in _refused(_plan(ctl, 1.0, **_auth(1.0)))
    assert sim.writes == []


def test_eta_follows_the_segment_table():
    segs = [{"upper_current_a": u, "rate_a_s": r} for u, r in SEGMENTS.values()]
    eta = mc.ramp_eta_s(0.0, 14.68, segs, 0.010)
    assert eta / 60 == pytest.approx(53.0, abs=0.05)
    assert mc.ramp_eta_s(14.68, 0.0, segs, 0.010) == pytest.approx(eta)   # same both ways
    assert mc.ramp_eta_s(0.0, 20.0, segs[:1], None) is None


def test_the_slow_checks_run_without_holding_the_link(tmp_path):
    """The thermometer path takes the environment monitor's lock, whose stop() closes
    links while holding it: holding the link around it is a lock-order deadlock."""
    held: list[bool] = []
    box: dict = {}

    def free(lock) -> bool:
        """Can ANOTHER thread take the lock right now? (Taken and released in that thread.)"""
        got = []

        def probe():
            ok = lock.acquire(timeout=0)
            got.append(ok)
            if ok:
                lock.release()

        t = threading.Thread(target=probe)
        t.start()
        t.join()
        return bool(got and got[0])

    def temperature(channel):
        held.append(not free(box["link"]._lock))
        return _temp()(channel)

    def tip():
        held.append(not free(box["link"]._lock))
        return _parked()

    def heating():                      # the 335 is asked here: never with this link held
        held.append(not free(box["link"]._lock))
        return dict(NO_335)

    in_lock: list[bool] = []

    def heating_now():                  # memory only, asked again holding the link
        in_lock.append(not free(box["link"]._lock))
        return False

    sim, ctl, _ = _make(tmp_path, temperature=temperature, heating=heating,
                        heating_now=heating_now)
    box["link"] = ctl.link
    r = ctl.start(1.0, origin="t", **_auth(1.0), tip_verdict=tip)
    assert r["started"], r
    assert held == [False, False, False]
    assert in_lock == [True]


# ── heating and field changes never at the same time ──
@pytest.mark.parametrize("verdict", [
    {"ok": False, "present": True, "ranges": {1: 0, 2: 1}, "mast_heating": False,
     "detail": "加热和改磁场不同时进行：加热输出 2（量程 1）开着"},
    {"ok": False, "present": True, "ranges": {1: 2, 2: 0}, "mast_heating": False,
     "detail": "output 1 on"},                                    # the operator's heater counts too
    {"ok": False, "present": True, "ranges": {1: 0, 2: 0}, "mast_heating": True, "detail": "watch"},
    {"ok": False, "present": True, "ranges": {1: 0, 2: None}, "mast_heating": False,
     "detail": "RANGE? 2 not answered"},                           # unknown is not an answer
    {"present": True, "detail": "no ok field"},
    "garbage",
])
def test_a_heater_that_is_on_or_unknown_refuses_any_field_change(tmp_path, verdict):
    sim, ctl, _ = _make(tmp_path, Sim625(current=3.0, setpoint=3.0), heating=lambda: verdict)
    for target, kw in ((1.0, {}), (5.0, _auth(5.0))):              # lowering and raising
        p = _plan(ctl, target, **kw)
        assert "no_heating" in _refused(p), p.refusal_text()
    r = ctl.start(1.0, origin="t", tip_verdict=_parked)
    assert not r["started"] and "no_heating" in r["error"] and sim.writes == []


def test_without_a_temperature_controller_the_gate_passes_and_says_so(tmp_path):
    _, ctl, _ = _make(tmp_path)
    p = _plan(ctl, 1.0, **_auth(1.0))
    check = next(c for c in p.checks if c.id == "no_heating")
    assert p.ok and check.ok and "no temperature controller" in check.detail


def test_a_heater_asking_that_raises_refuses(tmp_path):
    def boom():
        raise RuntimeError("335 link gone")

    _, ctl, _ = _make(tmp_path, heating=boom)
    assert "no_heating" in _refused(_plan(ctl, 1.0, **_auth(1.0)))


def test_heating_that_begins_after_the_plan_stops_the_write(tmp_path):
    """The in-lock recheck: MAST's own heating, from memory, just before SETI."""
    sim, ctl, _ = _make(tmp_path, heating_now=lambda: True)
    r = ctl.start(1.0, origin="t", **_auth(1.0), tip_verdict=_parked)
    assert not r["started"] and sim.setis() == [] and ctl.active is None
    assert _ledger(tmp_path)[-1]["changed"] == "mast_heating"


def test_a_controller_built_without_injection_asks_core_heater_control(tmp_path):
    """Not wired (no runtime in this process): the heater state is unknown, so no change."""
    from mast.core import heater_control

    heater_control.set_interlock_source(None)
    sim = Sim625()
    ctl = mc.Magnet625Controller(m625.Model625Link(sim, port="COM13", min_gap_s=0.0),
                                 settings=_settings, temperature=_temp(),
                                 ledger_path=lambda: tmp_path / "l.jsonl")
    p = _plan(ctl, 1.0, **_auth(1.0))
    assert "no_heating" in _refused(p) and sim.writes == []
    try:
        heater_control.set_interlock_source(lambda: dict(NO_335))
        assert _plan(ctl, 1.0, **_auth(1.0)).ok
    finally:
        heater_control.set_interlock_source(None)


def test_heating_and_field_changes_never_overlap_either_way(tmp_path):
    """Real controllers on simulated instruments, wired through the core ports as the
    runtime wires them: MAST heating blocks a field change; a MAST field ramp blocks
    heating; with the heater off and supervision ended, the field may change again."""
    from mast.core import heater_control
    from mast.instruments import heater_335 as hc
    from tests.v2.environment import test_heater_control as th

    sim335 = th.Sim335()
    hctl = hc.Heater335Controller(
        th.lsci.LakeshoreLink(sim335, port="COMX", expected_model="MODEL335"),
        settings=lambda: th.SETTINGS, temperature=lambda ch: None,
        field_state=lambda: {"ok": True},            # the 625 side is what this test exercises
        ledger_path=lambda: tmp_path / "heater.jsonl", clock=lambda: sim335.now,
        sleep=sim335.advance, background=False)          # field_busy: the real magnet port
    sim625, mctl, _ = _make(tmp_path, Sim625(step_a=0.5), heating=heater_control.interlock,
                            heating_now=heater_control.mast_heating)
    try:
        magnet_control.set_source(lambda: mctl)
        heater_control.set_source(lambda: hctl)
        heater_control.set_interlock_source(lambda: hctl.interlock_state())
        assert _plan(mctl, 1.0, **_auth(1.0)).ok                       # nothing heats
        assert hctl.start(10.0, origin="t", tip_verdict=th._parked, **th._auth(10.0))["started"]
        p = _plan(mctl, 1.0, **_auth(1.0))
        assert "no_heating" in _refused(p) and sim625.writes == []
        n335 = len(sim335.queries)
        assert heater_control.mast_heating() is True and len(sim335.queries) == n335   # no I/O
        hctl.heater_off(reason="t", origin="t")
        assert hctl.supervise()["outcome"] == "heater_off" and not hctl.mast_heating()
        assert _plan(mctl, 1.0, **_auth(1.0)).ok
        assert mctl.start(1.0, origin="t", **_auth(1.0), tip_verdict=_parked)["started"]
        hp = hctl.plan(10.0, tip_verdict=th._parked, **th._auth(10.0))
        assert "field_steady" in {c.id for c in hp.refusals}           # the other way
        assert mctl.supervise()["outcome"] == "done"
        hp = hctl.plan(10.0, tip_verdict=th._parked, **th._auth(10.0))
        assert "field_steady" not in {c.id for c in hp.refusals}
    finally:
        magnet_control.set_source(None)
        heater_control.set_source(None)
        heater_control.set_interlock_source(None)


# ── the write ────────────────────────────────────────────────────────────────
def test_start_sends_one_seti_pinned_and_reads_it_back(tmp_path):
    sim, ctl, _ = _make(tmp_path)
    r = ctl.start(5.0, origin="test", user_authorization="液面够，升到 5 T",
                  authorized_up_to_t=5.0, tip_verdict=_parked)
    assert r["started"], r
    assert sim.writes == [b"SETI 14.6800\r\n"]
    assert ctl.active["target_a"] == pytest.approx(14.68) and ctl.busy
    rows = _ledger(tmp_path)
    assert rows[-1]["action"] == "SETI" and rows[-1]["authorization"] == "液面够，升到 5 T"
    assert rows[-1]["authorized_up_to_t"] == 5.0 and rows[-1]["result"] == "accepted"


def test_a_refused_start_writes_nothing_and_is_ledgered(tmp_path):
    sim, ctl, _ = _make(tmp_path)
    r = ctl.start(1.0, origin="test", tip_verdict=_parked)       # no authorization
    assert not r["started"] and "user_authorization" in r["error"]
    assert sim.writes == []
    assert _ledger(tmp_path)[-1]["action"] == "refused"


def test_a_supply_that_ignores_writes_is_reported_not_stopped(tmp_path):
    sim, ctl, _ = _make(tmp_path, Sim625(accept_writes=False))
    r = ctl.start(1.0, origin="test", **_auth(1.0), tip_verdict=_parked)
    assert not r["started"] and r.get("not_accepted")
    assert sim.stops() == 0 and ctl.active is None


def test_an_unexpected_readback_is_frozen_with_stop(tmp_path):
    sim = Sim625()
    sim.setpoint_after_seti = 7.0
    _, ctl, _ = _make(tmp_path, sim)
    r = ctl.start(1.0, origin="test", **_auth(1.0), tip_verdict=_parked)
    assert not r["started"] and sim.stops() == 1


def test_a_write_that_raises_after_its_bytes_left_is_followed_by_stop(tmp_path):
    sim = Sim625()
    sim.raise_on_seti = True
    _, ctl, _ = _make(tmp_path, sim)
    r = ctl.start(1.0, origin="test", **_auth(1.0), tip_verdict=_parked)
    assert not r["started"] and sim.stops() == 1
    assert any(row.get("result") == "send_raised" for row in _ledger(tmp_path))


def test_abort_before_the_write_sends_nothing(tmp_path):
    sim, ctl, _ = _make(tmp_path)
    r = ctl.start(1.0, origin="test", **_auth(1.0), tip_verdict=_parked,
                  abort_check=lambda: True)
    assert not r["started"] and sim.writes == []


def test_a_stop_during_preparation_cancels_the_write(tmp_path):
    sim, ctl, _ = _make(tmp_path)

    def tip_then_stop():
        ctl.stop(reason="operator", origin="UI")
        return _parked()

    r = ctl.start(1.0, origin="test", **_auth(1.0), tip_verdict=tip_then_stop)
    assert not r["started"]
    assert sim.setis() == [] and sim.stops() == 1


def test_a_change_between_plan_and_write_is_refused(tmp_path):
    sim, ctl, _ = _make(tmp_path)

    def tip_and_panel_change():
        sim.setpoint = 0.5                     # someone types a set point at the panel
        sim.frozen = True
        return _parked()

    r = ctl.start(1.0, origin="test", **_auth(1.0), tip_verdict=tip_and_panel_change)
    assert not r["started"] and sim.setis() == []


def test_shutdown_refuses_new_ramps(tmp_path):
    sim, ctl, _ = _make(tmp_path)
    assert ctl.begin_shutdown() is None and sim.writes == []
    r = ctl.start(1.0, origin="t", **_auth(1.0), tip_verdict=_parked)
    assert not r["started"] and sim.writes == []


# ── supervision ──────────────────────────────────────────────────────────────
def _started(tmp_path, target=1.0, **sim_kw):
    sim = Sim625(**sim_kw)
    _, ctl, clk = _make(tmp_path, sim)
    r = ctl.start(target, origin="test", **_auth(target), tip_verdict=_parked)
    assert r["started"], r
    return sim, ctl, clk


def test_supervision_ends_on_arrival_without_writing(tmp_path):
    sim, ctl, _ = _started(tmp_path, step_a=0.5)
    out = ctl.supervise()
    assert out["outcome"] == "done", out
    assert sim.writes == [b"SETI 2.9360\r\n"] and ctl.active is None and not ctl.busy


def test_arrival_with_a_residual_beyond_5_ma_still_ends(tmp_path):
    """Setting accuracy is ±10 mA ±0.05 %: ramp done with SETI? = target is arrival."""
    sim, ctl, _ = _started(tmp_path, target=4.0, step_a=5.0)
    sim.read_offset_a = 0.020
    out = ctl.supervise()
    assert out["outcome"] == "done", out
    assert out["last"]["residual_a"] == pytest.approx(0.020, abs=1e-4)


def test_abort_mid_ramp_sends_stop(tmp_path):
    sim, ctl, _ = _started(tmp_path, target=4.0, step_a=0.1)
    calls = {"n": 0}

    def abort():
        calls["n"] += 1
        return calls["n"] > 3

    out = ctl.supervise(abort_check=abort)
    assert out["outcome"] == "aborted" and sim.stops() == 1


def test_a_stop_from_elsewhere_ends_supervision(tmp_path):
    sim, ctl, _ = _started(tmp_path, target=4.0, step_a=0.1)
    n = {"q": 0}

    def press_stop(s, cmd):
        n["q"] += 1
        if n["q"] == 3:
            ctl.stop(reason="StopMagnetRamp", origin="skill")

    sim.on_query = press_stop
    out = ctl.supervise()
    assert out["outcome"] == "stopped" and sim.stops() == 1


def test_a_stop_during_the_readback_is_not_missed(tmp_path):
    """The ramp records the STOP count from BEFORE its SETI, not after."""
    sim = Sim625(step_a=0.1)
    _, ctl, _ = _make(tmp_path, sim)
    state = {"seen_seti": False}

    def stop_on_readback(s, cmd):
        if cmd == "SETI?" and s.setis() and not state["seen_seti"]:
            state["seen_seti"] = True
            ctl.stop(reason="operator", origin="UI")

    sim.on_query = stop_on_readback
    r = ctl.start(4.0, origin="t", **_auth(4.0), tip_verdict=_parked)
    assert r["started"], r
    out = ctl.supervise()
    assert out["outcome"] == "stopped" and sim.stops() == 1


def test_a_stop_that_did_not_go_out_is_retried_and_the_ramp_stays_busy(tmp_path):
    sim, ctl, _ = _started(tmp_path, target=4.0, step_a=0.05)
    seen_busy: list[bool] = []
    n = {"q": 0}

    def press_stop(s, cmd):
        n["q"] += 1
        if n["q"] == 3:
            s.fail_stop_writes = 2
            res = ctl.stop(reason="UI", origin="UI")
            assert res["sent"] is False
        if n["q"] > 3:
            seen_busy.append(ctl.busy)

    sim.on_query = press_stop
    out = ctl.supervise()
    assert out["outcome"] == "stopped" and sim.stops() == 1
    assert seen_busy and all(seen_busy)


def test_a_stop_that_never_goes_out_ends_loudly(tmp_path):
    sim, ctl, _ = _started(tmp_path, target=4.0, step_a=0.05)
    sim.fail_stop_writes = 10 ** 6
    out = ctl.supervise(abort_check=lambda: True)
    assert out["outcome"] == "stop_failed" and sim.stops() == 0
    assert not ctl.busy and ctl.status()["last"]["outcome"] == "stop_failed"


def test_an_external_set_point_change_is_not_fought(tmp_path):
    sim, ctl, _ = _started(tmp_path, target=4.0, step_a=0.1)

    def panel(s, cmd):
        if cmd == "SETI?" and s.current > 0.5:
            s.setpoint = 2.0

    sim.on_query = panel
    out = ctl.supervise()
    assert out["outcome"] == "external_change"
    assert sim.stops() == 0 and len(sim.setis()) == 1


@pytest.mark.parametrize("erst", [[0, 1 << 5, 0], [0, 1 << 6, 0], [0, 1 << 4, 0], [1 << 3, 0, 0]])
def test_self_protection_writes_nothing(tmp_path, erst):
    """Quench / crowbar / remote inhibit / any hardware error (crowbar thrown)."""
    sim, ctl, _ = _started(tmp_path, target=4.0, step_a=0.1)

    def fault(s, cmd):
        if s.current > 0.5:
            s.erst = list(erst)

    sim.on_query = fault
    out = ctl.supervise(abort_check=lambda: sim.current > 0.5)   # an abort arrives too
    assert out["outcome"] == "instrument_protection" and sim.stops() == 0


def test_another_error_bit_sends_stop(tmp_path):
    sim, ctl, _ = _started(tmp_path, target=4.0, step_a=0.1)

    def fault(s, cmd):
        if s.current > 0.5:
            s.erst = [0, 1 << 3, 0]            # supply temperature high (>75 °C)

    sim.on_query = fault
    out = ctl.supervise()
    assert out["outcome"] == "instrument_error" and sim.stops() == 1


def test_three_failed_reads_in_a_row_send_stop(tmp_path):
    sim, ctl, _ = _started(tmp_path, target=4.0, step_a=0.1)
    sim.fail_queries = 3
    out = ctl.supervise()
    assert out["outcome"] == "lost_contact"
    assert sim.stops() == 1


def test_one_failed_read_is_survived(tmp_path):
    sim, ctl, _ = _started(tmp_path, step_a=0.5)
    sim.fail_queries = 1
    assert ctl.supervise()["outcome"] == "done" and sim.stops() == 0


def test_a_stalled_ramp_is_stopped(tmp_path):
    sim, ctl, _ = _started(tmp_path, target=4.0, step_a=0.0)   # output never moves
    out = ctl.supervise()
    assert out["outcome"] == "stalled" and sim.stops() == 1


# ── stop ─────────────────────────────────────────────────────────────────────
def test_stop_never_raises_and_says_why_it_failed(tmp_path):
    sim, ctl, _ = _make(tmp_path)
    sim.idn = b"LSCI,MODEL335,LSA0335,2.1\r\n"
    out = ctl.stop(reason="test", origin="test")
    assert out["sent"] is False and "MODEL625" in out["error"]
    assert sim.writes == []                    # never STOP a different instrument
    assert _ledger(tmp_path)[-1]["action"] == "STOP"


def test_stop_is_not_sent_while_the_supply_protects_the_magnet(tmp_path):
    sim, ctl, _ = _make(tmp_path)
    sim.erst = [0, 1 << 5, 0]                  # quench: the supply drives the output to 0
    out = ctl.stop(reason="E-STOP", origin="test")
    assert out["skipped"] and not out["sent"] and sim.writes == []


def test_stop_if_active_only_acts_on_a_mast_ramp(tmp_path):
    sim, ctl, _ = _make(tmp_path)
    assert ctl.stop_if_active(reason="shutdown", origin="t") is None and sim.writes == []
    ctl.start(1.0, origin="t", **_auth(1.0), tip_verdict=_parked)
    assert ctl.stop_if_active(reason="shutdown", origin="t")["sent"] is True


# ── reverse field ───────────────────────────────────────────────
A_1T = round(1.0 / K, 4)                      # 2.9360 A
VERIFIED = _settings(negative_segments_verified=True)


def _rauth(up_to, words=None):
    """The user's words naming a reverse field, and the magnitude they authorized."""
    return {"user_authorization": words or f"可以，反向 {up_to:g} T", "authorized_up_to_t": up_to}


def _gate(plan, gate_id):
    (c,) = [c for c in plan.checks if c.id == gate_id]
    return c


def test_a_negative_target_inside_the_limit_passes_and_writes_nothing_in_a_plan(tmp_path):
    sim, ctl, _ = _make(tmp_path)
    p = _plan(ctl, -1.0, **_rauth(1.0))
    assert p.ok, p.refusal_text()
    assert p.target_a == -A_1T and p.raising is True and p.limit_a == 15.0
    assert p.eta_s == pytest.approx(A_1T / 0.010, rel=1e-6)
    assert sim.writes == []


def test_a_negative_target_is_checked_by_magnitude(tmp_path):
    # |B| above the initialization maximum (a magnitude)
    _, ctl, _ = _make(tmp_path, settings=VERIFIED)
    assert "max_field" in _refused(_plan(ctl, -5.01, **_rauth(9.0)))
    assert _plan(ctl, -4.9, **_rauth(4.9)).ok
    # |I| above the supply's LIMIT, by magnitude: -2.936 A against a 2 A limit
    sim, ctl, _ = _make(tmp_path, settings=VERIFIED)
    sim.limit = "+02.0000,+2.0000,+0.0280"
    p = _plan(ctl, -1.0, **_rauth(1.0))
    assert "supply_limit" in _refused(p)
    assert ctl.start(-1.0, origin="t", **_rauth(1.0), tip_verdict=_parked)["started"] is False
    assert sim.writes == []


def test_a_negative_start_sends_a_negative_seti_and_follows_it_to_arrival(tmp_path):
    sim, ctl, _ = _make(tmp_path, Sim625(step_a=0.5))
    r = ctl.start(-1.0, origin="t", **_rauth(1.0), tip_verdict=_parked)
    assert r["started"], r
    assert sim.writes == [b"SETI -2.9360\r\n"]
    row = _ledger(tmp_path)[-1]
    assert row["current_a"] == -A_1T and row["raising"] is True and row["result"] == "accepted"
    out = ctl.supervise()
    assert out["outcome"] == "done", out
    assert sim.current == pytest.approx(-A_1T) and sim.stops() == 0 and not ctl.busy


def test_the_hard_guard_in_start_holds_even_if_a_plan_slips_through(tmp_path, monkeypatch):
    """build_write is bound to the LIMIT the plan read: a target outside it is never sent."""
    sim, ctl, _ = _make(tmp_path, settings=VERIFIED)
    real_plan = ctl.plan

    def slipped(*a, **kw):
        p = real_plan(*a, **kw)
        assert p.ok, p.refusal_text()
        p.target_a = -16.0                      # outside the 15 A LIMIT, every gate "passed"
        return p

    monkeypatch.setattr(ctl, "plan", slipped)
    r = ctl.start(-1.0, origin="t", **_rauth(1.0), tip_verdict=_parked)
    assert r["started"] is False and "outside" in r["error"] and sim.writes == []


def test_the_reverse_gates_run_before_any_write_on_a_refused_start(tmp_path):
    sim, ctl, _ = _make(tmp_path)
    r = ctl.start(-1.0, origin="t", tip_verdict=_parked)        # no words at all
    assert not r["started"] and "authorization_sign" in r["error"] and sim.writes == []
    assert _ledger(tmp_path)[-1]["action"] == "refused"


# -- the user's words must name the sign ---------------------------------------------------
@pytest.mark.parametrize("words", ["-2 T", "-2T", "−2T", "−2 T", "负2T", "负 2 T", "反向2T",
                                   "反向 2T", "反向场2T", "反向磁场 2 T", "reverse 2 T",
                                   "Reverse 2T", "negative 2 T", "negative field of 2 T",
                                   "go to -2T please", "液面够，升到负2T", "minus 2 T", "负的2T",
                                   "反转 2T", "升到 -2T", "B = -2 T", "到-2T", "(-2 T)",
                                   "目标：-2T"])
def test_words_that_name_a_reverse_field_authorize_a_negative_target(tmp_path, words):
    _, ctl, _ = _make(tmp_path, settings=VERIFIED)
    p = _plan(ctl, -2.0, user_authorization=words, authorized_up_to_t=2.0)
    assert p.ok, (words, p.refusal_text())


@pytest.mark.parametrize("words", ["可以", "升到 2 T", "液面够，可以升到2T", "go to 2 T",
                                   "1-2 T", "0.5-1 T",                      # a range, not a sign
                                   "从1T-2T", "1T-2T", "1 T - 2 T", "ramp 0 - 2 T", "0 – 2 T",
                                   "1T－2T", "1 T -2 T",
                                   "负责升到2T",                              # 负 is not on the number
                                   "reverse the ramp, go to 2 T", "positive 2 T", "+2 T"])
def test_words_that_do_not_name_a_reverse_field_do_not_authorize_a_negative_target(tmp_path, words):
    _, ctl, _ = _make(tmp_path, settings=VERIFIED)
    p = _plan(ctl, -2.0, user_authorization=words, authorized_up_to_t=2.0)
    assert "authorization_sign" in _refused(p), words
    r = ctl.start(-2.0, origin="t", user_authorization=words, authorized_up_to_t=2.0,
                  tip_verdict=_parked)
    assert not r["started"] and ctl.link.transport.writes == []


@pytest.mark.parametrize("words", ["-2 T", "负2T", "反向2T", "reverse 2 T", "negative 2 T",
                                   "升到 -2T，不是正的"])
def test_words_that_name_only_a_reverse_field_do_not_authorize_a_positive_target(tmp_path, words):
    _, ctl, _ = _make(tmp_path)
    p = _plan(ctl, 2.0, user_authorization=words, authorized_up_to_t=2.0)
    assert "authorization_sign" in _refused(p), words


def test_a_positive_target_still_passes_with_words_that_name_no_field_or_a_positive_one(tmp_path):
    _, ctl, _ = _make(tmp_path)
    for words in ("可以", "升到 2 T", "go to 2 T", "+2 T，反向的以后再说", "把磁场反向升到 +2 T",
                  "正2T", "正向2T", "positive 2 T", "从1T-2T，升到 2 T"):
        p = _plan(ctl, 2.0, user_authorization=words, authorized_up_to_t=2.0)
        assert "authorization_sign" not in _refused(p), words


@pytest.mark.parametrize("words", ["minus 2 T", "把磁场反向升到 2T", "升到2T，反向的以后再说",
                                   "reverse the ramp, go to 2 T", "负磁场的事先放一放，升到 2 T"])
def test_a_sign_word_that_touches_no_number_makes_the_sign_unclear_for_a_positive_target(
        tmp_path, words):
    _, ctl, _ = _make(tmp_path)
    p = _plan(ctl, 2.0, user_authorization=words, authorized_up_to_t=2.0)
    assert "authorization_sign" in _refused(p), words
    if "minus" not in words:                                   # "minus 2 T" names a field
        assert "+2 T" in _gate(p, "authorization_sign").detail
    assert ctl.start(2.0, origin="t", user_authorization=words, authorized_up_to_t=2.0,
                     tip_verdict=_parked)["started"] is False
    assert ctl.link.transport.writes == []


def test_dashes_are_signs_only_when_they_touch_the_number_and_follow_no_number():
    sf = mc.signed_fields_named_in
    for text in ("升到 -2T", "B = -2 T", "到-2T", "(-2 T)", "-2T", "目标：-2T", "−2T", "－2T",
                 "let -2 T"):
        assert sf(text) == [-2.0], text
    assert sf("从1T-2T") == [1.0, 2.0] and sf("1T-2T") == [1.0, 2.0]
    assert sf("1 T - 2 T") == [1.0, 2.0] and sf("ramp 0 - 2 T") == [2.0]
    assert sf("0 – 2 T") == [2.0] and sf("1T－2T") == [1.0, 2.0] and sf("1-2 T") == [2.0]
    assert sf("1 T -2 T") == [1.0, 2.0] and sf("1 mT-2 mT") == [0.001, 0.002]
    assert sf("minus 2 T") == [-2.0] and sf("负的2T") == [-2.0] and sf("反转 2T") == [-2.0]
    assert mc.scan_fields("+2 T") == ([(2.0, True)], False)
    assert mc.scan_fields("正向2T") == ([(2.0, True)], False)
    assert mc.scan_fields("把磁场反向升到 2T") == ([(2.0, False)], True)
    assert mc.scan_fields("负责升到2T，负载 1 T") == ([(2.0, False), (1.0, False)], False)


def test_the_authorized_field_is_a_magnitude_compared_with_the_targets_magnitude(tmp_path):
    _, ctl, _ = _make(tmp_path, settings=VERIFIED)
    assert "authorized_field" in _refused(_plan(
        ctl, -2.0, user_authorization="反向 1 T", authorized_up_to_t=1.0))
    assert "authorized_field" in _refused(_plan(
        ctl, -2.0, user_authorization="反向 2 T", authorized_up_to_t=-2.0))     # not a signed cap
    assert "authorized_field" not in _refused(_plan(
        ctl, -2.0, user_authorization="反向 2 T", authorized_up_to_t=2.0))
    # the words name 1 T of reverse field; the parameter claims 3 T
    assert "authorization_consistent" in _refused(_plan(
        ctl, -2.0, user_authorization="反向 1 T", authorized_up_to_t=3.0))
    # only the target's own side counts: "+3 T ... -1 T" does not stretch a -2 T target
    assert "authorization_consistent" in _refused(_plan(
        ctl, -2.0, user_authorization="之前是 3 T，现在反向 1 T", authorized_up_to_t=3.0))
    assert "authorization_consistent" in _refused(_plan(
        ctl, 2.0, user_authorization="升到 1 T，不要 -3 T", authorized_up_to_t=3.0))


def test_fields_named_in_keeps_its_magnitudes_and_the_signed_form_marks_the_sign():
    assert mc.fields_named_in("升到 2特，别超过 2.5 T；5 times") == [2.0, 2.5]
    assert mc.fields_named_in("反向 2T，-500 mT") == [2.0, 0.5]
    assert mc.signed_fields_named_in("升到 2特，别超过 2.5 T；5 times") == [2.0, 2.5]
    assert mc.signed_fields_named_in("-2T 或 负3T，或 reverse 4 T") == [-2.0, -3.0, -4.0]
    assert mc.signed_fields_named_in("1-2 T") == [2.0]               # a range: 2 is not negative
    assert mc.signed_fields_named_in("1 T - 2 T") == [1.0, 2.0]       # spaced dash: a range
    assert mc.signed_fields_named_in("负责升到2T") == [2.0]


def test_lowering_and_stopping_need_no_words_on_the_negative_side_either(tmp_path):
    def boom(_channel):
        raise AssertionError("lowering must not consult the thermometer")

    sim = Sim625(current=-5.0, setpoint=-5.0)
    _, ctl, _ = _make(tmp_path, sim, temperature=boom)
    for t in (0.0, -1.0):                                   # toward zero; |B| shrinks
        p = _plan(ctl, t)
        assert p.ok and p.raising is False, (t, p.refusal_text())
        assert "authorization_sign" not in {c.id for c in p.checks}


# -- zero crossing -----------------------------------------------------------------------
@pytest.mark.parametrize("present, target_t", [(3.0, -1.0), (-3.0, 1.0), (0.5, -5.0), (-14.0, 4.0),
                                               (1.0, -0.1), (-1.0, 0.1)])
def test_a_ramp_that_would_cross_zero_is_refused_with_the_reason(tmp_path, present, target_t):
    sim, ctl, _ = _make(tmp_path, Sim625(current=present, setpoint=present), settings=VERIFIED)
    p = _plan(ctl, target_t, **_rauth(5.0, f"可以，升到 {abs(target_t):g} T" if target_t > 0
                                      else f"反向 {abs(target_t):g} T"))
    assert "no_zero_crossing" in _refused(p), p.refusal_text()
    assert "0 T" in _gate(p, "no_zero_crossing").detail
    r = ctl.start(target_t, origin="t", **_rauth(5.0), tip_verdict=_parked)
    assert not r["started"] and sim.writes == []
    assert _ledger(tmp_path)[-1]["action"] == "refused"


def test_going_to_zero_from_either_side_is_allowed_without_authorization(tmp_path):
    for present in (3.0, -3.0, -12.0):                      # -12 A is beyond the first segment
        sim, ctl, _ = _make(tmp_path, Sim625(current=present, setpoint=present, step_a=2.0))
        p = _plan(ctl, 0.0)
        assert p.ok and p.raising is False, (present, p.refusal_text())
        assert _gate(p, "no_zero_crossing").ok
        r = ctl.start(0.0, origin="t", tip_verdict=_parked)
        assert r["started"] and sim.writes == [b"SETI 0.0000\r\n"], (present, r)
        out = ctl.supervise()
        assert out["outcome"] == "done" and sim.current == pytest.approx(0.0), (present, out)


def test_a_reading_within_the_current_tolerance_of_zero_is_not_a_side(tmp_path):
    """Each side counts only beyond the current tolerance (10 mA + 0.05 %)."""
    for present, target_t, words in ((0.005, -1.0, "反向 1 T"), (-0.005, 1.0, "升到 1 T"),
                                     (0.009, -1.0, "反向 1 T")):
        sim, ctl, _ = _make(tmp_path, Sim625(current=present, setpoint=present))
        p = _plan(ctl, target_t, user_authorization=words, authorized_up_to_t=1.0)
        assert _gate(p, "no_zero_crossing").ok, (present, p.refusal_text())
    # just beyond it, it is a side
    sim, ctl, _ = _make(tmp_path, Sim625(current=0.02, setpoint=0.02))
    assert "no_zero_crossing" in _refused(_plan(ctl, -1.0, **_rauth(1.0)))


def test_a_reverse_target_inside_the_tolerance_still_crosses_zero_from_beyond_it(tmp_path):
    for present, target_t, words in ((5.0, -0.001, "反向 1 mT"), (-5.0, 0.001, "升到 1 mT")):
        sim, ctl, _ = _make(tmp_path, Sim625(current=present, setpoint=present),
                            settings=_settings(negative_segments_verified=True))
        p = _plan(ctl, target_t, user_authorization=words, authorized_up_to_t=0.001)
        assert abs(p.target_a) < 0.010                       # inside the tolerance of zero
        assert "no_zero_crossing" in _refused(p), (present, p.refusal_text())
        assert not ctl.start(target_t, origin="t", user_authorization=words,
                             authorized_up_to_t=0.001, tip_verdict=_parked)["started"]
        assert sim.writes == []


def test_a_reverse_target_from_a_non_negative_output_is_a_raise_however_small(tmp_path):
    for present in (0.0, 0.004):
        _, ctl, _ = _make(tmp_path, Sim625(current=present, setpoint=present))
        p = _plan(ctl, -0.001)                               # -2.9 mA
        assert p.raising is True and "authorization_sign" in _refused(p), present
        assert "user_authorization" in _refused(p)
        ok = _plan(ctl, -0.001, user_authorization="反向 -3 mT 抵消剩磁".replace("反向 ", ""),
                   authorized_up_to_t=0.001)
        assert ok.ok, ok.refusal_text()
    # from a negative output the same size is only a lowering
    _, ctl, _ = _make(tmp_path, Sim625(current=-1.0, setpoint=-1.0))
    p = _plan(ctl, -0.001)
    assert p.raising is False and p.ok, p.refusal_text()


def test_unverified_negative_segments_also_need_rate_not_faster_than_the_first_segment(tmp_path):
    def plan_with(rate_text, settings=None):
        sim = Sim625()
        sim.rate_text = rate_text
        _, ctl, _ = _make(tmp_path, sim, settings=settings)
        return _plan(ctl, -1.0, **_rauth(1.0))

    assert plan_with("+0.0100").ok and plan_with("+0.0050").ok        # equal / slower: fine
    for rate in ("+0.0200", "+0.0101", "garbage"):                      # faster / unreadable
        p = plan_with(rate)
        assert _refused(p) == {"negative_rate"}, (rate, p.refusal_text())
    assert "RATE" in _gate(plan_with("+0.0200"), "negative_rate").detail
    assert plan_with("+0.0200", VERIFIED).ok                            # verified: not asked
    assert "negative_rate" not in {c.id for c in plan_with("+0.0200", VERIFIED).checks}
    # a positive target does not care about RATE
    sim = Sim625()
    sim.rate_text = "+0.0200"
    _, ctl, _ = _make(tmp_path, sim)
    assert _plan(ctl, 1.0, **_auth(1.0)).ok


def test_unverified_only_the_target_is_looked_at_so_lowering_from_beyond_the_boundary_passes(tmp_path):
    sim, ctl, _ = _make(tmp_path, Sim625(current=-12.0, setpoint=-12.0))
    assert _plan(ctl, 0.0).ok                                  # -12 A -> 0
    assert _plan(ctl, -2.0).ok                                 # -12 A -> -5.9 A, inside the boundary
    assert "negative_segments" in _refused(_plan(ctl, -3.5))   # -10.3 A: target beyond the boundary


# -- the segments are not known to act on |I| on the negative side ------------------------
def test_unverified_negative_segments_refuse_beyond_the_first_boundary_and_name_it(tmp_path):
    _, ctl, _ = _make(tmp_path)                      # negative_segments_verified is False
    p = _plan(ctl, -4.0, **_rauth(4.0))              # 11.74 A > 9 A, the first segment's top
    assert _refused(p) == {"negative_segments"}, p.refusal_text()
    detail = _gate(p, "negative_segments").detail
    assert "9" in detail and ("RATE?" in detail)
    assert "negative_segments" in ctl.start(-4.0, origin="t", **_rauth(4.0),
                                            tip_verdict=_parked)["error"]
    assert ctl.link.transport.writes == []
    # a positive target of the same size is not affected
    assert _plan(ctl, 4.0, **_auth(4.0)).ok


def test_unverified_negative_segments_allow_up_to_and_including_the_boundary(tmp_path):
    _, ctl, _ = _make(tmp_path)
    inside = _plan(ctl, -2.7, **_rauth(2.7))         # 7.93 A
    assert inside.ok and _gate(inside, "negative_segments").ok, inside.refusal_text()
    at = _plan(ctl, -9.0 * K, **_rauth(3.1))         # exactly 9.0000 A
    assert at.target_a == -9.0 and at.ok, at.refusal_text()
    over = _plan(ctl, -9.0002 * K, **_rauth(3.1))    # 9.0002 A
    assert "negative_segments" in _refused(over)


def test_the_verified_flag_lets_a_negative_target_past_the_first_boundary(tmp_path):
    _, ctl, _ = _make(tmp_path, settings=VERIFIED)
    p = _plan(ctl, -4.0, **_rauth(4.0))
    assert p.ok, p.refusal_text()
    assert _gate(p, "negative_segments").ok
    # ... but never past the supply's own limit, or the maximum field
    assert "supply_limit" in _refused(_plan(ctl, -4.9 * 1.1, **_rauth(9.0)))
    assert "max_field" in _refused(_plan(ctl, -5.01, **_rauth(9.0)))


def test_an_unreadable_segment_table_means_no_negative_target_until_verified(tmp_path):
    sim = Sim625()
    sim.segments = {n: (0.0, 0.0) for n in sim.segments}        # no usable segment
    _, ctl, _ = _make(tmp_path, sim)
    assert "negative_segments" in _refused(_plan(ctl, -0.1, **_rauth(0.1)))
    _, ctl, _ = _make(tmp_path, sim, settings=VERIFIED)
    assert "negative_segments" not in _refused(_plan(ctl, -0.1, **_rauth(0.1)))


def test_first_segment_upper_a_is_the_lowest_boundary_of_the_usable_segments():
    segs = [{"upper_current_a": 12.0, "rate_a_s": 0.005}, {"upper_current_a": 9.0, "rate_a_s": 0.01},
            {"upper_current_a": 0.0, "rate_a_s": 0.5}, {"upper_current_a": 3.0, "rate_a_s": 0.0},
            {"upper_current_a": None, "rate_a_s": 0.1}, {"upper_current_a": float("nan"), "rate_a_s": 1.0}]
    assert mc.first_segment_upper_a(segs) == 9.0
    assert mc.first_segment_upper_a([]) is None and mc.first_segment_upper_a(None) is None
    assert mc.first_segment_upper_a([{"upper_current_a": 5.0, "rate_a_s": None}]) is None


def test_whatever_the_unverified_guard_lets_through_is_safe_even_if_segments_do_not_apply(tmp_path):
    """The point of the guard: in a supply whose segments do NOT act on the negative side, every
    negative target an unverified plan allows is ramped no faster than the segment table
    prescribes by |I|; the targets it refuses are exactly where that would not hold."""
    allowed, refused_unsafe = [], []
    for k in range(1, 150):
        t = -k * 0.03
        sim = Sim625()
        sim.negative_segments_apply = False
        _, ctl, _ = _make(tmp_path, sim)
        p = _plan(ctl, t, **_rauth(5.0, "反向 5 T"))
        a = abs(round(t / K, 4))
        broken = [sim.rate_at(-x * 0.01) for x in range(0, int(a * 100) + 1)]
        table = [Sim625().rate_at(x * 0.01) for x in range(0, int(a * 100) + 1)]
        safe = all(b <= s + 1e-12 for b, s in zip(broken, table))
        if p.ok:
            allowed.append(t)
            assert safe, (t, p.refusal_text())
        elif not safe:
            refused_unsafe.append(t)
        if "negative_segments" in _refused(p):
            assert not safe, t                               # refused only where it matters
    assert allowed and refused_unsafe                        # neither side of the check is vacuous


# -- ETA ----------------------------------------------------------------------------------
def test_eta_is_symmetric_in_the_sign_of_the_current():
    segs = [{"upper_current_a": u, "rate_a_s": r} for u, r in SEGMENTS.values()]
    pos = mc.ramp_eta_s(0.0, 14.68, segs, 0.010)
    assert mc.ramp_eta_s(0.0, -14.68, segs, 0.010) == pytest.approx(pos)
    assert mc.ramp_eta_s(-14.68, 0.0, segs, 0.010) == pytest.approx(pos)
    assert mc.ramp_eta_s(-3.0, -10.0, segs, 0.010) == pytest.approx(mc.ramp_eta_s(3.0, 10.0, segs, 0.010))
    # through zero: down, then up the other side
    assert mc.ramp_eta_s(-3.0, 4.0, segs, 0.010) == pytest.approx(
        mc.ramp_eta_s(3.0, 0.0, segs, 0.010) + mc.ramp_eta_s(0.0, 4.0, segs, 0.010))
    assert mc.ramp_eta_s(-3.0, 4.0, segs[:1], None) is not None
    assert mc.ramp_eta_s(-3.0, 20.0, segs[:1], None) is None      # the far side has no rate
    assert mc.ramp_eta_s(0.0, -20.0, segs[:1], None) is None


def test_a_planned_negative_ramp_has_the_same_eta_as_the_positive_one(tmp_path):
    _, ctl, _ = _make(tmp_path, settings=VERIFIED)
    neg = _plan(ctl, -3.0, **_rauth(3.0))
    pos = _plan(ctl, 3.0, **_auth(3.0))
    assert neg.ok and pos.ok and neg.eta_s == pytest.approx(pos.eta_s)
    sim = Sim625(current=-round(3.0 / K, 4), setpoint=-round(3.0 / K, 4))
    _, ctl, _ = _make(tmp_path, sim)
    back = _plan(ctl, 0.0)
    assert back.eta_s == pytest.approx(pos.eta_s)                 # -x -> 0 takes as long as 0 -> x


# -- supervision and STOP at a negative current -------------------------------------------
def _started_neg(tmp_path, target=-1.0, settings=None, **sim_kw):
    sim = Sim625(**sim_kw)
    _, ctl, clk = _make(tmp_path, sim, settings=settings)
    r = ctl.start(target, origin="test", **_rauth(abs(target)), tip_verdict=_parked)
    assert r["started"], r
    return sim, ctl, clk


def test_arrival_with_a_residual_is_reported_with_its_sign_at_a_negative_target(tmp_path):
    sim, ctl, _ = _started_neg(tmp_path, -1.0, step_a=5.0)
    sim.read_offset_a = 0.020                                   # reads 20 mA toward zero
    out = ctl.supervise()
    assert out["outcome"] == "done", out
    assert out["last"]["residual_a"] == pytest.approx(0.020, abs=1e-4)
    assert sim.writes == [b"SETI -2.9360\r\n"]


def test_a_stalled_negative_ramp_is_stopped(tmp_path):
    sim, ctl, _ = _started_neg(tmp_path, -2.0, step_a=0.0)
    out = ctl.supervise()
    assert out["outcome"] == "stalled" and sim.stops() == 1


def test_abort_and_an_external_change_on_a_negative_ramp(tmp_path):
    sim, ctl, _ = _started_neg(tmp_path, -2.0, step_a=0.1)
    calls = {"n": 0}

    def abort():
        calls["n"] += 1
        return calls["n"] > 3

    assert ctl.supervise(abort_check=abort)["outcome"] == "aborted" and sim.stops() == 1
    sim, ctl, _ = _started_neg(tmp_path, -2.0, step_a=0.1)

    def panel(s, cmd):
        if cmd == "SETI?" and s.current < -0.5:
            s.setpoint = -1.0                                    # set elsewhere, also negative

    sim.on_query = panel
    out = ctl.supervise()
    assert out["outcome"] == "external_change" and sim.stops() == 0 and len(sim.setis()) == 1


def test_stop_works_at_a_negative_current_and_freezes_it_there(tmp_path):
    sim, ctl, _ = _started_neg(tmp_path, -3.0, step_a=0.2)
    n = {"q": 0}

    def press_stop(s, cmd):
        n["q"] += 1
        if n["q"] == 3:
            ctl.stop(reason="StopMagnetRamp", origin="skill")

    sim.on_query = press_stop
    out = ctl.supervise()
    assert out["outcome"] == "stopped" and sim.stops() == 1
    frozen = sim.current
    assert frozen < 0 and sim.writes[-1] == b"STOP\r\n"
    sim.on_query = None
    assert ctl.stop(reason="again", origin="t")["sent"] and sim.current == frozen
    # a supply frozen on the negative side does not lock MAST out of the way back
    p = _plan(ctl, 0.0)
    assert p.ok, p.refusal_text()


def test_stop_is_still_not_sent_while_the_supply_protects_a_negative_output(tmp_path):
    sim, ctl, _ = _make(tmp_path, Sim625(current=-5.0, setpoint=-5.0))
    sim.erst = [0, 1 << 5, 0]
    out = ctl.stop(reason="E-STOP", origin="test")
    assert out["skipped"] and sim.writes == []


# -- settings ----------------------------------------------------------------------------
def test_the_verified_flag_is_read_strictly_from_the_profile(monkeypatch):
    from mast.core import instrument_profile as ip

    def settings_with(value):
        stored = {} if value is None else {mc.PROFILE_NEGATIVE_SEGMENTS_VERIFIED: value}
        monkeypatch.setattr(ip, "get_config", lambda key, default=None: stored.get(key, default))
        return mc.MagnetSettings.from_profile()

    assert settings_with(None).negative_segments_verified is False       # default: not verified
    assert settings_with("no").negative_segments_verified is False
    assert settings_with("yes").negative_segments_verified is True
    for junk in ("true", "1", 1, True, "maybe", "", "ja"):
        assert settings_with(junk).negative_segments_verified is False, junk
    assert mc.MagnetSettings().negative_segments_verified is False
    assert settings_with("yes").as_dict()["negative_segments_verified"] is True


def test_the_verified_flag_is_a_profile_choice_with_a_conservative_default():
    from mast.core import instrument_profile as ip

    key = mc.PROFILE_NEGATIVE_SEGMENTS_VERIFIED
    assert key == "magnet_negative_segments_verified" and key in ip.EDITABLE_KEYS
    assert ip.spec_default(key) == "no"
    spec = {f["key"]: f for f in ip.field_specs()}[key]
    assert spec["type"] == "choice" and [c["value"] for c in spec["choices"]] == ["no", "yes"]
    assert ip.sanitize({key: "yes"})[key] == "yes"
    assert key not in ip.sanitize({key: "maybe"}) and key not in ip.sanitize({key: True})


# -- skills ------------------------------------------------------------------------------
def test_skill_specs_no_longer_say_no_reverse_field():
    from mast.skills.builtins.magnet_control import CheckMagnetFieldChange, RampMagnetField

    for cls in (CheckMagnetFieldChange, RampMagnetField):
        md = cls().metadata()
        (target,) = [p for p in md.parameters if p.name == "target_t"]
        assert target.min_value is None, "target_t is bipolar: no >= 0 bound"
        text = md.description + target.description
        assert ">= 0" not in text and "不允许反向场" not in text and "no reverse field" not in text
    ramp = RampMagnetField().metadata()
    assert "-2 T" in ramp.description
    assert any(n in ramp.description for n in ("不跨零", "never crosses zero"))


def test_ramp_skill_follows_a_negative_target(wired, monkeypatch):
    from mast.core import tip_park
    from mast.skills.builtins.magnet_control import CheckMagnetFieldChange, RampMagnetField

    sim, _ = wired
    monkeypatch.setattr(tip_park, "tip_parked", lambda ctx: _parked())
    ctx = SimpleNamespace(owner="ext:claude-code", check_abort=lambda: False)
    c = CheckMagnetFieldChange().execute(ctx, {"target_t": -1.0, **_rauth(1.0)})
    assert c.success and c.data["would_proceed"] is True and sim.writes == []
    r = RampMagnetField().execute(ctx, {"target_t": -1.0, **_rauth(1.0)})
    assert r.success, r.error
    assert sim.setis() == [-A_1T] and r.data["outcome"] == "done"
    refused = RampMagnetField().execute(ctx, {"target_t": 1.0})        # crossing, no words
    assert not refused.success and "no_zero_crossing" in refused.error


# ── skills ───────────────────────────────────────────────────────────────────
@pytest.fixture
def wired(tmp_path):
    sim, ctl, _ = _make(tmp_path, Sim625(step_a=0.5))
    magnet_control.set_source(lambda: ctl)
    yield sim, ctl
    magnet_control.set_source(None)


def test_skill_metadata():
    from mast.skills.builtins.magnet_control import (
        CheckMagnetFieldChange, RampMagnetField, StopMagnetRamp)

    ramp = RampMagnetField().metadata()
    assert ramp.category is SkillCategory.WRITE and ramp.safety_level is SafetyLevel.DANGEROUS
    assert needs_token(ramp)
    assert {p.name for p in ramp.parameters} == {
        "target_t", "user_authorization", "authorized_up_to_t", "require_tip_withdrawn"}
    stop = StopMagnetRamp().metadata()
    assert "StopMagnetRamp" in BYPASS_NAMES and not needs_token(stop)
    check = CheckMagnetFieldChange().metadata()
    assert check.category is SkillCategory.READ and not needs_token(check)


def test_ramp_skill_runs_and_follows_to_the_target(wired, monkeypatch):
    from mast.core import tip_park
    from mast.skills.builtins.magnet_control import RampMagnetField

    sim, _ = wired
    monkeypatch.setattr(tip_park, "tip_parked", lambda ctx: _parked())
    ctx = SimpleNamespace(owner="ext:claude-code", check_abort=lambda: False)
    r = RampMagnetField().execute(ctx, {"target_t": 1.0, "user_authorization": "go",
                                        "authorized_up_to_t": 1.0})
    assert r.success, r.error
    assert sim.setis() == [round(1.0 / K, 4)]
    assert r.data["outcome"] == "done"


def test_ramp_skill_refusal_is_a_failed_result_not_an_exception(wired, monkeypatch):
    from mast.core import tip_park
    from mast.skills.builtins.magnet_control import RampMagnetField

    sim, _ = wired
    monkeypatch.setattr(tip_park, "tip_parked", lambda ctx: _parked())
    r = RampMagnetField().execute(SimpleNamespace(), {"target_t": 1.0})
    assert not r.success and "user_authorization" in r.error and sim.writes == []


def test_stop_and_check_skills(wired, monkeypatch):
    from mast.core import tip_park
    from mast.skills.builtins.magnet_control import CheckMagnetFieldChange, StopMagnetRamp

    sim, _ = wired
    monkeypatch.setattr(tip_park, "tip_parked", lambda ctx: _parked())
    c = CheckMagnetFieldChange().execute(SimpleNamespace(), {"target_t": 9.0})
    assert c.success and c.data["would_proceed"] is False and sim.writes == []
    s = StopMagnetRamp().execute(SimpleNamespace(), {})
    assert s.success and sim.writes == [b"STOP\r\n"]


def test_the_module_is_switched_on_by_detecting_one_625_and_by_nothing_else(tmp_path):
    from mast.skills import hardware_modules as hm

    hm.set_enabled(None)                               # the operator switched nothing on
    magnet_control.set_source(None)
    try:
        assert "superconducting_magnet" not in hm.enabled_ids()
        assert {"RampMagnetField", "StopMagnetRamp",
                "CheckMagnetFieldChange"} <= hm.disabled_skill_names()
        magnet_control.set_source(lambda: None)        # wired, but no 625 (or two)
        assert "superconducting_magnet" not in hm.enabled_ids()
        _, ctl, _ = _make(tmp_path)
        magnet_control.set_source(lambda: ctl)         # exactly one identified 625
        assert "superconducting_magnet" in hm.enabled_ids()
        assert "RampMagnetField" not in hm.disabled_skill_names()
        assert not hm.MODULE_BY_ID["superconducting_magnet"].default_on
    finally:
        magnet_control.set_source(None)
        hm.set_enabled(None)


def test_skills_without_a_magnet_say_so():
    from mast.skills.builtins.magnet_control import RampMagnetField, StopMagnetRamp

    magnet_control.set_source(None)
    for cls, params in ((RampMagnetField, {"target_t": 1.0}), (StopMagnetRamp, {})):
        r = cls().execute(SimpleNamespace(), params)
        assert not r.success and r.data["reason"] == "no_magnet"


# ── runtime: one controller per link, E-STOP, shutdown ───────────────────────
class _Monitor:
    def __init__(self, sensors):
        self._sensors = sensors


def _runtime_with(*links):
    from mast.core.runtime import CoreRuntime

    rt = CoreRuntime.__new__(CoreRuntime)
    sensors = {}
    for i, link in enumerate(links):
        sensors[f"f{i}"] = m625.Model625Sensor(link, name=f"f{i}", quantity="field")
        sensors[f"c{i}"] = m625.Model625Sensor(link, name=f"c{i}", quantity="current")
    rt._monitor = _Monitor(sensors)
    return rt


def test_runtime_keeps_one_controller_per_link_and_none_when_ambiguous():
    a = m625.Model625Link(Sim625(), port="COM13", min_gap_s=0.0)
    b = m625.Model625Link(Sim625(), port="COM16", min_gap_s=0.0)
    rt = _runtime_with(a)
    ctl = rt.magnet_controller()
    assert ctl is not None and ctl.link is a and rt.magnet_controller() is ctl
    assert _runtime_with(a, b).magnet_controller() is None
    assert _runtime_with().magnet_controller() is None


# ── the heater's view of the field: is it at rest? ────────────────────────────
def _link(sim=None):
    return m625.Model625Link(sim or Sim625(), port="COM13", min_gap_s=0.0)


def test_the_runtime_says_the_field_is_at_rest_with_no_625_or_one_resting_625():
    rt = _runtime_with()
    v = rt.field_state()
    assert v["ok"] is True and v["present"] is False                  # no magnet supply here
    v = _runtime_with(_link()).field_state()
    assert v["ok"] is True and v["present"] is True


def test_the_runtime_refuses_a_ramping_625_whoever_started_it():
    sim = Sim625(step_a=0.5)                                          # slow: still ramping when read
    sim.setpoint = 5.0                                                # started at the panel
    v = _runtime_with(_link(sim)).field_state()
    assert v["ok"] is False and v["present"] is True


@pytest.mark.parametrize("erst", [[1, 0, 0], [0, 2, 0], [0, 0, 4]])
def test_the_runtime_refuses_a_625_with_an_error_bit(erst):
    sim = Sim625()
    sim.erst = erst
    assert _runtime_with(_link(sim)).field_state()["ok"] is False


def test_the_runtime_refuses_a_625_it_cannot_read():
    sim = Sim625()
    sim.fail_queries = 50
    assert _runtime_with(_link(sim)).field_state()["ok"] is False


def test_the_runtime_refuses_two_625s_and_a_625_without_a_controller(monkeypatch):
    a, b = _link(), m625.Model625Link(Sim625(), port="COM16", min_gap_s=0.0)
    two = _runtime_with(a, b).field_state()
    assert two["ok"] is False and ("不止一台" in two["detail"] or "more than one" in two["detail"])
    rt = _runtime_with(a)
    monkeypatch.setattr(rt, "magnet_controller", lambda: None)
    v = rt.field_state()
    assert v["ok"] is False and v["present"] is True


def test_the_runtime_refuses_while_mast_is_changing_the_field(monkeypatch):
    rt = _runtime_with(_link())
    monkeypatch.setattr(mc.Magnet625Controller, "busy", property(lambda self: True))
    assert rt.field_state()["ok"] is False


@pytest.mark.parametrize("st,ok", [
    ({"opst": 0b110, "current_a": 0.0, "hw_error_code": 0, "op_error_code": 0, "psh_error_code": 0}, True),
    ({"opst": 0b100, "current_a": 1.0, "hw_error_code": 0, "op_error_code": 0, "psh_error_code": 0}, False),
    ({"opst": 0b110, "current_a": 0.0, "hw_error_code": 0, "op_error_code": 8, "psh_error_code": 0}, False),
    ({"opst": None, "current_a": 0.0, "hw_error_code": 0, "op_error_code": 0, "psh_error_code": 0}, False),
    ({"opst": 0b110, "current_a": None, "hw_error_code": 0, "op_error_code": 0, "psh_error_code": 0}, False),
    ({"opst": 0b110, "current_a": 0.0, "hw_error_code": None, "op_error_code": 0, "psh_error_code": 0}, False),
    (None, False), ("x", False),
])
def test_the_panel_verdict_judges_the_cached_fast_state(st, ok):
    assert mc.panel_field_verdict(st)[0] is ok


def _estop_ready(rt, monkeypatch, tmp_path):
    monkeypatch.setattr(mc, "_default_ledger_path", lambda: tmp_path / "l.jsonl")
    rt._orch_abort = None
    rt._orch_run_aborts = {}
    rt._orch_run_aborts_lock = threading.Lock()
    rt._executor = None
    rt._orch_interrupts = None
    rt._pool = None
    monkeypatch.setattr(rt, "_optics_emergency_stop", lambda why: {}, raising=False)


def _wait_for(pred, timeout=10.0):
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def test_emergency_stop_reaches_the_magnet(tmp_path, monkeypatch):
    sim = Sim625()
    rt = _runtime_with(m625.Model625Link(sim, port="COM13", min_gap_s=0.0))
    _estop_ready(rt, monkeypatch, tmp_path)
    res = rt.emergency_stop("test")
    assert res["magnet"] == "dispatched"
    assert _wait_for(lambda: sim.stops() == 1)


def test_emergency_stop_retries_a_stop_that_did_not_go_out(tmp_path, monkeypatch):
    sim = Sim625()
    sim.fail_stop_writes = 2
    rt = _runtime_with(m625.Model625Link(sim, port="COM13", min_gap_s=0.0))
    _estop_ready(rt, monkeypatch, tmp_path)
    rt.emergency_stop("test")
    assert _wait_for(lambda: sim.stops() == 1, timeout=15.0)


def test_shutdown_hook_stops_only_a_mast_ramp_and_closes_the_door(tmp_path, monkeypatch):
    sim = Sim625()
    rt = _runtime_with(m625.Model625Link(sim, port="COM13", min_gap_s=0.0))
    monkeypatch.setattr(mc, "_default_ledger_path", lambda: tmp_path / "l.jsonl")
    rt._magnet_shutdown()                     # no controller made yet: nothing
    ctl = rt.magnet_controller()
    ctl._active = {"target_a": 1.0, "stop_seq": ctl._stop_seq}
    rt._magnet_shutdown()
    assert sim.stops() == 1
    ctl._active = None
    r = ctl.start(1.0, origin="t", **_auth(1.0), tip_verdict=_parked)
    assert not r["started"] and sim.setis() == []


# ── API: status, operator STOP, scans refused while busy, init page fact ─────
def test_status_and_operator_stop_routes(wired):
    from mast.api.routes.admin import magnet_control_status, magnet_control_stop
    from mast.api.schemas_admin import MagnetStopRequest

    sim, _ = wired
    st = magnet_control_status()
    assert st.present and st.can_set_field and st.active is None
    r = magnet_control_stop(MagnetStopRequest(reason="test"))
    assert r.sent and sim.writes == [b"STOP\r\n"]
    magnet_control.set_source(None)
    assert magnet_control_status().present is False
    assert magnet_control_stop(MagnetStopRequest()).present is False


def test_every_scan_that_closes_the_port_is_refused_while_a_ramp_is_busy(wired):
    from fastapi import HTTPException

    from mast.api.routes.admin import (
        adopt_sensors, discover_environment_devices, rescan_sensors)
    from mast.api.schemas_admin import SensorAdoptRequest

    _, ctl = wired
    ctl._active = {"target_a": 1.0, "stop_seq": ctl._stop_seq}
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(ctx=SimpleNamespace())))
    for call in (lambda: rescan_sensors(req), lambda: discover_environment_devices(req),
                 lambda: adopt_sensors(req, SensorAdoptRequest(sensors=[]))):
        with pytest.raises(HTTPException) as e:
            call()
        assert e.value.status_code == 409
    ctl._active = None
    assert discover_environment_devices(req).degraded is True     # not busy: goes through


def test_init_page_learns_whether_a_magnet_supply_is_here(wired):
    from mast.api.routes import instrument_init as route
    from mast.core import instrument_init as ii

    vals = route._stored_values(None)
    assert vals[ii.RUNTIME_MAGNET_SUPPLY] == "lakeshore_625"
    magnet_control.set_source(lambda: None)
    assert route._stored_values(None)[ii.RUNTIME_MAGNET_SUPPLY] == "none"
    magnet_control.set_source(None)
    assert ii.RUNTIME_MAGNET_SUPPLY not in route._stored_values(None)


def test_settings_come_from_the_instrument_profile(monkeypatch):
    from mast.core import instrument_profile as ip

    monkeypatch.setattr(ip, "get_config", lambda key, default=None: {
        mc.PROFILE_MAX_FIELD: 5.0, mc.PROFILE_FIELD_CONSTANT: 0.3406,
        mc.PROFILE_TEMPERATURE_CHANNEL: " Magnet ", mc.PROFILE_MAX_TEMPERATURE: 4.5,
    }.get(key, default))
    s = mc.MagnetSettings.from_profile()
    assert (s.max_field_t, s.field_constant_t_per_a, s.temperature_channel,
            s.max_temperature_k) == (5.0, 0.3406, "Magnet", 4.5)
    assert s.control_configured
    for key in (mc.PROFILE_MAX_FIELD, mc.PROFILE_FIELD_CONSTANT,
                mc.PROFILE_TEMPERATURE_CHANNEL, mc.PROFILE_MAX_TEMPERATURE):
        assert key in ip.EDITABLE_KEYS


def test_out_of_range_magnet_limits_are_dropped_not_clamped():
    from mast.core.instrument_profile import sanitize

    out = sanitize({"magnet_max_field_t": 51, "magnet_field_constant_t_per_a": 0.3406,
                    "magnet_max_temperature_k": -1, "tilt_limit_deg": 99})
    assert "magnet_max_field_t" not in out            # 51 is not stored as 30 T
    assert "magnet_max_temperature_k" not in out
    assert out["magnet_field_constant_t_per_a"] == pytest.approx(0.3406)
    assert out["tilt_limit_deg"] == 45.0              # other keys keep their clamping
