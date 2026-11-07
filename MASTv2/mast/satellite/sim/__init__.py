"""Python 参考卫星（模拟设备）。

    python -m mast.satellite.sim --hub ws://127.0.0.1:7862/ws/satellite/v1 --id sim1 \\
        --token <令牌> --allow-plain-loopback

它既是测试替身，也是前端演示用的数据源，也是 C# 卫星（``satellite/``）的行为参照。
"""

from mast.satellite.sim.client import AuthRejected, SimConfig, SimSatellite, SimThread

__all__ = ["SimConfig", "SimSatellite", "SimThread", "AuthRejected"]
