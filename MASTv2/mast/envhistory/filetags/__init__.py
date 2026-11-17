# -*- coding: utf-8 -*-
"""数据文件的环境标签：保存时把当时已知的环境（温度、磁场、真空、液位……）附在文件旁边。

设计文档：``docs/v2/design/data_env_tags.md``

数据文件本体一律不改。每个新存下的 ``.sxm / .dat / .3ds`` 旁边写
``.mast/envtags/<完整文件名>.json``（schema ``mast.envtags/1``，不覆盖、原子），同时在环境历史库
的 ``file_env_tags`` 表里记一份中央记录（按文件身份查）。图库读它们来显示与筛选。

布局::

    kinds.py     纯函数：传感器 → 种类（按驱动类，退到按单位）、规范单位、主值、有效数字
    ring.py      EnvironmentMonitor 的 sink：每个传感器最近约 2 h 的逐条读数（内存，有界）
    acq.py       数据文件头 → 采集窗口（与图库同一套时刻解析）
    snapshot.py  组装一份记录（来源全部注入）
    sidecar.py   sidecar 路径、身份、不覆盖写、改名留存、读与校验
    settings.py  开关与新鲜度阈值（图库 config.json，带 TTL）
    tagger.py    队列 + 按需工作线程：钩子只 submit，I/O 全在工作线程

不变式（与 :mod:`mast.envhistory` 同三条，外加最要紧的一条）：

* **取值只读内存缓存与环境历史库，绝不碰设备**：不调传感器的任何方法（基类的
  ``is_healthy()`` 里面就是 ``read()``），不调 ``read_all()``，不拿任何设备链路锁。读数过期了就
  照记读数时刻与早了多久，**绝不为了刷新去重读**；
* 不新增采样；不判级；不反噬 —— 失败只记日志，保存与技能永远不等它。
"""
from __future__ import annotations

SCHEMA = "mast.envtags/1"

__all__ = ["SCHEMA", "get_tagger", "set_tagger", "ReadingRing", "FileEnvTagger"]


def __getattr__(name: str):
    """惰性导出：``import mast.envhistory.filetags`` 不拉别的模块。"""
    if name in ("get_tagger", "set_tagger", "FileEnvTagger"):
        from mast.envhistory.filetags import tagger
        return getattr(tagger, name)
    if name == "ReadingRing":
        from mast.envhistory.filetags.ring import ReadingRing
        return ReadingRing
    raise AttributeError(name)
