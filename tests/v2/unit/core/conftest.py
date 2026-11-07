"""tests/v2/unit/core 的共用夹具：**单元测试绝不连本机的 Nanonis 端口**。

为什么：开发环境里 Nanonis Mimea 模拟器常开在 127.0.0.1:6501–6504 / 3364，而这几个口
同一时刻只服务一个客户端。有三条连接池测试按「没人在听」的假设去 ``connect_all()`` / 重连默认
端口 —— 模拟器开着时它们真连上了，占住、再随手关掉别人正在用的口。
``nanonis_spm`` 在测试里是 mock，不会发协议字节，但「占口 + 粗暴关闭」本身就是个陷阱。

- ``unused_tcp_ports(n)``：给 n 个**保证没人监听**的本机端口（先绑临时端口、再关掉）。要测
  「连不上」的测试用它，别用 6501–6504 的默认值。
- 自动生效的守卫：这个目录下任何测试去连本机 6501–6504 / 3364，connect 被拦（抛
  ConnectionRefusedError，和「模拟器没开」一样），测试结束时**判失败**并说清楚怎么改 ——
  只拦、不报的话，下一条这样的测试照样会在模拟器开着时连上去。
"""

from __future__ import annotations

import socket

import pytest

_NANONIS_PORTS = frozenset({6501, 6502, 6503, 6504, 3364})
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0", ""})


@pytest.fixture
def unused_tcp_ports():
    """``unused_tcp_ports(n=4) -> [port, …]``：n 个此刻没人监听的本机 TCP 端口。"""

    def _get(n: int = 4) -> list[int]:
        socks = []
        try:
            for _ in range(int(n)):
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.bind(("127.0.0.1", 0))
                socks.append(s)
            return [s.getsockname()[1] for s in socks]
        finally:
            for s in socks:
                s.close()

    return _get


@pytest.fixture(autouse=True)
def _no_real_nanonis_ports(monkeypatch, request):
    attempts: list[tuple[str, int]] = []
    orig_connect = socket.socket.connect
    orig_connect_ex = socket.socket.connect_ex

    def _hit(addr) -> tuple[str, int] | None:
        try:
            host, port = str(addr[0]), int(addr[1])
        except Exception:  # noqa: BLE001 — 不是 (host, port) 形状的地址，放行
            return None
        if port in _NANONIS_PORTS and host in _LOCAL_HOSTS:
            return host, port
        return None

    def _connect(self, addr):
        hit = _hit(addr)
        if hit:
            attempts.append(hit)
            raise ConnectionRefusedError(
                f"[tests/v2/unit/core] 单元测试不许连本机 Nanonis 端口 {hit[0]}:{hit[1]}")
        return orig_connect(self, addr)

    def _connect_ex(self, addr):
        hit = _hit(addr)
        if hit:
            attempts.append(hit)
            return 10061                      # WSAECONNREFUSED
        return orig_connect_ex(self, addr)

    monkeypatch.setattr(socket.socket, "connect", _connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _connect_ex)
    request.node._nanonis_port_attempts = attempts      # 给守卫的自检用
    yield
    if attempts:
        ports = sorted({p for _h, p in attempts})
        pytest.fail(
            f"{request.node.nodeid} 去连了本机的 Nanonis 端口 {ports}（{len(attempts)} 次）。"
            "模拟器开着时这会真连上、占住别人的口。要测「连不上」就用 unused_tcp_ports 夹具给的"
            "端口（tests/v2/unit/core/conftest.py），或者 patch socket.connect（照 "
            "test_connection_backoff.py）。", pytrace=False)
