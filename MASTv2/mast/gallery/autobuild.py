# -*- coding: utf-8 -*-
"""数据图库的自动增量更新：MAST 运行期间定时起一轮增量构建。

定时线程只做一件事：每隔 N 分钟（图库配置里的 ``auto_interval_min``，每轮重新读，改了立刻生效）
调 :func:`mast.gallery.service.start_auto`。**要不要起**（开关、有没有启用的数据根、已有构建在跑、
离上次构建结束不足 1 分钟）全在 ``service`` 里、在锁里判断，这里不重复。

* **启动后第一轮要等** :data:`FIRST_DELAY_S`。启动时的后台 Python 负载会抢占 GIL；7.1.8 就是这样把
  Lake Shore 串口识别线程饿死、没认出磁体电源（7.1.9 已修读取，但没理由再给启动期加负载）。
  串口识别在 ``CoreRuntime`` 初始化里同步做完、早于 API 应用创建，所以这个延迟只是余量，不是在等
  某个信号；同一个等待期也挡住「打开图库页」的触发（``service.hold_auto``）。
* **扫描进行中不暂停**：新数据正是扫描时产生的。对实时回路的限制靠自动构建的线程封顶
  （``service.AUTO_WORKERS``）和「刚写过的文件不收」。
* 关停：:meth:`AutoBuildScheduler.stop` 登记成 ``CoreRuntime`` 的关停钩子 —— 置停止、请在跑的构建
  取消、不等待，关停不被一次构建拖住。
* **pytest 里不会自己起**：:func:`start_for_runtime` 在测试进程里什么也不做；测试直接构造
  :class:`AutoBuildScheduler` 并注入时钟与等待函数。
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from collections.abc import Callable

from mast.gallery import config as _config
from mast.gallery import paths as _paths
from mast.gallery import service as _service

logger = logging.getLogger(__name__)

#: 启动后第一轮之前的等待（秒）。
FIRST_DELAY_S = 180.0
#: 启动等待期（``service.hold_auto``）比第一次睡眠短这么多秒。
HOLD_MARGIN_S = 5.0
#: 间隔睡眠的切片（秒）：每片醒来重新读一次配置，改了间隔或开关不必等满旧间隔。
POLL_SLICE_S = 30.0
#: 环境变量 ``MAST_GALLERY_AUTOBUILD=0`` 整个关掉（不起线程）。
ENV_DISABLE = "MAST_GALLERY_AUTOBUILD"


class AutoBuildScheduler:
    """后台定时线程。``clock`` / ``wait`` 可注入：``wait(秒) -> 是否被叫停``。"""

    def __init__(self, *, first_delay_s: float = FIRST_DELAY_S, slice_s: float = POLL_SLICE_S,
                 clock: Callable[[], float] = time.monotonic,
                 wait: Callable[[float], bool] | None = None,
                 interval_s: Callable[[], float] | None = None) -> None:
        self._first_delay_s = float(first_delay_s)
        self._slice_s = float(slice_s)
        self._clock = clock
        self._stop = threading.Event()
        self._wait = wait or self._stop.wait
        self._interval_s = interval_s or self._configured_interval_s
        self._thread: threading.Thread | None = None
        self.ticks = 0                      # 起过几轮（测试与日志用）

    @staticmethod
    def _configured_interval_s() -> float:
        try:
            return _config.load_config(_paths.layout()).auto_interval_min * 60.0
        except Exception as exc:  # noqa: BLE001 — 读不到配置就按默认间隔，不让线程死
            logger.debug("gallery autobuild: config unreadable (%s); using default interval", exc)
            return _config.AUTO_INTERVAL_DEFAULT_MIN * 60.0

    # ── 生命周期 ────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread is not None:
            return
        # 等待期比线程自己的第一次睡眠短一点：Windows 上定时等待可能比请求的略早醒来，
        # 刚好撞上等待期的末尾会让第一轮白白错过、再等一个完整间隔。
        _service.hold_auto(max(0.0, self._first_delay_s - HOLD_MARGIN_S))
        self._thread = threading.Thread(target=self._loop, name="gallery-autobuild", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """关停钩子：置停止、请在跑的构建取消，**不等**。幂等。"""
        self._stop.set()
        try:
            _service.shutdown()
        except Exception as exc:  # noqa: BLE001
            logger.debug("gallery autobuild: service shutdown failed: %s", exc)

    def join(self, timeout: float | None = None) -> bool:
        th = self._thread
        if th is not None:
            th.join(timeout)
        return th is None or not th.is_alive()

    # ── 主循环 ──────────────────────────────────────────────────────────
    def tick(self) -> bool:
        """起一轮（若条件满足）。永不抛。"""
        try:
            started, _st = _service.start_auto("timer")
        except Exception as exc:  # noqa: BLE001
            logger.warning("gallery autobuild: tick failed: %s", exc)
            return False
        if started:
            self.ticks += 1
            logger.info("gallery autobuild: incremental build started (timer)")
        return started

    def _loop(self) -> None:
        if self._wait(self._first_delay_s):
            return
        while not self._stop.is_set():
            self.tick()
            last = self._clock()
            while True:
                remaining = last + self._interval_s() - self._clock()
                if remaining <= 0:
                    break
                if self._wait(min(self._slice_s, remaining)):
                    return
            if self._stop.is_set():
                return


def start_for_runtime(add_shutdown_hook: Callable | None = None) -> AutoBuildScheduler | None:
    """服务启动时调一次。测试进程里、或 ``MAST_GALLERY_AUTOBUILD=0`` 时什么也不做。"""
    if "pytest" in sys.modules or os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    if os.environ.get(ENV_DISABLE, "").strip().lower() in ("0", "false", "off", "no"):
        logger.info("gallery autobuild disabled by %s", ENV_DISABLE)
        return None
    sched = AutoBuildScheduler()
    if callable(add_shutdown_hook):
        try:
            add_shutdown_hook(sched.stop, name="gallery auto build")
        except Exception as exc:  # noqa: BLE001
            logger.warning("gallery autobuild: 关停钩子登记失败(%s)", exc)
    sched.start()
    logger.info("gallery autobuild: scheduler started (first round in %.0f s)", FIRST_DELAY_S)
    return sched
