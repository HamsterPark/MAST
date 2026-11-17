"""Sample-stage heating on the Lake Shore 335: ``mast.instruments.heater_335`` and its skills.

What is pinned here:

* the only bytes temperature control can build are ``SETP``, ``RAMP`` and ``RANGE`` for
  output 1 or 2, and a bare terminator before a heater-off. The read-only driver still
  builds queries only, never one that clears something when read (``HTRST?``, ``*ESR?``,
  ``OPSTR?``) or reveals the lock code;
* every gate refuses (none clamps), and refuses before anything is written; the
  configuration is read again just before the first write;
* turning a heater on never lets the loop see a parked setpoint: the setpoint is brought
  to the present temperature while the heater is still off (a 335 ramp starts from the
  OLD setpoint, manual p. 63; a parked setpoint such as 40 K is the typical case);
* a command the instrument ignores leaves the heater as it was; a heater this change
  turned on and could not finish is turned off; a range it raised is put back;
* a heater-off is done when RANGE? reads 0, not when its bytes left; one that does not
  read off is sent again, in the background after supervision gives up;
* supervision turns the heater off on overtemperature, a sensor fault, the guard
  thermometer, saturation, lost contact; it writes nothing when the instrument or the
  panel took over; afterwards the watch keeps the same hard limits, and a restarted MAST
  picks the watch up from the ledger.

Findings of an independent review are marked "review #n".

The simulator models the manual's semantics: a ramp runs from the old setpoint at the
ramp rate; turning ramping off mid-ramp freezes the setpoint; SETP? may report the live
ramping value or the final one (the manual does not say, so both are tested). In zone
mode (§5.3 p. 69) the setpoint crossing into another zone, by a SETP or during a ramp,
loads that zone's P, I, D, manual output, heater range, ramp rate and control input; a
manual change holds until the next crossing. What the manual leaves open (does entering
zone mode, or writing a zone, apply a zone at once?) is a switch, and both answers are
tested.

Also covered: the maximum setpoint is the stage's
absolute maximum (TLIMIT window, ceiling cap), zone tables and zone-mode heating, the
range-above-maximum hard limit, and the 50 ms message gap on every read path. The
field-control side of the heating / field interlock is in test_magnet_field_control.py.
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

from mast.core import heater_control
from mast.core.instrument_lock import BYPASS_NAMES
from mast.core.types import SafetyLevel, SkillCategory
from mast.environment import lakeshore_temp as lsci
from mast.environment.serial_transport import SerialUnavailable
from mast.instruments import heater_335 as hc

PARKED = SimpleNamespace(state="parked", reason="withdrawn")


def _parked():
    return PARKED


# ── a Model 335 on a fake port ────────────────────────────────────────────────
#: A zone nobody set up (factory defaults, Table 8-1 p. 134).
UNUSED = {"ub": 0.0, "p": 50.0, "i": 20.0, "d": 0.0, "mout": 0.0, "range": 0, "input": 0,
          "rate": 0.1}


class Sim335:
    """Answers the queries the controller asks; applies SETP / RAMP / RANGE / ZONE /
    OUTMODE like the manual, zone mode included."""

    supports_generations = True
    supports_min_gap = True

    def __init__(self, *, idn: str = "LSCI,MODEL335,TEST0001/0000000,1.10"):
        self.settings = SimpleNamespace(port="COMX")
        self.idn = idn
        self.gen = 1
        self.open = True
        self.now = 0.0
        self.emul = "0,0"
        self.mode = 0
        # TLIMIT on A at the maximum setpoint of SETTINGS (40 K): the window is
        # [min(target + allowance, maximum), maximum].
        self.inputs = {"A": {"k": 4.342, "st": 0, "intype": "3,0,6,1,1", "tlimit": 40.0},
                       "B": {"k": 3.878, "st": 0, "intype": "1,0,0,0,1", "tlimit": 0.0}}
        # Typical initial state: output 1 open loop parked at 500 K, output 2 closed-loop
        # PID on A parked at 40 K, both off, ramping off.
        self.outputs = {1: self._out(sp=500.0, outmode="3,0,0"),
                        2: self._out(sp=40.0, outmode="1,1,0")}
        self.zones = {n: [dict(UNUSED) for _ in range(10)] for n in (1, 2)}
        self.outmode_loads_zone = False          # entering zone mode applies a zone at once
        self.zone_write_reloads = False          # writing the zone in force applies it at once
        self.zone_loads: list[tuple] = []        # (output, zone, time) each time a zone loads
        self.accept_writes = True
        self.ignore_indices: set[int] = set()   # write numbers (0-based) to ignore
        self.ignore_verbs: set[str] = set()     # verbs whose writes are ignored
        self.raise_once: "str | None" = None     # verb whose next write raises after applying
        self.raise_always: set[str] = set()      # verbs whose writes always raise, unapplied
        self.setp_reports = "final"              # or "live"
        self.kelvin_offset = 0.0                 # regulated temperature = setpoint + this
        self.follow = True                       # False: the control sensor does not see the heater
        self.htr_pct: "float | None" = None      # HTR? while on (default 50 %)
        self.fail_reads = 0
        self.events: list = []                   # (time, callable) run once when now >= time
        self.writes: list[bytes] = []
        self.queries: list[bytes] = []
        self.range_on: list[tuple] = []          # (output, setpoint, kelvin) when RANGE > 0 lands
        self.gaps: list = []
        self.closed = 0

    @staticmethod
    def _out(sp: float, outmode: str) -> dict:
        return {"range": 0, "sp": sp, "ramp_on": 0, "rate": 0.0, "ramping": 0, "ramp_from": sp,
                "ramp_to": sp, "ramp_t0": 0.0, "outmode": outmode, "htrset": "0,1,2,+1.000,1",
                "mout": 0.0, "pid": "+50.0,+20.0,+0.0", "zone": None}

    # zone mode (§4.5.1.6.2 p. 58, §5.3 p. 69)
    def set_zones(self, n: int, rows: list[dict], *, mode: "str | None" = None) -> None:
        """Store a table (rows in ZONE order, the rest unused); optionally the mode, e.g.
        ``"2,1,0"``. The zone in force is the setpoint's, as if it had crossed into it
        earlier and the heater had been turned off by hand since."""
        self.zones[n] = [dict(UNUSED, **r) for r in rows] + [dict(UNUSED) for _ in range(10 - len(rows))]
        if mode is not None:
            self.outputs[n]["outmode"] = mode
        self.outputs[n]["zone"] = self._zone_of(n, self.outputs[n]["sp"])

    def _zone_mode(self, n: int) -> bool:
        return self.outputs[n]["outmode"].startswith("2,")

    def _used(self, n: int) -> list[int]:
        used, prev = [], 0.0
        for k, z in enumerate(self.zones[n], 1):
            if not z["ub"] > prev:
                break
            used.append(k)
            prev = z["ub"]
        return used

    def _zone_of(self, n: int, x: float) -> "int | None":
        for k in self._used(n):
            if x <= self.zones[n][k - 1]["ub"] + 1e-12:
                return k
        return None

    def _enter_zone(self, n: int, k: "int | None") -> None:
        """The setpoint crossed into zone k: load its parameters (p. 69)."""
        o = self.outputs[n]
        o["zone"] = k
        if k is None:
            return
        z = self.zones[n][k - 1]
        o["pid"] = f"+{z['p']:.1f},+{z['i']:.1f},+{z['d']:.1f}"
        o["mout"], o["rate"] = z["mout"], z["rate"]
        if z["input"]:
            mode, _inp, powerup = o["outmode"].split(",")
            o["outmode"] = f"{mode},{z['input']},{powerup}"
        if z["range"] > 0 and o["range"] == 0:
            self.range_on.append((n, o["sp"], self.inputs["A"]["k"]))
        o["range"] = z["range"]
        self.zone_loads.append((n, k, self.now))

    def _ramp(self, n: int, o: dict) -> None:
        """Move a ramping setpoint to ``now``; in zone mode stop at each zone boundary it
        crosses, load the next zone (its rate included) and go on from there."""
        while o["ramping"]:
            r = o["rate"] / 60.0
            x0, t0, to = o["ramp_from"], o["ramp_t0"], o["ramp_to"]
            d = 1.0 if to > x0 else -1.0
            x = x0 + d * r * (self.now - t0)
            done = (x - to) * d >= 0
            if done:
                x = to
            zc = o["zone"]
            if self._zone_mode(n) and zc is not None:
                if d > 0 and x > self.zones[n][zc - 1]["ub"]:
                    b = self.zones[n][zc - 1]["ub"]
                    o.update(sp=b, ramp_from=b, ramp_t0=t0 + (b - x0) / r)
                    self._enter_zone(n, zc + 1 if zc + 1 in self._used(n) else None)
                    continue
                if d < 0 and zc > 1 and x <= self.zones[n][zc - 2]["ub"]:
                    b = self.zones[n][zc - 2]["ub"]
                    o.update(sp=b, ramp_from=b, ramp_t0=t0 + (x0 - b) / r)
                    self._enter_zone(n, zc - 1)
                    continue
            o["sp"] = x
            if done:
                o["ramping"] = 0
            break

    # transport protocol
    @property
    def open_generation(self) -> int:
        return self.gen

    @property
    def is_open(self) -> bool:
        return self.open

    def close(self) -> None:
        self.open = False
        self.closed += 1

    def reopen(self, idn: "str | None" = None) -> None:
        self.open = True
        self.gen += 1
        if idn:
            self.idn = idn

    # time
    def advance(self, dt: float) -> None:
        self.now += float(dt)
        self._update()

    def _update(self) -> None:
        for t, fn in list(self.events):
            if self.now >= t:
                self.events.remove((t, fn))
                fn(self)
        for n, o in self.outputs.items():
            if o["ramping"]:
                self._ramp(n, o)
        o2 = self.outputs[2]
        if self.follow and o2["range"] > 0 and o2["outmode"][:3] in ("1,1", "2,1"):
            self.inputs["A"]["k"] = o2["sp"] + self.kelvin_offset

    def transact(self, payload: bytes, *, read_until=None, read_size=None, timeout=None,
                 lines: int = 1, expect_generation=None, min_gap_s=None) -> bytes:
        if expect_generation is not None and (not self.open or expect_generation != self.gen):
            raise SerialUnavailable("not the verified handle")
        if not self.open:
            self.reopen()
        self.gaps.append(min_gap_s)
        self._update()
        text = payload.decode("ascii").strip()
        if not text:                             # a bare terminator
            self.writes.append(payload)
            return b""
        verb = text.split()[0]
        if verb.endswith("?"):
            self.queries.append(payload)
            if self.fail_reads > 0:
                self.fail_reads -= 1
                return b""
            return (self._answer(text) + "\r\n").encode("ascii")
        idx = len(self.writes)
        self.writes.append(payload)
        if verb in self.raise_always:
            raise SerialUnavailable("write failed")
        if self.raise_once == verb:
            self.raise_once = None
            self._apply(text)
            raise SerialUnavailable("write raised after the bytes left")
        if self.accept_writes and idx not in self.ignore_indices and verb not in self.ignore_verbs:
            self._apply(text)
        return b""

    def _answer(self, text: str) -> str:
        verb, _, arg = text.partition(" ")
        if verb == "*IDN?":
            return self.idn
        if verb == "EMUL?":
            return self.emul
        if verb == "MODE?":
            return str(self.mode)
        if verb == "ZONE?":
            n, k = (int(x) for x in arg.split(","))
            z = self.zones[n][k - 1]
            return (f"+{z['ub']:09.3f},+{z['p']:06.1f},+{z['i']:06.1f},+{z['d']:05.1f},"
                    f"+{z['mout']:06.2f},{z['range']},{z['input']},+{z['rate']:05.1f}")
        if verb in ("KRDG?", "RDGST?", "INTYPE?", "TLIMIT?"):
            i = self.inputs[arg]
            return {"KRDG?": f"+{i['k']:.4f}", "RDGST?": f"{i['st']:03d}",
                    "INTYPE?": i["intype"], "TLIMIT?": f"+{i['tlimit']:.1f}"}[verb]
        o = self.outputs[int(arg)]
        if verb == "SETP?":
            live = self.setp_reports == "live" or not o["ramping"]
            return f"+{(o['sp'] if live else o['ramp_to']):.3f}"
        htr = (self.htr_pct if self.htr_pct is not None else 50.0) if o["range"] else 0.0
        return {"RANGE?": str(o["range"]), "RAMP?": f"{o['ramp_on']},{o['rate']:.1f}",
                "RAMPST?": str(o["ramping"]), "OUTMODE?": o["outmode"],
                "HTRSET?": o["htrset"], "MOUT?": f"+{o['mout']:.2f}",
                "HTR?": f"+{htr:.1f}", "PID?": o["pid"]}[verb]

    def _apply(self, text: str) -> None:
        verb, _, args = text.partition(" ")
        parts = args.split(",")
        n = int(parts[0])
        o = self.outputs[n]
        if verb == "RANGE":
            o["range"] = int(parts[1])
            if o["range"] > 0:
                self.range_on.append((n, o["sp"], self.inputs["A"]["k"]))
        elif verb == "RAMP":
            on, rate = int(parts[1]), float(parts[2])
            if not on and o["ramping"]:
                o["ramping"] = 0                      # freezes at the live value (p. 63)
            o["ramp_on"], o["rate"] = on, rate
        elif verb == "SETP":
            v = float(parts[1])
            if o["ramp_on"] and o["rate"] > 0 and abs(v - o["sp"]) > 1e-9:
                o.update(ramp_from=o["sp"], ramp_to=v, ramp_t0=self.now, ramping=1)
            else:
                o.update(sp=v, ramp_to=v, ramping=0)
                if self._zone_mode(n):                # a jump into another zone loads it
                    k = self._zone_of(n, v)
                    if k != o["zone"]:
                        self._enter_zone(n, k)
        elif verb == "ZONE":
            assert len(parts) == 10, text
            k = int(parts[1])
            self.zones[n][k - 1] = {"ub": float(parts[2]), "p": float(parts[3]),
                                    "i": float(parts[4]), "d": float(parts[5]),
                                    "mout": float(parts[6]), "range": int(parts[7]),
                                    "input": int(parts[8]), "rate": float(parts[9])}
            if self.zone_write_reloads and self._zone_mode(n) and o["zone"] == k:
                self._enter_zone(n, k)
        elif verb == "OUTMODE":
            was_zone = self._zone_mode(n)
            o["outmode"] = f"{int(parts[1])},{int(parts[2])},{int(parts[3])}"
            if self._zone_mode(n) and not was_zone:
                k = self._zone_of(n, o["sp"])
                if self.outmode_loads_zone:
                    self._enter_zone(n, k)
                else:
                    o["zone"] = k
            elif not self._zone_mode(n):
                o["zone"] = None
        else:  # pragma: no cover
            raise AssertionError(f"unexpected write {text}")

    def write_texts(self) -> list[str]:
        return [w.decode("ascii").strip() for w in self.writes]

    def cmds(self) -> list[str]:
        """The writes without the bare terminators that precede a heater-off."""
        return [t for t in self.write_texts() if t]


SETTINGS = hc.HeaterSettings(output=2, control_input="A", max_setpoint_k=40.0, max_range=1,
                             max_current_a=1.0, max_rate_k_per_min=1.0)
UNSET = hc.HeaterSettings()


def make(sim: Sim335, tmp_path: Path, *, settings: hc.HeaterSettings = SETTINGS,
         temperature=None, field_busy: bool = False) -> hc.Heater335Controller:
    link = lsci.LakeshoreLink(sim, port="COMX", expected_model="MODEL335")
    return hc.Heater335Controller(
        link, settings=lambda: settings, temperature=temperature or (lambda ch: None),
        field_busy=lambda: field_busy, field_state=lambda: {"ok": True},
        ledger_path=lambda: tmp_path / "heater_ledger.jsonl",
        clock=lambda: sim.now, sleep=sim.advance, background=False)


def _auth(k: float) -> dict:
    return {"user_authorization": f"用户：可以加热到 {k:g} K", "authorized_up_to_k": k}


def _ledger(tmp_path: Path) -> list[dict]:
    p = tmp_path / "heater_ledger.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def _module_source(mod) -> str:
    return Path(mod.__file__).read_text(encoding="utf-8")


# ── 1. bytes ──────────────────────────────────────────────────────────────────
ZONE_ARGS = (1, 25.0, 50, 20, 0, 0, 1, 0, 0.5)     # zone, ub, P, I, D, mout, range, input, rate


MAXK = {"max_setpoint_k": 500.0}     # the byte factory's absolute ceiling, for the format pins


def test_build_write_makes_only_setp_ramp_range_zone_outmode_and_a_bare_terminator():
    assert hc.build_write("SETP", 2, 4.3456, **MAXK) == b"SETP 2,4.346\r\n"
    assert hc.build_write("RAMP", 2, 1, 0.5) == b"RAMP 2,1,0.5\r\n"
    assert hc.build_write("RAMP", 1, 0, 100) == b"RAMP 1,0,100.0\r\n"
    assert hc.build_write("RANGE", 2, 0) == b"RANGE 2,0\r\n"
    assert hc.build_write("RANGE", 1, 3) == b"RANGE 1,3\r\n"
    assert hc.build_write("FLUSH") == b"\r\n"
    assert hc.build_write("ZONE", 2, *ZONE_ARGS, **MAXK) == b"ZONE 2,1,25.000,50.0,20.0,0.0,0.00,1,0,0.5\r\n"
    assert hc.build_write("ZONE", 1, 10, 420.0004, 1000, 0.1, 200, 0.0, 3, 2, 100, **MAXK) == \
        b"ZONE 1,10,420.000,1000.0,0.1,200.0,0.00,3,2,100.0\r\n"
    assert hc.build_write("ZONE", 2, 7, 0, 50, 20, 0, 0, 0, 0, 0.1, **MAXK) == \
        b"ZONE 2,7,0.000,50.0,20.0,0.0,0.00,0,0,0.1\r\n"          # an unused zone
    assert hc.build_write("OUTMODE", 2, 2, 1, 0) == b"OUTMODE 2,2,1,0\r\n"
    assert hc.build_write("OUTMODE", 1, 1, 2, 0) == b"OUTMODE 1,1,2,0\r\n"

    def zone(**over):
        names = ("zone", "ub", "p", "i", "d", "mout", "range", "input", "rate")
        vals = dict(zip(names, ZONE_ARGS), **over)
        return ("ZONE", 2, *(vals[k] for k in names))

    # the site's maximum is in the byte factory: no limit, no SETP / ZONE; nothing above it
    for kw in ({}, {"max_setpoint_k": None}, {"max_setpoint_k": float("nan")}):
        with pytest.raises(ValueError):
            hc.build_write("SETP", 2, 5.0, **kw)
        with pytest.raises(ValueError):
            hc.build_write("ZONE", 2, *ZONE_ARGS, **kw)
    assert hc.build_write("SETP", 2, 50.0, max_setpoint_k=50.0) == b"SETP 2,50.000\r\n"
    for over in (50.001, 52.0):
        with pytest.raises(ValueError):
            hc.build_write("SETP", 2, over, max_setpoint_k=50.0)
    with pytest.raises(ValueError):                        # a zone bound above the site's maximum
        hc.build_write("ZONE", 2, 1, 51.0, 50, 20, 0, 0, 1, 0, 0.5, max_setpoint_k=50.0)
    for bad in [("SETP", 2, -0.001), ("SETP", 2, 500.001), ("SETP", 2, float("nan")),
                ("SETP", 2, True), ("SETP", 2, "5"), ("SETP", 3, 5.0), ("SETP", True, 5.0),
                ("SETP", 2), ("RAMP", 2, 1, 0), ("RAMP", 2, 1, 0.04), ("RAMP", 2, 1, 100.1),
                ("RAMP", 2, 2, 1.0), ("RAMP", 2, True, 1.0), ("RANGE", 2, 4), ("RANGE", 2, -1),
                ("RANGE", 2, True), ("RANGE", 2, 1.0), ("OUTMODE", 2, 1), ("PID", 2, 50),
                ("MOUT", 2, 0), ("TLIMIT", 2, 50), ("ATUNE", 2, 1), ("DFLT", 2, 99),
                ("SETP 2,5;RANGE", 2, 3), ("FLUSH", 2), ("FLUSH", None, 1),
                # ZONE: every field checked against the manual (p. 126); mout only 0
                zone(zone=0), zone(zone=11), zone(zone=True), zone(ub=-0.001), zone(ub=500.001),
                zone(ub=float("inf")), zone(p=0.04), zone(p=1000.1), zone(i=0.0),
                zone(i=1000.1), zone(d=-0.1), zone(d=200.1), zone(mout=0.01), zone(mout=100),
                zone(mout=True), zone(range=4), zone(range=-1), zone(range=1.0),
                zone(input=3), zone(input=-1), zone(rate=0), zone(rate=0.04),
                zone(rate=100.1), zone(rate="1"), ("ZONE", 2, 1, 25.0), ("ZONE", 3, *ZONE_ARGS),
                ("ZONE", 2, *ZONE_ARGS, 0),
                # OUTMODE: closed-loop PID or zone only, a real input, power-up enable 0
                ("OUTMODE", 2, 0, 1, 0), ("OUTMODE", 2, 3, 1, 0), ("OUTMODE", 2, 4, 1, 0),
                ("OUTMODE", 2, 5, 1, 0), ("OUTMODE", 2, 2, 0, 0), ("OUTMODE", 2, 2, 3, 0),
                ("OUTMODE", 2, 2, 1, 1), ("OUTMODE", 2, True, 1, 0), ("OUTMODE", 2, 2, 1),
                ("OUTMODE", 3, 2, 1, 0)]:
        with pytest.raises(ValueError):
            hc.build_write(*bad, **MAXK)


#: Every setting command in the manual's command summary (Table 6-6, p. 106).
ALL_335_SETTERS = ("*CLS", "*ESE", "*OPC", "*RST", "*SRE", "*WAI", "ALARM", "ALMRST", "ANALOG",
                   "ATUNE", "BRIGT", "CRVDEL", "CRVHDR", "CRVPT", "DFLT", "DIOCUR", "DISPFLD",
                   "DISPLAY", "EMUL", "FILTER", "HTRSET", "IEEE", "INCRV", "INNAME", "INTYPE",
                   "LEDS", "LOCK", "MNMXRST", "MODE", "MOUT", "OPSTE", "OUTMODE", "PID",
                   "POLARITY", "RAMP", "RANGE", "RELAY", "SCAL", "SETP", "TLIMIT", "WARMUP", "ZONE")


def test_the_byte_factory_knows_exactly_five_setters_of_the_manuals_list():
    """The verbs build_write knows, from its source and from its behaviour."""
    src = _module_source(hc).split("def build_write", 1)[1].split("\ndef ", 1)[0]
    assert set(re.findall(r'v == "([A-Z]+)"', src)) == {"FLUSH", "SETP", "RAMP", "RANGE", "ZONE",
                                                         "OUTMODE"}
    known = set()
    for verb in ALL_335_SETTERS:
        try:
            hc.build_write(verb, 2, 1)
        except ValueError as exc:
            if "not a temperature-control command" in str(exc):
                continue
        known.add(verb)
    assert known == {"SETP", "RAMP", "RANGE", "ZONE", "OUTMODE"}


def test_the_read_only_driver_builds_queries_only_and_never_a_clearing_one():
    for verb in lsci._STATE_QUERIES:
        assert verb.endswith("?")
        arg = None if verb in ("*IDN?", "EMUL?", "MODE?") else "2,1" if verb == "ZONE?" else "A"
        assert lsci.build_query(verb, arg)
    for bad in ("SETP 2,5", "RANGE 2,3", "RANGE", "HTRST?", "*ESR?", "OPSTR?", "LOCK?",
                "RANGE?;RANGE 2,3", "*RST", "DFLT 99", "ZONE 2,1,25,50,20,0,0,1,0,1"):
        with pytest.raises(lsci.LakeshoreError):
            lsci.build_query(bad)
    with pytest.raises(lsci.LakeshoreError):
        lsci.build_query("RANGE?", "2;RANGE 2,3")
    assert not (lsci.FORBIDDEN_QUERIES & lsci._STATE_QUERIES)
    assert {"HTRST?", "*ESR?", "OPSTR?", "LOCK?"} <= lsci.FORBIDDEN_QUERIES


def test_zone_queries_take_exactly_an_output_and_a_zone():
    assert lsci.build_query("ZONE?", "2,1") == b"ZONE? 2,1\r\n"
    assert lsci.build_query("ZONE?", " 1,10 ") == b"ZONE? 1,10\r\n"
    for bad in (None, "", "2", "A", "2,0", "2,11", "3,1", "0,1", "2, 1", "2,1,3", "2,01",
                "2,1;RANGE 2,3", "2,1\r\nRANGE 2,3", "22,1", "2,1 x", 2):
        with pytest.raises(lsci.LakeshoreError):
            lsci.build_query("ZONE?", bad)
    with pytest.raises(lsci.LakeshoreError):        # a comma is only for ZONE?
        lsci.build_query("RANGE?", "2,1")


def test_the_settings_snapshot_sends_what_it_always_sent():
    """The new read-only queries serve temperature control; the instrument-state panel
    (polled by an experiment session) must not grow by them."""
    mod = _module_source(lsci)
    # the snapshot, and the helper it asks the input name/type through (settings cache)
    src = (mod.split("def read_snapshot", 1)[1].split("\ndef ", 1)[0]
           + mod.split("def _read_channel_settings", 1)[1].split("\ndef ", 1)[0])
    asked = set(re.findall(r'"([A-Z*]+\?)"', src))
    assert asked == {"*IDN?", "INNAME?", "KRDG?", "CRDG?", "SRDG?", "INTYPE?", "RDGST?",
                     "RANGE?", "HTR?", "SETP?", "OUTMODE?", "RAMP?"}


def test_the_read_path_does_not_import_the_write_module():
    import mast.environment as env_pkg

    for py in Path(env_pkg.__file__).parent.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "heater_335" not in node.module, py
            if isinstance(node, ast.Import):
                assert not any("heater_335" in a.name for a in node.names), py


def test_every_transact_in_the_write_module_sends_build_write_bytes():
    tree = ast.parse(_module_source(hc))
    built: set[str] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and getattr(node.value.func, "id", "") == "build_write"):
            built |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "attr", "") == "transact"]
    assert calls, "the write module sends something"
    for c in calls:
        arg = c.args[0]
        assert isinstance(arg, ast.Name) and arg.id in built, ast.unparse(c)


def test_no_other_module_writes_setp_ramp_range_zone_or_outmode():
    import mast

    root = Path(mast.__file__).parent
    pat = re.compile(r"""b?["'](SETP |RAMP |RANGE |ZONE |OUTMODE )\d""")
    offenders = [str(p.relative_to(root)) for p in root.rglob("*.py")
                 if p.name != "heater_335.py" and pat.search(p.read_text(encoding="utf-8"))]
    assert offenders == []


# ── 2. the transport keeps the manual's spacing ───────────────────────────────
class _FakeSer:
    def __init__(self):
        self.timeout = 0.5
        self.is_open = True
        self.write_times: list[float] = []

    def reset_input_buffer(self):
        pass

    def write(self, data):
        self.write_times.append(time.monotonic())

    def flush(self):
        pass

    def read(self, n):
        return b""


def test_serial_transport_spaces_messages_around_a_spaced_caller():
    from mast.environment.serial_transport import SerialSettings, SerialTransport

    t = SerialTransport(SerialSettings(port="COMX"))
    t._ser = _FakeSer()
    t.transact(b"KRDG? A\r\n", read_size=0)                 # the monitor: not spaced
    t.transact(b"RANGE? 2\r\n", read_size=0, min_gap_s=0.08)   # waits after the poll
    t.transact(b"KRDG? B\r\n", read_size=0)                  # the next poll waits too
    w = t._ser.write_times
    assert w[1] - w[0] >= 0.075 and w[2] - w[1] >= 0.075


def test_the_link_asks_the_transport_to_space_every_message(tmp_path):
    sim = Sim335()
    make(sim, tmp_path).plan(10.0, tip_verdict=_parked, **_auth(10.0))
    assert sim.gaps and all(g == lsci.MIN_COMMAND_GAP_S for g in sim.gaps)


def test_the_monitor_and_the_settings_snapshot_ask_for_the_gap_too():
    """The manual's 50 ms (§6.3.5 p. 104) on every read path, not only the link's."""
    sim = Sim335()
    sensor = lsci.LakeshoreTemperatureSensor(name="SPM", channel="A", transport=sim)
    assert sensor.read().status == "ok"
    assert sensor.instrument_state(channels=["A"]) is not None
    assert lsci.identify_model(sim) == "MODEL335"
    assert len(sim.gaps) > 10 and all(g == lsci.MIN_COMMAND_GAP_S for g in sim.gaps)


def test_a_transport_that_does_not_space_gets_no_gap_argument():
    class Plain:                                   # an older fake: no min_gap_s at all
        def transact(self, payload, *, read_until=None, timeout=None):
            return b"+4.2000\r\n"

    assert lsci.LakeshoreTemperatureSensor(name="x", channel="A", transport=Plain()).read().value == 4.2


class _AnsweringSer(_FakeSer):
    def __init__(self):
        super().__init__()
        self._reply = b""

    def write(self, data):
        super().write(data)
        self._reply = b"+4.3420\r\n"

    def read(self, n):
        out, self._reply = self._reply, b""
        return out


def test_back_to_back_monitor_reads_keep_the_manuals_gap():
    """Real transport, real clock: three reads of two sensors in a row (the monitor's
    loop) and one snapshot query are each 60 ms apart on the wire."""
    from mast.environment.serial_transport import SerialSettings, SerialTransport

    t = SerialTransport(SerialSettings(port="COMX"))
    t._ser = _AnsweringSer()
    a = lsci.LakeshoreTemperatureSensor(name="A", channel="A", transport=t)
    b = lsci.LakeshoreTemperatureSensor(name="B", channel="B", transport=t)
    for s in (a, b, a):
        assert s.read().value == pytest.approx(4.342)
    lsci._query(t, "RANGE?", "2")
    w = t._ser.write_times
    assert len(w) == 4
    assert all(later - earlier >= lsci.MIN_COMMAND_GAP_S - 0.005 for earlier, later in zip(w, w[1:]))


# ── 3. gates ──────────────────────────────────────────────────────────────────
def test_a_clean_raise_passes_every_gate_and_writes_nothing(tmp_path):
    sim = Sim335()
    plan = make(sim, tmp_path).plan(10.0, tip_verdict=_parked, **_auth(10.0))
    assert plan.ok, plan.refusal_text()
    assert plan.raising and not plan.noop and sim.writes == []
    assert plan.rate_k_per_min == 1.0 and plan.heater_range == 1
    assert plan.eta_s == pytest.approx((10.0 - 4.342) / 1.0 * 60.0)


def test_not_configured_refuses_before_touching_the_controller(tmp_path):
    sim = Sim335()
    ctl = make(sim, tmp_path, settings=hc.HeaterSettings(output=2, control_input="A"))
    plan = ctl.plan(10.0, tip_verdict=_parked, **_auth(10.0))
    assert not plan.ok and plan.refusals[0].id == "configured"
    assert sim.queries == [] and sim.writes == []
    r = ctl.start(10.0, origin="t", tip_verdict=_parked, **_auth(10.0))
    assert not r["started"] and sim.writes == []


@pytest.mark.parametrize("target,kw,gate", [
    (40.5, {}, "max_setpoint"),
    (0.5, {}, "max_setpoint"),
    (10.0, {"rate_k_per_min": 1.5}, "rate"),
    (10.0, {"rate_k_per_min": 0.04}, "rate"),
    (10.0, {"heater_range": 2}, "heater_range"),
    (10.0, {"heater_range": "high"}, "heater_range"),
    (10.0, {"heater_range": 0}, "heater_range"),
])
def test_bad_requests_are_refused_not_clamped(tmp_path, target, kw, gate):
    sim = Sim335()
    plan = make(sim, tmp_path).plan(target, tip_verdict=_parked, **_auth(41.0), **kw)
    assert gate in {c.id for c in plan.refusals}
    assert sim.writes == []


def test_a_range_by_name_is_accepted(tmp_path):
    plan = make(Sim335(), tmp_path).plan(10.0, heater_range="low", tip_verdict=_parked,
                                         **_auth(10.0))
    assert plan.ok and plan.heater_range == 1


@pytest.mark.parametrize("mutate,gate", [
    (lambda s: setattr(s, "emul", "1,0"), "no_emulation"),
    (lambda s: s.outputs[2].update(htrset="1,1,2,+1.000,1"), "current_output"),
    (lambda s: s.outputs[2].update(outmode="3,1,0"), "closed_loop"),
    (lambda s: s.outputs[2].update(outmode="0,1,0"), "closed_loop"),
    (lambda s: s.outputs[2].update(outmode="2,1,0"), "zone_table"),     # zone mode, no table
    (lambda s: s.outputs[2].update(outmode="1,2,0"), "control_input"),
    (lambda s: s.outputs[2].update(outmode="1,1,1"), "powerup_off"),
    (lambda s: s.outputs[2].update(mout=5.0), "no_manual_output"),
    (lambda s: s.inputs["A"].update(intype="3,0,6,1,3"), "kelvin_units"),
    (lambda s: s.inputs["A"].update(st=1), "reading_valid"),
    (lambda s: s.inputs["A"].update(k=0.0), "reading_valid"),
    (lambda s: s.inputs["A"].update(k=45.0), "present_below_max"),
    (lambda s: s.inputs["A"].update(tlimit=0.0), "instrument_limit"),
    (lambda s: s.inputs["A"].update(tlimit=11.0), "instrument_limit"),
    (lambda s: s.outputs[2].update(ramping=1, ramp_to=20.0), "not_ramping"),
])
def test_controller_state_gates(tmp_path, mutate, gate):
    sim = Sim335()
    mutate(sim)
    plan = make(sim, tmp_path).plan(10.0, tip_verdict=_parked, **_auth(10.0))
    assert gate in {c.id for c in plan.refusals}, plan.refusal_text()
    assert sim.writes == []


def test_output_1_as_found_at_the_lab_is_refused(tmp_path):
    """Open loop at a 500 K setpoint: turning it on would deliver constant power."""
    sim = Sim335()
    s = hc.HeaterSettings(output=1, control_input="A", max_setpoint_k=40.0, max_range=1,
                          max_current_a=1.0, max_rate_k_per_min=1.0)
    plan = make(sim, tmp_path, settings=s).plan(10.0, tip_verdict=_parked, **_auth(10.0))
    assert {"closed_loop", "control_input"} <= {c.id for c in plan.refusals}


def test_an_unanswered_reading_is_not_an_answer(tmp_path):
    sim = Sim335()
    real = sim._answer
    sim._answer = lambda text: "" if text.startswith("TLIMIT?") else real(text)
    plan = make(sim, tmp_path).plan(10.0, tip_verdict=_parked, **_auth(10.0))
    assert "readable" in {c.id for c in plan.refusals}
    assert "temperature_limit_k" in plan.refusal_text()


def test_a_field_change_in_progress_is_refused(tmp_path):
    plan = make(Sim335(), tmp_path, field_busy=True).plan(10.0, tip_verdict=_parked, **_auth(10.0))
    assert "field_steady" in {c.id for c in plan.refusals}


@pytest.mark.parametrize("reading,ok", [
    (SimpleNamespace(value_k=3.9, age_s=2.0, reason=""), True),
    (SimpleNamespace(value_k=4.6, age_s=2.0, reason=""), False),
    (SimpleNamespace(value_k=3.9, age_s=120.0, reason=""), False),
    (SimpleNamespace(value_k=0.0, age_s=2.0, reason=""), False),
    (None, False),
])
def test_the_guard_thermometer_when_configured(tmp_path, reading, ok):
    s = hc.HeaterSettings(output=2, control_input="A", max_setpoint_k=40.0, max_range=1,
                          max_current_a=1.0, max_rate_k_per_min=1.0, guard_channel="Magnet", guard_max_k=4.5)
    plan = make(Sim335(), tmp_path, settings=s, temperature=lambda ch: reading).plan(
        10.0, tip_verdict=_parked, **_auth(10.0))
    assert ("guard_temperature" not in {c.id for c in plan.refusals}) is ok


def test_raising_needs_the_users_words_naming_the_temperature(tmp_path):
    ctl = make(Sim335(), tmp_path)
    assert "user_authorization" in {c.id for c in ctl.plan(10.0, tip_verdict=_parked).refusals}
    example = ctl.plan(10.0, tip_verdict=_parked, user_authorization="可以加热到 20 K",
                       authorized_up_to_k=20.0)
    assert "user_authorization" in {c.id for c in example.refusals}
    vague = ctl.plan(10.0, tip_verdict=_parked, user_authorization="可以加热",
                     authorized_up_to_k=30.0)
    assert "authorization_names_temperature" in {c.id for c in vague.refusals}


def test_temperatures_right_after_chinese_text_are_named():
    """\\w in the look-behind counted the CJK character and hid the number."""
    assert hc.temperatures_named_in("可以加热到20K") == [20.0]
    assert hc.temperatures_named_in("加热到 20 K") == [20.0]
    assert hc.temperatures_named_in("到12.5kelvin") == [12.5]
    assert hc.temperatures_named_in("到 20 K，速率 1 K/min") == [20.0]
    assert hc.temperatures_named_in("1.5K") == [1.5]
    assert hc.temperatures_named_in("20 km") == []


def test_an_authorization_covers_its_temperature_and_no_further(tmp_path):
    ctl = make(Sim335(), tmp_path)
    over = ctl.plan(10.0, tip_verdict=_parked, user_authorization="可以加热到 8 K",
                    authorized_up_to_k=8.0)
    assert "authorized_temperature" in {c.id for c in over.refusals}
    mismatch = ctl.plan(10.0, tip_verdict=_parked, user_authorization="可以加热到12K",
                        authorized_up_to_k=15.0)
    assert "authorization_consistent" in {c.id for c in mismatch.refusals}
    rate_words = ctl.plan(10.0, tip_verdict=_parked,
                          user_authorization="可以加热到 12 K，速率 1 K/min",
                          authorized_up_to_k=12.0)
    assert rate_words.ok, rate_words.refusal_text()


def test_lowering_needs_no_authorization_but_more_power_does(tmp_path):
    sim = Sim335()
    sim.inputs["A"]["k"] = 20.0
    sim.outputs[2].update(range=1, sp=20.0, ramp_to=20.0)
    s = hc.HeaterSettings(output=2, control_input="A", max_setpoint_k=40.0, max_range=3,
                          max_current_a=1.0, max_rate_k_per_min=1.0)
    ctl = make(sim, tmp_path, settings=s)
    plan = ctl.plan(10.0, heater_range=1, tip_verdict=_parked)
    assert plan.ok and plan.raising is False, plan.refusal_text()
    more_power = ctl.plan(10.0, heater_range=3, tip_verdict=_parked)    # more power needs a fresh authorization
    assert more_power.raising and "user_authorization" in {c.id for c in more_power.refusals}


def test_the_tip_must_be_withdrawn_unless_waived(tmp_path):
    ctl = make(Sim335(), tmp_path)
    assert "tip" in {c.id for c in ctl.plan(10.0, **_auth(10.0)).refusals}
    engaged = SimpleNamespace(state="engaged", reason="feedback on")
    assert "tip" in {c.id for c in ctl.plan(10.0, tip_verdict=lambda: engaged,
                                            **_auth(10.0)).refusals}
    assert ctl.plan(10.0, require_tip_withdrawn=False, **_auth(10.0)).ok


def test_the_wrong_instrument_or_another_unit_is_refused(tmp_path):
    sim = Sim335(idn="LSCI,MODEL336,TEST0001/0000000,1.10")
    plan = make(sim, tmp_path).plan(10.0, tip_verdict=_parked, **_auth(10.0))
    assert [c.id for c in plan.refusals] == ["identity"] and sim.closed == 1
    sim2 = Sim335()
    ctl = make(sim2, tmp_path)
    assert ctl.plan(10.0, tip_verdict=_parked, **_auth(10.0)).ok
    sim2.reopen(idn="LSCI,MODEL335,TEST0002/0000000,1.10")       # a different 335
    plan2 = ctl.plan(10.0, tip_verdict=_parked, **_auth(10.0))
    assert [c.id for c in plan2.refusals] == ["identity"] and sim2.writes == []


def test_a_pin_handed_to_a_new_link_is_kept(tmp_path):
    sim = Sim335()
    link = lsci.LakeshoreLink(sim, port="COMX", pinned_serial="TEST0002/0000000")
    with pytest.raises(lsci.WrongController):
        link.query("EMUL?")


# ── 4. the write ──────────────────────────────────────────────────────────────
def test_start_from_off_never_lets_the_parked_setpoint_drive(tmp_path):
    sim = Sim335()
    ctl = make(sim, tmp_path)
    r = ctl.start(10.0, origin="t", tip_verdict=_parked, **_auth(10.0))
    assert r["started"], r.get("error")
    assert sim.cmds() == ["RAMP 2,0,1.0", "SETP 2,4.342", "RAMP 2,1,1.0", "RANGE 2,1",
                          "SETP 2,10.000"]
    # When the heater came on, its setpoint was the measured temperature, not 40 K.
    assert len(sim.range_on) == 1 and sim.range_on[0][1] == pytest.approx(4.342)
    assert ctl.busy and ctl.active["target_k"] == 10.0
    row = _ledger(tmp_path)[-1]
    assert row["action"] == "start" and row["result"] == "accepted"
    assert row["authorization"].startswith("用户") and row["tip_waived"] is False
    assert row["control_input"] == "A" and row["basis_k"] == 10.0


@pytest.mark.parametrize("reports", ["final", "live"])
def test_both_readings_of_setp_are_accepted(tmp_path, reports):
    sim = Sim335()
    sim.setp_reports = reports
    r = make(sim, tmp_path).start(10.0, origin="t", tip_verdict=_parked, **_auth(10.0))
    assert r["started"], r.get("error")


def test_start_with_the_heater_already_on_changes_only_ramp_and_setpoint(tmp_path):
    sim = Sim335()
    sim.inputs["A"]["k"] = 8.0
    sim.outputs[2].update(range=1, sp=8.0, ramp_to=8.0, ramp_on=1, rate=0.5)
    r = make(sim, tmp_path).start(10.0, origin="t", tip_verdict=_parked, **_auth(10.0))
    assert r["started"], r.get("error")
    assert sim.cmds() == ["RAMP 2,1,1.0", "SETP 2,10.000"]


def test_already_there_is_a_noop(tmp_path):
    sim = Sim335()
    sim.inputs["A"]["k"] = 10.0
    sim.outputs[2].update(range=1, sp=10.0, ramp_to=10.0)
    r = make(sim, tmp_path).start(10.0, origin="t", tip_verdict=_parked)
    assert r.get("noop") and sim.writes == []


def test_a_controller_that_ignores_writes_is_reported_and_left_off(tmp_path):
    sim = Sim335()
    sim.accept_writes = False
    r = make(sim, tmp_path).start(10.0, origin="t", tip_verdict=_parked, **_auth(10.0))
    assert not r["started"] and r.get("not_accepted")
    assert sim.cmds() == ["RAMP 2,0,1.0"] and sim.outputs[2]["range"] == 0


def test_an_ignored_range_is_not_followed_by_anything(tmp_path):
    sim = Sim335()
    sim.ignore_indices = {3}                       # RANGE 2,1
    r = make(sim, tmp_path).start(10.0, origin="t", tip_verdict=_parked, **_auth(10.0))
    assert r.get("not_accepted") and sim.outputs[2]["range"] == 0
    assert sim.cmds()[-1] == "RANGE 2,1"


def test_a_heater_turned_on_that_cannot_finish_is_turned_off(tmp_path):
    sim = Sim335()
    sim.ignore_indices = {4}                       # the final SETP 2,10.000
    r = make(sim, tmp_path).start(10.0, origin="t", tip_verdict=_parked, **_auth(10.0))
    assert not r["started"] and r["off"]["confirmed"]
    assert sim.cmds()[-1] == "RANGE 2,0" and sim.outputs[2]["range"] == 0


def test_a_write_that_raises_after_its_bytes_left_turns_the_heater_off(tmp_path):
    sim = Sim335()
    sim.raise_once = "RANGE"
    r = make(sim, tmp_path).start(10.0, origin="t", tip_verdict=_parked, **_auth(10.0))
    assert not r["started"] and r["off"]["confirmed"]
    assert sim.write_texts()[-2:] == ["", "RANGE 2,0"]          # terminator first
    assert sim.outputs[2]["range"] == 0


def test_a_range_raised_on_a_heater_that_was_on_is_put_back(tmp_path):
    """The range went 1 -> 3, the final SETP was ignored."""
    sim = Sim335()
    sim.inputs["A"]["k"] = 8.0
    sim.outputs[2].update(range=1, sp=8.0, ramp_to=8.0, ramp_on=1, rate=0.5)
    sim.ignore_indices = {2}                       # RAMP, RANGE taken; SETP 2,10 ignored
    s = hc.HeaterSettings(output=2, control_input="A", max_setpoint_k=40.0, max_range=3,
                          max_current_a=1.0, max_rate_k_per_min=1.0)
    r = make(sim, tmp_path, settings=s).start(10.0, heater_range=3, origin="t",
                                              tip_verdict=_parked, **_auth(10.0))
    assert not r["started"] and r.get("restored")
    assert sim.cmds()[-1] == "RANGE 2,1" and sim.outputs[2]["range"] == 1


def test_abort_before_the_write_sends_nothing(tmp_path):
    sim = Sim335()
    r = make(sim, tmp_path).start(10.0, origin="t", tip_verdict=_parked,
                                  abort_check=lambda: True, **_auth(10.0))
    assert not r["started"] and sim.writes == []


def test_an_abort_during_the_sequence_stops_it_and_turns_a_new_heater_off(tmp_path):
    """The sequence must look for an abort between steps, not only before step 1."""
    sim = Sim335()
    r = make(sim, tmp_path).start(
        10.0, origin="t", tip_verdict=_parked, **_auth(10.0),
        abort_check=lambda: len([w for w in sim.writes if w.strip()]) >= 4)   # after RANGE
    assert not r["started"] and r["off"]["confirmed"]
    assert "SETP 2,10.000" not in sim.cmds() and sim.outputs[2]["range"] == 0


def test_an_abort_before_the_heater_came_on_leaves_it_off_and_writes_no_more(tmp_path):
    sim = Sim335()
    r = make(sim, tmp_path).start(
        10.0, origin="t", tip_verdict=_parked, **_auth(10.0),
        abort_check=lambda: len(sim.writes) >= 2)
    assert r.get("interrupted") and sim.cmds() == ["RAMP 2,0,1.0", "SETP 2,4.342"]
    assert sim.outputs[2]["range"] == 0


def test_a_heater_off_during_preparation_cancels_the_change(tmp_path):
    sim = Sim335()
    ctl = make(sim, tmp_path)

    def tip_then_off():
        ctl.heater_off(reason="panel", origin="t")
        return PARKED

    r = ctl.start(10.0, origin="t", tip_verdict=tip_then_off, **_auth(10.0))
    assert not r["started"] and sim.cmds() == ["RANGE 2,0"]


@pytest.mark.parametrize("mutate,what", [
    (lambda s: s.outputs[2].update(sp=30.0), "设定值"),          # the suite runs in zh
    (lambda s: s.outputs[2].update(outmode="3,1,0"), "outmode"),
    (lambda s: s.inputs["A"].update(tlimit=0.0), "temperature_limit_k"),
    (lambda s: s.outputs[2].update(mout=10.0), "manual_output_pct"),
])
def test_a_change_between_plan_and_write_is_refused(tmp_path, mutate, what):
    sim = Sim335()
    ctl = make(sim, tmp_path)

    def tip_then_panel():
        mutate(sim)                                # someone at the panel
        return PARKED

    r = ctl.start(10.0, origin="t", tip_verdict=tip_then_panel, **_auth(10.0))
    assert not r["started"] and what in r["error"] and sim.writes == []


def test_a_refused_start_writes_nothing_and_is_ledgered(tmp_path):
    sim = Sim335()
    r = make(sim, tmp_path).start(10.0, origin="t", tip_verdict=_parked)
    assert not r["started"] and sim.writes == []
    assert _ledger(tmp_path)[-1]["action"] == "refused"


def test_shutdown_refuses_new_changes_and_holds_a_running_ramp(tmp_path):
    sim = Sim335()
    ctl = make(sim, tmp_path)
    assert ctl.start(10.0, origin="t", tip_verdict=_parked, **_auth(10.0))["started"]
    n = len(sim.cmds())
    sim.advance(60.0)
    ctl.begin_shutdown()
    tail = sim.cmds()[n:]
    assert tail[0].startswith("SETP 2,") and tail[1] == "RAMP 2,0,1.0"
    assert sim.outputs[2]["range"] == 1           # the heater keeps regulating
    r = ctl.start(12.0, origin="t", tip_verdict=_parked, **_auth(12.0))
    assert not r["started"]


# ── 5. supervision ────────────────────────────────────────────────────────────
def _started(tmp_path, *, reports="final", settings=SETTINGS, temperature=None,
             target=10.0, **sim_kw):
    sim = Sim335()
    sim.setp_reports = reports
    for k, v in sim_kw.items():
        setattr(sim, k, v)
    ctl = make(sim, tmp_path, settings=settings, temperature=temperature)
    r = ctl.start(target, origin="t", tip_verdict=_parked, **_auth(max(target, 10.0)))
    assert r["started"], r.get("error")
    return sim, ctl, len(sim.cmds())


@pytest.mark.parametrize("reports", ["final", "live"])
def test_supervision_ends_settled_and_the_watch_takes_over(tmp_path, reports):
    sim, ctl, n = _started(tmp_path, reports=reports)
    out = ctl.supervise(settle_s=60.0)
    assert out["outcome"] == "done", out
    assert sim.cmds()[n:] == [] and sim.outputs[2]["range"] == 1
    assert ctl.active is None and not ctl.busy and ctl.watching


def test_overtemperature_turns_the_heater_off(tmp_path):
    sim, ctl, n = _started(tmp_path, kelvin_offset=5.0)
    out = ctl.supervise()
    assert out["outcome"] == "overtemperature"
    assert sim.cmds()[n:] == ["RANGE 2,0"] and not ctl.watching


def test_lowering_does_not_trip_the_overtemperature_ceiling(tmp_path):
    """The ceiling must not be the target plus the allowance when that lies below the start."""
    sim = Sim335()
    sim.inputs["A"]["k"] = 20.0
    sim.outputs[2].update(range=1, sp=20.0, ramp_to=20.0, ramp_on=1, rate=1.0)
    ctl = make(sim, tmp_path)
    assert ctl.start(10.0, origin="t", tip_verdict=_parked)["started"]
    n = len(sim.cmds())
    out = ctl.supervise()
    assert out["outcome"] == "done" and sim.cmds()[n:] == []


def test_a_control_sensor_fault_turns_the_heater_off(tmp_path):
    sim, ctl, n = _started(tmp_path)
    sim.events.append((100.0, lambda s: s.inputs["A"].update(st=32)))
    out = ctl.supervise()
    assert out["outcome"] == "sensor_fault" and sim.cmds()[n:] == ["RANGE 2,0"]


def test_a_saturated_heater_that_never_arrives_is_turned_off(tmp_path):
    """A control sensor that does not see the heater defeats TLIMIT too."""
    sim, ctl, n = _started(tmp_path, follow=False, htr_pct=100.0)
    out = ctl.supervise()
    assert out["outcome"] == "saturated" and sim.cmds()[n:] == ["RANGE 2,0"]


def test_the_instrument_turning_itself_off_is_followed_by_nothing(tmp_path):
    sim, ctl, n = _started(tmp_path)
    sim.events.append((100.0, lambda s: s.outputs[2].update(range=0)))   # TLIMIT trip
    out = ctl.supervise()
    assert out["outcome"] == "turned_off_elsewhere" and sim.cmds()[n:] == []
    assert not ctl.watching


def test_an_external_setpoint_change_is_not_fought_but_watched(tmp_path):
    sim, ctl, n = _started(tmp_path)
    sim.events.append((100.0, lambda s: s.outputs[2].update(sp=25.0, ramp_to=25.0, ramping=0)))
    out = ctl.supervise()
    assert out["outcome"] == "external_change" and sim.cmds()[n:] == []
    assert ctl.watching and ctl.status()["watch"]["basis_k"] is None


def test_abort_mid_ramp_holds_without_turning_the_heater_off(tmp_path):
    sim, ctl, n = _started(tmp_path)
    out = ctl.supervise(abort_check=lambda: sim.now > 120.0)
    assert out["outcome"] == "aborted"
    tail = sim.cmds()[n:]
    assert len(tail) == 2 and tail[0].startswith("SETP 2,") and tail[1] == "RAMP 2,0,1.0"
    assert sim.outputs[2]["range"] == 1 and sim.outputs[2]["ramping"] == 0
    assert ctl.watching


def test_the_guard_thermometer_turns_the_heater_off(tmp_path):
    s = hc.HeaterSettings(output=2, control_input="A", max_setpoint_k=40.0, max_range=1,
                          max_current_a=1.0, max_rate_k_per_min=1.0, guard_channel="Magnet", guard_max_k=4.5)
    box = {"k": 3.9}
    sim, ctl, n = _started(tmp_path, settings=s, temperature=lambda ch: SimpleNamespace(
        value_k=box["k"], age_s=1.0, reason=""))
    sim.events.append((100.0, lambda _s: box.update(k=4.8)))
    out = ctl.supervise()
    assert out["outcome"] == "guard" and sim.cmds()[n:] == ["RANGE 2,0"]


def test_lost_contact_turns_the_heater_off(tmp_path):
    sim, ctl, n = _started(tmp_path)
    sim.events.append((100.0, lambda s: setattr(s, "fail_reads", 10_000)))
    out = ctl.supervise()
    assert out["outcome"] in ("lost_contact", "off_failed")
    assert "RANGE 2,0" in sim.cmds()[n:]        # the mode does not answer: RAMP 0 goes first
    assert sim.outputs[2]["range"] == 0


def test_an_off_that_does_not_read_back_is_not_reported_as_done(tmp_path):
    """The controller ignored the RANGE 0 and supervision must not say 'off'."""
    sim, ctl, n = _started(tmp_path, kelvin_offset=5.0)
    sim.ignore_verbs = {"RANGE"}
    out = ctl.supervise()
    assert out["outcome"] == "off_failed" and sim.outputs[2]["range"] == 1
    tail = sim.write_texts()[n:]
    assert tail.count("RANGE 2,0") > 2 and "" in tail          # resent, terminator first
    assert ctl.retrying and 2 in ctl.status()["retrying_off"]
    sim.ignore_verbs = set()                                   # the panel back in remote
    ctl.background_step()
    assert sim.outputs[2]["range"] == 0 and not ctl.retrying


def test_an_off_for_another_output_does_not_end_supervision(tmp_path):
    """HeaterOff(output=1) must not end the supervision of output 2."""
    sim, ctl, n = _started(tmp_path)
    sim.events.append((100.0, lambda s: ctl.heater_off(reason="skill", origin="t", outputs=(1,))))
    out = ctl.supervise()
    assert out["outcome"] == "done" and sim.outputs[2]["range"] == 1


def test_a_hold_that_did_not_hold_does_not_end_supervision(tmp_path):
    """A failed hold must not be reported as 'held'."""
    sim, ctl, n = _started(tmp_path)

    def failing_hold(s):
        s.raise_always = {"SETP"}
        ctl.hold(reason="skill", origin="t")
        s.raise_always = set()

    sim.events.append((100.0, failing_hold))
    out = ctl.supervise()
    assert ctl._last_hold["held"] is False
    assert out["outcome"] == "done"


def test_a_heater_off_from_elsewhere_ends_supervision(tmp_path):
    sim, ctl, n = _started(tmp_path)
    sim.events.append((100.0, lambda s: ctl.heater_off(reason="skill", origin="HeaterOff")))
    out = ctl.supervise()
    assert out["outcome"] == "heater_off" and sim.cmds()[n:] == ["RANGE 2,0"]


def test_a_hold_from_elsewhere_ends_supervision(tmp_path):
    sim, ctl, n = _started(tmp_path)
    sim.events.append((100.0, lambda s: ctl.hold(reason="skill", origin="Hold")))
    out = ctl.supervise()
    assert out["outcome"] == "held" and sim.outputs[2]["range"] == 1 and ctl.watching


# ── 6. the watch ──────────────────────────────────────────────────────────────
def _watched(tmp_path, **kw):
    sim, ctl, n = _started(tmp_path, **kw)
    assert ctl.supervise()["outcome"] == "done" and ctl.watching
    return sim, ctl, len(sim.cmds())


def test_the_watch_turns_an_overheating_stage_off(tmp_path):
    sim, ctl, n = _watched(tmp_path)
    sim.follow = False
    sim.inputs["A"]["k"] = 13.0                    # ceiling: 10 + 2
    assert ctl.background_step()
    assert sim.cmds()[n:] == ["RANGE 2,0"] and not ctl.watching


def test_the_watch_turns_a_saturated_heater_off(tmp_path):
    sim, ctl, n = _watched(tmp_path)
    sim.follow = False
    sim.inputs["A"]["k"] = 9.0
    sim.htr_pct = 100.0
    ctl.background_step()
    assert sim.cmds()[n:] == []                    # not yet
    sim.advance(hc.SATURATION_S + 1)
    ctl.background_step()
    assert sim.cmds()[n:] == ["RANGE 2,0"]


def test_the_watch_ends_quietly_when_the_heater_is_off(tmp_path):
    sim, ctl, n = _watched(tmp_path)
    sim.outputs[2]["range"] = 0                    # ALL OFF at the panel
    assert ctl.background_step() is False
    assert sim.cmds()[n:] == [] and not ctl.watching


def test_the_watch_writes_nothing_else(tmp_path):
    sim, ctl, n = _watched(tmp_path)
    sim.outputs[2].update(sp=11.5, ramp_to=11.5)   # the operator nudges the setpoint
    for _ in range(5):
        sim.advance(hc.WATCH_POLL_S)
        ctl.background_step()
    assert sim.cmds()[n:] == [] and ctl.watching


def test_a_restarted_mast_watches_the_heating_again(tmp_path):
    sim, ctl, n = _watched(tmp_path)
    again = make(sim, tmp_path)                    # same ledger, a new process
    assert again.watching and again.status()["watch"]["output"] == 2
    again.heater_off(reason="t", origin="t")
    third = make(sim, tmp_path)
    assert not third.watching


def test_a_restart_mid_change_watches_too(tmp_path):
    sim = Sim335()
    ctl = make(sim, tmp_path)
    assert ctl.start(10.0, origin="t", tip_verdict=_parked, **_auth(10.0))["started"]
    again = make(sim, tmp_path)                    # MAST died during the ramp
    assert again.watching and again.status()["watch"]["basis_k"] == 10.0


# ── 7. hold and heater off ────────────────────────────────────────────────────
def test_hold_writes_nothing_when_nothing_ramps(tmp_path):
    sim = Sim335()
    res = make(sim, tmp_path).hold(reason="t", origin="t")
    assert res["nothing_to_hold"] and sim.writes == []


def test_hold_sets_the_present_temperature_before_ramping_goes_off(tmp_path):
    sim, ctl, n = _started(tmp_path)
    sim.advance(120.0)
    res = ctl.hold(reason="t", origin="t")
    tail = sim.cmds()[n:]
    assert res["held"] and tail[0] == f"SETP 2,{sim.inputs['A']['k']:.3f}" and tail[1] == "RAMP 2,0,1.0"


def test_hold_reads_the_input_the_output_is_controlled_by(tmp_path):
    """The temperature must come from the input the output is controlled by, checked."""
    sim, ctl, n = _started(tmp_path)
    sim.advance(120.0)
    sim.inputs["A"]["intype"] = "3,0,6,1,3"        # A now reads in sensor units
    res = ctl.hold(reason="t", origin="t")
    assert res["held"] and sim.cmds()[n:] == ["RAMP 2,0,1.0"]     # ramp off only


def test_heater_off_covers_the_configured_and_the_active_output(tmp_path):
    sim = Sim335()
    ctl = make(sim, tmp_path)
    res = ctl.heater_off(reason="t", origin="t")
    assert res["confirmed"] and sim.write_texts() == ["", "RANGE 2,0"]


def test_heater_off_where_control_is_not_set_up_writes_nothing(tmp_path):
    """An unconfigured station must not be written to, by HeaterOff either."""
    sim = Sim335()
    res = make(sim, tmp_path, settings=UNSET).heater_off(reason="t", origin="t")
    assert res["refused"] and sim.writes == []


def test_heater_off_never_raises(tmp_path):
    sim = Sim335(idn="LSCI,MODEL336,TEST0001/0000000,1.10")
    res = make(sim, tmp_path).heater_off(reason="t", origin="t")
    assert res["sent"] is False and res["error"] and sim.writes == []


def test_a_readback_that_times_out_is_not_called_ignored(tmp_path):
    """A SETP that landed must not be reported as not accepted when SETP? timed out."""
    sim = Sim335()
    ctl = make(sim, tmp_path)
    with ctl.link.session() as gen:
        sim.fail_reads = 1
        result, back = ctl._step(gen, 2, "SETP", 4.342)
    assert result == "unexpected" and back is None


# ── 7b. the maximum setpoint is the stage's absolute maximum ──────────────────
@pytest.mark.parametrize("tlimit,target,ok", [     # TLIMIT? answers with one decimal here
    (40.0, 10.0, True),            # at the maximum setpoint
    (12.0, 10.0, True),            # exactly target + allowance
    (11.9, 10.0, False),           # below target + allowance
    (40.1, 10.0, False),           # above the maximum setpoint: the cut above the stage
    (60.0, 10.0, False),           # what the tests used to take
    (0.0, 10.0, False),            # off
    (40.0, 39.0, True),            # near the maximum the need is capped at it
    (39.9, 39.0, False),
])
def test_the_instrument_limit_window(tmp_path, tlimit, target, ok):
    sim = Sim335()
    sim.inputs["A"]["tlimit"] = tlimit
    plan = make(sim, tmp_path).plan(target, tip_verdict=_parked, **_auth(target))
    assert ("instrument_limit" not in {c.id for c in plan.refusals}) is ok, plan.refusal_text()
    assert sim.writes == []


def test_the_lab_stage_heats_to_420_k_with_tlimit_at_420_k(tmp_path):
    """A 420 K stage maximum with TLIMIT 420 K. The old rule wanted
    TLIMIT >= target + 10 %, which refused every target above ~382 K and pushed the
    hardware cut above the stage's maximum."""
    s = hc.HeaterSettings(output=2, control_input="A", max_setpoint_k=420.0, max_range=1,
                          max_current_a=1.0, max_rate_k_per_min=1.0)
    sim = Sim335()
    sim.inputs["A"]["tlimit"] = 420.0
    ctl = make(sim, tmp_path, settings=s)
    for target in (300.0, 400.0, 420.0):
        plan = ctl.plan(target, tip_verdict=_parked, **_auth(target))
        assert plan.ok, (target, plan.refusal_text())
    sim.inputs["A"]["tlimit"] = 430.0
    plan = ctl.plan(400.0, tip_verdict=_parked, **_auth(400.0))
    assert [c.id for c in plan.refusals] == ["instrument_limit"]
    assert "绝对上限" in plan.refusal_text()


def test_the_overshoot_ceiling_never_exceeds_the_maximum_setpoint(tmp_path):
    ctl = make(Sim335(), tmp_path)
    assert ctl._ceiling(10.0, SETTINGS) == pytest.approx(12.0)
    assert ctl._ceiling(39.0, SETTINGS) == pytest.approx(40.0)        # 42.9 capped
    assert ctl._ceiling(None, SETTINGS) == pytest.approx(40.0)        # after a takeover


def test_overshoot_above_the_stage_maximum_turns_the_heater_off(tmp_path):
    """Target at the maximum, 0.3 K of PID overshoot: off (it used to be allowed +2 K)."""
    sim, ctl, n = _started(tmp_path, target=40.0, kelvin_offset=0.3)
    out = ctl.supervise()
    assert out["outcome"] == "overtemperature", out
    assert sim.cmds()[n:] == ["RANGE 2,0"]
    assert out["last"]["kelvin"] <= 40.0 + 0.3 + 1e-6


def test_after_a_takeover_the_watch_keeps_the_maximum_itself(tmp_path):
    sim, ctl, n = _started(tmp_path)
    sim.events.append((100.0, lambda s: s.outputs[2].update(sp=39.0, ramp_to=39.0, ramping=0)))
    assert ctl.supervise()["outcome"] == "external_change" and ctl.watching
    sim.follow = False
    sim.inputs["A"]["k"] = 40.5                    # under the old cap (42 K), over the stage
    ctl.background_step()
    assert sim.cmds()[n:] == ["RANGE 2,0"] and not ctl.watching


# ── 7c. a heater range above the maximum is a hard limit ──────────────────────
def test_a_range_above_the_maximum_turns_the_heater_off_during_supervision(tmp_path):
    """It used to end supervision as a takeover, leaving the higher range on."""
    sim, ctl, n = _started(tmp_path)               # max_range 1
    sim.events.append((100.0, lambda s: s.outputs[2].update(range=2)))
    out = ctl.supervise()
    assert out["outcome"] == "range_over_max", out
    assert sim.cmds()[n:] == ["RANGE 2,0"] and sim.outputs[2]["range"] == 0


def test_the_watch_turns_a_range_above_the_maximum_off(tmp_path):
    sim, ctl, n = _watched(tmp_path)
    sim.outputs[2]["range"] = 3
    ctl.background_step()
    assert sim.cmds()[n:] == ["RANGE 2,0"] and not ctl.watching


# ── 7d. zone tables: the pure checks ─────────────────────────────────────────
ZSET = hc.HeaterSettings(output=2, control_input="A", max_setpoint_k=40.0, max_range=2,
                         max_current_a=1.0, max_rate_k_per_min=1.0)
#: Zone 1 to 6 K (low, 0.5 K/min), zone 2 to 15 K (medium, 1 K/min), zone 3 to 40 K
#: (medium, 1 K/min, input A: the configured one, i.e. no switch). Zones 4-10 unused.
TABLE = [{"ub": 6.0, "p": 50.0, "i": 20.0, "range": 1, "rate": 0.5},
         {"ub": 15.0, "p": 60.0, "i": 25.0, "range": 2, "rate": 1.0},
         {"ub": 40.0, "p": 70.0, "i": 30.0, "range": 2, "input": 1, "rate": 1.0}]


def _zones(rows=TABLE) -> "list[hc.Zone]":
    out = [hc.Zone(r["ub"], r.get("p", 50.0), r.get("i", 20.0), r.get("d", 0.0),
                   r.get("mout", 0.0), r["range"], r.get("input", 0), r["rate"]) for r in rows]
    return out + [hc.UNUSED_ZONE] * (10 - len(out))


def _verdict(zones, **over):
    kw = dict(control_input="A", max_range=2, max_rate_k_per_min=1.0, max_setpoint_k=40.0)
    kw.update(over)
    return hc.validate_zone_table(zones, **kw)


def _zone_json(rows=TABLE) -> str:
    return json.dumps([{"upper_bound_k": r["ub"], "p": r.get("p", 50.0), "i": r.get("i", 20.0),
                        "d": r.get("d", 0.0), "range": r["range"], "rate_k_per_min": r["rate"],
                        **({"input": r["input"]} if "input" in r else {})} for r in rows])


def _zone_sim(**kw) -> Sim335:
    """Zone mode on output 2 with TABLE; the setpoint parked at 40 K (zone 3), heater off."""
    sim = Sim335()
    for k, v in kw.items():
        setattr(sim, k, v)
    sim.set_zones(2, TABLE, mode="2,1,0")
    return sim


def test_a_good_zone_table_passes():
    v = _verdict(_zones())
    assert v.ok and v.used == [1, 2, 3] and v.covers_k == 40.0 and v.problems == []
    unused_odd = _zones()[:3] + [hc.Zone(0.0, 0.0, 0.0, 500.0, 50.0, 3, 2, 0.0)] + [hc.UNUSED_ZONE] * 6
    assert _verdict(unused_odd).ok            # an unused zone (upper bound 0 K) is never applied


@pytest.mark.parametrize("row,change", [
    (0, {"p": 0.05}), (0, {"p": 1000.5}), (1, {"i": 0.0}), (1, {"i": 1001.0}), (1, {"d": -0.5}),
    (1, {"d": 200.5}), (1, {"mout": 0.01}), (2, {"range": 3}), (0, {"input": 2}),
    (1, {"rate": 1.5}), (1, {"rate": 0.05}),
])
def test_each_zone_rule_refuses_and_names_the_zone(row, change):
    rows = [dict(r) for r in TABLE]
    rows[row].update(change)
    v = _verdict(_zones(rows))
    assert not v.ok and any(f"第 {row + 1} 区" in p for p in v.problems), v.problems


@pytest.mark.parametrize("zones,word", [
    ([hc.UNUSED_ZONE] * 10, "空的"),
    (_zones([dict(TABLE[0]), dict(TABLE[1]), dict(TABLE[2], ub=12.0)]), "第 3 区的上界 12"),
    (_zones(TABLE)[:3] + [hc.UNUSED_ZONE, hc.Zone(50.0, 50, 20, 0, 0, 1, 0, 0.5)] + [hc.UNUSED_ZONE] * 5,
     "第 5 区的上界 50"),
    (_zones(TABLE)[:9], "应为 10 区"),
    (_zones(TABLE)[:4] + [None] + _zones(TABLE)[5:], "没读到"),
])
def test_tables_that_do_not_ascend_or_are_unknown_are_refused(zones, word):
    v = _verdict(zones)
    assert not v.ok and any(word in p for p in v.problems), v.problems


def test_the_last_zone_must_reach_the_maximum_setpoint():
    assert _verdict(_zones(), max_setpoint_k=40.0).ok
    v = _verdict(_zones(), max_setpoint_k=420.0)
    assert not v.ok and any("最高设定温度 420" in p for p in v.problems)


def test_zone_tables_from_what_a_caller_writes():
    z = hc.zone_table_from(json.dumps([
        {"upper_bound_k": 6, "p": 50, "i": 20, "d": 0, "range": "low", "rate_k_per_min": 0.5,
         "input": "default"},
        {"ub": 40, "P": 60, "I": 25, "D": 0, "heater_range": 2, "rate": 1, "input": "A",
         "mout": 0}]))
    assert len(z) == 10 and z[0].heater_range == 1 and z[0].input_code == 0
    assert z[1] == hc.Zone(40.0, 60.0, 25.0, 0.0, 0.0, 2, 1, 1.0)
    assert z[2:] == [hc.UNUSED_ZONE] * 8
    assert hc.zone_table_from({"zones": [z[0]]})[0] == z[0]
    good = {"upper_bound_k": 6, "p": 50, "i": 20, "d": 0, "range": 1, "rate_k_per_min": 0.5}
    for bad in ("not json", "[]", "{}", json.dumps([{}]), json.dumps([dict(good, mout_pct=1)]),
                json.dumps([dict(good, rnage=1)]), json.dumps([good] * 11),
                json.dumps([dict(good, range="max")]), json.dumps([dict(good, input="C")]),
                json.dumps([dict(good, p="x")]), json.dumps([dict(good, p=True)]),
                json.dumps([dict(good, ub=6)]), json.dumps([{k: v for k, v in good.items() if k != "d"}]),
                json.dumps(["zone"]), 42):
        with pytest.raises(hc.HeaterControlError):
            hc.zone_table_from(bad)


def test_the_zones_on_the_way_include_both_neighbours_at_a_boundary():
    zs, used = _zones(), [1, 2, 3]
    assert hc.zone_of(4.342, zs, used) == 1 and hc.zone_of(6.0, zs, used) == 1
    assert hc.zone_of(6.0005, zs, used) == 2 and hc.zone_of(40.5, zs, used) is None
    assert hc.zones_on_path(4.342, 10.0, zs, used) == [1, 2]
    assert hc.zones_on_path(10.0, 4.342, zs, used) == [1, 2]
    assert hc.zones_on_path(6.005, 10.0, zs, used) == [1, 2]       # within 0.01 K of zone 1
    assert hc.zones_on_path(6.02, 10.0, zs, used) == [2]
    assert hc.zones_on_path(4.0, 40.0, zs, used) == [1, 2, 3]
    assert hc.zones_on_path(4.0, 40.5, zs, used) is None           # above the table
    assert hc.zone_eta_s(4.342, 10.0, zs, used, 0.5) == pytest.approx(
        (6.0 - 4.342) / 0.5 * 60.0 + (10.0 - 6.0) / 1.0 * 60.0)


# ── 7e. heating in zone mode ──────────────────────────────────────────────────
def test_zone_mode_plan_takes_range_and_rate_from_the_zones_on_the_way(tmp_path):
    sim = _zone_sim()
    plan = make(sim, tmp_path, settings=ZSET).plan(10.0, heater_range="medium",
                                                   tip_verdict=_parked, **_auth(10.0))
    assert plan.ok, plan.refusal_text()
    assert plan.control_mode == 2 and plan.zone_path == [1, 2] and plan.zone_ranges == [1, 2]
    assert plan.heater_range == 1 and plan.requested_range == 2      # the start zone's
    assert plan.rate_k_per_min == 0.5                                # min(1.0, zone 1's 0.5)
    assert "不用" in next(c.detail for c in plan.checks if c.id == "zone_range")
    assert plan.eta_s == pytest.approx((6.0 - 4.342) / 0.5 * 60.0 + 4.0 * 60.0)
    d = plan.as_dict()
    assert d["zone_table"]["ok"] and d["instrument"]["zones"][1]["upper_bound_k"] == 15.0
    assert json.dumps(d)                                             # a skill result
    assert sim.writes == []


@pytest.mark.parametrize("change,gate", [
    (lambda rows: rows[1].update(range=0), "zone_path"),      # the table would turn it off
    (lambda rows: rows[1].update(range=3), "zone_table"),     # above the maximum range
    (lambda rows: rows[2].update(ub=30.0), "zone_table"),     # ends below the maximum
    (lambda rows: rows[0].update(input=2), "zone_table"),     # switches to input B
    (lambda rows: rows[1].update(rate=2.0), "zone_table"),    # faster than the maximum
    (lambda rows: rows[0].update(mout=5.0), "zone_table"),    # a manual output
])
def test_zone_mode_refusals(tmp_path, change, gate):
    rows = [dict(r) for r in TABLE]
    change(rows)
    sim = Sim335()
    sim.set_zones(2, rows, mode="2,1,0")
    plan = make(sim, tmp_path, settings=ZSET).plan(10.0, tip_verdict=_parked, **_auth(10.0))
    assert gate in {c.id for c in plan.refusals}, plan.refusal_text()
    assert sim.writes == []


def test_zone_mode_more_power_on_the_way_is_raising(tmp_path):
    """Cooling 20 -> 12 K with the heater on at low: zones 2-3 load medium on the way."""
    sim = _zone_sim()
    sim.inputs["A"]["k"] = 20.0
    sim.outputs[2].update(range=1, sp=20.0, ramp_to=20.0, zone=3)   # low, set by hand in zone 3
    plan = make(sim, tmp_path, settings=ZSET).plan(12.0, tip_verdict=_parked)
    assert plan.raising and "user_authorization" in {c.id for c in plan.refusals}


def test_zone_mode_a_higher_range_on_the_way_is_raising_even_when_cooling(tmp_path):
    """Zone 1 (to 6 K) is medium, zones 2-3 low: cooling 20 -> 3 K with the heater on at low
    passes into the medium zone, though the target is below the present temperature."""
    rows = [dict(TABLE[0], range=2), dict(TABLE[1], range=1), dict(TABLE[2], range=1)]
    sim = Sim335()
    sim.set_zones(2, rows, mode="2,1,0")
    sim.inputs["A"]["k"] = 20.0
    sim.outputs[2].update(range=1, sp=20.0, ramp_to=20.0, zone=3)
    plan = make(sim, tmp_path, settings=ZSET).plan(3.0, tip_verdict=_parked)
    assert plan.raising and "user_authorization" in {c.id for c in plan.refusals}


def test_zone_mode_start_when_the_setpoint_jump_loads_the_start_zone(tmp_path):
    """The parked setpoint (40 K) is in zone 3. SETP T now crosses into zone 1 and the 335
    loads its range: the heater comes on at step 2, regulating at T now, and step 4 is
    not sent."""
    sim = _zone_sim()
    r = make(sim, tmp_path, settings=ZSET).start(10.0, origin="t", tip_verdict=_parked,
                                                **_auth(10.0))
    assert r["started"], r.get("error")
    assert sim.cmds() == ["RAMP 2,0,0.5", "SETP 2,4.342", "RAMP 2,1,0.5", "SETP 2,10.000"]
    assert sim.zone_loads[0][:2] == (2, 1)
    assert sim.range_on[0][1] == pytest.approx(4.342)                # on at T now, not 40 K
    row = _ledger(tmp_path)[-1]
    assert [st["cmd"] for st in row["steps"]] == ["RAMP", "SETP", "ZONE_LOADED", "RAMP", "RANGE", "SETP"]
    assert row["steps"][4].get("skipped") is True and row["control_mode"] == 2
    assert row["zone_path"] == [1, 2]


def test_zone_mode_start_without_a_crossing_sets_the_start_zones_range(tmp_path):
    sim = Sim335()
    sim.outputs[2].update(sp=5.0, ramp_to=5.0)               # parked in zone 1 already
    sim.set_zones(2, TABLE, mode="2,1,0")
    r = make(sim, tmp_path, settings=ZSET).start(10.0, origin="t", tip_verdict=_parked,
                                                **_auth(10.0))
    assert r["started"], r.get("error")
    assert sim.cmds() == ["RAMP 2,0,0.5", "SETP 2,4.342", "RAMP 2,1,0.5", "RANGE 2,1",
                          "SETP 2,10.000"]
    assert sim.zone_loads == []
    assert len(sim.range_on) == 1 and sim.range_on[0][1] == pytest.approx(4.342)


def test_zone_mode_start_turns_the_heater_off_when_step_2_loads_an_unexpected_range(tmp_path):
    sim = _zone_sim()
    real = sim._enter_zone

    def odd(n, k):                     # the 335 picks something MAST did not expect
        real(n, k)
        sim.outputs[n]["range"] = 3

    sim._enter_zone = odd
    r = make(sim, tmp_path, settings=ZSET).start(10.0, origin="t", tip_verdict=_parked,
                                                **_auth(10.0))
    assert not r["started"] and r["off"]["confirmed"]
    assert sim.cmds() == ["RAMP 2,0,0.5", "SETP 2,4.342", "RANGE 2,0"]
    assert sim.outputs[2]["range"] == 0


def test_zone_mode_start_off_when_a_later_step_is_ignored_after_the_zone_turned_it_on(tmp_path):
    """Step 2 loaded the start zone and its range (the heater is on because of this
    change); step 3, ramping on, is then ignored. The heater is turned off, not left as
    'found'."""
    sim = _zone_sim()
    sim.ignore_indices = {2}                       # RAMP 2,1,0.5
    r = make(sim, tmp_path, settings=ZSET).start(10.0, origin="t", tip_verdict=_parked,
                                                **_auth(10.0))
    assert not r["started"] and r["off"]["confirmed"], r
    assert sim.cmds() == ["RAMP 2,0,0.5", "SETP 2,4.342", "RAMP 2,1,0.5", "RANGE 2,0"]
    assert sim.outputs[2]["range"] == 0


def test_zone_mode_start_refuses_when_the_way_changed_since_the_plan(tmp_path):
    sim = _zone_sim()

    def tip_then_warmer():
        sim.inputs["A"]["k"] = 7.0                 # now in zone 2: the way is [2], not [1, 2]
        return PARKED

    r = make(sim, tmp_path, settings=ZSET).start(10.0, origin="t", tip_verdict=tip_then_warmer,
                                                **_auth(10.0))
    assert not r["started"] and "分区路径变了" in r["error"] and sim.writes == []


def test_zone_mode_start_refuses_when_the_table_changed_since_the_plan(tmp_path):
    sim = _zone_sim()

    def tip_then_panel():
        sim.zones[2][2]["p"] = 99.0                # someone edits zone 3 at the panel
        return PARKED

    r = make(sim, tmp_path, settings=ZSET).start(10.0, origin="t", tip_verdict=tip_then_panel,
                                                **_auth(10.0))
    assert not r["started"] and "分区表变了" in r["error"] and sim.writes == []


def _zone_started(tmp_path, **kw):
    sim = _zone_sim(**kw)
    ctl = make(sim, tmp_path, settings=ZSET)
    assert ctl.start(10.0, origin="t", tip_verdict=_parked, **_auth(10.0))["started"]
    return sim, ctl, len(sim.cmds())


@pytest.mark.parametrize("reports", ["final", "live"])
def test_zone_mode_supervision_follows_the_zones_and_settles(tmp_path, reports):
    sim, ctl, n = _zone_started(tmp_path, setp_reports=reports)
    out = ctl.supervise()
    assert out["outcome"] == "done", out
    assert sim.cmds()[n:] == []
    assert [z[1] for z in sim.zone_loads] == [1, 2]                  # zone 2 loaded on the way
    assert sim.outputs[2]["range"] == 2 and sim.outputs[2]["rate"] == 1.0
    assert ctl.watching


def test_zone_mode_a_range_above_the_maximum_turns_the_heater_off(tmp_path):
    sim, ctl, n = _zone_started(tmp_path)
    sim.events.append((100.0, lambda s: s.outputs[2].update(range=3)))
    out = ctl.supervise()
    assert out["outcome"] == "range_over_max"
    assert sim.cmds()[n:] == ["RAMP 2,0,0.5", "RANGE 2,0"]          # the ramp stopped, then off
    sim.advance(1200.0)                            # long past the 6 K boundary
    assert sim.outputs[2]["range"] == 0 and sim.outputs[2]["ramping"] == 0


# ── 7e'. zone mode: an off must also stop a ramping setpoint (p. 69, p. 63) ───
def test_the_simulator_models_the_hazard_a_manual_off_is_undone_at_the_next_zone(tmp_path):
    """Without MAST reacting: RANGE 0 by hand mid-ramp, and the next crossing turns the
    heater back on with that zone's range. This is what the off and the watch prevent."""
    sim, ctl, n = _zone_started(tmp_path)
    sim.advance(60.0)                              # zone 1, ramping towards 10 K
    sim._apply("RANGE 2,0")                        # ALL OFF at the panel
    assert sim.outputs[2]["range"] == 0 and sim.outputs[2]["ramping"] == 1
    sim.advance(400.0)                             # the setpoint crosses 6 K
    assert sim.outputs[2]["range"] == 2 and sim.zone_loads[-1][1] == 2


def test_mast_turning_the_heater_off_mid_ramp_also_stops_the_ramp(tmp_path):
    """(a) HeaterOff during a zone-mode ramp: RAMP 0 at the rate it had, then RANGE 0;
    confirmed only when RANGE? reads 0 and RAMPST? reads 0. No crossing turns it on again."""
    sim, ctl, n = _zone_started(tmp_path)
    box = {}
    sim.events.append((100.0, lambda s: box.update(
        off=ctl.heater_off(reason="skill", origin="HeaterOff"))))
    out = ctl.supervise()
    assert out["outcome"] == "heater_off", out
    assert box["off"]["confirmed"] and box["off"]["zone_ramp"][2]["sent"] == "taken"
    assert sim.cmds()[n:] == ["RAMP 2,0,0.5", "RANGE 2,0"]
    sim.advance(1200.0)
    assert sim.outputs[2]["range"] == 0 and [z[1] for z in sim.zone_loads] == [1]
    assert not ctl.watching and not ctl.retrying


def test_a_hard_condition_mid_ramp_in_zone_mode_stops_the_ramp_too(tmp_path):
    s = hc.HeaterSettings(output=2, control_input="A", max_setpoint_k=40.0, max_range=2,
                          max_current_a=1.0, max_rate_k_per_min=1.0, guard_channel="Magnet", guard_max_k=4.5)
    box = {"k": 3.9}
    sim = _zone_sim()
    ctl = make(sim, tmp_path, settings=s, temperature=lambda ch: SimpleNamespace(
        value_k=box["k"], age_s=1.0, reason=""))
    assert ctl.start(10.0, origin="t", tip_verdict=_parked, **_auth(10.0))["started"]
    n = len(sim.cmds())
    sim.events.append((100.0, lambda _s: box.update(k=4.8)))
    out = ctl.supervise()
    assert out["outcome"] == "guard" and sim.cmds()[n:] == ["RAMP 2,0,0.5", "RANGE 2,0"]
    sim.advance(1200.0)
    assert sim.outputs[2]["range"] == 0


def test_in_closed_loop_pid_a_heater_off_writes_exactly_what_it_always_did(tmp_path):
    sim, ctl, n = _started(tmp_path)               # closed-loop PID, ramping towards 10 K
    sim.advance(60.0)
    w = len(sim.write_texts())
    res = ctl.heater_off(reason="t", origin="t")
    assert res["confirmed"] and res["zone_ramp"][2]["zone_mode"] is False
    assert sim.write_texts()[w:] == ["", "RANGE 2,0"]
    assert sim.outputs[2]["ramping"] == 1          # the ramp is left alone, as before


def test_an_unanswered_rampst_in_zone_mode_is_not_done_and_is_retried(tmp_path):
    sim, ctl, n = _zone_started(tmp_path)
    sim.advance(60.0)
    real = sim._answer
    sim._answer = lambda text: "" if text == "RAMPST? 2" else real(text)
    res = ctl.heater_off(reason="t", origin="t")
    assert not res["confirmed"] and res["zone_ramp"][2]["stopped"] is False
    assert sim.outputs[2]["range"] == 0 and ctl.retrying and 2 in ctl.status()["retrying_off"]
    assert "RAMP 2,0,0.5" in sim.cmds()            # stopped anyway, just not read back
    sim._answer = real
    ctl.background_step()                          # the retry
    assert not ctl.retrying and ctl._last_off["confirmed"]


def test_a_ramp_stop_the_335_ignores_is_retried_by_the_off(tmp_path):
    sim, ctl, n = _zone_started(tmp_path)
    sim.advance(60.0)
    sim.ignore_verbs = {"RAMP"}
    res = ctl.heater_off(reason="t", origin="t")
    assert not res["confirmed"] and ctl.retrying and sim.outputs[2]["ramping"] == 1
    sim.ignore_verbs = set()
    ctl.background_step()
    assert not ctl.retrying and sim.outputs[2]["ramping"] == 0 and sim.outputs[2]["range"] == 0


def test_an_off_whose_zone_check_breaks_is_not_done(tmp_path, monkeypatch):
    """RANGE? read 0, but the zone-mode check did not run (the link failed half way): not
    confirmed, retried."""
    sim, ctl, n = _zone_started(tmp_path)

    def boom(*_a, **_k):
        raise RuntimeError("link gone half way")

    monkeypatch.setattr(ctl, "_stop_zone_ramp", boom)
    res = ctl.heater_off(reason="t", origin="t")
    assert res["readback"][2] == "0" and not res["confirmed"] and ctl.retrying


def test_turned_off_elsewhere_mid_ramp_in_zone_mode_mast_stops_the_ramp_only(tmp_path):
    """(b) ALL OFF at the panel during MAST's zone-mode ramp: MAST writes RAMP 0 and
    nothing else, says so in the ledger, and the next zone does not turn it back on."""
    sim, ctl, n = _zone_started(tmp_path)
    sim.events.append((100.0, lambda s: s.outputs[2].update(range=0)))   # ALL OFF / TLIMIT
    out = ctl.supervise()
    assert out["outcome"] == "turned_off_elsewhere", out
    assert sim.cmds()[n:] == ["RAMP 2,0,0.5"] and out["ramp_stop"]["stopped"]
    rows = [r for r in _ledger(tmp_path) if r["action"] == "zone_ramp_stop"]
    assert len(rows) == 1 and rows[0]["kind"] == "de-escalating write after a takeover"
    assert not ctl.watching                        # off and not ramping: nothing to watch
    sim.advance(1200.0)
    assert sim.outputs[2]["range"] == 0 and [z[1] for z in sim.zone_loads] == [1]


def test_a_ramp_stop_after_a_takeover_that_is_ignored_keeps_the_watch(tmp_path):
    sim, ctl, n = _zone_started(tmp_path)

    def all_off(s):
        s.outputs[2].update(range=0)
        s.ignore_verbs = {"RAMP"}                  # and the 335 will not take RAMP 0 now

    sim.events.append((100.0, all_off))
    assert ctl.supervise()["outcome"] == "turned_off_elsewhere"
    assert ctl.watching and ctl.mast_heating()     # the field stays put meanwhile
    assert ctl.background_step() is True
    assert sim.cmds()[n:].count("RAMP 2,0,0.5") == 2 and ctl.watching
    sim.ignore_verbs = set()
    ctl.background_step()                          # lands now: off and not ramping
    assert not ctl.watching and sim.outputs[2]["ramping"] == 0
    assert sim.cmds()[n:].count("RAMP 2,0,0.5") == 3


def test_the_watch_stops_a_zone_ramp_when_the_heater_is_turned_off_elsewhere(tmp_path):
    """Heating MAST watches (supervision ended on a takeover of the setpoint, which is
    ramping again) is turned off at the panel: the watch stops the ramp before it ends."""
    sim, ctl, n = _zone_started(tmp_path)
    sim.events.append((100.0, lambda s: s.outputs[2].update(ramp_from=s.outputs[2]["sp"],
                                                           ramp_to=12.0, ramp_t0=s.now)))
    assert ctl.supervise()["outcome"] == "external_change" and ctl.watching
    sim.outputs[2]["range"] = 0                    # ALL OFF while it still ramps
    ctl.background_step()
    assert sim.cmds()[n:] == ["RAMP 2,0,0.5"] and not ctl.watching
    sim.advance(1200.0)
    assert sim.outputs[2]["range"] == 0


def test_heating_mast_did_not_start_is_never_written_to(tmp_path):
    """The operator's own zone-mode ramp, heater then turned off: MAST supervises and
    watches nothing, so it writes nothing (no RAMP 0 either)."""
    sim = _zone_sim()
    ctl = make(sim, tmp_path, settings=ZSET)
    _zone_ramp_heater_off(sim)                     # the operator's ramp, heater off by hand
    assert sim.outputs[2]["ramping"] == 1
    assert not ctl.mast_heating()
    assert ctl.supervise()["outcome"] == "no_active_change"
    assert ctl.background_step() is False
    assert sim.writes == []


def test_held_means_ramping_off_and_rampst_zero(tmp_path):
    sim, ctl, n = _started(tmp_path)
    sim.advance(120.0)
    seen = {"n": 0}
    real = sim._answer

    def answer(text):
        if text == "RAMPST? 2":
            seen["n"] += 1
            if seen["n"] > 1:                      # after the RAMP 0: no answer
                return ""
        return real(text)

    sim._answer = answer
    res = ctl.hold(reason="t", origin="t")
    assert res["held"] is False and res["ramping"] is None and "RAMPST?" in res["error"]


def test_zone_mode_a_range_off_the_way_is_a_takeover(tmp_path):
    """Medium is within the maximum, but no zone on the way (1 and 2, both low here)
    loads it: someone set it by hand. MAST stops following and watches."""
    rows = [dict(r) for r in TABLE]
    rows[1]["range"] = 1                           # zones on the way (1, 2): low only
    sim = Sim335()
    sim.set_zones(2, rows, mode="2,1,0")
    ctl = make(sim, tmp_path, settings=ZSET)
    assert ctl.start(10.0, origin="t", tip_verdict=_parked, **_auth(10.0))["started"]
    n = len(sim.cmds())
    sim.events.append((100.0, lambda s: s.outputs[2].update(range=2)))   # by hand, within max
    out = ctl.supervise()
    assert out["outcome"] == "external_change" and sim.cmds()[n:] == [] and ctl.watching


@pytest.mark.parametrize("event,word", [
    (lambda s: s.outputs[2].update(outmode="1,1,0"), "控制模式"),     # left zone mode
    (lambda s: s.zones[2][2].update(p=99.0), "分区表"),               # a zone edited
])
def test_zone_mode_leaving_it_or_editing_the_table_is_a_takeover(tmp_path, event, word):
    sim, ctl, n = _zone_started(tmp_path)
    sim.events.append((50.0, event))
    out = ctl.supervise()
    assert out["outcome"] == "external_change" and word in out["detail"], out
    assert sim.cmds()[n:] == [] and ctl.watching


# ── 7f. writing the zone table and the control mode ──────────────────────────
def test_a_zone_table_is_written_zone_by_zone_and_read_back(tmp_path):
    sim = Sim335()                                 # closed-loop PID, factory zones
    ctl = make(sim, tmp_path, settings=ZSET)
    res = ctl.write_zone_table(_zone_json(), origin="t")
    assert res["written"] and not res["noop"], res
    assert sim.cmds() == ["ZONE 2,1,6.000,50.0,20.0,0.0,0.00,1,0,0.5",
                          "ZONE 2,2,15.000,60.0,25.0,0.0,0.00,2,0,1.0",
                          "ZONE 2,3,40.000,70.0,30.0,0.0,0.00,2,1,1.0"]   # 4-10 already unused
    assert [z["state"] for z in res["zones"]] == ["new"] * 3 + ["old"] * 7
    assert sum(1 for q in sim.queries if q.startswith(b"ZONE? 2,")) >= 3 + 2 * 10
    assert sim.outputs[2]["range"] == 0
    rows = [r for r in _ledger(tmp_path) if r["action"] == "zone_table"]
    assert rows[0]["phase"] == "begin" and rows[0]["old"][0]["upper_bound_k"] == 0.0
    assert rows[0]["writing"] == [1, 2, 3] and rows[-1]["result"] == "written"
    again = ctl.write_zone_table(_zone_json(), origin="t")
    assert again["written"] and again["noop"] and len(sim.cmds()) == 3


def _no_zone_5(sim):
    real = sim._answer
    sim._answer = lambda text: "" if text == "ZONE? 2,5" else real(text)


@pytest.mark.parametrize("prepare,word", [
    (lambda sim, ctl: sim.outputs[2].update(range=1), "加热器开着"),
    (lambda sim, ctl: sim.outputs[2].update(ramping=1, ramp_to=20.0, ramp_on=1, rate=1.0), "扫温"),
    (lambda sim, ctl: setattr(sim, "emul", "1,0"), "EMUL"),
    (lambda sim, ctl: sim.outputs[2].update(htrset="1,1,2,+1.000,1"), "电压输出"),
    (lambda sim, ctl: ctl._start_watch(2, "A", 10.0, why="test"), "MAST 自己的加热"),
    (lambda sim, ctl: _no_zone_5(sim), "没读到"),
])
def test_configuration_writes_refuse_and_write_nothing(tmp_path, prepare, word):
    sim = Sim335()
    sim.set_zones(2, TABLE)
    ctl = make(sim, tmp_path, settings=ZSET)
    prepare(sim, ctl)
    new = [dict(r) for r in TABLE]
    new[0]["p"] = 55.0
    res = ctl.write_zone_table(_zone_json(new), origin="t")
    assert not res["written"] and word in res["error"], res["error"]
    mode = ctl.set_control_mode("zone", origin="t")
    assert not mode["set"] and (word in mode["error"] or word == "没读到"), mode["error"]
    assert sim.writes == []


def test_configuration_writes_need_temperature_control_set_up_and_a_steady_field(tmp_path):
    sim = Sim335()
    for ctl, word in ((make(sim, tmp_path, settings=UNSET), "初始化里还没有设定控温"),
                      (make(sim, tmp_path, settings=ZSET, field_busy=True), "磁场")):
        assert word in ctl.write_zone_table(_zone_json(), origin="t")["error"]
        assert word in ctl.set_control_mode("zone", origin="t")["error"]
    assert sim.writes == [] and sim.queries == []


def test_an_unusable_new_table_writes_nothing(tmp_path):
    sim = Sim335()
    ctl = make(sim, tmp_path, settings=ZSET)
    for rows in ([dict(TABLE[0]), dict(TABLE[1])],                   # ends at 15 K < 40 K
                 [dict(TABLE[0], range=3)] + [dict(r) for r in TABLE[1:]],
                 [dict(TABLE[0], ub=600.0)]):                         # beyond the byte factory
        res = ctl.write_zone_table(_zone_json(rows), origin="t")
        assert not res["written"] and "没有写任何东西" in res["error"], res
    assert ctl.write_zone_table("[{]", origin="t")["error"]
    assert sim.writes == []


def test_a_zone_that_does_not_read_back_puts_the_old_table_back(tmp_path):
    sim = Sim335()
    ctl = make(sim, tmp_path, settings=ZSET)
    sim.ignore_indices = {1}                       # the 335 ignores the second ZONE
    res = ctl.write_zone_table(_zone_json(), origin="t")
    assert not res["written"] and res["restored"] is True, res
    unused = "0.000,50.0,20.0,0.0,0.00,0,0,0.1"
    assert sim.cmds() == ["ZONE 2,1,6.000,50.0,20.0,0.0,0.00,1,0,0.5",
                          "ZONE 2,2,15.000,60.0,25.0,0.0,0.00,2,0,1.0",
                          f"ZONE 2,1,{unused}", f"ZONE 2,2,{unused}"]
    assert all(z["state"] == "old" for z in res["zones"]) and sim.zones[2][:2] == [UNUSED, UNUSED]
    assert "旧表已恢复" in res["error"]
    assert _ledger(tmp_path)[-1]["result"] == "restored"


def test_a_zone_write_that_raises_is_read_back_before_anything_else(tmp_path):
    sim = Sim335()
    sim.raise_always = {"ZONE"}
    res = make(sim, tmp_path, settings=ZSET).write_zone_table(_zone_json(), origin="t")
    assert not res["written"] and res["restored"] is True
    assert [z["state"] for z in res["zones"]][0] == "old" and sim.zones[2][0] == UNUSED


def test_a_zone_write_that_turns_the_heater_on_is_undone_and_the_heater_turned_off(tmp_path):
    """Zone mode, the zone in force rewritten, and (unknown from the manual) the 335 applies
    it at once: the heater comes on. MAST turns it off and writes the old zone back."""
    old = [dict(TABLE[0], range=0)] + [dict(r) for r in TABLE[1:]]
    sim = Sim335()
    sim.outputs[2].update(sp=5.0, ramp_to=5.0)     # zone 1 in force, heater off
    sim.set_zones(2, old, mode="2,1,0")
    sim.zone_write_reloads = True
    res = make(sim, tmp_path, settings=ZSET).write_zone_table(_zone_json(), origin="t")
    assert not res["written"] and res["off"]["confirmed"] and res["restored"] is True, res
    assert sim.cmds() == ["ZONE 2,1,6.000,50.0,20.0,0.0,0.00,1,0,0.5", "RANGE 2,0",
                          "ZONE 2,1,6.000,50.0,20.0,0.0,0.00,0,0,0.5"]
    assert sim.outputs[2]["range"] == 0 and sim.zones[2][0]["range"] == 0
    assert "已关" in res["error"]


def test_an_abort_during_a_zone_table_write_puts_the_old_table_back(tmp_path):
    sim = Sim335()
    res = make(sim, tmp_path, settings=ZSET).write_zone_table(
        _zone_json(), origin="t", abort_check=lambda: len(sim.cmds()) >= 1)
    assert not res["written"] and res["restored"] is True
    assert sim.cmds() == ["ZONE 2,1,6.000,50.0,20.0,0.0,0.00,1,0,0.5",
                          "ZONE 2,1,0.000,50.0,20.0,0.0,0.00,0,0,0.1"]


def test_an_old_zone_mast_may_not_write_is_reported_not_written(tmp_path):
    old = [dict(r) for r in TABLE]
    old[1]["mout"] = 5.0                           # set at the panel: MAST never writes this
    sim = Sim335()
    sim.set_zones(2, old)
    sim.ignore_indices = {1}                       # zone 2's write is ignored
    new = [dict(TABLE[0], p=55.0)] + [dict(r) for r in TABLE[1:]]
    res = make(sim, tmp_path, settings=ZSET).write_zone_table(_zone_json(new), origin="t")
    assert not res["written"] and res["restored"] is False and res["not_restorable"] == [2]
    assert sim.zones[2][1]["mout"] == 5.0 and sim.zones[2][0]["p"] == 50.0
    zone_cmds = [c for c in sim.cmds() if c.startswith("ZONE")]
    assert zone_cmds and all(c.split(",")[6] == "0.00" for c in zone_cmds)   # never a manual output


def test_the_control_mode_switches_to_zone_with_a_good_table_and_back(tmp_path):
    sim = Sim335()
    sim.set_zones(2, TABLE)                        # still closed-loop PID
    ctl = make(sim, tmp_path, settings=ZSET)
    res = ctl.set_control_mode("zone", origin="t")
    assert res["set"] and res["old_mode"] == 1 and sim.outputs[2]["outmode"] == "2,1,0", res
    assert sim.cmds() == ["OUTMODE 2,2,1,0"] and sim.outputs[2]["range"] == 0
    assert ctl.set_control_mode(2, origin="t")["noop"] and len(sim.cmds()) == 1
    back = ctl.set_control_mode("closed_loop", origin="t")
    assert back["set"] and sim.cmds()[-1] == "OUTMODE 2,1,1,0"
    begins = [r for r in _ledger(tmp_path) if r["action"] == "control_mode" and r.get("phase") == "begin"]
    assert [(r["old_mode"], r["mode"]) for r in begins] == [(1, 2), (2, 1)]


@pytest.mark.parametrize("prepare,mode,word", [
    (lambda sim: None, "zone", "存着的分区表不能用"),                   # factory table: empty
    (lambda sim: sim.outputs[2].update(outmode="3,1,0"), "zone", "open loop"),
    (lambda sim: sim.outputs[2].update(outmode="1,2,0"), "zone", "不换控制输入"),
    (lambda sim: sim.outputs[2].update(outmode="1,1,1"), "zone", "Powerup"),
    (lambda sim: None, "open_loop", "不是 1"),
    (lambda sim: None, 3, "不是 1"),
])
def test_the_control_mode_refusals(tmp_path, prepare, mode, word):
    sim = Sim335()
    prepare(sim)
    res = make(sim, tmp_path, settings=ZSET).set_control_mode(mode, origin="t")
    assert not res["set"] and word in res["error"], res["error"]
    assert sim.writes == []


def test_entering_zone_mode_that_turns_the_heater_on_is_undone(tmp_path):
    """Unknown from the manual: entering zone mode applies the setpoint's zone (40 K:
    zone 3, medium). MAST turns the heater off and goes back to closed-loop PID."""
    sim = Sim335()
    sim.set_zones(2, TABLE)
    sim.outmode_loads_zone = True
    res = make(sim, tmp_path, settings=ZSET).set_control_mode("zone", origin="t")
    assert not res["set"] and res["restored"] is True and res["off"]["confirmed"], res
    assert sim.cmds() == ["OUTMODE 2,2,1,0", "RANGE 2,0", "OUTMODE 2,1,1,0"]
    assert sim.outputs[2]["range"] == 0 and sim.outputs[2]["outmode"] == "1,1,0"
    assert _ledger(tmp_path)[-1]["result"] == "restored"


def test_a_control_mode_the_335_ignores_is_reported_and_nothing_else_written(tmp_path):
    sim = Sim335()
    sim.set_zones(2, TABLE)
    sim.ignore_verbs = {"OUTMODE"}
    res = make(sim, tmp_path, settings=ZSET).set_control_mode("zone", origin="t")
    assert not res["set"] and res["restored"] is True and sim.cmds() == ["OUTMODE 2,2,1,0"]
    assert sim.outputs[2]["outmode"] == "1,1,0"


def test_configuration_writes_hold_the_change_lock(tmp_path):
    """A heating change cannot start during a configuration write, and the magnet's
    interlock sees MAST busy meanwhile."""
    sim = Sim335()
    ctl = make(sim, tmp_path, settings=ZSET)
    seen = {}

    def abort():
        seen.setdefault("busy", ctl.busy)
        seen.setdefault("heating", ctl.mast_heating())
        seen.setdefault("start", ctl.start(10.0, origin="t", tip_verdict=_parked, **_auth(10.0)))
        return False

    assert ctl.write_zone_table(_zone_json(), origin="t", abort_check=abort)["written"]
    assert seen["busy"] and seen["heating"] and not seen["start"]["started"]
    assert not ctl.busy


# ── 7g. what the magnet's interlock reads ─────────────────────────────────────
def test_the_interlock_state_reads_both_outputs_and_mast_heating(tmp_path):
    sim = Sim335()
    ctl = make(sim, tmp_path)
    st = ctl.interlock_state()
    assert st["ok"] and st["ranges"] == {1: 0, 2: 0} and sim.writes == []
    sim.outputs[1]["range"] = 2                    # the operator's heater on output 1
    assert not ctl.interlock_state()["ok"]
    sim.outputs[1]["range"] = 0
    real = sim._answer
    sim._answer = lambda text: "" if text == "RANGE? 2" else real(text)
    v = ctl.interlock_state()
    assert not v["ok"] and v["ranges"][2] is None  # unknown is not off
    sim._answer = real
    ctl._start_watch(2, "A", 10.0, why="test")
    v = ctl.interlock_state()
    assert not v["ok"] and v["mast_heating"] and sim.writes == []


def _zone_ramp_heater_off(sim: Sim335) -> None:
    """Output 2 in zone mode, its setpoint ramping from 4.342 K (zone 1) towards 10 K, heater
    off: the panel's own heating, turned off by hand. The 6 K boundary (reached at
    t = 198.96 s) would turn it back on."""
    sim._apply("RAMP 2,0,0.5")
    sim._apply("SETP 2,4.342")                     # jumps into zone 1, which loads low
    sim._apply("RAMP 2,1,0.5")
    sim._apply("SETP 2,10.000")
    sim._apply("RANGE 2,0")


def test_a_zone_ramp_with_the_heater_off_is_not_off_for_the_field(tmp_path):
    sim = _zone_sim()
    ctl = make(sim, tmp_path, settings=ZSET)
    assert ctl.interlock_state()["ok"]             # zone mode, not ramping: off
    _zone_ramp_heater_off(sim)
    v = ctl.interlock_state()
    assert not v["ok"] and v["ranges"] == {1: 0, 2: 0} and v["not_off"] == [2]
    assert "输出 2：分区模式，设定值还在扫" in v["detail"]
    sim.advance(400.0)                             # what the gate guards against
    assert sim.outputs[2]["range"] == 2
    assert sim.writes == []                        # the interlock only reads


def test_a_closed_loop_ramp_with_the_heater_off_is_off_for_the_field(tmp_path):
    sim, ctl, n = _started(tmp_path)               # closed-loop PID, ramping
    ctl._active = None                             # (not MAST's any more, for this check)
    sim.outputs[2]["range"] = 0
    assert sim.outputs[2]["ramping"] == 1
    v = ctl.interlock_state()
    assert v["ok"], v["detail"]                    # no zone table in force: nothing reloads


@pytest.mark.parametrize("silent,word", [("OUTMODE? 2", "OUTMODE? 没读到"),
                                         ("RAMPST? 2", "RAMPST? 没读到")])
def test_an_unknown_mode_or_ramp_state_is_not_off_for_the_field(tmp_path, silent, word):
    sim = _zone_sim()
    ctl = make(sim, tmp_path, settings=ZSET)
    real = sim._answer
    sim._answer = lambda text: "" if text == silent else real(text)
    v = ctl.interlock_state()
    assert not v["ok"] and v["not_off"] == [2] and word in v["detail"], v["detail"]


def test_a_hold_in_zone_mode_with_the_heater_off_does_not_move_the_setpoint(tmp_path):
    """The setpoint just past 6 K (zone 2), the stage in zone 1, heater off: SETP <T now>
    would ramp back across 6 K within a quarter second and load zone 1's range. The hold
    is RAMP 0 alone, and the heater stays off."""
    sim = _zone_sim()
    ctl = make(sim, tmp_path, settings=ZSET)
    _zone_ramp_heater_off(sim)
    sim.advance(199.2)                             # 6.004 K: zone 2 loaded medium ...
    assert sim.outputs[2]["range"] == 2
    sim._apply("RANGE 2,0")                        # ... and the operator turned it off again
    sim.inputs["A"]["k"] = 4.5                     # the stage lags behind, in zone 1
    loads = len(sim.zone_loads)
    res = ctl.hold(reason="t", origin="t")
    assert res["held"] and res["zone_heater_off"], res
    assert sim.cmds() == ["RAMP 2,0,1.0"]          # zone 2's rate, unchanged
    assert sim.outputs[2]["range"] == 0 and len(sim.zone_loads) == loads


def test_the_core_port_for_the_interlock(tmp_path):
    sim = Sim335()
    ctl = make(sim, tmp_path)
    try:
        heater_control.set_interlock_source(None)
        assert heater_control.interlock()["ok"] is False             # not wired: unknown
        heater_control.set_interlock_source(lambda: (_ for _ in ()).throw(RuntimeError("x")))
        assert heater_control.interlock()["ok"] is False
        heater_control.set_interlock_source(lambda: "garbage")
        assert heater_control.interlock()["ok"] is False
        heater_control.set_interlock_source(ctl.interlock_state)
        assert heater_control.interlock()["ok"] is True
        heater_control.set_source(None)
        assert heater_control.mast_heating() is False
        heater_control.set_source(lambda: ctl)
        ctl._start_watch(2, "A", 10.0, why="test")
        n = len(sim.queries)
        assert heater_control.mast_heating() is True and len(sim.queries) == n   # memory only
    finally:
        heater_control.set_source(None)
        heater_control.set_interlock_source(None)


# ── 8. skills, module, bypass ─────────────────────────────────────────────────
def test_skill_metadata_and_where_they_live():
    from mast.agents._shared.buffer_hitl import REMEDY_TOOL_NAMES
    from mast.core.state import NO_STATE_WRITEBACK_TAGS
    from mast.skills.builtins import heater_control as sk
    from mast.skills.hardware_modules import DETECTION_IMPLIED_MODULES, SKILL_OWNER

    metas = {c().metadata().name: c().metadata() for c in (
        sk.CheckSampleTemperatureChange, sk.SetSampleTemperature, sk.HoldSampleTemperature,
        sk.HeaterOff, sk.SetHeaterZoneTable, sk.SetHeaterControlMode)}
    for name in ("SetSampleTemperature", "SetHeaterZoneTable", "SetHeaterControlMode"):
        assert metas[name].safety_level == SafetyLevel.DANGEROUS, name
        assert metas[name].category == SkillCategory.WRITE, name
        assert name not in BYPASS_NAMES and name not in REMEDY_TOOL_NAMES, name
    assert metas["CheckSampleTemperatureChange"].category == SkillCategory.READ
    for name in ("CheckSampleTemperatureChange", "HoldSampleTemperature", "HeaterOff"):
        assert metas[name].safety_level == SafetyLevel.AUTO
    for name in ("HoldSampleTemperature", "HeaterOff"):
        assert name in BYPASS_NAMES and name in REMEDY_TOOL_NAMES
    assert {"optional-hardware", "cryostat"} <= set(metas["SetHeaterZoneTable"].tags)
    assert {"optional-hardware", "cryostat"} <= set(metas["SetHeaterControlMode"].tags)
    for name, m in metas.items():
        assert SKILL_OWNER[name] == "temperature_control"
        assert not NO_STATE_WRITEBACK_TAGS.isdisjoint(m.tags), name
    assert "temperature_control" in DETECTION_IMPLIED_MODULES


def test_the_module_follows_detection():
    from mast.skills import hardware_modules as hm

    try:
        heater_control.set_source(lambda: object())
        assert "temperature_control" in hm.enabled_ids()
        heater_control.set_source(lambda: None)
        assert "temperature_control" not in hm.enabled_ids()
    finally:
        heater_control.set_source(None)


def test_skills_without_a_controller_say_so():
    from mast.skills.builtins import heater_control as sk

    heater_control.set_source(None)
    for cls, params in ((sk.CheckSampleTemperatureChange, {"target_k": 10.0}),
                        (sk.SetSampleTemperature, {"target_k": 10.0}),
                        (sk.HoldSampleTemperature, {}), (sk.HeaterOff, {}),
                        (sk.SetHeaterZoneTable, {"zones": "[]"}),
                        (sk.SetHeaterControlMode, {"mode": "zone"})):
        r = cls().execute(SimpleNamespace(), params)
        assert not r.success and r.data["reason"] == "no_controller"


def test_the_zone_skills_through_the_port(tmp_path):
    from mast.skills.builtins import heater_control as sk

    sim = Sim335()
    ctl = make(sim, tmp_path, settings=ZSET)
    try:
        heater_control.set_source(lambda: ctl)
        assert sk.SetHeaterZoneTable().validate_params({"zones": _zone_json()}) == []
        r = sk.SetHeaterZoneTable().execute(SimpleNamespace(owner="ext"), {"zones": _zone_json()})
        assert r.success, r.error
        assert _ledger(tmp_path)[0]["origin"] == "SetHeaterZoneTable (ext)"
        assert sk.SetHeaterControlMode().validate_params({"mode": "zone"}) == []
        assert sk.SetHeaterControlMode().validate_params({"mode": "open_loop"})
        m = sk.SetHeaterControlMode().execute(SimpleNamespace(), {"mode": "zone"})
        assert m.success and sim.outputs[2]["outmode"] == "2,1,0", m.error
        c = sk.CheckSampleTemperatureChange().execute(SimpleNamespace(), {"target_k": 10.0})
        assert c.data["zones"][0]["upper_bound_k"] == 6.0 and c.data["control_mode"] == 2
        assert c.data["zone_path"] == [1, 2] and c.data["heater_range"] == 1
        bad = sk.SetHeaterZoneTable().execute(SimpleNamespace(), {"zones": "[]"})
        assert not bad.success and "数组" in bad.error
        on = sk.SetHeaterControlMode().execute(SimpleNamespace(), {"mode": "zone"})
        assert on.success and on.data["noop"]
    finally:
        heater_control.set_source(None)


def test_skills_through_the_port(tmp_path):
    from mast.skills.builtins import heater_control as sk

    sim = Sim335()
    ctl = make(sim, tmp_path)
    try:
        heater_control.set_source(lambda: ctl)
        r = sk.HeaterOff().execute(SimpleNamespace(owner="ext"), {})
        assert r.success and sim.cmds() == ["RANGE 2,0"]
        r1 = sk.HeaterOff().execute(SimpleNamespace(owner="ext"), {"output": 1})
        assert r1.success and sim.cmds()[-1] == "RANGE 1,0"
        h = sk.HoldSampleTemperature().execute(SimpleNamespace(), {})
        assert h.success and h.data["nothing_to_hold"]
        c = sk.CheckSampleTemperatureChange().execute(SimpleNamespace(), {"target_k": 10.0})
        assert c.success and c.data["would_proceed"] is False      # no authorization, no tip
    finally:
        heater_control.set_source(None)


def test_the_port_asks_a_learning_source_and_tolerates_a_plain_one():
    seen = []

    def learning(*, learn=False):
        seen.append(learn)
        return "ctl"

    try:
        heater_control.set_source(learning)
        assert heater_control.controller(learn=True) == "ctl" and seen == [True]
        heater_control.set_source(lambda: "plain")
        assert heater_control.controller(learn=True) == "plain"
    finally:
        heater_control.set_source(None)


# ── 9. runtime ────────────────────────────────────────────────────────────────
class _Monitor:
    def __init__(self, sensors):
        self._sensors = sensors


def _runtime_with(*sims, model="MODEL335"):
    from mast.core.runtime import CoreRuntime

    rt = CoreRuntime.__new__(CoreRuntime)
    sensors = {}
    for i, sim in enumerate(sims):
        for ch in ("A", "B"):
            sensors[f"{ch}{i}"] = lsci.LakeshoreTemperatureSensor(
                name=f"{ch}{i}", channel=ch, transport=sim, model=model)
    rt._monitor = _Monitor(sensors)
    rt._lsci_learned_at = time.monotonic()        # no background learning in these tests
    return rt


@pytest.fixture()
def quiet_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(hc, "_default_ledger_path", lambda: tmp_path / "l.jsonl")
    return tmp_path


def test_runtime_keeps_one_controller_per_335_and_none_when_ambiguous(quiet_ledger):
    a, b = Sim335(), Sim335()
    rt = _runtime_with(a)
    ctl = rt.heater_controller()
    assert ctl is not None and ctl.link.transport is a and rt.heater_controller() is ctl
    assert _runtime_with(a, b).heater_controller() is None
    assert _runtime_with().heater_controller() is None
    assert _runtime_with(a, model="MODEL218").heater_controller() is None
    assert _runtime_with(a, model="").heater_controller() is None


def test_runtime_learns_an_unknown_model_when_asked(quiet_ledger):
    """Inputs adopted before the model was recorded must not leave MAST without a way
    to turn the heater off after a restart."""
    sim = Sim335()
    rt = _runtime_with(sim, model="")
    assert rt.heater_controller() is None
    ctl = rt.heater_controller(learn=True)
    assert ctl is not None and ctl.link.transport is sim
    assert all(s.model == "MODEL335" for s in rt._monitor._sensors.values())


def test_a_rebuilt_controller_keeps_the_serial_pin(quiet_ledger):
    sim = Sim335()
    rt = _runtime_with(sim)
    first = rt.heater_controller()
    first.link.query("EMUL?")                       # verifies and pins
    sim2 = Sim335(idn="LSCI,MODEL335,TEST0002/0000000,1.10")
    rt._monitor = _runtime_with(sim2)._monitor      # a rescan: new transport objects
    second = rt.heater_controller()
    assert second is not first and second.link.pinned_serial == first.link.pinned_serial
    with pytest.raises(lsci.WrongController):
        second.link.query("EMUL?")


def test_the_runtime_interlock_for_field_changes(quiet_ledger):
    """What core.heater_control.interlock asks: no 335 passes; one 335 answers with both
    outputs and MAST's heating; anything it cannot tell refuses."""
    assert _runtime_with().heater_interlock()["ok"] is True            # no Lake Shore at all
    v = _runtime_with(Sim335(), model="MODEL218").heater_interlock()   # a monitor, no heater
    assert v["ok"] is True and v["present"] is False
    sim = Sim335()
    rt = _runtime_with(sim)
    assert rt.heater_interlock()["ok"] is True
    sim.outputs[1]["range"] = 1                                        # someone's heater on
    v = rt.heater_interlock()
    assert v["ok"] is False and v["ranges"][1] == 1 and sim.writes == []
    sim.outputs[1]["range"] = 0
    ctl = rt.heater_controller()
    ctl._background = False                                            # no watch thread here
    ctl._start_watch(2, "A", 10.0, why="test")                         # MAST's heating
    assert rt.heater_interlock()["ok"] is False
    two = _runtime_with(Sim335(), Sim335()).heater_interlock()          # which one?
    assert two["ok"] is False and ("不止一台" in two["detail"] or "more than one" in two["detail"])
    mute = Sim335()
    mute.idn = "garbage"                                               # model never learned
    v = _runtime_with(mute, model="").heater_interlock()
    assert v["ok"] is False and "型号" in v["detail"]


def test_the_runtime_wires_the_interlock_into_core_heater_control():
    src = Path(__file__).parents[3] / "MASTv2" / "mast" / "core" / "runtime.py"
    text = src.read_text(encoding="utf-8")
    assert "_hc.set_interlock_source(self.heater_interlock)" in text


def test_a_sensor_learns_its_model_from_the_first_snapshot():
    sim = Sim335()
    s = lsci.LakeshoreTemperatureSensor(name="SPM", channel="A", transport=sim)
    assert s.model == ""
    s.instrument_state(channels=["A"])
    assert s.model == "MODEL335"


def _estop_ready(rt, monkeypatch):
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


PROFILE = {"heater_output": 2, "heater_control_input": "A", "heater_max_setpoint_k": 40.0,
           "heater_max_range": 1, "heater_max_current_a": 1.0, "heater_max_rate_k_per_min": 1.0}


def test_emergency_stop_turns_both_heaters_off_where_control_is_set_up(quiet_ledger, monkeypatch):
    from mast.core import instrument_profile as ip

    sim = Sim335()
    rt = _runtime_with(sim, model="")              # even before the model is known
    _estop_ready(rt, monkeypatch)
    monkeypatch.setattr(ip, "_profile", dict(PROFILE))
    res = rt.emergency_stop("test")
    assert res["heater"] == "dispatched"
    assert _wait_for(lambda: sim.cmds() == ["RANGE 1,0", "RANGE 2,0"])


def test_emergency_stop_leaves_a_station_that_never_opted_in_alone(quiet_ledger, monkeypatch):
    from mast.core import instrument_profile as ip

    sim = Sim335()
    rt = _runtime_with(sim)
    _estop_ready(rt, monkeypatch)
    monkeypatch.setattr(ip, "_profile", {})
    res = rt.emergency_stop("test")
    assert res["heater"] == "temperature control not set up"
    time.sleep(0.3)
    assert sim.writes == []


def test_shutdown_hook_holds_only_a_mast_change(quiet_ledger):
    sim = Sim335()
    rt = _runtime_with(sim)
    rt._heater_shutdown()                     # no controller made yet: nothing
    ctl = rt.heater_controller()
    rt._heater_shutdown()                     # nothing active: nothing written
    assert sim.writes == []
    r = ctl.start(10.0, origin="t", tip_verdict=_parked, **_auth(10.0))
    assert not r["started"]                   # the door is closed


# ── 9b. the heater's rated current (HTRSET? is read, never written) ───────────
def _rated(max_range, rating, **kw) -> hc.HeaterSettings:
    return hc.HeaterSettings(output=2, control_input="A", max_setpoint_k=40.0, max_range=max_range,
                             max_current_a=rating, max_rate_k_per_min=1.0, **kw)


@pytest.mark.parametrize("setup,rng,expect", [
    (["0", "2", "1", "+0.707", "1"], 3, 0.707),               # code 1 = 0.707 A
    (["0", "2", "1", "+0.707", "1"], 2, 0.707 / 10 ** 0.5),   # 224 mA: each step down / sqrt(10)
    (["0", "2", "1", "+0.707", "1"], 1, 0.0707),
    (["0", "2", "1", "+0.707", "1"], 0, 0.0),
    (["0", "2", "0", "+0.200", "1"], 3, 0.2),                 # user specified: the user value
    (["0", "2", "0", "+0.200", "1"], 2, 0.2 / 10 ** 0.5),
    (["0", "2", "2", "+9.000", "1"], 3, 1.0),                 # a code ignores the user field
    (["0", "2", "3", "+0.100", "1"], 3, 1.414),               # the larger of the two printed values
    (["0", "2", "4", "+0.100", "1"], 3, 1.732),
])
def test_full_scale_current_of_a_range(setup, rng, expect):
    assert hc.heater_full_scale_a(setup, rng) == pytest.approx(expect, rel=1e-9)


@pytest.mark.parametrize("setup,rng", [
    ([""], 1), (["garbage"], 1), (["0", "2", "1"], 1), (["0", "2", "7", "+0.7", "1"], 1),
    (["0", "2", "0", "+0.000", "1"], 1), (["0", "2", "0", "nan", "1"], 1),
    (["0", "2", "1", "+0.707", "1"], 4), (None, 1),
])
def test_full_scale_current_unknown_is_none(setup, rng):
    assert hc.heater_full_scale_a(setup, rng) is None
    assert not hc.rating_verdict(setup, rng, 0.2)[0]


def test_the_rating_tolerance_is_float_noise_only():
    setup = ["0", "2", "0", "+1.000", "1"]
    fs = hc.heater_full_scale_a(setup, 2)
    assert hc.rating_verdict(setup, 2, fs * (1 - 1e-9))[0]           # noise: allowed
    assert not hc.rating_verdict(setup, 2, fs * (1 - 1e-3))[0]       # a real excess: refused


def test_the_rating_refuses_a_range_that_can_overdrive_the_heater(tmp_path):
    """HTRSET 2 = 0,2,1,0.707,1: High 707 mA, Medium 224 mA > a 200 mA heater."""
    sim = Sim335()
    sim.outputs[2]["htrset"] = "0,2,1,+0.707,1"
    plan = make(sim, tmp_path, settings=_rated(2, 0.2)).plan(10.0, tip_verdict=_parked, **_auth(10.0))
    assert [c.id for c in plan.refusals] == ["current_rating"], plan.refusal_text()
    text = plan.refusal_text()
    assert "224" in text and "200" in text and "HTRSET" in text and "0.2 A" in text
    assert sim.writes == []
    ok = make(sim, tmp_path, settings=_rated(1, 0.2)).plan(10.0, tip_verdict=_parked, **_auth(10.0))
    assert ok.ok, ok.refusal_text()


def test_a_user_maximum_current_equal_to_the_rating_allows_every_range(tmp_path):
    sim = Sim335()
    sim.outputs[2]["htrset"] = "0,2,0,+0.200,1"
    plan = make(sim, tmp_path, settings=_rated(3, 0.2)).plan(10.0, tip_verdict=_parked, **_auth(10.0))
    assert plan.ok, plan.refusal_text()
    sim.outputs[2]["htrset"] = "0,2,0,+0.201,1"             # a hair above is refused
    plan = make(sim, tmp_path, settings=_rated(3, 0.2)).plan(10.0, tip_verdict=_parked, **_auth(10.0))
    assert "current_rating" in {c.id for c in plan.refusals}


def test_an_unreadable_heater_setup_refuses(tmp_path):
    sim = Sim335()
    sim.outputs[2]["htrset"] = "0,2,9,+0.200,1"           # a code that is in no table
    plan = make(sim, tmp_path, settings=_rated(1, 0.2)).plan(10.0, tip_verdict=_parked, **_auth(10.0))
    assert "current_rating" in {c.id for c in plan.refusals}, plan.refusal_text()
    assert sim.writes == []


def test_no_rating_configured_means_no_heating(tmp_path):
    s = hc.HeaterSettings(output=2, control_input="A", max_setpoint_k=40.0, max_range=1,
                          max_rate_k_per_min=1.0)
    assert not s.control_configured
    plan = make(Sim335(), tmp_path, settings=s).plan(10.0, tip_verdict=_parked, **_auth(10.0))
    assert [c.id for c in plan.refusals] == ["configured"]
    assert "额定电流" in plan.refusal_text() or "rated current" in plan.refusal_text()
    assert not _rated(1, 0.0).control_configured and not _rated(1, -1.0).control_configured


def _rated_started(tmp_path, htrset="0,1,0,+0.500,1"):
    """Low range on a 0.5 A user maximum: 50 mA full scale, exactly the 0.05 A rating."""
    sim = Sim335()
    sim.outputs[2]["htrset"] = htrset
    ctl = make(sim, tmp_path, settings=_rated(1, 0.05))
    r = ctl.start(10.0, origin="t", tip_verdict=_parked, **_auth(10.0))
    assert r["started"], r.get("error")
    return sim, ctl, len(sim.cmds())


def test_a_higher_maximum_current_set_mid_heat_turns_the_heater_off(tmp_path):
    sim, ctl, n = _rated_started(tmp_path)
    sim.events.append((100.0, lambda s: s.outputs[2].update(htrset="0,1,0,+1.700,1")))
    out = ctl.supervise()
    assert out["outcome"] == "current_over_rating", out
    assert sim.cmds()[n:] == ["RANGE 2,0"] and sim.outputs[2]["range"] == 0


def test_an_unchanged_heater_setup_does_not_trouble_supervision(tmp_path):
    sim, ctl, n = _rated_started(tmp_path)
    assert ctl.supervise()["outcome"] == "done"
    assert sim.cmds()[n:] == [] and sim.outputs[2]["range"] == 1
    assert any(q.startswith(b"HTRSET?") for q in sim.queries[-400:])


def test_losing_the_rating_mid_heat_turns_the_heater_off_even_when_htrset_is_garbled(tmp_path):
    sim = Sim335()
    sim.outputs[2]["htrset"] = "0,1,0,+0.500,1"
    box = {"s": _rated(1, 0.05)}
    link = lsci.LakeshoreLink(sim, port="COMX", expected_model="MODEL335")
    ctl = hc.Heater335Controller(
        link, settings=lambda: box["s"], temperature=lambda ch: None, field_busy=lambda: False,
        field_state=lambda: {"ok": True}, ledger_path=lambda: tmp_path / "heater_ledger.jsonl", clock=lambda: sim.now,
        sleep=sim.advance, background=False)
    assert ctl.start(10.0, origin="t", tip_verdict=_parked, **_auth(10.0))["started"]
    n = len(sim.cmds())
    box["s"] = hc.HeaterSettings(output=2, control_input="A", max_setpoint_k=40.0, max_range=1,
                                 max_rate_k_per_min=1.0)            # the rating is gone
    sim.outputs[2]["htrset"] = "garbage"
    out = ctl.supervise()
    assert out["outcome"] == "current_over_rating", out
    assert sim.cmds()[n:] == ["RANGE 2,0"]


def test_the_watch_turns_a_higher_maximum_current_off(tmp_path):
    sim, ctl, n = _rated_started(tmp_path)
    assert ctl.supervise()["outcome"] == "done" and ctl.watching
    n = len(sim.cmds())
    sim.outputs[2]["htrset"] = "0,1,0,+1.700,1"
    for _ in range(hc.ZONE_RECHECK_POLLS):
        assert ctl.background_step()
        if not ctl.watching:
            break
    assert sim.cmds()[n:] == ["RANGE 2,0"] and not ctl.watching


# ── 9c. the per-zone manual output (feed-forward), capped at initialization ────
def _mout_settings(cap, **kw) -> hc.HeaterSettings:
    return hc.HeaterSettings(output=2, control_input="A", max_setpoint_k=40.0, max_range=2,
                             max_current_a=1.0, max_mout_pct=cap, max_rate_k_per_min=1.0, **kw)


def test_build_write_zone_takes_a_manual_output_up_to_the_cap():
    row = (1, 25.0, 50, 20, 0)
    assert hc.build_write("ZONE", 2, *row, 20, 1, 0, 0.5, max_mout_pct=25.0, **MAXK) == \
        b"ZONE 2,1,25.000,50.0,20.0,0.0,20.00,1,0,0.5\r\n"
    assert hc.build_write("ZONE", 2, *row, 25, 1, 0, 0.5, max_mout_pct=25.0,
                          **MAXK).startswith(b"ZONE 2,1,25.000")
    for bad in (30, 25.1, -0.1, 101, True):
        with pytest.raises(ValueError):
            hc.build_write("ZONE", 2, *row, bad, 1, 0, 0.5, max_mout_pct=25.0, **MAXK)
    for cap_zero in (0.01, 20):                      # the default cap is 0: as before
        with pytest.raises(ValueError):
            hc.build_write("ZONE", 2, *row, cap_zero, 1, 0, 0.5, **MAXK)
    assert hc.build_write("ZONE", 2, *row, 0, 1, 0, 0.5, **MAXK) == \
        b"ZONE 2,1,25.000,50.0,20.0,0.0,0.00,1,0,0.5\r\n"


def test_a_cap_above_100_never_lets_a_manual_output_above_100_through():
    with pytest.raises(ValueError):
        hc.build_write("ZONE", 2, 1, 25.0, 50, 20, 0, 101, 1, 0, 0.5, max_mout_pct=500.0, **MAXK)


def test_the_zone_table_verdict_and_parser_follow_the_cap():
    rows = [dict(TABLE[0], mout=20.0), dict(TABLE[1]), dict(TABLE[2])]
    assert _verdict(_zones(rows), max_mout_pct=25.0).ok
    v = _verdict(_zones([dict(rows[0], mout=30.0)] + rows[1:]), max_mout_pct=25.0)
    assert not v.ok and any("第 1 区的手动输出" in p or "zone 1: manual output" in p for p in v.problems)
    assert not _verdict(_zones(rows)).ok               # cap 0: a nonzero mout is refused
    good = {"upper_bound_k": 6, "p": 50, "i": 20, "d": 0, "range": "low", "rate_k_per_min": 0.5}
    z = hc.zone_table_from(json.dumps([dict(good, mout_pct=20)]), max_mout_pct=25.0)
    assert z[0].mout_pct == 20.0
    for bad, cap in ((30, 25.0), (20, 0.0), (-1, 25.0), ("x", 25.0)):
        with pytest.raises(hc.HeaterControlError):
            hc.zone_table_from(json.dumps([dict(good, mout_pct=bad)]), max_mout_pct=cap)


def _mout_json(mout) -> str:
    return json.dumps([{"upper_bound_k": r["ub"], "p": r["p"], "i": r["i"], "d": 0.0,
                        "range": r["range"], "rate_k_per_min": r["rate"], "mout_pct": mout,
                        **({"input": r["input"]} if "input" in r else {})} for r in TABLE])


def test_a_zone_table_with_a_manual_output_is_written_and_read_back(tmp_path):
    sim = Sim335()
    ctl = make(sim, tmp_path, settings=_mout_settings(25.0))
    res = ctl.write_zone_table(_mout_json(20.0), origin="t")
    assert res["written"], res
    assert sim.cmds()[0] == "ZONE 2,1,6.000,50.0,20.0,0.0,20.00,1,0,0.5"
    assert sim.zones[2][0]["mout"] == 20.0
    refused = ctl.write_zone_table(_mout_json(30.0), origin="t")
    assert not refused["written"] and len(sim.cmds()) == 3         # nothing more was sent
    none = make(Sim335(), tmp_path, settings=_mout_settings(0.0))
    assert not none.write_zone_table(_mout_json(20.0), origin="t")["written"]


@pytest.mark.parametrize("cap,mout,ok", [(0.0, 20.0, False), (25.0, 20.0, True),
                                         (25.0, 30.0, False), (25.0, 25.0, True),
                                         (0.0, 0.0, True)])
def test_closed_loop_pid_with_a_panel_manual_output_follows_the_cap(tmp_path, cap, mout, ok):
    sim = Sim335()
    sim.outputs[2]["mout"] = mout
    plan = make(sim, tmp_path, settings=_mout_settings(cap)).plan(10.0, tip_verdict=_parked,
                                                                    **_auth(10.0))
    ids = {c.id for c in plan.refusals}
    assert ("no_manual_output" not in ids) is ok, plan.refusal_text()
    assert sim.writes == []
    if not ok:
        assert "MOUT" not in "".join(sim.write_texts())            # MAST does not write it


def test_an_unreadable_manual_output_refuses(tmp_path):
    sim = Sim335()
    real = sim._answer
    sim._answer = lambda text: "" if text == "MOUT? 2" else real(text)
    plan = make(sim, tmp_path, settings=_mout_settings(25.0)).plan(10.0, tip_verdict=_parked,
                                                                    **_auth(10.0))
    assert not plan.ok and sim.writes == []


def test_a_manual_output_raised_above_the_cap_mid_heat_turns_the_heater_off(tmp_path):
    sim = Sim335()
    sim.outputs[2]["mout"] = 20.0
    ctl = make(sim, tmp_path, settings=_mout_settings(25.0))
    assert ctl.start(10.0, origin="t", tip_verdict=_parked, **_auth(10.0))["started"]
    n = len(sim.cmds())
    sim.events.append((100.0, lambda s: s.outputs[2].update(mout=30.0)))
    out = ctl.supervise()
    assert out["outcome"] == "mout_over_cap", out
    assert sim.cmds()[n:] == ["RANGE 2,0"] and sim.outputs[2]["range"] == 0


def test_a_manual_output_within_the_cap_stays_on_through_many_rechecks(tmp_path):
    sim = Sim335()
    sim.outputs[2]["mout"] = 20.0
    ctl = make(sim, tmp_path, settings=_mout_settings(25.0))
    assert ctl.start(10.0, origin="t", tip_verdict=_parked, **_auth(10.0))["started"]
    assert ctl.supervise()["outcome"] == "done" and ctl.watching
    n = len(sim.cmds())
    for _ in range(3 * hc.ZONE_RECHECK_POLLS):
        assert ctl.background_step()
    assert ctl.watching and sim.cmds()[n:] == [] and sim.outputs[2]["range"] == 2
    assert sum(1 for q in sim.queries if q.startswith(b"MOUT? 2")) >= 3


def test_the_watch_turns_a_manual_output_above_the_cap_off(tmp_path):
    sim = Sim335()
    sim.outputs[2]["mout"] = 20.0
    ctl = make(sim, tmp_path, settings=_mout_settings(25.0))
    assert ctl.start(10.0, origin="t", tip_verdict=_parked, **_auth(10.0))["started"]
    assert ctl.supervise()["outcome"] == "done" and ctl.watching
    n = len(sim.cmds())
    sim.outputs[2]["mout"] = 30.0
    for _ in range(hc.ZONE_RECHECK_POLLS):
        assert ctl.background_step()
        if not ctl.watching:
            break
    assert sim.cmds()[n:] == ["RANGE 2,0"] and not ctl.watching


def test_range_off_takes_the_output_away_whatever_the_manual_output_is(tmp_path):
    sim = Sim335()
    sim.outputs[2]["mout"] = 20.0
    ctl = make(sim, tmp_path, settings=_mout_settings(25.0))
    assert ctl.start(10.0, origin="t", tip_verdict=_parked, **_auth(10.0))["started"]
    assert sim.outputs[2]["range"] == 2 and sim._answer("HTR? 2") != "+0.0"
    res = ctl.heater_off(reason="t", origin="t")
    assert res["confirmed"] and sim.outputs[2]["range"] == 0
    assert sim._answer("HTR? 2") == "+0.0" and sim.outputs[2]["mout"] == 20.0
    assert "MOUT" not in "".join(sim.write_texts())


# ── 9d. off order, limits in the factory, the field, rechecks ─────────────────
def test_zone_mode_off_stops_the_ramp_before_the_setpoint_can_cross_a_boundary(tmp_path):
    """The 335 loads a zone's range as the setpoint crosses into it (p. 69). The off must not
    leave a window for that: the ramp is stopped first (sent only), then RANGE 0."""
    sim, ctl, n = _zone_started(tmp_path)
    sim._update()
    sp = sim.outputs[2]["sp"]
    to_boundary_s = (6.0 - sp) / (0.5 / 60.0)
    sim.advance(to_boundary_s - 0.3)               # 0.3 s from the 6 K boundary of zone 1
    res = ctl.heater_off(reason="t", origin="t")
    assert res["confirmed"], res
    assert sim.cmds()[n:] == ["RAMP 2,0,0.5", "RANGE 2,0"]
    sim.advance(1200.0)
    assert sim.outputs[2]["range"] == 0 and [z[1] for z in sim.zone_loads] == [1]
    assert res["zone_ramp"][2]["sent"] == "taken" and res["zone_ramp"][2]["stopped"] is True


def test_closed_loop_off_still_starts_with_range_0(tmp_path):
    sim, ctl, n = _started(tmp_path)
    ctl.heater_off(reason="t", origin="t")
    assert sim.cmds()[n:] == ["RANGE 2,0"]


def test_hold_never_writes_a_setpoint_above_the_maximum(tmp_path):
    sim, ctl, n = _started(tmp_path)                # SETTINGS: maximum setpoint 40 K
    sim.follow = False
    sim.inputs["A"]["k"] = 45.0                      # the stage is above the maximum
    res = ctl.hold(reason="t", origin="t")
    assert res["held"], res
    setps = [c for c in sim.cmds()[n:] if c.startswith("SETP")]
    assert setps == ["SETP 2,40.000"]


def test_a_zone_row_above_the_maximum_is_refused_by_the_factory_also_on_a_restore(tmp_path):
    sim = Sim335()
    ctl = make(sim, tmp_path, settings=ZSET)
    res = ctl._write_zone(None, 2, 1, hc.Zone(60.0, 50.0, 20.0, 0.0, 0.0, 1, 0, 0.5))
    assert res["refused"] and sim.writes == []
    ok = ctl._write_zone(None, 2, 1, hc.Zone(40.0, 50.0, 20.0, 0.0, 0.0, 1, 0, 0.5))
    assert not ok.get("refused") and sim.writes


def _verdict_text(plan) -> str:
    return plan.refusal_text() if not plan.ok else ""


@pytest.mark.parametrize("busy,state,ok", [
    (lambda: False, lambda: {"ok": True}, True),
    (lambda: True, lambda: {"ok": True}, False),
    (lambda: (_ for _ in ()).throw(RuntimeError("no port")), lambda: {"ok": True}, False),
    (lambda: False, lambda: {"ok": False, "detail": "the 625 is ramping"}, False),
    (lambda: False, lambda: (_ for _ in ()).throw(RuntimeError("no link")), False),
    (lambda: False, lambda: None, False),
    (lambda: False, lambda: {"detail": "no verdict"}, False),
])
def test_unknown_about_the_field_is_not_at_rest(tmp_path, busy, state, ok):
    sim = Sim335()
    link = lsci.LakeshoreLink(sim, port="COMX", expected_model="MODEL335")
    ctl = hc.Heater335Controller(
        link, settings=lambda: SETTINGS, temperature=lambda ch: None, field_busy=busy,
        field_state=state, ledger_path=lambda: tmp_path / "l.jsonl", clock=lambda: sim.now,
        sleep=sim.advance, background=False)
    plan = ctl.plan(10.0, tip_verdict=_parked, **_auth(10.0))
    assert ("field_steady" not in {c.id for c in plan.refusals}) is ok, plan.refusal_text()
    cfg = ctl._config_refusal(SETTINGS)
    assert (cfg == "") is ok, cfg
    assert sim.writes == []


def test_the_field_ports_default_to_not_at_rest_when_not_wired(monkeypatch):
    from mast.core import magnet_control

    monkeypatch.setattr(magnet_control, "_state_source", None)
    assert magnet_control.field_state()["ok"] is False
    monkeypatch.setattr(magnet_control, "_state_source", lambda: {"ok": True, "present": False})
    assert magnet_control.field_state()["ok"] is True
    monkeypatch.setattr(magnet_control, "_state_source", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert magnet_control.field_state()["ok"] is False
    monkeypatch.setattr(magnet_control, "_state_source", lambda: "garbage")
    assert magnet_control.field_state()["ok"] is False
    monkeypatch.setattr(magnet_control, "_state_source", None)
    assert hc._default_field_state()["ok"] is False
    monkeypatch.setattr(magnet_control, "controller", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(RuntimeError):
        hc._default_field_busy()                    # not "not busy": the caller refuses


def _failing(sim, prefix, times):
    """The next *times* queries starting with *prefix* come back empty."""
    real = sim._answer
    left = {"n": times}

    def answer(text):
        if text.startswith(prefix) and left["n"] > 0:
            left["n"] -= 1
            return ""
        return real(text)

    sim._answer = answer


def test_a_recheck_that_cannot_read_twice_in_a_row_turns_the_heater_off(tmp_path):
    sim, ctl, n = _rated_started(tmp_path)
    _failing(sim, "HTRSET?", 10_000)
    out = ctl.supervise()
    assert out["outcome"] == "recheck_unreadable", out
    assert sim.cmds()[n:] == ["RANGE 2,0"] and sim.outputs[2]["range"] == 0


def test_one_unreadable_recheck_is_tried_again_not_acted_on(tmp_path):
    sim, ctl, n = _rated_started(tmp_path)
    _failing(sim, "MOUT?", 1)
    assert ctl.supervise()["outcome"] == "done"
    assert sim.cmds()[n:] == [] and sim.outputs[2]["range"] == 1


@pytest.mark.parametrize("tlimit,word", [(0.0, "off"), (45.0, "above"), (11.0, "below")])
def test_the_335s_own_limit_is_rechecked_during_supervision(tmp_path, tlimit, word):
    sim, ctl, n = _rated_started(tmp_path)           # target 10 K: the window is [12, 40] K
    sim.events.append((100.0, lambda s: s.inputs["A"].update(tlimit=tlimit)))
    out = ctl.supervise()
    assert out["outcome"] == "temperature_limit", out
    assert sim.cmds()[n:] == ["RANGE 2,0"]


def test_the_watch_rechecks_the_335s_own_limit_too(tmp_path):
    sim, ctl, n = _rated_started(tmp_path)
    assert ctl.supervise()["outcome"] == "done" and ctl.watching
    n = len(sim.cmds())
    sim.inputs["A"]["tlimit"] = 0.0
    for _ in range(hc.ZONE_RECHECK_POLLS):
        assert ctl.background_step()
        if not ctl.watching:
            break
    assert sim.cmds()[n:] == ["RANGE 2,0"] and not ctl.watching


def test_the_watch_turns_the_heater_off_when_the_recheck_cannot_read(tmp_path):
    sim, ctl, n = _rated_started(tmp_path)
    assert ctl.supervise()["outcome"] == "done" and ctl.watching
    n = len(sim.cmds())
    _failing(sim, "TLIMIT?", 10_000)
    for _ in range(2 * hc.ZONE_RECHECK_POLLS):
        assert ctl.background_step()
        if not ctl.watching:
            break
    assert sim.cmds()[n:] == ["RANGE 2,0"] and not ctl.watching


def test_a_second_start_while_a_change_is_supervised_is_refused(tmp_path):
    sim, ctl, n = _started(tmp_path)
    sim.advance(1200.0)                              # the ramp is over, supervise has not ended
    assert sim.outputs[2]["ramping"] == 0 and ctl.active is not None
    r = ctl.start(12.0, origin="t", tip_verdict=_parked, **_auth(12.0))
    assert not r["started"] and ctl.active is not None
    assert "supervised" in r["error"] or "监督" in r["error"]
    assert len(sim.cmds()) == n


def test_a_start_with_an_unconfirmed_off_pending_is_refused(tmp_path):
    sim = Sim335()
    ctl = make(sim, tmp_path)
    ctl._retry_off[2] = 1.0
    r = ctl.start(10.0, origin="t", tip_verdict=_parked, **_auth(10.0))
    assert not r["started"] and sim.writes == []


def test_pid_start_with_the_setpoint_above_the_temperature_brings_it_down_first(tmp_path):
    """The panel's setpoint is 35 K, the stage 20 K, the heater on: a 'cool to 15 K' is not
    raising, yet the heater is pushing up. The setpoint goes to T now before the ramp starts."""
    sim = Sim335()
    sim.follow = False
    sim.inputs["A"]["k"] = 20.0
    sim.outputs[2].update(range=1, sp=35.0, ramp_to=35.0, ramp_from=35.0)
    ctl = make(sim, tmp_path)
    r = ctl.start(15.0, origin="t", tip_verdict=_parked)
    assert r["started"], r.get("error")
    assert sim.cmds() == ["RAMP 2,0,1.0", "SETP 2,20.000", "RAMP 2,1,1.0", "SETP 2,15.000"]
    assert ctl.active["start_setpoint_k"] == pytest.approx(20.0)


def test_pid_start_with_the_setpoint_at_the_temperature_is_as_before(tmp_path):
    sim = Sim335()
    sim.follow = False
    sim.inputs["A"]["k"] = 20.0
    sim.outputs[2].update(range=1, sp=20.0, ramp_to=20.0, ramp_from=20.0)
    ctl = make(sim, tmp_path)
    assert ctl.start(15.0, origin="t", tip_verdict=_parked)["started"]
    assert sim.cmds() == ["RAMP 2,1,1.0", "SETP 2,15.000"]


@pytest.mark.parametrize("outmode", ["1,2,0", "3,1,0", "0,1,0"])
def test_a_control_mode_or_input_changed_at_the_panel_ends_the_change_in_pid_too(tmp_path, outmode):
    sim, ctl, n = _started(tmp_path)
    sim.events.append((100.0, lambda s: s.outputs[2].update(outmode=outmode)))
    out = ctl.supervise()
    assert out["outcome"] == "external_change", out
    assert ctl.watching and sim.cmds()[n:] == []         # left alone, the watch keeps the limits


def test_a_change_that_never_read_its_state_is_watched_not_forgotten(tmp_path):
    sim, ctl, n = _started(tmp_path)
    assert ctl.hold(reason="t", origin="t")["held"]
    sim.fail_reads = 1                                   # the first status read fails
    out = ctl.supervise()
    sim.fail_reads = 0
    assert out["outcome"] == "held" and out["last"] == {}, out
    assert ctl.watching


@pytest.mark.parametrize("fields,ok", [
    (["0", "2", "1", "+0.707", "1"], True),                  # output 2 / output 1: the same five fields
    (["0", "1", "4", "+0.000", "1"], True),
    (["0", "2", "1", "+0.707"], False),                      # four
    (["0", "2", "1", "+0.707", "1", "0"], False),            # six
    (["2", "2", "1", "+0.707", "1"], False), (["0", "3", "1", "+0.707", "1"], False),
    (["0", "2", "5", "+0.707", "1"], False), (["0", "2", "1", "x", "1"], False),
    (["0", "2", "1", "+0.707", "3"], False), ([], False), (None, False),
])
def test_htrset_is_parsed_exactly(fields, ok):
    assert (hc.parse_htrset(fields) is not None) is ok
    assert (hc.heater_full_scale_a(fields, 3) is not None) is ok


def test_output_1_and_output_2_replies_have_the_same_format_in_the_plan(tmp_path):
    for out_no, htr in ((1, "0,1,1,+0.707,1"), (2, "0,2,1,+0.707,1")):
        sim = Sim335()
        sim.outputs[out_no]["htrset"] = htr
        sim.outputs[out_no].update(outmode="1,1,0", sp=20.0, ramp_to=20.0, ramp_from=20.0)
        s = hc.HeaterSettings(output=out_no, control_input="A", max_setpoint_k=40.0, max_range=1,
                              max_current_a=0.2, max_rate_k_per_min=1.0)
        plan = make(sim, tmp_path, settings=s).plan(10.0, tip_verdict=_parked, **_auth(10.0))
        assert "current_rating" not in {c.id for c in plan.refusals}, plan.refusal_text()
        sim.outputs[out_no]["htrset"] = htr + ",0"           # a format that does not match
        plan = make(sim, tmp_path, settings=s).plan(10.0, tip_verdict=_parked, **_auth(10.0))
        assert "current_rating" in {c.id for c in plan.refusals}


# ── 10. profile and initialization ────────────────────────────────────────────
def test_heater_limits_are_dropped_not_clamped():
    from mast.core.instrument_profile import sanitize

    clean = sanitize({"heater_output": 3, "heater_max_setpoint_k": 900.0, "heater_max_range": 5,
                      "heater_max_rate_k_per_min": 0.0, "heater_control_input": "A"})
    assert clean == {"heater_control_input": "A"}
    ok = sanitize({"heater_output": 2, "heater_max_setpoint_k": 40.0, "heater_max_range": 1,
                   "heater_max_current_a": 0.2, "heater_max_rate_k_per_min": 1.0})
    assert ok == {"heater_output": 2, "heater_max_setpoint_k": 40.0, "heater_max_range": 1,
                  "heater_max_current_a": 0.2, "heater_max_rate_k_per_min": 1.0}
    # a rating of zero or below is dropped, not clamped: no rating = no heating
    assert sanitize({"heater_max_current_a": 0.0}) == {}
    assert sanitize({"heater_max_current_a": -0.2}) == {}
    assert sanitize({"heater_max_mout_pct": 101.0}) == {} and sanitize({"heater_max_mout_pct": -1.0}) == {}
    assert sanitize({"heater_max_mout_pct": 25.0}) == {"heater_max_mout_pct": 25.0}


def test_settings_read_from_the_profile(monkeypatch):
    from mast.core import instrument_profile as ip

    monkeypatch.setattr(ip, "_profile", {"heater_output": 2, "heater_control_input": "a",
                                         "heater_max_setpoint_k": 40.0, "heater_max_range": 1,
                                         "heater_max_current_a": 0.2,
                                         "heater_max_rate_k_per_min": 1.0})
    s = hc.HeaterSettings.from_profile()
    assert s.control_configured and s.output == 2 and s.control_input == "A"
    assert s.max_current_a == 0.2
    monkeypatch.setattr(ip, "_profile", {"heater_output": 2, "heater_control_input": "a",
                                         "heater_max_setpoint_k": 40.0, "heater_max_range": 1,
                                         "heater_max_rate_k_per_min": 1.0})
    assert not hc.HeaterSettings.from_profile().control_configured     # no rating, no heating
    monkeypatch.setattr(ip, "_profile", {"heater_output": 2})
    assert not hc.HeaterSettings.from_profile().control_configured


def test_the_manual_output_cap_comes_from_the_profile(monkeypatch):
    from mast.core import instrument_profile as ip

    base = {"heater_output": 2, "heater_control_input": "A", "heater_max_setpoint_k": 40.0,
            "heater_max_range": 1, "heater_max_current_a": 0.2, "heater_max_rate_k_per_min": 1.0}
    monkeypatch.setattr(ip, "_profile", dict(base))
    assert hc.HeaterSettings.from_profile().max_mout_pct == 0.0        # not set: no feed-forward
    monkeypatch.setattr(ip, "_profile", dict(base, heater_max_mout_pct=25.0))
    s = hc.HeaterSettings.from_profile()
    assert s.max_mout_pct == 25.0 and s.control_configured and s.as_dict()["max_mout_pct"] == 25.0
    monkeypatch.setattr(ip, "_profile", dict(base, heater_max_mout_pct=250.0))
    assert hc.HeaterSettings.from_profile().max_mout_pct == 0.0        # out of range: off


def test_the_heating_group_appears_only_with_a_335():
    from mast.core import instrument_init as ii

    items = ii.items_for_group("heater")
    assert {it.key for it in items} == {"heater_output", "heater_control_input",
                                        "heater_max_setpoint_k", "heater_max_range",
                                        "heater_max_current_a", "heater_max_mout_pct",
                                        "heater_max_rate_k_per_min"}
    assert all(ii.RUNTIME_TEMPERATURE_CONTROLLER in it.depends_on for it in items)
    none_here = {ii.RUNTIME_TEMPERATURE_CONTROLLER: "none"}
    assert all(ii.na_reason(it, none_here) for it in items)
    here = {ii.RUNTIME_TEMPERATURE_CONTROLLER: "lakeshore_335"}
    assert not any(ii.na_reason(it, here) for it in items)


def test_the_init_route_reports_the_335():
    from mast.api.routes import instrument_init as route
    from mast.core import instrument_init as ii

    try:
        heater_control.set_source(lambda: object())
        assert route._stored_values(None)[ii.RUNTIME_TEMPERATURE_CONTROLLER] == "lakeshore_335"
        heater_control.set_source(lambda: None)
        assert route._stored_values(None)[ii.RUNTIME_TEMPERATURE_CONTROLLER] == "none"
        heater_control.set_source(None)
        assert ii.RUNTIME_TEMPERATURE_CONTROLLER not in route._stored_values(None)
    finally:
        heater_control.set_source(None)
