"""自检：tests/v2/unit/core/conftest.py 的守卫真的在拦本机 Nanonis 端口。

一条什么都匹配不到的守卫和一条全绿的守卫，输出一模一样 —— 所以这里亲手去连一次，确认被拦、
被记下；再连一个保证没人监听的端口，确认守卫不误伤正常的「连不上」。
"""

from __future__ import annotations

import socket

import pytest


@pytest.mark.parametrize("port", [6501, 6502, 6503, 6504, 3364])
def test_a_local_nanonis_port_is_blocked_and_recorded(request, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(ConnectionRefusedError, match="不许连本机 Nanonis 端口"):
            s.connect(("127.0.0.1", port))
        assert s.connect_ex(("localhost", port)) != 0
    finally:
        s.close()
    attempts = request.node._nanonis_port_attempts
    assert attempts == [("127.0.0.1", port), ("localhost", port)]
    # 清掉：否则守卫会在收尾时判本测试失败 —— 那正是它对真正的违规者该做的
    attempts.clear()


def test_an_unused_port_is_left_alone(request, unused_tcp_ports):
    (port,) = unused_tcp_ports(1)
    assert port not in (6501, 6502, 6503, 6504, 3364)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2.0)
    try:
        with pytest.raises(OSError) as ei:
            s.connect(("127.0.0.1", port))
        assert "不许连" not in str(ei.value)          # 是系统的「拒绝连接」，不是守卫
    finally:
        s.close()
    assert request.node._nanonis_port_attempts == []
