"""手动控制面板的设备清单（契约 §13.1）：把三类提供者的 ManualDevice 收到一起。

* 本机光学台与 Nanonis 光学组：``mast.instruments.manual.local_manual_devices()``；
* 对光相机：``mast.instruments.camera.manual_devices()``；
* 卫星设备：:func:`mast.satellite.manual.satellite_manual_devices`（按卫星上报的 caps 生成）。

每一类都在工作线程里调、有时限，延迟 import：读不到（没装、抛异常、超时）就把那一类记进
``degraded_sources``，别的类照常返回 —— 端点永不因为某一类失败而失败。这里不做校验；
按技能注册表核对动作、填参数细节的是端点（``mast.api.routes.manual``）。
"""

from __future__ import annotations

import concurrent.futures
import logging
import threading
import time
from typing import Any, Callable
from mast.i18n import tr

logger = logging.getLogger(__name__)

#: 每一类提供者的时限（秒）。提供者不该碰硬件（只报配置和缓存的状态），1 s 内应该回来。
PROVIDER_TIMEOUT_S = 3.0

SOURCE_LABEL = {"local": tr("本机光学台 / Nanonis 光学组", "Local optical stages / Nanonis optical groups"), "camera": tr("对光相机", "Alignment camera"), "satellite": tr("卫星设备", "Satellite devices")}


def _local() -> tuple[list[dict], list[str]]:
    from mast.instruments.manual import local_manual_devices

    notes: list[str] = []
    return list(local_manual_devices(notes=notes) or []), notes


def _camera() -> tuple[list[dict], list[str]]:
    from mast.instruments.camera import manual_devices

    return list(manual_devices() or []), []


def _satellite() -> tuple[list[dict], list[str]]:
    from mast.satellite.manual import satellite_manual_devices

    return satellite_manual_devices()


PROVIDERS: dict[str, Callable[[], tuple[list[dict], list[str]]]] = {
    "local": _local, "camera": _camera, "satellite": _satellite}


def _run_with_timeout(fn: Callable[[], Any], timeout_s: float) -> Any:
    """在守护线程里跑 *fn*，超时就放弃等（线程自己跑完了事，不拖住端点）。"""
    box: dict[str, Any] = {}
    done = threading.Event()

    def _go() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 — 原样交回调用方
            box["error"] = exc
        finally:
            done.set()

    threading.Thread(target=_go, name="manual-devices-provider", daemon=True).start()
    if not done.wait(timeout_s):
        raise concurrent.futures.TimeoutError(tr(f"{timeout_s:g} s 内没有返回", f"No return within {timeout_s:g} s"))
    if "error" in box:
        raise box["error"]
    return box["value"]


def collect_manual_devices(*, timeout_s: float | None = None,
                           providers: dict[str, Callable] | None = None
                           ) -> tuple[list[dict], list[str], list[str]]:
    """``(devices, degraded_sources, notes)``。devices 是提供者给的原样 dict。
    ``timeout_s`` 缺省取 :data:`PROVIDER_TIMEOUT_S`（调用时读）。"""
    limit = PROVIDER_TIMEOUT_S if timeout_s is None else float(timeout_s)
    devices: list[dict] = []
    degraded: list[str] = []
    notes: list[str] = []
    for source, fn in (providers or PROVIDERS).items():
        label = SOURCE_LABEL.get(source, source)
        try:
            items, more = _run_with_timeout(fn, limit)
        except Exception as exc:  # noqa: BLE001
            degraded.append(source)
            notes.append(tr(f"{label}读不到：{type(exc).__name__}: {exc}", f"{label} cannot be read: {type(exc).__name__}: {exc}"))
            logger.info("manual devices: %s unavailable: %s", source, exc)
            continue
        notes.extend(str(n) for n in more or [])
        for item in items:
            if isinstance(item, dict):
                devices.append(item)
                continue
            if source not in degraded:
                degraded.append(source)
            notes.append(tr(f"{label}给了一条不是对象的设备描述，已丢弃：{item!r:.80}", f"{label} gave a device description that is not an object; discarded: {item!r:.80}"))
    return devices, degraded, notes


def _is_local_device(rid: str) -> bool:
    return rid.startswith("local/") and not rid.startswith("local/camera/")


def _local_readouts(ids: list[str]) -> dict[str, dict]:
    from mast.instruments.manual import local_manual_readouts

    return dict(local_manual_readouts(ids) or {})


def _satellite_readouts(ids: list[str]) -> dict[str, dict]:
    from mast.satellite.manual import satellite_readouts

    return satellite_readouts(ids)


#: ``GET /api/manual/readouts`` 的两类提供者（测试换替身用）。
READOUT_PROVIDER: Callable[[list[str]], dict[str, dict]] = _local_readouts
SATELLITE_READOUT_PROVIDER: Callable[[list[str]], dict[str, dict]] = _satellite_readouts


def _readouts_from(provider: Callable[[list[str]], dict[str, dict]], ids: list[str], what: str,
                   limit: float, now: float) -> dict[str, dict]:
    out: dict[str, dict] = {}
    try:
        got = _run_with_timeout(lambda: provider(ids), limit)
        out.update({str(k): v for k, v in (got or {}).items() if str(k) in ids})
        for rid in ids:
            out.setdefault(rid, {"t": now, "axes": [], "error": tr(f"没有这台{what}的读数", f"No readout for this {what}")})
    except Exception as exc:  # noqa: BLE001
        for rid in ids:
            out[rid] = {"t": now, "axes": [], "error": tr(f"{what}读数不可用：{type(exc).__name__}: {exc}", f"{what} readout unavailable: {type(exc).__name__}: {exc}")}
    return out


def collect_readouts(ids: list[str], *, timeout_s: float | None = None) -> dict[str, dict]:
    """批量读数 ``{id: {t, axes, error}}``（契约 §13.1）。只读：不执行技能、不取令牌、不入账。

    * 本机台子与 Nanonis 光学组：devices 的 ``local_manual_readouts`` 直接读已经打开的设备
      （没打开的不去打开，不碰 Nanonis）；
    * 卫星设备：只回枢纽缓存的最近读数，不向卫星发命令；
    * 相机不走这里（读 ``/api/camera/{id}/stats``），该项的 error 写明。
    每一类有时限；读不到或超时，那几项各回一条 error，整体照样返回。"""
    limit = PROVIDER_TIMEOUT_S if timeout_s is None else float(timeout_s)
    now = time.time()
    local = [i for i in ids if _is_local_device(i)]
    camera = [i for i in ids if i.startswith("local/camera/")]
    sats = [i for i in ids if not i.startswith("local/") and "/" in i]
    out: dict[str, dict] = {}
    if local:
        out.update(_readouts_from(READOUT_PROVIDER, local, tr("本机设备", "local devices"), limit, now))
    if sats:
        out.update(_readouts_from(SATELLITE_READOUT_PROVIDER, sats, tr("卫星设备", "satellite devices"), limit, now))
    for rid in camera:
        out[rid] = {"t": now, "axes": [], "error": tr("相机不走这个端点：读 /api/camera/{id}/stats", "Cameras do not use this endpoint: read /api/camera/{id}/stats")}
    for rid in ids:
        out.setdefault(rid, {"t": now, "axes": [], "error": tr(f"认不出的设备 id：{rid!r}", f"Unrecognised device id: {rid!r}")})
    return out


__all__ = ["collect_manual_devices", "collect_readouts", "PROVIDERS", "READOUT_PROVIDER",
           "SATELLITE_READOUT_PROVIDER", "PROVIDER_TIMEOUT_S", "SOURCE_LABEL"]
