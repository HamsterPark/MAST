# -*- coding: utf-8 -*-
"""进程内唯一的后台构建（API 与自动更新共用）。

* 全进程同一时刻最多一个构建；已经在跑时 :func:`start_build` 原样返回当前状态。
* 请求线程只启动、不等待（「UI 绝不冻结」）。
* **状态目录在启动时解析一次**，交给后台线程全程使用 —— 见 :mod:`mast.gallery.paths`。
* :func:`_reset_for_tests` 只清内存里的状态、等后台线程退出，不重新解析或缓存路径。

自动增量更新（见设计文档）也走这一个槽，由 :mod:`mast.gallery.autobuild` 的定时线程与
「打开图库页」的请求触发（:func:`start_auto`）：

* 自动构建线程数封顶 :data:`AUTO_WORKERS`，手动构建用用户设的 workers；
* 两次构建之间至少隔 :data:`MIN_GAP_S`（服务端判断，多个标签页同时触发也不会连发）；
* **手动点击绝不被吞**：撞上正在跑的自动构建，就请它取消并记一个待办，同一个线程在它退出后
  立刻接着跑手动那一轮（请求线程照样只启动、不等待）；手动要「全部重出」而在跑的是
  手动增量构建时同理，记待办、不打断。
"""
from __future__ import annotations

import threading
import time

from mast.gallery import build as _build
from mast.gallery import config as _config
from mast.gallery import paths as _paths
from mast.i18n import tr

_LOCK = threading.Lock()

#: 自动构建的线程数上限。图库的渲染与 FFT 放 GIL，和实时回路（串口读、Nanonis 轮询）抢的是同一把锁；
#: 量过的数字见设计文档 §8。
AUTO_WORKERS = 2
#: 两次构建之间的最小间隔（秒）：从上一次构建**结束**算起。
MIN_GAP_S = 60.0

#: 可注入的单调时钟（测试用）。
_now = time.monotonic

#: 当前（或上一次）构建：``(线程, 取消事件, 进度)``。**先启动线程、再整体替换，读的时候一次取出。**
#: ``get_status`` 不拿锁：若先发布进度再 ``start()``，或把线程和进度放在两个全局里分开读，状态请求会读到
#: 「新构建的进度（phase=inventory）+ 还没启动的线程（running=False）」，轮询方据此当成构建已经结束。
#: 出图任务槽（:mod:`mast.gallery.figures.service`）在端到端里撞上过同一个窗口。
_SLOT: tuple[threading.Thread | None, threading.Event | None, _build.Progress] = (None, None, _build.Progress())

# 以下全部受 _LOCK 保护。
_BUSY = False                       # 有构建线程在跑；由线程自己在锁里决定收工时清掉
_CUR: dict = {"force": False, "trigger": "manual"}   # 正在跑的这一轮
_PENDING: dict | None = None        # 手动请求撞上自动构建（或撞上增量构建要全部重出）时记下的待办
_LAST_END: float | None = None      # 上一次构建结束的时刻（_now 时钟）
_HOLD_UNTIL = 0.0                   # 自动触发在此刻之前一律不起（启动后让设备识别先做完）
_SHUTDOWN = False                   # 关停中：不再起任何构建


def _alive(th: threading.Thread | None) -> bool:
    return th is not None and th.is_alive()


def get_status() -> dict:
    """键正好是 ``GalleryBuildStatus`` 的字段。只读。"""
    th, _ev, prog = _SLOT
    snap = prog.snapshot()
    snap["running"] = _alive(th)
    return {k: snap.get(k) for k in _build.STATUS_KEYS}


def _kwargs(trigger: str) -> dict:
    kw: dict = {"skip_recent_s": _build.RECENT_SKIP_S}
    if trigger == "auto":
        kw["max_workers"] = AUTO_WORKERS
    return kw


def _fresh_progress(trigger: str) -> _build.Progress:
    prog = _build.Progress()
    prog.update(running=True, phase="inventory", started=_build.now_str(),
                message=tr("准备中…", "Preparing..."), trigger=trigger)
    return prog


def _run(lay: _paths.Layout, job: dict, prog: _build.Progress, cancel: threading.Event) -> None:
    """后台线程主体。一个线程可以连着跑多轮：手动请求撞上自动构建时，记下的待办在这里接着跑。"""
    global _SLOT, _BUSY, _PENDING, _LAST_END, _CUR
    me = threading.current_thread()
    try:
        while True:
            try:
                _build.run_build(force=bool(job["force"]), progress=prog, cancel=cancel, state=lay.state,
                                 **_kwargs(job["trigger"]))
            except Exception as exc:  # noqa: BLE001 — run_build 自己不抛，这里只是兜底
                prog.update(running=False, phase="error", finished=_build.now_str(),
                            message=tr(f"构建失败：{exc}", f"Build failed: {exc}"), detail=str(exc))
            with _LOCK:
                _LAST_END = _now()
                nxt, _PENDING = _PENDING, None
                if nxt is None or _SHUTDOWN:
                    _BUSY = False
                    return
                # 线程还活着，所以换槽的这一刻读到的 running 始终是 True，
                # 不会有「新进度 + 线程已死」的窗口（与 _launch_locked 同一条规则）。
                job = {"force": bool(nxt["force"]), "trigger": "manual"}
                cancel = threading.Event()
                prog = _fresh_progress("manual")
                _CUR = dict(job)
                _SLOT = (me, cancel, prog)
    finally:
        with _LOCK:
            if _SLOT[0] is me:
                _BUSY = False          # 兜底：线程不管怎么退出，槽都不能卡在「忙」


def _launch_locked(force: bool, trigger: str) -> None:
    """起一个构建线程。调用方持有 ``_LOCK`` 且已确认没有构建在跑。"""
    global _SLOT, _BUSY, _CUR
    lay = _paths.layout()
    cancel = threading.Event()
    prog = _fresh_progress(trigger)
    job = {"force": bool(force), "trigger": trigger}
    th = threading.Thread(target=_run, args=(lay, job, prog, cancel), name="gallery-build", daemon=True)
    _BUSY = True
    _CUR = dict(job)
    try:
        th.start()
    except Exception:
        _BUSY = False
        raise
    _SLOT = (th, cancel, prog)


def start_build(force: bool = False) -> dict:
    """手动构建（API 的「增量更新」「全部重出」）。**不会被吞。**

    * 没有构建在跑：起一个；
    * 在跑的是自动构建：请它取消，记下待办，它退出后同一个线程立刻接着跑这一轮；
    * 在跑的是手动构建：本轮已经覆盖这次请求就原样返回当前状态；要「全部重出」而在跑的是增量，
      记待办（不打断用户自己点的那一轮）。
    """
    global _PENDING
    force = bool(force)
    with _LOCK:
        if _BUSY and _alive(_SLOT[0]):
            _th, ev, prog = _SLOT
            if _CUR["trigger"] == "auto":
                _PENDING = {"force": force or bool(_PENDING and _PENDING["force"])}
                if ev is not None:
                    ev.set()
                prog.update(message=tr("正在取消自动更新，随后按你的要求重新构建…",
                                       "Cancelling the automatic update; your build starts right after..."))
            elif force and not _CUR["force"]:
                _PENDING = {"force": True}
                prog.update(message=tr("这一轮结束后会接着「全部重出」", "A full rebuild follows once this round ends"))
            return get_status()
        if _SHUTDOWN:
            return get_status()
        _launch_locked(force, "manual")
    return get_status()


def _field_ramp_in_progress() -> bool:
    """MAST 正在调磁场（升降场）吗？是 ⇒ 自动更新这一轮不起，免得给磁体电源的串口监督线程添负载。

    只读内存状态：``present()`` 与 ``magnet_controller()`` 不做 I/O，只拿控制器缓存的短锁；
    ``busy`` / ``active`` 只看一把非阻塞的 ``locked()`` 和一把只护字典的状态锁（持有期间不发串口命令）。
    **不碰串口链路锁。** 任何异常都当作「没在调」—— 图库不能因为查磁体而被挡住。"""
    try:
        from mast.core import magnet_control as _mc

        if not _mc.present():
            return False
        ctl = _mc.controller()
        return ctl is not None and bool(ctl.busy or ctl.active)
    except Exception:  # noqa: BLE001
        return False


def start_auto(reason: str = "timer") -> tuple[bool, dict]:
    """自动增量更新。返回 ``(这一次是否真的起了一轮, 当前状态)``。以下任一条成立就不起：

    关停中 · 启动后的等待期没过 · 开关关着 · 没有启用的数据根 · 已有构建在跑 ·
    离上一次构建结束不到 :data:`MIN_GAP_S` · MAST 正在调磁场（手动构建不受影响）。判断全在服务端、在锁里：多个标签页同时来也只会起一轮。
    ``reason`` 只用于日志。
    """
    lay = _paths.layout()
    cfg = _config.load_config(lay)            # 只读；没有配置文件也不会创建任何东西
    if not cfg.auto_build or not any(r.enabled for r in cfg.roots):
        return False, get_status()
    if _field_ramp_in_progress():
        return False, get_status()
    with _LOCK:
        if _SHUTDOWN or (_BUSY and _alive(_SLOT[0])):
            return False, get_status()
        now = _now()
        if now < _HOLD_UNTIL:
            return False, get_status()
        if _LAST_END is not None and now - _LAST_END < MIN_GAP_S:
            return False, get_status()
        _launch_locked(False, "auto")
    return True, get_status()


def hold_auto(seconds: float) -> None:
    """自动触发（定时与打开页面）在 ``seconds`` 秒内一律不起。启动后让设备识别先做完用。"""
    global _HOLD_UNTIL
    with _LOCK:
        _HOLD_UNTIL = max(_HOLD_UNTIL, _now() + max(0.0, float(seconds)))


def cancel_build() -> dict:
    global _PENDING
    with _LOCK:
        th, ev, prog = _SLOT
        _PENDING = None                      # 用户说停就是停，不接待办
        if _alive(th) and ev is not None:
            ev.set()
            prog.update(message=tr("正在取消…（已经在跑的几个任务会跑完）", "Cancelling... (the tasks already running will finish)"))
    return get_status()


def shutdown(*, wait_s: float = 0.0) -> None:
    """关停：不再起新构建，请在跑的取消。默认不等 —— 关停不能被一次构建拖住
    （渲染任务在下一个任务开始前检查取消，正在跑的几个会跑完）。"""
    global _SHUTDOWN, _PENDING
    with _LOCK:
        _SHUTDOWN = True
        _PENDING = None
        th, ev, _prog = _SLOT
        if _alive(th) and ev is not None:
            ev.set()
    if wait_s > 0 and th is not None:
        th.join(wait_s)


def wait(timeout: float | None = None) -> bool:
    """等后台构建结束（测试与 CLI 用）。返回是否已经没有在跑的构建。线程连着跑多轮时一直等到最后一轮。"""
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        th = _SLOT[0]
        if th is not None:
            th.join(None if deadline is None else max(0.0, deadline - time.monotonic()))
        if not _alive(_SLOT[0]):
            return True
        if deadline is not None and time.monotonic() >= deadline:
            return False


def _reset_for_tests(timeout: float = 60.0) -> None:
    global _SLOT, _BUSY, _PENDING, _LAST_END, _HOLD_UNTIL, _SHUTDOWN, _now, _CUR
    with _LOCK:
        th, ev, _prog = _SLOT
        _PENDING = None
    if _alive(th):
        if ev is not None:
            ev.set()
        th.join(timeout)
    with _LOCK:
        _SLOT = (None, None, _build.Progress())
        _BUSY = False
        _CUR = {"force": False, "trigger": "manual"}
        _LAST_END = None
        _HOLD_UNTIL = 0.0
        _SHUTDOWN = False
        _now = time.monotonic
