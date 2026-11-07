"""7.0.1: ``ZSpectr_Start`` gets a recv timeout that covers the whole Z sweep.

``ZSpectr_Start`` with Get data = 1 blocks on the controller for the whole sweep before it
answers, like ``BiasSpectr_Start``. ``AcquireSTS`` has raised its recv timeout from the
controller's own settings (a 422 s spectrum against a ~5 s socket timeout left
its reply on a socket nobody read and misaligned every later call); ``AcquireZSpectr`` and
``AcquireDeltaFCurve`` never did, so any Z sweep longer than the pool's timeout repeated that.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from mast.core.types import NanonisCallRecord
from mast.skills.builtins.deltaf_curve import AcquireDeltaFCurve
from mast.skills.builtins.spectroscopy import (
    _SWEEP_BUDGET_FACTOR,
    _SWEEP_BUDGET_FALLBACK_S,
    _SWEEP_BUDGET_PAD_S,
    AcquireZSpectr,
)


@dataclass
class FakeCtx:
    canned: dict[str, Any] = field(default_factory=dict)
    calls: list[tuple[str, tuple]] = field(default_factory=list)
    recv_timeouts: dict[str, float | None] = field(default_factory=dict)

    def safe_call(self, method: str, *args, role: str = "main",
                  recv_timeout_s: float | None = None) -> NanonisCallRecord:
        self.calls.append((method, args))
        self.recv_timeouts[method] = recv_timeout_s
        if method in self.canned:
            return NanonisCallRecord(method=method, args=args, return_value=self.canned[method])
        return NanonisCallRecord(method=method, args=args, return_value=("", b"", []))


def _triple(*variables) -> tuple:
    return ("", b"\x00\x00\x00\x00", list(variables))


#: 200 points, backward sweep, 2 sweeps; settling 10 ms + integration 20 ms per point, and
#: 0.6 s of per-sweep overhead -> 200 * 0.03 * 2 * 2 + 0.6 * 2 = 25.2 s.
PROPS = _triple(1, 200, 0, 0, [], 0, 0, [], 2, 0)
TIMING = _triple(0.1, 0.2, 1.0, 0.01, 0.02, 0.1, 0.2)
EXPECTED_CORE_S = 25.2


def _expected_budget(core_s: float) -> float:
    return core_s * _SWEEP_BUDGET_FACTOR + _SWEEP_BUDGET_PAD_S


def test_acquire_zspectr_raises_the_recv_timeout_to_cover_the_sweep():
    ctx = FakeCtx(canned={"ZSpectr_PropsGet": PROPS, "ZSpectr_TimingGet": TIMING})
    res = AcquireZSpectr().execute(ctx, {})
    budget = ctx.recv_timeouts["ZSpectr_Start"]
    assert budget == pytest.approx(_expected_budget(EXPECTED_CORE_S))
    assert budget > 60, "a 25 s sweep must not run against the ~5 s default"
    names = [m for m, _ in ctx.calls]
    assert names.index("ZSpectr_PropsGet") < names.index("ZSpectr_Start")
    assert res.data.get("sweep_estimate_s") == pytest.approx(EXPECTED_CORE_S)


def test_unreadable_settings_fall_back_to_the_long_side():
    """「读不到」不是「很快」: an unreadable sweep gets the conservative ceiling, never the default."""
    ctx = FakeCtx(canned={"ZSpectr_PropsGet": ("", b"", []), "ZSpectr_TimingGet": ("", b"", [])})
    AcquireZSpectr().execute(ctx, {})
    assert ctx.recv_timeouts["ZSpectr_Start"] == pytest.approx(
        _expected_budget(_SWEEP_BUDGET_FALLBACK_S))


def test_one_element_sequences_in_the_reply_are_read_as_their_value():
    """Nanonis numeric fields can arrive as 1-element tuples (the real-rig shape)."""
    props = _triple((1,), (200,), 0, 0, [], 0, 0, [], (2,), 0)
    timing = _triple(*[(x,) for x in (0.1, 0.2, 1.0, 0.01, 0.02, 0.1, 0.2)])
    ctx = FakeCtx(canned={"ZSpectr_PropsGet": props, "ZSpectr_TimingGet": timing})
    AcquireZSpectr().execute(ctx, {})
    assert ctx.recv_timeouts["ZSpectr_Start"] == pytest.approx(_expected_budget(EXPECTED_CORE_S))


def test_the_deltaf_curve_uses_the_same_budget():
    ctx = FakeCtx(canned={
        "PLL_OutOnOffGet": _triple(1),
        "PLL_CenterFreqGet": _triple(25000.0),
        "PLL_FreqShiftGet": _triple(-1.5),
        "PLL_AmpCtrlSetpntGet": _triple(1e-10),
        "ZSpectr_PropsGet": PROPS,
        "ZSpectr_TimingGet": TIMING,
    })
    AcquireDeltaFCurve().execute(ctx, {"z_sweep_distance_m": 5e-10, "channel_indexes": "1,2,3"})
    assert ctx.recv_timeouts.get("ZSpectr_Start") == pytest.approx(
        _expected_budget(EXPECTED_CORE_S))
