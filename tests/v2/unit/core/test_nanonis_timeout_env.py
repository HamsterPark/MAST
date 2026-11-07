"""``MAST_NANONIS_TIMEOUT_S``:Nanonis recv 超时的外部入口。

从前这个超时写死 5 s,而 7.0 要连的 stmsim 模拟器同步渲染一帧要 2–15 s ——
一次慢渲染就让 recv 超时、迟到的回包落在没人读的 socket 上、流从此错位。

这里钉住三件事:
  1. 合法值生效,而且是**构造时**读的(``default_factory``),不是 import 时;
  2. 非法值**忽略并保留默认**、说一声,不夹紧到边界(与端口变量同一个态度);
  3. 读到的值真的落到了两个消费者上:socket 的 recv 超时(连上与重连两条路),
     以及 ``nanonis_patch`` 每次往返后恢复的那个默认值。

第 3 条是关键:只断言 ``NanonisConfig().timeout_s`` 会让这个测试在消费者
改成读别的东西之后依然绿。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

_MASTV2_ROOT = str(Path(__file__).resolve().parents[4] / "MASTv2")
if sys.path[0] != _MASTV2_ROOT:
    while _MASTV2_ROOT in sys.path:
        sys.path.remove(_MASTV2_ROOT)
    sys.path.insert(0, _MASTV2_ROOT)

from mast.config import NANONIS_TIMEOUT_RANGE_S, NanonisConfig  # noqa: E402

_VAR = "MAST_NANONIS_TIMEOUT_S"


def test_unset_keeps_the_five_second_default(monkeypatch):
    monkeypatch.delenv(_VAR, raising=False)
    assert NanonisConfig().timeout_s == 5.0


@pytest.mark.parametrize("raw, expected", [
    ("15", 15.0),
    ("2.5", 2.5),
    (" 30 ", 30.0),
    ("0.5", 0.5),        # 下边界本身合法
    ("600", 600.0),      # 上边界本身合法
])
def test_a_valid_value_is_used(monkeypatch, raw, expected):
    monkeypatch.setenv(_VAR, raw)
    assert NanonisConfig().timeout_s == expected


def test_it_is_read_per_construction_not_at_import(monkeypatch):
    """``default_factory``:测试 / 启动器在 import 之后才设变量,也要生效。"""
    monkeypatch.setenv(_VAR, "12")
    assert NanonisConfig().timeout_s == 12.0
    monkeypatch.setenv(_VAR, "7")
    assert NanonisConfig().timeout_s == 7.0


@pytest.mark.parametrize("raw", [
    "abc", "5s", "nan", "inf", "-inf", "1e400",
    "0", "-1", "0.49", "600.01", "0.05", "6000",
])
def test_an_invalid_value_keeps_the_default_and_says_so(monkeypatch, caplog, raw):
    """不夹紧:写 0.05 的人要的不是 0.5,写 6000 的人要的也不是 600。"""
    monkeypatch.setenv(_VAR, raw)
    with caplog.at_level(logging.WARNING, logger="mast.config"):
        val = NanonisConfig().timeout_s
    assert val == 5.0, f"{raw!r} 应被忽略,却得到 {val}"
    assert any(_VAR in r.getMessage() for r in caplog.records), (
        f"{raw!r} 被静默忽略了 —— 操作员会以为设置生效了")


def test_the_range_stays_below_the_patch_leak_threshold():
    """上限必须低于 nanonis_patch 判定「库泄漏的假超时」的值,否则设了也白设:
    patch 会在每次往返后把它当成泄漏换回 5 s。"""
    from mast.core import connection, nanonis_patch

    lo, hi = NANONIS_TIMEOUT_RANGE_S
    assert 0 < lo < 5.0 < hi
    assert hi < nanonis_patch._LIB_BOGUS_TIMEOUT_S
    assert hi <= connection._MAX_CALL_RECV_TIMEOUT_S


class _Sock:
    """只记 settimeout;connect 什么都不做(不碰网络)。"""

    made: list["_Sock"] = []

    def __init__(self, *args, **kwargs):
        self.timeouts: list[float] = []
        _Sock.made.append(self)

    def settimeout(self, value):
        self.timeouts.append(value)

    def connect(self, addr):
        pass

    def close(self):
        pass


def test_the_value_reaches_the_sockets_and_the_send_patch(monkeypatch):
    from mast.core import connection, nanonis_patch

    monkeypatch.setenv(_VAR, "15")
    # 模块级全局:先登记原值,测试结束由 monkeypatch 还原。
    monkeypatch.setattr(nanonis_patch, "_default_recv_timeout_s",
                        nanonis_patch._default_recv_timeout_s)
    monkeypatch.setattr(connection.socket, "socket", _Sock)
    monkeypatch.setattr(connection, "Nanonis", lambda sock: object())
    _Sock.made = []

    cfg = NanonisConfig()
    pool = connection.ConnectionPool(cfg)
    results = pool.connect_all()

    assert all(results.values()), results
    assert len(_Sock.made) == 4
    for sock in _Sock.made:
        # 先用短的 connect 超时,连上之后换成配置的 recv 超时
        assert sock.timeouts == [cfg.connect_timeout_s, 15.0]
    assert nanonis_patch._default_recv_timeout_s == 15.0

    # 重连那条路(_reconnect_role)也读同一个值
    with pool._struct_lock:
        pool._connections.pop("main")
    _Sock.made = []
    assert pool._reconnect_role("main") is True
    assert _Sock.made[0].timeouts == [cfg.connect_timeout_s, 15.0]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
