"""Spectroscopy skills redo an IDEMPOTENT call once after a transport failure —
and only those.

On Nanonis Mimea the first ConfigureSTS after the simulator started failed: the
pool's own resend of ``BiasSpectr_LimitsSet`` was reset by the still-busy
controller, while another caller reconnecting 20 ms later succeeded. Setting a
property to an absolute value twice leaves the controller where once would, so
those calls get one more try after a pause. ``*_Start`` runs a sweep: never.
"""
from __future__ import annotations

import socket
from unittest.mock import MagicMock, patch

import pytest

from mast.core.connection import _mark_transport, is_transport_failure
from mast.core.types import NanonisCallRecord
from mast.skills.builtins import spectroscopy as sp

_OK = ("", b"", [])


@pytest.fixture(autouse=True)
def _no_pause(monkeypatch):
    # raising=False: run against a version without the retry, the assertions —
    # not this fixture — must be what fails.
    monkeypatch.setattr(sp, "_IDEMPOTENT_RETRY_PAUSE_S", 0.0, raising=False)


def _transport(method: str, text: str = "'X' timed out: the controller sent no reply within 5.0 s"):
    rec = NanonisCallRecord(method=method, error=text)
    _mark_transport(rec)
    return rec


class _Ctx:
    """A context whose replies are scripted per command; unscripted ones are OK."""

    def __init__(self, script: dict | None = None):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.sent: list[str] = []

    def safe_call(self, method, *args, **kwargs):
        self.sent.append(method)
        todo = self.script.get(method)
        if todo:
            return todo.pop(0)
        return NanonisCallRecord(method=method, args=args, return_value=_OK)


_STS = {"start_v": -1.0, "end_v": 1.0, "num_points": 128}


def test_configure_sts_redoes_a_setter_after_a_transport_failure():
    ctx = _Ctx({"BiasSpectr_LimitsSet": [_transport("BiasSpectr_LimitsSet")]})
    res = sp.ConfigureSTS().execute(ctx, dict(_STS))
    assert res.success, res.error
    # 没传 num_sweeps 时，ConfigureSTS 先 BiasSpectr_PropsGet 读回当前扫掠数
    # 以**保持操作员的设置**（读不到才退回 1），再 PropsSet —— 所以序列里多了这一次
    # 读（它也走 _call_idempotent，这里没让它失败，故只出现一次）。
    assert ctx.sent == ["BiasSpectr_Open", "BiasSpectr_LimitsSet", "BiasSpectr_LimitsSet",
                        "BiasSpectr_PropsGet", "BiasSpectr_PropsSet", "BiasSpectr_AdvPropsSet"]
    assert len(res.nanonis_calls) == 6, "the failed attempt stays on the record"


def test_an_application_error_is_not_redone():
    locked = NanonisCallRecord(method="BiasSpectr_LimitsSet", error="Access Denied: Parameter is Locked")
    ctx = _Ctx({"BiasSpectr_LimitsSet": [locked]})
    res = sp.ConfigureSTS().execute(ctx, dict(_STS))
    assert not res.success
    assert res.error == "Access Denied: Parameter is Locked"
    assert ctx.sent == ["BiasSpectr_Open", "BiasSpectr_LimitsSet"]


def test_a_second_transport_failure_reports_both_attempts():
    first = _transport("BiasSpectr_LimitsSet", "'BiasSpectr_LimitsSet' timed out: no reply within 5.0 s")
    second = _transport("BiasSpectr_LimitsSet", "ConnectionResetError: [WinError 10054]")
    ctx = _Ctx({"BiasSpectr_LimitsSet": [first, second]})
    res = sp.ConfigureSTS().execute(ctx, dict(_STS))
    assert not res.success
    assert "WinError 10054" in res.error and "first attempt" in res.error
    assert "timed out" in res.error, "the cause is not lost"
    assert ctx.sent.count("BiasSpectr_LimitsSet") == 2, "once more, not until it works"


def test_the_sweep_start_is_never_resent():
    for skill, start in ((sp.AcquireSTS(), "BiasSpectr_Start"), (sp.AcquireZSpectr(), "ZSpectr_Start")):
        ctx = _Ctx({start: [_transport(start)]})
        res = skill.execute(ctx, {})
        assert not res.success
        assert ctx.sent.count(start) == 1, f"{start} would run a second sweep"


def test_the_idempotent_steps_of_the_acquire_skills_are_redone():
    ctx = _Ctx({"BiasSpectr_PropsGet": [_transport("BiasSpectr_PropsGet")],
                "BiasSpectr_Start": [NanonisCallRecord(method="BiasSpectr_Start", error="stop here")]})
    sp.AcquireSTS().execute(ctx, {})
    assert ctx.sent[:4] == ["BiasSpectr_Open", "BiasSpectr_PropsGet", "BiasSpectr_PropsGet",
                            "BiasSpectr_TimingGet"]

    ctx2 = _Ctx({"ZSpectr_PropsSet": [_transport("ZSpectr_PropsSet")],
                 "ZSpectr_Start": [NanonisCallRecord(method="ZSpectr_Start", error="stop here")]})
    sp.AcquireZSpectr().execute(ctx2, {})
    # 7.0.1: like the bias sweep, the Z sweep reads its settings for the recv budget first
    assert ctx2.sent == ["ZSpectr_Open", "ZSpectr_PropsSet", "ZSpectr_PropsSet",
                         "ZSpectr_PropsGet", "ZSpectr_TimingGet", "ZSpectr_Start"]


def test_the_configure_family_redoes_its_setter():
    cases = (
        (sp.ConfigureZSpectr(), {"z_offset_m": 0.0, "z_sweep_distance_m": 1e-9, "num_points": 64},
         "ZSpectr_RangeSet"),
        (sp.ConfigureSTSTiming(), {"z_avg_time_s": 0.1, "init_settling_s": 0.01,
                                   "max_slew_rate_v_s": 10.0, "settling_s": 0.001,
                                   "integration_s": 0.01}, "BiasSpectr_TimingSet"),
        (sp.ConfigureSTSChannels(), {"channel_indexes": "0, 1"}, "BiasSpectr_ChsSet"),
        (sp.ConfigureZSpectrTiming(), {"z_avg_time_s": 0.1, "init_settling_s": 0.01,
                                       "max_slew_rate_v_s": 10.0, "settling_s": 0.001,
                                       "integration_s": 0.01}, "ZSpectr_TimingSet"),
    )
    for skill, params, setter in cases:
        ctx = _Ctx({setter: [_transport(setter)]})
        res = skill.execute(ctx, params)
        assert res.success, (type(skill).__name__, res.error)
        assert ctx.sent.count(setter) == 2, type(skill).__name__
        assert len(res.nanonis_calls) == len(ctx.sent), type(skill).__name__


def test_unflagged_records_pass_straight_through():
    """The abort gate and test doubles return plain records: no retry, no change."""
    ctx = _Ctx({"BiasSpectr_LimitsSet": [NanonisCallRecord(method="BiasSpectr_LimitsSet",
                                                           error="aborted by operator")]})
    res = sp.ConfigureSTS().execute(ctx, dict(_STS))
    assert res.error == "aborted by operator"
    assert ctx.sent.count("BiasSpectr_LimitsSet") == 1


# ── end to end through the REAL ConnectionPool ──────────────────────────────

class _FakeConfig:
    host = "localhost"
    port_main = 6501
    port_monitor = 6502
    port_data = 6503
    port_emergency = 6504
    timeout_s = 5.0


class _Mimea:
    """LimitsSet: no reply (timeout), then a reset on the resend, then fine —
    the observed sequence; everything else answers at once."""

    def __init__(self):
        self.limits = [socket.timeout("timed out"),
                       ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接。")]
        self.sent: list[str] = []

    def close(self):
        pass

    def __getattr__(self, name):
        if "_" not in name or name.startswith("__"):
            raise AttributeError(name)

        def _call(*_a):
            self.sent.append(name)
            if name == "BiasSpectr_LimitsSet" and self.limits:
                raise self.limits.pop(0)
            return _OK
        return _call


class _PoolCtx:
    def __init__(self, pool):
        self.pool = pool

    def safe_call(self, method, *args, **kwargs):
        return self.pool.safe_call(method, *args, **kwargs)


def test_the_flag_travels_from_the_real_pool_to_the_skill():
    from mast.core.connection import ConnectionPool

    nn = _Mimea()
    pool = ConnectionPool(_FakeConfig())
    pool._connections["main"] = (MagicMock(spec=socket.socket), nn)
    with patch.object(pool, "_reconnect_role", return_value=True):
        res = sp.ConfigureSTS().execute(_PoolCtx(pool), dict(_STS))
    assert res.success, res.error
    first_limits = [c for c in res.nanonis_calls if c.method == "BiasSpectr_LimitsSet"][0]
    assert is_transport_failure(first_limits)
    assert first_limits.error.startswith("'BiasSpectr_LimitsSet' timed out"), first_limits.error
    assert "within 30.0 s" in first_limits.error, "the call after Open ran under the load grace"
    assert nn.sent.count("BiasSpectr_LimitsSet") == 3   # pool resend + one skill retry
