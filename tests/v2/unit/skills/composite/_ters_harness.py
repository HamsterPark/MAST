"""TERS 组合技能测试的公共替身：一台「有物理」的假 STM + 真 ExecutionContext + 真技能注册表。

* :class:`FakeStm` —— 回 Nanonis 形状的回包 ``(header, raw, [values])``。反馈闭合时电流贴着设定点
  （1 % 噪声），设定点改了 Z 按 κ 跟着走；反馈断开时电流 ∝ exp(−2κ·Δz)；退针后为 0。可以注入一段
  电流尖峰（守卫测试）、让某个读数失败、记下所有写命令与数字线脉冲。
* :func:`make_ctx` —— **真** ExecutionContext（ctx.run 的全部闸门、令牌、中止、状态回写都在），
  池子是 :class:`FakeStm`。
* :func:`sim_bench` / :func:`sat_bench` —— 本机模拟光谱仪 / 卫星枢纽替身上的光谱仪（+ 偏振台）。
"""
from __future__ import annotations

import json
import math
import threading
import time
from collections import Counter
from types import SimpleNamespace
from typing import Any

import numpy as np

from mast.core.types import HardwareState, NanonisCallRecord

KAPPA_PER_M = 10e9          # 10 nm⁻¹ ≈ 1 Å⁻¹


class FakeStm:
    """Pool double with a minimal tunnelling-junction model. Thread-safe."""

    def __init__(self, *, setpoint=1e-10, bias=0.5, z=50e-9, noise=0.01, seed=1):
        self.lock = threading.RLock()
        self.rng = np.random.default_rng(seed)
        self.noise = float(noise)
        self.bias = float(bias)
        self.setpoint = float(setpoint)
        self.fb = True
        self.z = float(z)
        self.z_fb_ref = float(z)          # z where I == setpoint with feedback closed
        self.i_ref = float(setpoint)
        self.x, self.y = 0.0, 0.0
        self.safetip = True
        self.safetip_threshold = 1e-8
        self.lockin = False
        self.scan = False
        self.tip_lift = 0.0
        self.withdrawn = False
        self.z_max = 1.5e-6
        self.speed = 1e-6
        self.writes: list[tuple] = []
        self.pulses: list[tuple] = []
        self.reads: Counter = Counter()
        self.spikes: list[tuple[float, float, float]] = []   # (t0, t1, current)
        self.fail: set[str] = set()
        self.on_pulse = None          # callback(port, lines) for TTL tests
        self.z_drift_pm_s = 0.0
        # Z change when the piezo extends towards the sample: -1 = extension lowers Z (Mimea, rig;
        # Withdraw parks on +z_max). +1 models the opposite rig. The junction follows it everywhere.
        self.extend_sign = -1
        self._t0 = time.monotonic()

    # -- model --
    def current(self) -> float:
        now = time.monotonic()
        for t0, t1, val in self.spikes:
            if t0 <= now <= t1:
                return float(val)
        if self.withdrawn:
            return float(self.rng.normal(0.0, 1e-13))
        if self.fb:
            base = self.setpoint
        else:
            base = self.i_ref * math.exp(2 * KAPPA_PER_M * self.extend_sign * (self.z - self.z_fb_ref))
        return float(base * (1.0 + self.noise * self.rng.normal()))

    def z_now(self) -> float:
        drift = self.z_drift_pm_s * 1e-12 * (time.monotonic() - self._t0)
        return self.z + drift + float(self.rng.normal(0.0, 1e-13))

    def _set_feedback(self, on: bool) -> None:
        if on and not self.fb:
            self.withdrawn = False
            # the loop pulls back to the setpoint height
            self.z = (self.z_fb_ref + math.log(self.setpoint / self.i_ref)
                      / (2 * KAPPA_PER_M * self.extend_sign))
        if not on and self.fb:
            self.i_ref = self.setpoint
            self.z_fb_ref = self.z
        self.fb = bool(on)

    # -- pool API --
    def safe_call(self, method: str, *args: Any, role: str = "main", **_kw: Any):
        with self.lock:
            if method in self.fail:
                return NanonisCallRecord(method=method, args=args, error=f"{method} failed (fake)")
            if not method.endswith("Get") and "Get" not in method:
                self.writes.append((method, args))
            self.reads[method] += 1
            v = self._dispatch(method, args)
            return NanonisCallRecord(method=method, args=args, return_value=("", b"", v))

    def _dispatch(self, m: str, a: tuple) -> list:
        if m == "Bias_Get":
            return [self.bias]
        if m == "Bias_Set":
            self.bias = float(a[0])
            return []
        if m == "ZCtrl_SetpntGet":
            return [self.setpoint]
        if m == "ZCtrl_SetpntSet":
            old = self.setpoint
            self.setpoint = float(a[0])
            if self.fb:
                self.z = self.z + math.log(self.setpoint / old) / (2 * KAPPA_PER_M * self.extend_sign)
            return []
        if m == "ZCtrl_OnOffGet":
            return [int(self.fb)]
        if m == "ZCtrl_OnOffSet":
            self._set_feedback(bool(int(a[0])))
            return []
        if m == "ZCtrl_StatusGet":
            return [2 if self.fb else 1]
        if m == "ZCtrl_ZPosGet":
            return [self.z_now()]
        if m == "ZCtrl_ZPosSet":
            if not self.fb:
                self.z = float(a[0])
            return []
        if m == "ZCtrl_TipLiftGet":
            return [self.tip_lift]
        if m == "ZCtrl_LimitsGet":
            return [self.z_max, -self.z_max]
        if m == "ZCtrl_Withdraw":
            self.fb = False
            self.withdrawn = True
            self.z = self.z_max if self.extend_sign < 0 else -self.z_max
            return []
        if m == "ZCtrl_SwitchOffDelayGet":
            return [0.0]
        if m == "ZCtrl_CtrlListGet":
            return [1, 1, ["Current"], 0]
        if m == "Current_Get":
            return [self.current()]
        if m == "Scan_StatusGet":
            return [int(self.scan)]
        if m == "Scan_FrameGet":
            return [0.0, 0.0, 1e-8, 1e-8, 0.0]
        if m == "SafeTip_OnOffGet":
            return [int(self.safetip)]
        if m == "SafeTip_PropsGet":
            return [0, 0, self.safetip_threshold]
        if m == "LockIn_ModOnOffGet":
            return [int(self.lockin)]
        if m == "FolMe_XYPosSet":
            self.x, self.y = float(a[0]), float(a[1])
            return []
        if m == "FolMe_XYPosGet":
            return [self.x, self.y]
        if m == "FolMe_SpeedGet":
            return [self.speed, 0]
        if m == "DigLines_Pulse":
            self.pulses.append((a[0], list(a[1]), a[2], time.time()))
            if self.on_pulse is not None:
                self.on_pulse(a[0], list(a[1]))
            return []
        if m == "DigLines_PropsSet":
            return []
        return []

    # -- state double --
    def snapshot(self) -> HardwareState:
        with self.lock:
            return HardwareState(bias_v=self.bias, current_a=self.current(), z_pos_m=self.z_now(),
                                 x_pos_m=self.x, y_pos_m=self.y, z_controller_on=self.fb,
                                 z_controller_status="On" if self.fb else "Off",
                                 setpoint_a=self.setpoint, withdrawn=self.withdrawn,
                                 scan_running=self.scan, lockin_mod_on=self.lockin)


class FakeState:
    def __init__(self, stm: FakeStm):
        self._stm = stm
        self.refreshes = 0

    def snapshot(self):
        return self._stm.snapshot()

    def refresh(self):
        self.refreshes += 1
        return self._stm.snapshot()

    def apply_patch(self, **_kw):
        return None


_REGISTRY = None


def skill_registry():
    """One discovered SkillRegistry per test process (discovery imports ~600 skills)."""
    global _REGISTRY
    if _REGISTRY is None:
        from mast.core.registry import SkillRegistry

        reg = SkillRegistry()
        reg.discover("mast.skills.builtins", "mast.skills.composite")
        _REGISTRY = reg
    return _REGISTRY


_CUT = "<嵌套过深,已截断>"


def _cut_paths(obj: Any, path: str = "data") -> list[str]:
    if obj == _CUT:
        return [path]
    if isinstance(obj, dict):
        return [c for k, v in obj.items() for c in _cut_paths(v, f"{path}.{k}")]
    if isinstance(obj, list):
        return [c for i, v in enumerate(obj) for c in _cut_paths(v, f"{path}[{i}]")]
    return []


def make_ctx(stm: FakeStm, *, abort: threading.Event | None = None, owner: str = "test",
             run_id: str = "") -> Any:
    """A real ExecutionContext. The outermost run of a TERS composite must also come back whole
    through the API's ``jsonable`` (it cuts at depth 6) — checked on every test's result."""
    from mast.api.direct_exec import jsonable
    from mast.core.execution_context import ExecutionContext

    class _Checked(ExecutionContext):
        _tl = threading.local()

        def run(self, skill_name, params, version=None):
            depth = getattr(self._tl, "depth", 0)
            self._tl.depth = depth + 1
            try:
                res = super().run(skill_name, params, version)
            finally:
                self._tl.depth = depth
            if depth == 0 and res is not None and str((res.data or {}).get("schema", "")) == "mast.ters_run/1":
                cut = _cut_paths(jsonable(res.data or {}))
                assert not cut, f"{skill_name}: the API would truncate {cut[:5]}"
            return res

    return _Checked(pool=stm, state=FakeState(stm), registry=skill_registry(),
                    abort_event=abort, owner=owner, run_id=run_id or f"t-{time.time_ns()}")


class OkGuard:
    """port_guard double: no conflicts (or the given ones)."""

    def __init__(self, conflicts: list | None = None, message: str = ""):
        self.conflicts = list(conflicts or [])
        self.message = message

    def check(self, *, refresh=False, reason="", max_age_s=5.0):
        from mast.core.port_guard import PortGuardReport

        rep = PortGuardReport(checked_ms=int(time.time() * 1000), ok=not self.conflicts,
                              enforce=True, conflicts=list(self.conflicts), reason=reason)
        return rep

    def last_report(self):
        return None


def write_bench(tmp_path, devices: list[dict], delay_line: dict | None = None):
    from mast.instruments.registry import InstrumentRegistry, reset_instrument_registry

    p = tmp_path / "optical_instruments.json"
    p.write_text(json.dumps({"devices": devices, "delay_line": delay_line}), encoding="utf-8")
    reg = InstrumentRegistry(p)
    reset_instrument_registry(reg)
    return reg


SIM_CCD = {"id": "sim_ccd", "type": "sim_spectrometer", "role": "raman_ccd",
           "laser_line_nm": 633.0, "time_scale": 0.0, "pixels": 400,
           "limits": {"max_exposure_ms": 600000, "max_frames": 1000, "max_total_s": 3600}}

#: a 50 mm linear stage (sim_rotation without wrap = a generic linear axis) as the delay line
DELAY_STAGE = {"id": "delay", "type": "sim_rotation", "speed_deg_s": 1e6,
               "axes": [{"name": "x", "channel": 1, "min_pos": 0.0, "max_pos": 50000.0,
                         "unit": "um"}]}
DELAY_LINE = {"device_id": "delay", "axis": "x", "unit_per_mm": 1000.0, "zero_offset_mm": 25.0}


class TriggeredSim:
    """Wraps a SimSpectrometer so that, once a trigger response is configured, every frame
    waits for one ``trigger()`` (= one DigLines_Pulse on the fake STM). ``drop={k}`` swallows
    the k-th trigger (0-based) — a lost TTL — so the frame count falls behind."""

    def __init__(self, sim, *, drop: set | None = None, exposure_scale: float = 0.0):
        import threading as _t

        self.sim = sim
        self.drop = set(drop or ())
        self.n_trig = 0
        self._sem = _t.Semaphore(0)
        self.scale = exposure_scale
        orig = sim._acquire_raw

        def acquire_raw(**kw):
            trig = (sim.settings.get("trigger") or {})
            if not trig or trig.get("response") in (None, "NoResponse"):
                return orig(**kw)
            return self._triggered(**kw)
        sim._acquire_raw = acquire_raw

    def trigger(self, *_a):
        k = self.n_trig
        self.n_trig += 1
        if k not in self.drop:
            self._sem.release()

    def _triggered(self, *, exposure_ms, frames, save, base_name, tag, meta, timeout_s,
                   check_abort, on_frame):
        from mast.instruments.base import MotionAborted
        from mast.instruments.spectrometer import AcquisitionResult, utc_now_iso

        sim = self.sim
        exp = float(exposure_ms if exposure_ms is not None else sim.settings["exposure_ms"])
        n = int(frames if frames is not None else sim.settings["frames"])
        sim._stop.clear()
        sim._running = True
        started = utc_now_iso()
        out = []
        deadline = time.monotonic() + float(timeout_s)
        try:
            for i in range(n):
                while not self._sem.acquire(timeout=0.02):
                    if check_abort is not None and check_abort():
                        raise MotionAborted("aborted")
                    if sim._stop.is_set():
                        raise MotionAborted("stopped")
                    if time.monotonic() > deadline:
                        raise MotionAborted("trigger timeout")
                time.sleep(exp / 1000.0 * self.scale)
                fr = sim._spectrum(exp)
                out.append(fr)
                on_frame(i, fr, sim._axis())
        finally:
            sim._running = False
        sim.acquisitions += 1
        return AcquisitionResult(frames=out, axis_nm=sim._axis(), files=[], started_utc=started,
                                 finished_utc=utc_now_iso(),
                                 meta={"exposure_ms": exp, "frames": n, "sim": True,
                                       "frame_meta": [{"frame_tracking": i + 1} for i in range(len(out))]})


def spectrum_of(path: str) -> np.ndarray:
    return np.load(path)


def ns(**kw):
    return SimpleNamespace(**kw)
