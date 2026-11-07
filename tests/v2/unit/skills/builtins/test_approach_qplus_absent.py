"""An approach on a rig with no qPlus channel carries no qPlus block.

On Nanonis Mimea (a plain STM) every ApproachTip result carried
``qplus_status: unavailable`` and 「读不到 PLL 激励状态 —— 判不了是否撞针」: the
crash check asks the PLL first, and a rig without one cannot answer.
``_approach_evidence`` always meant such a rig to contribute nothing.

The criterion is the controller's own signal table, read and found without an
oscillation-amplitude channel. Every "cannot tell" keeps the block as before —
those tests are guards and pass on the old code too; only the first one changes.
"""
from __future__ import annotations

import pytest

from mast.core.types import NanonisCallRecord
from mast.skills.builtins import approach as ap
from mast.skills.builtins import qplus_amplitude as qa

_STM_SIGNALS = ["Bias (V)", "Current (A)", "Z (m)", "Input 1 (V)", "LI Demod 1 X (A)"]
_QPLUS_SIGNALS = _STM_SIGNALS + ["OC M1 Freq. Shift (Hz)", "OC M1 Amplitude (m)"]


class _Ctx:
    def __init__(self, names=None, names_error: str = ""):
        self.names = list(names or [])
        self.names_error = names_error
        self.sent: list[str] = []

    def safe_call(self, method, *args, **kwargs):
        self.sent.append(method)
        if method == "Signals_NamesGet":
            if self.names_error:
                return NanonisCallRecord(method=method, error=self.names_error)
            return NanonisCallRecord(method=method,
                                     return_value=("", b"", [len(self.names), self.names]))
        # no PLL on any of these rigs: the crash check cannot read its excitation
        return NanonisCallRecord(method=method, error=f"{method}: no such module")


@pytest.fixture(autouse=True)
def _no_amplitude_index_on_file(monkeypatch):
    monkeypatch.setattr(qa, "_remembered_index", lambda: None)


def _qplus_keys(ev: dict) -> list[str]:
    return [k for k in ev if k.startswith("qplus")]


# ── behaviour: what an approach result carries ──────────────────────────────

def test_no_amplitude_channel_means_no_qplus_block():
    ctx = _Ctx(_STM_SIGNALS)
    ev = ap._approach_evidence(ctx)
    assert not _qplus_keys(ev), ev
    assert "PLL_OutOnOffGet" not in ctx.sent, "nothing to ask a PLL that is not there"


def test_an_amplitude_channel_keeps_the_witness():
    ev = ap._approach_evidence(_Ctx(_QPLUS_SIGNALS))
    assert ev.get("qplus_status") == "unavailable", ev   # the PLL cannot be read here
    assert "qplus_note" in ev


@pytest.mark.parametrize("ctx", [_Ctx(names_error="Signals_NamesGet: timed out"),
                                 _Ctx(names=[])],
                         ids=["unreadable table", "empty table"])
def test_cannot_tell_keeps_the_block(ctx):
    assert ap._approach_evidence(ctx).get("qplus_status") == "unavailable"


def test_an_amplitude_index_on_file_keeps_the_block(monkeypatch):
    monkeypatch.setattr(qa, "_remembered_index", lambda: 42)
    assert ap._approach_evidence(_Ctx(_STM_SIGNALS)).get("qplus_status") == "unavailable"


# ── the criterion itself ─────────────────────────────────────────────────────

def test_the_criterion_answers_absent_only_on_a_clean_read():
    assert ap._qplus_channel_absent(_Ctx(_STM_SIGNALS))
    assert not ap._qplus_channel_absent(_Ctx(_QPLUS_SIGNALS))
    assert not ap._qplus_channel_absent(_Ctx(names_error="boom"))
    assert not ap._qplus_channel_absent(_Ctx(names=[]))


def test_an_index_on_file_is_answered_without_asking_the_controller(monkeypatch):
    monkeypatch.setattr(qa, "_remembered_index", lambda: 42)
    ctx = _Ctx(_STM_SIGNALS)
    assert not ap._qplus_channel_absent(ctx)
    assert "Signals_NamesGet" not in ctx.sent


def test_a_failing_probe_is_never_absent():
    class _Boom:
        def safe_call(self, *a, **k):
            raise RuntimeError("controller gone")

    assert not ap._qplus_channel_absent(_Boom())
