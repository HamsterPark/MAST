# -*- coding: utf-8 -*-
"""短回包的容错：整条 ``Scan.PropsGet`` 布局对不上时，仍救回开头的 continuous。

═══════════════════════════════════════════════════════════════════════════
部分固件的 Nanonis V5e 回包比声明的 16 字段规格短，整条解析越界
═══════════════════════════════════════════════════════════════════════════

服务日志里曾反复出现::

    Scan_PropsGet 读失败: response layout mismatch: declared
    ['I','I','I','i','*-c','i','*-c','i','i','*+c','i','*+i','i','i','*2c','I']
    overran a 69-byte reply ...

链路：整条布局越界 ⇒ ``Variables`` 读空 ⇒ ``imaging._scan_props_continuous`` 读不到
continuous ⇒ ``StartScan`` 的连续闸门走 None 分支 **拒绝起扫** ⇒ 现场表现为「连续
扫描明明关着，MAST 却说读不到、不肯扫」。

修法（``nanonis_patch._patched_Scan_PropsGet`` + ``_salvage_scan_props_leading``）：
continuous/bouncy/autosave 是 body 前 12 字节的三个定长 uint32，与后面变长数组怎么排
无关 —— 整条规格对不上时退一步只取这三个，让闸门至少读得到 continuous。
"""
from __future__ import annotations

import struct

import pytest

from mast.core import nanonis_patch as NP


def _short_body(cont: int) -> bytes:
    """一份会让 16 字段规格**读越界**的短回包（仿真机）。

    开头三个定长标志（continuous / bouncy=0 / autosave=1）+ 空 series + 空 comment
    + status=0 的错误尾；**没有** modules/params/autopaste 段，所以完整规格会读过尾部。
    """
    return (
        struct.pack(">III", cont, 0, 1)   # continuous / bouncy / autosave
        + struct.pack(">i", 0)            # series name size = 0（空串）
        + struct.pack(">i", 0)            # comment size = 0（空串）
        + struct.pack(">ii", 0, 0)        # 错误尾：status=0, desc_len=0 → 无错误
    )


class _Fake:
    """只借解码助手与真的 parseError —— 纯字节就能复现这条链路（同 2D 参数那个测试）。"""

    displayInfo = 0
    decodeStringPrepended = NP.Nanonis.decodeStringPrepended
    decodeSingularString = NP.Nanonis.decodeSingularString
    decodeArray = NP.Nanonis.decodeArray
    decodeArrayPrepended = NP.Nanonis.decodeArrayPrepended
    parseError = NP._patched_parseError


@pytest.fixture(autouse=True)
def _restore_the_upstream_parse_error(monkeypatch):
    """把 ``_original_parseError`` 换回真的两行实现（否则 MagicMock 让断言空转，
    见 ``test_scan_props_2d_parameters.py`` 里的详细说明）。"""
    def _upstream(self, response, index):
        return bytes(response)[index + 8:].decode(errors="replace")

    monkeypatch.setattr(NP, "_original_parseError", _upstream)


class _FakeQS:
    """一个只有 ``quickSend`` 的假客户端：直接回预置的三元组（模拟真机那一发的结果）。"""

    def __init__(self, triple):
        self._triple = triple
        self.cmds: list = []

    def quickSend(self, command, body, body_types, response_types):
        self.cmds.append(command)
        return self._triple


# ── _salvage_scan_props_leading：纯函数 ───────────────────────────────────────

@pytest.mark.parametrize("cont", [0, 1])
def test_salvage_reads_continuous_both_states(cont):
    out = NP._salvage_scan_props_leading(_short_body(cont))
    assert out == [cont, 0, 1]


def test_salvage_bails_on_garbage_leading_bytes():
    # 前 12 字节不是合理的 0/1/2 标志（布局被整体打乱）⇒ 不信，返回 None。
    body = struct.pack(">III", 999, 0, 0) + struct.pack(">ii", 0, 0)
    assert NP._salvage_scan_props_leading(body) is None


def test_salvage_bails_when_too_short():
    assert NP._salvage_scan_props_leading(b"\x00\x00\x00\x01") is None
    assert NP._salvage_scan_props_leading(b"") is None


# ── _patched_Scan_PropsGet：只在「布局不符且 Variables 空」时救 ────────────────

def test_patched_scan_propsget_salvages_leading_flags():
    body = _short_body(cont=0)
    mismatch = [NP.LAYOUT_MISMATCH_PREFIX + ": declared [...] overran a "
                "28-byte reply ...", body, []]
    client = _FakeQS(mismatch)
    out = NP._patched_Scan_PropsGet(client)
    # 错误被清掉（connection.py 只有 ret[0] 非空才当 error；清掉下游才会读 Variables）。
    assert out[0] == ""
    # 只带回三个定长标志：continuous=0 / bouncy=0 / autosave=1。
    assert out[2] == [0, 0, 1]
    assert client.cmds == ["Scan.PropsGet"]


def test_patched_scan_propsget_passes_through_when_spec_fits():
    # Variables 非空（规格对得上）⇒ 原样返回，零行为变化，不触发 salvage。
    good = ["", b"whatever", [1, 0, 1, 0, "Au_%03i"]]
    out = NP._patched_Scan_PropsGet(_FakeQS(good))
    assert out is good


def test_patched_scan_propsget_passes_through_non_layout_errors():
    # 是别的错误（不是布局不符）⇒ 不碰，原样返回，让仪器自己那句话到达调用方。
    other = ["NeedModule: Scan module not loaded", b"", []]
    out = NP._patched_Scan_PropsGet(_FakeQS(other))
    assert out is other


# ── 端到端：短回包 → 整条规格 overrun → salvage 救回（真机上的链路）─────────

def test_short_reply_overruns_full_spec_then_salvage_recovers():
    body = _short_body(cont=0)
    # 1) 完整 16 字段规格在这份短回包上必须失败（复现真机的 LAYOUT_MISMATCH）。
    triple = NP._patched_parseGeneralResponse(_Fake(), body, NP._SCAN_PROPS_GET_SPEC)
    assert triple[0].startswith(NP.LAYOUT_MISMATCH_PREFIX), (
        "短回包本该让完整规格越界，实际: %r" % triple[0][:200])
    assert triple[2] == [], "越界后 Variables 应为空"
    # 2) _patched_Scan_PropsGet 用这份结果应能救回 continuous。
    out = NP._patched_Scan_PropsGet(_FakeQS(triple))
    assert out[0] == ""
    assert out[2][0] == 0


# ── 与消费者对接：救回的 Variables 喂进连续闸门，读得到「关」⇒ 放行 ───────────

def test_salvaged_variables_feed_the_continuous_gate():
    from mast.skills.builtins.imaging import _scan_props_continuous, _continuous_state

    for cont in (0, 1):
        body = _short_body(cont)
        triple = NP._patched_parseGeneralResponse(
            _Fake(), body, NP._SCAN_PROPS_GET_SPEC)
        out = NP._patched_Scan_PropsGet(_FakeQS(triple))
        # imaging 侧照常从 (err, raw, Variables) 里取 continuous。
        assert _scan_props_continuous(out) == cont
    # continuous=0 ⇒ 闸门判定「确认关」⇒ StartScan 放行（而不是 None→拒绝）。
    body0 = _short_body(0)
    out0 = NP._patched_Scan_PropsGet(
        _FakeQS(NP._patched_parseGeneralResponse(_Fake(), body0, NP._SCAN_PROPS_GET_SPEC)))
    assert _continuous_state(_scan_props_continuous(out0)) is False
