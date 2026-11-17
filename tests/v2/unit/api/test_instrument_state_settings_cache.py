"""GET /api/environment/instrument-state 不再每次整扫串口。

设置类读数（625：顺从电压/速率/限值/分段/PSH 参数/失超/模式；335：输入名与类型、
输出设定点/模式/斜坡）缓存 60 s；只在 MAST 自己写之后（mark_sent）或过期后重读。
活值（读数、状态位、加热量程与加热输出、625 设定电流）每次都问。响应形状逐键钉住：
外部 agent 依赖它。
"""

from __future__ import annotations

import pytest

from mast.environment import lakeshore_625 as m625
from mast.environment import lakeshore_temp as lt
from mast.environment.lakeshore_temp import LakeshoreLink, LakeshoreTemperatureSensor
from tests.v2.environment.test_lakeshore_instrument_state import RecordedTransport
from tests.v2.environment.test_magnet_and_level_meter import Fake625
from tests.v2.unit.api.test_admin import _client_with_monitor, _FakeMonitor

SETTINGS_625 = {"SETV?", "RATE?", "RATEP?", "LIMIT?", "RSEG?", "RSEGS?", "TRIG?", "PSHS?",
                "PSHIS?", "QNCH?", "XPGM?", "MODE?"}


def _verbs(fake) -> list[str]:
    return [w.decode().strip().split(" ")[0] for w in fake.writes]


# ═══════════════════════════ 625 ═══════════════════════════


def _link625():
    fake = Fake625()
    return fake, m625.Model625Link(fake, port="COM13", min_gap_s=0.0)


def test_625_second_readout_asks_no_settings_and_is_identical():
    fake, link = _link625()
    first = link.read_state(settings_max_age_s=60.0)
    n_full = len(fake.writes)
    assert n_full >= 25
    fake.writes.clear()
    link._fast = None                                  # 强制：监控缓存也过期了，最坏情形
    link._full = None
    second = link.read_state(settings_max_age_s=60.0)
    assert not (set(_verbs(fake)) & SETTINGS_625), _verbs(fake)
    assert len(fake.writes) <= 9, _verbs(fake)         # 4 (fast) + 5 live
    a, b = first.as_dict(), second.as_dict()
    a.pop("read_at"), b.pop("read_at")
    assert a == b and set(a) == set(b)


def test_625_with_a_fresh_monitor_cache_a_poll_is_five_queries():
    fake, link = _link625()
    link.read_state(settings_max_age_s=60.0)
    link.fast_state(2.0)
    fake.writes.clear()
    link._full = None
    link.read_state(settings_max_age_s=60.0)
    assert sorted(_verbs(fake)) == sorted(["RDGV?", "RDGRV?", "SETI?", "SETF?", "PSH?"])


def test_625_live_values_are_read_every_time():
    fake, link = _link625()
    link.read_state(settings_max_age_s=60.0)
    fake.replies = dict(fake.replies, **{"SETI?": b"+05.0000\r\n", "PSH?": b"1\r\n",
                                         "RDGV?": b"+1.2500\r\n"})
    link._full = None
    st = link.read_state(settings_max_age_s=60.0)
    assert st.setpoint_current_a == pytest.approx(5.0)
    assert st.psh_state_code == 1 and st.voltage_v == pytest.approx(1.25)


def test_625_default_call_is_still_a_complete_fresh_sweep():
    """plan()/start() 的 read_state(max_age_s=0.0) 不能吃缓存：设置全部重问。"""
    fake, link = _link625()
    link.read_state(settings_max_age_s=60.0)
    fake.writes.clear()
    link.read_state(max_age_s=0.0)
    assert SETTINGS_625 <= set(_verbs(fake))
    assert len(fake.writes) >= 25


def test_625_mark_sent_invalidates_the_settings():
    fake, link = _link625()
    link.read_state(settings_max_age_s=60.0)
    link.mark_sent()                                   # MAST 刚写过（SETI / STOP）
    fake.writes.clear()
    link.read_state(settings_max_age_s=60.0)
    assert SETTINGS_625 <= set(_verbs(fake))


def test_625_settings_expire_after_their_age(monkeypatch):
    fake, link = _link625()
    link.read_state(settings_max_age_s=60.0)
    t0, data, un = link._settings
    link._settings = (t0 - 61.0, data, un)             # 61 s 前的整扫
    link._full = None
    fake.writes.clear()
    link.read_state(settings_max_age_s=60.0)
    assert SETTINGS_625 <= set(_verbs(fake))
    # 新整扫已重新入缓存
    fake.writes.clear()
    link._full = None
    link.read_state(settings_max_age_s=60.0)
    assert not (set(_verbs(fake)) & SETTINGS_625)


def test_625_a_reverified_handle_drops_the_settings():
    fake, link = _link625()
    link.read_state(settings_max_age_s=60.0)
    link._verify_identity()
    assert link._settings is None


def test_625_unanswered_settings_stay_listed_from_the_cache():
    fake = Fake625(replies={k: v for k, v in Fake625().replies.items() if k != "QNCH?"})
    link = m625.Model625Link(fake, port="COM13", min_gap_s=0.0)
    full = link.read_state(settings_max_age_s=60.0)
    assert "QNCH?" in full.unanswered
    link._full = None
    again = link.read_state(settings_max_age_s=60.0)
    assert "QNCH?" in again.unanswered


def test_625_sensor_instrument_state_uses_the_cache():
    fake, link = _link625()
    sensor = m625.Model625Sensor(link, name="magnet_field", quantity="field")
    sensor.instrument_state()
    link._full = None
    fake.writes.clear()
    sensor.instrument_state()
    assert not (set(_verbs(fake)) & SETTINGS_625)


# ═══════════════════════════ 335 ═══════════════════════════

SETTINGS_335 = ("*IDN?", "INNAME?", "INTYPE?", "SETP?", "OUTMODE?", "RAMP?")


def _verbs335(t: RecordedTransport) -> list[str]:
    return [w.split(" ")[0] for w in t.written]


def _snap(t, cache, age=60.0, **kw):
    return lt.read_snapshot(t, port="COM13", settings=cache, settings_max_age_s=age, **kw)


def test_335_second_readout_asks_only_live_values_and_is_identical():
    t = RecordedTransport()
    cache = lt.SettingsCache()
    first = _snap(t, cache)
    assert len(t.written) == 23
    t.written.clear()
    second = _snap(t, cache)
    assert not (set(_verbs335(t)) & set(SETTINGS_335)), t.written
    assert len(t.written) == 12
    assert first.as_dict() == second.as_dict()


def test_335_live_values_are_read_every_time():
    t = RecordedTransport()
    cache = lt.SettingsCache()
    _snap(t, cache)
    t.table.update({"KRDG? A": "+4.200", "RANGE? 1": "3", "HTR? 1": "+012.5",
                    "RDGST? B": "016"})
    snap = _snap(t, cache)
    assert snap.channels[0].kelvin == pytest.approx(4.2)
    assert snap.outputs[0].heater_on and snap.outputs[0].heater_pct == pytest.approx(12.5)
    assert snap.channels[1].health == lt.HEALTH_FAULTY


def test_335_link_mark_sent_invalidates_the_settings():
    """MAST 自己写（heater_335 → link.mark_sent）之后，面板读到的是新值。"""
    t = RecordedTransport()
    cache = lt.settings_cache_for(t)
    assert cache is not None
    _snap(t, cache)
    LakeshoreLink(t, port="COM13", min_gap_s=0.0).mark_sent()
    t.table["SETP? 2"] = "+77.000"
    t.written.clear()
    snap = _snap(t, cache)
    assert snap.outputs[1].setpoint == pytest.approx(77.0)
    assert set(SETTINGS_335[1:]) <= set(_verbs335(t))


def test_335_settings_expire_after_their_age():
    t = RecordedTransport()
    cache = lt.SettingsCache()
    _snap(t, cache)
    cache._at -= 61.0
    t.written.clear()
    _snap(t, cache)
    assert len(t.written) == 23


def test_335_a_sweep_with_an_unanswered_setting_is_not_cached():
    t = RecordedTransport()
    t.table["INTYPE? B"] = ""                           # 超时（空回复）
    cache = lt.SettingsCache()
    _snap(t, cache)
    assert cache.fresh(60.0) is None
    t.table["INTYPE? B"] = "1,0,0,0,1"
    t.written.clear()
    _snap(t, cache)
    assert len(t.written) == 23                         # 下一次仍是整扫
    assert cache.fresh(60.0) is not None


def test_335_a_write_during_a_sweep_keeps_the_sweep_out_of_the_cache():
    """整扫不持链路锁：扫到一半 MAST 写了，扫完不能把写之前读到的旧值存进缓存。"""
    class _WriteMidSweep(RecordedTransport):
        def __init__(self, cache):
            super().__init__()
            self.cache = cache

        def transact(self, payload, **kw):
            if payload.decode().strip() == "OUTMODE? 1":
                self.cache.invalidate()                 # 等价于 link.mark_sent()
            return super().transact(payload, **kw)

    cache = lt.SettingsCache()
    t = _WriteMidSweep(cache)
    _snap(t, cache)
    assert cache.fresh(60.0) is None


def test_335_other_channels_do_not_reuse_a_cache_taken_for_a_b():
    t = RecordedTransport()
    cache = lt.SettingsCache()
    _snap(t, cache)
    t.written.clear()
    snap = _snap(t, cache, channels=["A"])
    assert [c.channel for c in snap.channels] == ["A"]
    assert "INNAME?" in _verbs335(t)                    # 重问了，没拿 A+B 的缓存去充


def test_335_no_cache_means_a_full_sweep_as_before():
    t = RecordedTransport()
    lt.read_snapshot(t, port="COM13")
    t.written.clear()
    lt.read_snapshot(t, port="COM13")
    assert len(t.written) == 23


def test_335_sensors_on_one_port_share_the_cache():
    t = RecordedTransport()
    a = LakeshoreTemperatureSensor(name="SPM", port="COM13", channel="A", transport=t)
    b = LakeshoreTemperatureSensor(name="Magnet", port="COM13", channel="B", transport=t)
    a.instrument_state()
    t.written.clear()
    b.instrument_state()
    assert len(t.written) == 12


# ═══════════════════════════ endpoint ═══════════════════════════

MAGNET_KEYS = {"field", "current_a", "setpoint_current_a", "ramping", "ramp_done",
               "any_error", "quench_detected"}
CHANNEL_KEYS = {"label", "kelvin", "health"}
OUTPUT_KEYS = {"output", "range_label", "heater_pct"}


def _both_monitor():
    t335 = RecordedTransport()
    fake625 = Fake625()
    link = m625.Model625Link(fake625, port="COM13", min_gap_s=0.0)
    mon = _FakeMonitor(running=True)
    mon._sensors = {
        "SPM": LakeshoreTemperatureSensor(name="SPM", port="COM13", channel="A", transport=t335),
        "Magnet": LakeshoreTemperatureSensor(name="Magnet", port="COM13", channel="B",
                                             transport=t335),
        "magnet_field": m625.Model625Sensor(link, name="magnet_field", quantity="field"),
        "magnet_current": m625.Model625Sensor(link, name="magnet_current", quantity="current"),
    }
    return mon, t335, fake625, link


def test_endpoint_shape_is_unchanged_and_repeat_calls_skip_the_sweep():
    mon, t335, fake625, link = _both_monitor()
    client = _client_with_monitor(mon)
    r1 = client.get("/api/environment/instrument-state")
    assert r1.status_code == 200, r1.text
    first_cost = (len(t335.written), len(fake625.writes))
    assert first_cost[0] >= 23 and first_cost[1] >= 25
    link._full = None                                   # 5 s 的整扫缓存过了；设置缓存还在
    t335.written.clear()
    fake625.writes.clear()
    r2 = client.get("/api/environment/instrument-state")
    assert r2.status_code == 200
    assert len(t335.written) == 12
    assert not (set(_verbs(fake625)) & SETTINGS_625)

    def strip(body):
        for inst in body["instruments"]:
            if inst.get("magnet"):
                inst["magnet"].pop("read_at", None)
        return body

    assert strip(r1.json()) == strip(r2.json())

    body = r2.json()
    assert body["degraded"] is False
    by_kind = {i["kind"]: i for i in body["instruments"]}
    assert set(by_kind) == {"lakeshore_temp", "lakeshore_625"}
    temp = by_kind["lakeshore_temp"]
    for ch in temp["channels"]:
        assert CHANNEL_KEYS <= set(ch)
    for out in temp["outputs"]:
        assert OUTPUT_KEYS <= set(out)
    assert MAGNET_KEYS <= set(by_kind["lakeshore_625"]["magnet"])
    assert set(temp) >= {"kind", "model", "idn", "port", "any_heater_on", "channels", "outputs"}


def test_endpoint_shows_a_write_immediately():
    mon, t335, fake625, link = _both_monitor()
    client = _client_with_monitor(mon)
    client.get("/api/environment/instrument-state")
    # MAST 自己写了 335 的设定点和 625 的 SETI：两边的链路都 mark_sent
    LakeshoreLink(t335, port="COM13", min_gap_s=0.0).mark_sent()
    link.mark_sent()
    t335.table["SETP? 2"] = "+55.000"
    fake625.replies = dict(fake625.replies, **{"RATE?": b"+0.0500\r\n"})
    body = client.get("/api/environment/instrument-state").json()
    by_kind = {i["kind"]: i for i in body["instruments"]}
    assert by_kind["lakeshore_temp"]["outputs"][1]["setpoint"] == pytest.approx(55.0)
    assert by_kind["lakeshore_625"]["magnet"]["ramp_rate_a_s"] == pytest.approx(0.05)


def test_the_cache_cost_is_paid_once_per_age_not_per_request():
    mon, t335, fake625, link = _both_monitor()
    client = _client_with_monitor(mon)
    for _ in range(5):
        link._full = None
        assert client.get("/api/environment/instrument-state").status_code == 200
    ids = [w for w in t335.written if w.startswith("*IDN?")]
    assert len(ids) == 1
    assert _verbs(fake625).count("RATE?") == 1
