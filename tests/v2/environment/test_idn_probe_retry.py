"""身份探测在同一波特率问两次：上一个波特率留下的乱码不能让 625 认不出来。

7.2.0 启动时曾出现 Model 625（9600 7O1）
「有回应但认不出」，之后手动扫描又认出来了。探测顺序是 57600 → 9600 → 115200：
57600 发出的 ``*IDN?`` 到了 9600 的仪器那里是乱码；乱码里没解出行结束符时，它留在仪器的
命令缓冲里，9600 那一次正经的 ``*IDN?`` 跟它拼成一条无效命令，没有回答。

修法：每个候选波特率问两次 ``*IDN?``。第一次的 CR LF 把缓冲清掉，第二次得到回答。
发出去的仍然**只有** ``*IDN?``（见 test_magnet_and_level_meter 的同名约束）。
"""
from __future__ import annotations

import logging

import pytest

from mast.environment import autodetect, lakeshore_temp
from mast.environment.lakeshore_temp import IDN_ATTEMPTS, IDN_RETRY_TIMEOUT_S, probe_lakeshore

IDN_625 = b"LSCI,MODEL625,6251234,1.5\r\n"


class _Rs232Magnet:
    """9600 7O1 的 625。别的波特率发来的字节到它这里是不带行结束符的乱码，留在命令缓冲里，
    让下一条命令作废（不回答）；作废那一行自己的 CR LF 把缓冲清掉。"""

    def __init__(self) -> None:
        self.dirty = False
        self.log: list[tuple[int, bytes, float | None]] = []

    def open(self, settings):
        dev = self

        class _T:
            def transact(self, payload, **k):
                dev.log.append((settings.baudrate, bytes(payload), k.get("timeout")))
                if settings.baudrate != 9600:
                    dev.dirty = True
                    return b""
                if dev.dirty:
                    dev.dirty = False
                    return b""
                return IDN_625

            def close(self):
                pass

        return _T()


@pytest.fixture()
def magnet(monkeypatch):
    dev = _Rs232Magnet()
    monkeypatch.setattr(lakeshore_temp, "open_transport", dev.open)
    return dev


def test_the_junk_from_a_wrong_baud_no_longer_hides_the_625(magnet):
    info = probe_lakeshore("COM13")
    assert info is not None and info.model == "MODEL625"
    assert info.settings.baudrate == 9600
    assert {p for _b, p, _t in magnet.log} == {b"*IDN?\r\n"}, "探测只许发 *IDN?"


def test_the_fake_reproduces_the_field_miss_with_a_single_query(magnet, monkeypatch):
    """替身确实再现了那种漏认：每个波特率只问一次（7.2.0 的行为）就认不出。"""
    monkeypatch.setattr(lakeshore_temp, "IDN_ATTEMPTS", 1)
    assert probe_lakeshore("COM13") is None


def test_identify_port_reports_the_625(magnet, monkeypatch):
    monkeypatch.setattr(autodetect, "probe_lm510", lambda port, **k: None)
    kind, _info, heard = autodetect.identify_port("COM13")
    assert kind == autodetect.KIND_LAKESHORE_625 and heard is True


def test_a_silent_port_costs_one_short_repeat_per_baud(monkeypatch):
    calls: list[tuple[int, float | None]] = []

    def _silent(settings):
        class _T:
            def transact(self, payload, **k):
                calls.append((settings.baudrate, k.get("timeout")))
                return b""

            def close(self):
                pass

        return _T()

    monkeypatch.setattr(lakeshore_temp, "open_transport", _silent)
    assert probe_lakeshore("COM1", timeout=0.5) is None
    n = len(lakeshore_temp.LSCI_SERIAL_CANDIDATES)
    assert len(calls) == n * IDN_ATTEMPTS
    repeats = [t for i, (_b, t) in enumerate(calls) if i % IDN_ATTEMPTS]
    assert repeats and all(t is not None and t <= IDN_RETRY_TIMEOUT_S for t in repeats)


def test_unidentified_replies_are_logged_with_their_bytes(monkeypatch, caplog):
    """「有回应但认不出」要带上收到的字节，下次才分得清是截断、乱码还是别的仪器。"""
    def _junk(settings):
        class _T:
            def transact(self, payload, **k):
                return b"\x8f\xfeLS"

            def close(self):
                pass

        return _T()

    monkeypatch.setattr(lakeshore_temp, "open_transport", _junk)
    monkeypatch.setattr(autodetect, "probe_lm510", lambda port, **k: None)
    monkeypatch.setattr(autodetect, "probe_model625", lambda port, **k: None)
    with caplog.at_level(logging.INFO, logger="mast.environment.autodetect"):
        kind, _info, heard = autodetect.identify_port("COM19")
    assert kind == "" and heard is True
    assert any("replies heard but not identified" in r.getMessage() and "\\x8f\\xfeLS" in r.getMessage()
               for r in caplog.records), [r.getMessage() for r in caplog.records]
