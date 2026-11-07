"""端口与设备归属检查（原则：控制端口冲突时，以 MAST 为控制核心与枢纽）。

查三件事（契约 §10.5）：

1. **Nanonis TCP**：6501–6504（MAST 的四个角色口）与 3364（VI Server）上有没有**不是本进程**
   的客户端。Nanonis 在本机时两头都看得见：服务端口上被 accept 的连接给出对端地址，本机对端
   再按地址找到客户端进程；别的机器连进来的只能报 IP。
2. **本机光学设备的串口**（``config/optical_instruments.json`` 里配了 ``port`` 的）。**绝不为检测
   去打开串口**（打开会拨动 DTR/RTS）。默认只做两件事：
   (a) 可能占串口的厂家程序（LabVIEW、PI MikroMove …）在跑 → warn；
   (b) MAST 自己打开配置的 COM 口**失败时**，驱动调 :func:`serial_open_hint` 在错误里点名可能的
       占用方（列出正在运行的厂家程序）。
   跨进程句柄扫描（枚举系统句柄表、把别的进程的 File 句柄复制过来读对象名、与
   ``QueryDosDevice("COMx")`` 比对）能给出确切占用者，但**默认关闭**：``NtQueryObject`` 卡住的
   线程杀不掉，长期运行的服务里会越积越多；在实验机上复制别的厂家进程的句柄也太侵入、容易惹
   杀毒软件。要用的人在 ``port_guard.json`` 里设 ``serial_handle_scan: true``。
3. **USB 设备**（Kinesis 等）：没有不打开设备就能查归属的办法，只按「厂家界面进程在不在跑」给
   提醒（warn，不阻断）。

处理方式：**不自动杀别的程序**（契约 §15）。发现冲突就记 WARNING、发 EventBus
``SATELLITE``（``kind="port_guard"``）与 LiveHub ``mast/port_guard`` 状态流；组合技能开始前调
:func:`check`，``report.ok`` 为假就拒绝开始，把 ``report.message`` 告诉操作员。

配置（可选）``<数据根>/config/port_guard.json``::

    {"enforce": true,                       // false：只报告，check() 永远 ok
     "serial_handle_scan": false,           // true：跨进程句柄扫描查串口确切占用者（默认关）
     "serial_vendor_processes": ["labview.exe", "pimikromove.exe"],
     "vi_server_port": 3364,
     "allow": [{"process": "SomeTool.exe", "ports": [6504], "note": "只读直连"},
               {"remote_ip": "<那台机器的 IP>", "note": "…"}],
     "vendor_processes": {"labview.exe": "LabVIEW", …}}

MAST 自己停用的角色口（端口设成 0，例如为外部只读直连腾出的 6504）上的外部客户端
只记 info —— 那是用户自己的取舍。
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import socket
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from mast.i18n import tr

logger = logging.getLogger(__name__)

DEFAULT_NANONIS_PORTS = {"main": 6501, "monitor": 6502, "data": 6503, "emergency": 6504}
DEFAULT_VI_SERVER_PORT = 3364
CACHE_S = 5.0
#: ``refresh=True`` 也至少隔这么久才真查一次：一次全量检查要 1–2 s CPU，而回环模式下
#: ``GET /api/port-guard?refresh=true`` 不需要认证，本机任何网页都能用 no-cors 请求反复按它。
MIN_REFRESH_S = 2.0
_ACTIVE_STATES = frozenset({"ESTABLISHED", "SYN_SENT", "SYN_RECV"})
DEFAULT_VENDOR_PROCESSES = {
    "labview.exe": "LabVIEW",
    "kinesis.exe": "Thorlabs Kinesis",
    "thorlabs.motioncontrol.kinesis.exe": "Thorlabs Kinesis",
    "aptuser.exe": "Thorlabs APT",
    "pimikromove.exe": "PI MikroMove",
    "lightfield.exe": "LightField",
    "spectrasense.exe": "SpectraSense",
    # 光学站点（docs/v2/design/optical_station_witec.md §1 陷阱 7）：这几个厂家程序一启动就独占串口。
    # 进程名出处：只读调查（Prefetch 与安装目录）——Rainbow OEM 的 Prefetch 是
    # ``RAINBOW OEM.EXE``（用户配置目录写成 ``Rainbow_OEM.exe``，两种写法都收）；NKT CONTROL 的
    # 进程就叫 ``CONTROL.exe``（与 Windows 控制面板 ``control.exe`` 同名——只按进程名认，控制面板
    # 开着也会被点名，所以这里的显示名写明了）；``TC100.exe``；Thorlabs 功率计 GUI 的进程名是
    # ``Thorlabs Optical Power Monitor.exe``（走 USB，不占串口）。
    "rainbow oem.exe": "NPI Rainbow OEM",
    "rainbow_oem.exe": "NPI Rainbow OEM",
    "control.exe": "NKT CONTROL (control.exe)",
    "tc100.exe": "TC100",
    "thorlabs optical power monitor.exe": "Thorlabs Optical Power Monitor",
}
#: 没开句柄扫描时，串口冲突只能按「这些程序在跑」来提醒（LabVIEW 的 VISA、PI 的 MikroMove、
#: 光学站点的 NPI Rainbow OEM / NKT CONTROL / TC100）。
#: 可在 port_guard.json 的 ``serial_vendor_processes`` 里改。
DEFAULT_SERIAL_VENDOR_PROCESSES = ("labview.exe", "pimikromove.exe",
                                   "rainbow oem.exe", "rainbow_oem.exe", "control.exe", "tc100.exe")
#: 这些厂家界面可能占着 USB 运动控制器（按驱动类型对应）。
_USB_VENDORS = {"thorlabs_benchtop_piezo": ("kinesis.exe", "thorlabs.motioncontrol.kinesis.exe",
                                            "aptuser.exe", "labview.exe"),
                "thorlabs_pdxc2": ("kinesis.exe", "thorlabs.motioncontrol.kinesis.exe",
                                   "labview.exe"),
                # Thorlabs 功率计（TLPM / VISA，USB）：Optical Power Monitor 开着时设备被它占着
                "thorlabs_pm": ("thorlabs optical power monitor.exe", "labview.exe")}


@dataclass
class Finding:
    kind: str                 # nanonis_tcp | nanonis_vi_server | serial_port | usb_device | vendor_gui
    resource: str
    severity: str             # conflict | warn | info
    pid: int | None = None
    process: str = ""
    exe: str | None = None
    cmdline: str | None = None
    remote: str | None = None
    detail: str = ""
    allowed_by: str | None = None

    def key(self) -> tuple:
        return (self.kind, self.resource, self.pid, self.remote)


@dataclass
class PortGuardReport:
    checked_ms: int
    ok: bool
    enforce: bool
    conflicts: list[Finding] = field(default_factory=list)
    warnings: list[Finding] = field(default_factory=list)
    infos: list[Finding] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    nanonis: dict = field(default_factory=dict)
    optics: list[dict] = field(default_factory=list)
    duration_ms: int = 0
    complete: bool = True     # 某项检查没跑成（权限不足、没有 psutil……）时为 False
    reason: str = ""
    serial_handle_scan: bool = False   # port_guard.json 里打开了跨进程句柄扫描才是 True

    @property
    def message(self) -> str:
        if not self.conflicts:
            if self.errors:
                return tr("没有发现冲突（但有检查没跑完整：" + "；".join(self.errors[:3]) + "）",
                          "No conflicts found (but some checks did not complete: "
                          + "; ".join(self.errors[:3]) + ")")
            return tr("没有发现端口或设备归属冲突", "No port or device ownership conflicts found")
        parts = []
        for f in self.conflicts:
            who = f.process or tr("未知进程", "unknown process")
            if f.pid is not None:
                who += tr(f"（pid {f.pid}）", f" (pid {f.pid})")
            if f.remote:
                who += tr(f"（来自 {f.remote}）", f" (from {f.remote})")
            parts.append(tr(f"{f.resource} 被 {who} 占用", f"{f.resource} is held by {who}"))
        head = tr("端口/设备归属冲突：" + "；".join(parts) + "。",
                  "Port/device ownership conflict: " + "; ".join(parts) + ". ")
        tail = (tr("MAST 是控制枢纽：请先在那边断开或关闭（MAST 不会替你关别的程序），再开始。",
                   "MAST is the control hub: disconnect or close it there first "
                   "(MAST will not close other programs for you), then start.")
                if self.enforce else
                tr("（enforce=false：只报告，不阻止开始）",
                   "(enforce=false: report only, starting is not blocked)"))
        return head + tail

    def to_dict(self) -> dict:
        d = asdict(self)
        d["message"] = self.message
        return d


class PortConflictError(RuntimeError):
    def __init__(self, report: PortGuardReport) -> None:
        super().__init__(report.message)
        self.report = report


# ── 配置 ────────────────────────────────────────────────────────────────────

def _data_root() -> Path:
    for var in ("MAST2_PROJECT_ROOT", "MAST_PROJECT_ROOT"):
        v = os.environ.get(var, "").strip()
        if v:
            return Path(v)
    from mast._runtime_paths import project_root

    return Path(project_root())


def load_guard_config(path: Path | None = None) -> tuple[dict, str | None]:
    p = path or (_data_root() / "config" / "port_guard.json")
    if not p.exists():
        return {}, None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}, tr(f"{p} 顶层不是对象", f"{p}: top level is not an object")
        return raw, None
    except (OSError, ValueError) as exc:
        return {}, tr(f"读不了 {p}：{exc}", f"cannot read {p}: {exc}")


# ── 系统信息（可替换，测试用替身） ──────────────────────────────────────────

def _psutil():
    import psutil  # noqa: PLC0415 — 冻结包里有（随 pyexec 一起打进去，见 mast2.spec）

    return psutil


def _default_connections() -> list:
    return list(_psutil().net_connections(kind="tcp"))


def _default_local_ips() -> set[str]:
    ips = {"127.0.0.1", "::1", "0.0.0.0", "::"}
    try:
        for addrs in _psutil().net_if_addrs().values():
            for a in addrs:
                if getattr(a, "family", None) in (socket.AF_INET, socket.AF_INET6):
                    ips.add(str(a.address).split("%")[0])
    except Exception:  # noqa: BLE001
        pass
    return ips


def _default_process_info(pid: int) -> dict:
    info: dict[str, Any] = {"pid": pid, "name": "", "exe": None, "cmdline": None}
    try:
        p = _psutil().Process(pid)
        info["name"] = p.name()
        try:
            info["exe"] = p.exe()
        except Exception:  # noqa: BLE001
            pass
        try:
            info["cmdline"] = " ".join(p.cmdline())[:300]
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        pass
    return info


def _default_processes() -> list[dict]:
    """只取 pid 与进程名：取 exe 对每个系统进程都要一次被拒的 OpenProcess，慢一个数量级。"""
    out = []
    try:
        for p in _psutil().process_iter(["pid", "name"]):
            out.append({"pid": p.info.get("pid"), "name": p.info.get("name") or "", "exe": None})
    except Exception:  # noqa: BLE001
        pass
    return out


# ── Windows：不打开串口，查谁打开着它 ───────────────────────────────────────

def query_dos_device(name: str) -> list[str]:
    """``COM17`` → ``["\\Device\\VCP0"]``（读符号链接，不打开设备）。"""
    if sys.platform != "win32":
        return []
    import ctypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    buf = ctypes.create_unicode_buffer(4096)
    n = k32.QueryDosDeviceW(str(name), buf, len(buf))
    if not n:
        return []
    raw = buf[:n]
    return [s for s in raw.split("\x00") if s]


def win_file_handle_owners(target_names: Iterable[str], *, budget_s: float = 5.0,
                           per_handle_timeout_s: float = 0.25,
                           exclude_pids: Iterable[int] = (),
                           file_types: tuple[int, ...] = (2,)) -> dict:
    """枚举所有进程的 File 句柄，找出对象名在 ``target_names`` 里的（不区分大小写）。

    返回 ``{"owners": {name: [pid, …]}, "denied_pids": [...], "stuck": n, "complete": bool,
    "scanned": n}``。只用 NtQuerySystemInformation / DuplicateHandle / NtQueryObject：复制来的
    句柄指向同一个文件对象，**不触发打开**（串口驱动收不到 IRP_MJ_CREATE，DTR/RTS 不动）。
    个别句柄查名字可能卡住（同步 I/O 挂起中），所以放在工作线程里、逐个计时，卡住的跳过。
    """
    if sys.platform != "win32":
        return {"owners": {}, "denied_pids": [], "stuck": 0, "complete": False,
                "scanned": 0, "error": "not windows"}
    import ctypes
    import msvcrt
    import tempfile
    from ctypes import wintypes

    targets = {str(t).lower(): str(t) for t in target_names if t}
    owners: dict[str, list[int]] = {v: [] for v in targets.values()}
    if not targets:
        return {"owners": owners, "denied_pids": [], "stuck": 0, "complete": True, "scanned": 0}

    ntdll = ctypes.WinDLL("ntdll")
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class ENTRY(ctypes.Structure):
        _fields_ = [("Object", ctypes.c_void_p), ("UniqueProcessId", ctypes.c_size_t),
                    ("HandleValue", ctypes.c_size_t), ("GrantedAccess", wintypes.ULONG),
                    ("CreatorBackTraceIndex", wintypes.USHORT),
                    ("ObjectTypeIndex", wintypes.USHORT),
                    ("HandleAttributes", wintypes.ULONG), ("Reserved", wintypes.ULONG)]

    class UNICODE_STRING(ctypes.Structure):
        _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT),
                    ("Buffer", ctypes.c_void_p)]

    ntdll.NtQuerySystemInformation.argtypes = [wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG,
                                               ctypes.POINTER(wintypes.ULONG)]
    ntdll.NtQuerySystemInformation.restype = ctypes.c_long
    ntdll.NtQueryObject.argtypes = [wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p,
                                    wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
    ntdll.NtQueryObject.restype = ctypes.c_long
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.DuplicateHandle.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
                                    ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
                                    wintypes.BOOL, wintypes.DWORD]
    k32.DuplicateHandle.restype = wintypes.BOOL
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.GetFileType.argtypes = [wintypes.HANDLE]
    k32.GetFileType.restype = wintypes.DWORD

    SystemExtendedHandleInformation = 64
    STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
    PROCESS_DUP_HANDLE = 0x0040
    DUPLICATE_SAME_ACCESS = 0x2
    want_types = frozenset(int(t) for t in file_types)

    def _snapshot():
        size = 4 << 20
        while True:
            buf = ctypes.create_string_buffer(size)
            ret = wintypes.ULONG(0)
            st = ntdll.NtQuerySystemInformation(SystemExtendedHandleInformation, buf, size,
                                                ctypes.byref(ret)) & 0xFFFFFFFF
            if st == STATUS_INFO_LENGTH_MISMATCH:
                size = max(size * 2, int(ret.value) + (1 << 20))
                if size > (1 << 30):
                    raise OSError(tr("句柄表超过 1 GiB", "Handle table exceeds 1 GiB"))
                continue
            if st != 0:
                raise OSError(tr(f"NtQuerySystemInformation 失败 0x{st:08x}",
                                 f"NtQuerySystemInformation failed 0x{st:08x}"))
            hdr = 2 * ctypes.sizeof(ctypes.c_size_t)
            n = ctypes.c_size_t.from_buffer(buf, 0).value
            n = min(n, (size - hdr) // ctypes.sizeof(ENTRY))
            return buf, (ENTRY * n).from_buffer(buf, hdr)

    # 用自己打开的一个文件找出「File」对象的类型号（各版本 Windows 不一样）。
    own_pid = os.getpid()
    with tempfile.TemporaryFile() as tf:
        own_h = msvcrt.get_osfhandle(tf.fileno())
        keep_buf, entries = _snapshot()
        file_type = None
        for e in entries:
            if e.UniqueProcessId == own_pid and e.HandleValue == own_h:
                file_type = e.ObjectTypeIndex
                break
    if file_type is None:
        return {"owners": owners, "denied_pids": [], "stuck": 0, "complete": False,
                "scanned": 0,
                "error": tr("找不到 File 对象的类型号", "File object type index not found")}
    skip = {0, 4, own_pid, *map(int, exclude_pids)}
    items = [(int(e.UniqueProcessId), int(e.HandleValue)) for e in entries
             if e.ObjectTypeIndex == file_type and int(e.UniqueProcessId) not in skip]
    del entries, keep_buf

    cur = k32.GetCurrentProcess()
    procs: dict[int, Any] = {}
    denied: set[int] = set()
    stuck_pids: set[int] = set()
    lock = threading.Lock()

    def _name_of(h) -> str | None:
        buf = ctypes.create_string_buffer(2048)
        ret = wintypes.ULONG(0)
        st = ntdll.NtQueryObject(h, 1, buf, len(buf), ctypes.byref(ret)) & 0xFFFFFFFF
        if st != 0:
            return None
        us = UNICODE_STRING.from_buffer(buf, 0)
        if not us.Buffer or not us.Length:
            return None
        return ctypes.wstring_at(us.Buffer, us.Length // 2)

    def _proc_handle(pid: int):
        with lock:
            if pid in denied or pid in stuck_pids:
                return None
            hp = procs.get(pid)
            if hp is None:
                hp = k32.OpenProcess(PROCESS_DUP_HANDLE, False, pid)
                if not hp:
                    denied.add(pid)
                    return None
                procs[pid] = hp
            return hp

    def _worker(start: int, ctl: dict) -> None:
        # 每个工作线程有自己的 ctl：被判卡住的旧线程醒来后看见 stop 就走，碰不到新线程的进度。
        for i in range(start, len(items)):
            if ctl["stop"]:
                return
            ctl["i"], ctl["t"] = i, time.monotonic()
            pid, hv = items[i]
            hp = _proc_handle(pid)
            if hp is None:
                continue
            dup = wintypes.HANDLE()
            if not k32.DuplicateHandle(hp, wintypes.HANDLE(hv), cur, ctypes.byref(dup), 0, False,
                                       DUPLICATE_SAME_ACCESS):
                continue
            try:
                # 默认只看字符设备（FILE_TYPE_CHAR = 串口）；测试用磁盘文件（1）验证这套机制。
                name = _name_of(dup) if k32.GetFileType(dup) in want_types else None
            finally:
                k32.CloseHandle(dup)
            if ctl["stop"]:
                return
            if name and name.lower() in targets:
                with lock:
                    owners[targets[name.lower()]].append(pid)
        ctl["finished"] = True

    deadline = time.monotonic() + budget_s
    pos = 0
    stuck = 0
    complete = True
    while pos < len(items):
        ctl = {"i": pos, "t": time.monotonic(), "stop": False, "finished": False}
        th = threading.Thread(target=_worker, args=(pos, ctl), daemon=True,
                              name="port-guard-handles")
        th.start()
        while th.is_alive():
            th.join(0.02)
            if not th.is_alive():
                break
            if time.monotonic() - ctl["t"] > per_handle_timeout_s:
                ctl["stop"] = True
                stuck += 1
                with lock:
                    stuck_pids.add(items[ctl["i"]][0])   # 这个进程的其余句柄也不碰了
                pos = ctl["i"] + 1
                break
            if time.monotonic() > deadline:
                ctl["stop"] = True
                complete = False
                pos = len(items)
                break
        if ctl["finished"]:
            pos = len(items)
        if time.monotonic() > deadline and pos < len(items):
            complete = False
            break
    if not stuck:
        # 有线程被判卡住时进程句柄就不关了：它醒来后可能还要用，关掉的句柄值会被复用，
        # 那时它的 CloseHandle 会关错东西。漏几个进程句柄是更小的代价。
        for hp in procs.values():
            try:
                k32.CloseHandle(hp)
            except Exception:  # noqa: BLE001
                pass
    for k in owners:
        owners[k] = sorted(set(owners[k]))
    return {"owners": owners, "denied_pids": sorted(denied), "stuck": stuck,
            "stuck_pids": sorted(stuck_pids), "complete": complete and not stuck,
            "scanned": len(items)}


def _default_serial_owners(ports: list[str]) -> dict:
    """``["COM17", …]`` → ``{"owners": {"COM17": [pid…]}, …}``。"""
    names: dict[str, str] = {}
    unmapped: list[str] = []
    for port in ports:
        devs = query_dos_device(port)
        if devs:
            names[devs[0]] = port
        else:
            unmapped.append(port)
    res = win_file_handle_owners(names.keys()) if names else {
        "owners": {}, "denied_pids": [], "stuck": 0, "complete": True, "scanned": 0}
    owners = {names[k]: v for k, v in (res.get("owners") or {}).items() if k in names}
    out = dict(res)
    out["owners"] = owners
    out["unmapped"] = unmapped                   # 系统里没有这个 COM 口（没插 / 驱动没装）
    out["device_names"] = {v: k for k, v in names.items()}
    return out


# ── 检查器 ──────────────────────────────────────────────────────────────────

class PortGuard:
    def __init__(self, *, nanonis: Any = None, guard_config_path: Path | None = None,
                 optics_config: dict | None = None,
                 connections_fn: Callable[[], list] = _default_connections,
                 local_ips_fn: Callable[[], set[str]] = _default_local_ips,
                 process_info_fn: Callable[[int], dict] = _default_process_info,
                 processes_fn: Callable[[], list[dict]] = _default_processes,
                 serial_owners_fn: Callable[[list[str]], dict] = _default_serial_owners,
                 own_pid: int | None = None, publish: bool = True) -> None:
        self.nanonis = nanonis
        self.guard_config_path = guard_config_path
        self.optics_config = optics_config
        self._connections = connections_fn
        self._local_ips = local_ips_fn
        self._proc_info = process_info_fn
        self._processes = processes_fn
        self._serial_owners = serial_owners_fn
        self.own_pid = int(own_pid if own_pid is not None else os.getpid())
        self.publish = publish
        self._lock = threading.Lock()
        self._last: PortGuardReport | None = None
        self._last_mono = 0.0
        self._last_conflict_keys: set[tuple] = set()

    # ── 配置读取 ──
    def _nanonis_ports(self) -> tuple[str, dict[str, int]]:
        n = self.nanonis
        if n is None:
            try:
                from mast.config import NanonisConfig

                n = NanonisConfig()
                try:
                    from mast.webui.settings_store import NANONIS_KEY_TO_ATTR, settings_store_for_runtime

                    st = settings_store_for_runtime()
                    for key, attr in NANONIS_KEY_TO_ATTR.items():
                        v = st.get(key)
                        if v not in (None, ""):
                            setattr(n, attr, v)
                except Exception:  # noqa: BLE001
                    pass
            except Exception:  # noqa: BLE001
                n = None
        host = str(getattr(n, "host", "127.0.0.1") or "127.0.0.1")
        ports = {}
        for role, default in DEFAULT_NANONIS_PORTS.items():
            try:
                ports[role] = int(getattr(n, f"port_{role}", default) if n is not None else default)
            except (TypeError, ValueError):
                ports[role] = default
        return host, ports

    def _optics_devices(self) -> list[dict]:
        cfg = self.optics_config
        if cfg is None:
            try:
                from mast.instruments.config import load_config

                cfg = load_config()
            except Exception as exc:  # noqa: BLE001
                logger.debug("optical_instruments config unreadable: %s", exc)
                cfg = {}
        out = []
        for e in (cfg or {}).get("devices") or []:
            if isinstance(e, dict):
                out.append(e)
        return out

    # ── 主检查 ──
    def check(self, *, refresh: bool = False, max_age_s: float = CACHE_S,
              reason: str = "") -> PortGuardReport:
        with self._lock:
            age = time.monotonic() - self._last_mono
            if self._last is not None and (age < MIN_REFRESH_S
                                           or (not refresh and age < max_age_s)):
                return self._last
        t0 = time.monotonic()
        gcfg, gerr = load_guard_config(self.guard_config_path)
        enforce = bool(gcfg.get("enforce", True))
        rep = PortGuardReport(checked_ms=int(time.time() * 1000), ok=True, enforce=enforce,
                              reason=reason)
        if gerr:
            rep.errors.append(gerr)
        findings: list[Finding] = []
        try:
            findings += self._check_nanonis(rep, gcfg)
        except Exception as exc:  # noqa: BLE001
            rep.complete = False
            rep.errors.append(tr(f"Nanonis 端口检查失败：{type(exc).__name__}: {exc}",
                                f"Nanonis port check failed: {type(exc).__name__}: {exc}"))
        try:
            findings += self._check_optics(rep, gcfg)
        except Exception as exc:  # noqa: BLE001
            rep.complete = False
            rep.errors.append(tr(f"光学设备检查失败：{type(exc).__name__}: {exc}",
                                f"Optics device check failed: {type(exc).__name__}: {exc}"))
        self._apply_allowlist(findings, gcfg)
        seen: set[tuple] = set()
        for f in findings:
            if f.key() in seen:
                continue
            seen.add(f.key())
            {"conflict": rep.conflicts, "warn": rep.warnings}.get(f.severity, rep.infos).append(f)
        rep.ok = (not rep.conflicts) or not enforce
        rep.duration_ms = int((time.monotonic() - t0) * 1000)
        with self._lock:
            self._last = rep
            self._last_mono = time.monotonic()
        self._alert(rep)
        return rep

    def last_report(self) -> PortGuardReport | None:
        with self._lock:
            return self._last

    def _proc(self, pid: int | None) -> dict:
        if pid is None:
            return {"name": "", "exe": None, "cmdline": None}
        try:
            return self._proc_info(int(pid)) or {}
        except Exception:  # noqa: BLE001
            return {"name": "", "exe": None, "cmdline": None}

    def _check_nanonis(self, rep: PortGuardReport, gcfg: dict) -> list[Finding]:
        host, role_ports = self._nanonis_ports()
        vi_port = int(gcfg.get("vi_server_port", DEFAULT_VI_SERVER_PORT) or DEFAULT_VI_SERVER_PORT)
        used = {p: role for role, p in role_ports.items() if p}
        watch: dict[int, str] = {}
        for role, default in DEFAULT_NANONIS_PORTS.items():
            if role_ports.get(role):
                watch[role_ports[role]] = role
            elif default not in used:
                watch[default] = tr(f"{role}(MAST 已停用)", f"{role} (disabled in MAST)")
        watch[vi_port] = "vi_server"
        local_ips = self._local_ips()
        try:
            host_ip = str(ipaddress.ip_address(host))
        except ValueError:
            try:
                host_ip = socket.gethostbyname(host)
            except OSError:
                host_ip = host
        nanonis_local = host_ip in local_ips or host_ip.startswith("127.")
        conns = self._connections()
        rep.nanonis = {"host": host, "host_ip": host_ip, "local": nanonis_local,
                       "ports": role_ports, "vi_server_port": vi_port,
                       "watched": {str(p): r for p, r in sorted(watch.items())},
                       "server_pids": [], "mast_connections": 0}

        def _ip(a) -> str:
            return str(getattr(a, "ip", a[0] if a else "")).split("%")[0]

        def _port(a) -> int | None:
            if not a:
                return None
            return int(getattr(a, "port", a[1]))

        def _is_local(ip: str) -> bool:
            return ip in local_ips or ip.startswith("127.") or ip == "::1"

        client_by_addr: dict[tuple[str, int], int | None] = {}
        out: list[Finding] = []
        servers: set[int] = set()
        handled: set[tuple] = set()
        for c in conns:
            st = str(getattr(c, "status", "") or "")
            la, ra = getattr(c, "laddr", None), getattr(c, "raddr", None)
            if st == "LISTEN" and _port(la) in watch:
                if getattr(c, "pid", None):
                    servers.add(int(c.pid))
                continue
            if st not in _ACTIVE_STATES or not ra:
                continue
            if _port(ra) in watch and (_ip(ra) == host_ip or (nanonis_local and _is_local(_ip(ra)))):
                client_by_addr[(_ip(la), _port(la))] = getattr(c, "pid", None)
        rep.nanonis["server_pids"] = sorted(servers)
        if servers:
            sp = sorted(servers)[0]
            rep.nanonis["server_process"] = self._proc(sp).get("name", "")
        elif nanonis_local and role_ports.get("main"):
            rep.infos.append(Finding(kind="nanonis_tcp", resource=f"tcp:{role_ports['main']}",
                                     severity="info",
                                     detail=tr("本机没有进程在监听 Nanonis 主端口（Nanonis 没开？）",
                                               "No local process is listening on the Nanonis main port "
                                               "(is Nanonis running?)")))

        def _classify(port: int, pid: int | None, remote: str | None) -> None:
            if pid is not None and int(pid) == self.own_pid:
                rep.nanonis["mast_connections"] += 1
                return
            role = watch.get(port, "?")
            key = (port, pid, remote)
            if key in handled:
                return
            handled.add(key)
            info = self._proc(pid)
            kind = "nanonis_vi_server" if role == "vi_server" else "nanonis_tcp"
            used_by_mast = role in role_ports and bool(role_ports.get(role))
            sev = "conflict" if (role == "vi_server" or used_by_mast) else "info"
            label = "VI Server" if role == "vi_server" else role
            who = info.get("name") or (tr("远端客户端", "remote client") if remote
                                       else tr("未知进程", "unknown process"))
            if sev == "conflict":
                detail = tr(f"{who} 连着 Nanonis {port}（{label}）。MAST 是控制枢纽，同一台 Nanonis "
                            "不该有第二个控制方。",
                            f"{who} is connected to Nanonis {port} ({label}). MAST is the control hub; "
                            "one Nanonis should not have a second controller.")
            else:
                detail = tr(f"{who} 连着 Nanonis {port}（{label}）；这个口 MAST 自己停用了，只记录。",
                            f"{who} is connected to Nanonis {port} ({label}); MAST has this port "
                            "disabled itself, so this is only recorded.")
            out.append(Finding(kind=kind, resource=f"tcp:{port}({label})", severity=sev,
                               pid=int(pid) if pid is not None else None,
                               process=info.get("name") or "", exe=info.get("exe"),
                               cmdline=info.get("cmdline"), remote=remote, detail=detail))

        # 本机客户端（无论 Nanonis 在哪）
        for (lip, lport), pid in client_by_addr.items():
            for c in conns:
                if (_ip(getattr(c, "laddr", None)) == lip and _port(getattr(c, "laddr", None)) == lport
                        and getattr(c, "raddr", None)):
                    _classify(_port(c.raddr), pid, None)
                    break
        # Nanonis 在本机：服务端看得见所有连进来的，包括别的机器
        if nanonis_local:
            for c in conns:
                st = str(getattr(c, "status", "") or "")
                la, ra = getattr(c, "laddr", None), getattr(c, "raddr", None)
                if st not in _ACTIVE_STATES or not ra or _port(la) not in watch:
                    continue
                if getattr(c, "pid", None) and int(c.pid) not in servers and servers:
                    continue                          # 不是 Nanonis 那边 accept 的
                peer = (_ip(ra), _port(ra))
                if _is_local(peer[0]):
                    if peer in client_by_addr:
                        continue                      # 上面已经按进程分过了
                    # 本机对端，但没找到客户端套接字（瞬时竞态）
                    _classify(_port(la), None, f"{peer[0]}:{peer[1]}")
                else:
                    _classify(_port(la), None, f"{peer[0]}:{peer[1]}")
        return out

    def _check_optics(self, rep: PortGuardReport, gcfg: dict) -> list[Finding]:
        devices = self._optics_devices()
        out: list[Finding] = []
        vendors = {str(k).lower(): str(v) for k, v in
                   (gcfg.get("vendor_processes") or DEFAULT_VENDOR_PROCESSES).items()}
        procs = None
        serial: dict[str, list[dict]] = {}
        for d in devices:
            port = str(d.get("port") or "").strip()
            entry = {"id": d.get("id"), "type": d.get("type"), "enabled": bool(d.get("enabled", True)),
                     "port": port or None, "serial_no": d.get("serial_no")}
            rep.optics.append(entry)
            if port.upper().startswith("COM"):
                serial.setdefault(port.upper(), []).append(entry)
        scan = bool(gcfg.get("serial_handle_scan", False))
        rep.serial_handle_scan = scan
        if serial and not scan:
            # 默认路径：不跨进程枚举句柄 —— NtQueryObject 卡住的线程杀不掉，服务一跑
            # 好几天、每个组合技能开始前都查一次，卡住的线程会越积越多；在实验机上复制别的厂家
            # 进程的句柄也太侵入，容易惹杀毒软件。只按「可能占串口的厂家程序在跑」给 warn。
            procs = self._processes() if procs is None else procs
            sv = {str(x).lower() for x in
                  (gcfg.get("serial_vendor_processes") or DEFAULT_SERIAL_VENDOR_PROCESSES)}
            running = [p for p in procs if str(p.get("name") or "").lower() in sv]
            for port, entries in serial.items():
                for e in entries:
                    e["owners"] = None
                    e["scan"] = "disabled"
                if not any(e["enabled"] for e in entries):
                    continue
                ids = ", ".join(str(e["id"]) for e in entries)
                for p in running:
                    nm = str(p.get("name") or "")
                    out.append(Finding(
                        kind="serial_port", resource=f"{port}（{ids}）", severity="warn",
                        pid=p.get("pid"), process=nm,
                        detail=tr(f"{vendors.get(nm.lower(), nm)} 在运行，可能占着 {port}（没开句柄"
                                  "扫描，查不了确切归属；要查就在 port_guard.json 里设 "
                                  "serial_handle_scan=true）",
                                  f"{vendors.get(nm.lower(), nm)} is running and may hold {port} "
                                  "(handle scan is off, so the exact owner is unknown; to check, set "
                                  "serial_handle_scan=true in port_guard.json)")))
        if serial and scan:
            try:
                res = self._serial_owners(sorted(serial))
            except Exception as exc:  # noqa: BLE001
                res = {"owners": {}, "complete": False, "error": f"{type(exc).__name__}: {exc}"}
            if not res.get("complete", False):
                rep.complete = False
                rep.errors.append(tr("串口归属没查完整：", "Serial port ownership check incomplete: ")
                                  + str(res.get("error") or
                                        tr(f"卡住 {res.get('stuck', 0)} 个句柄，"
                                           f"{len(res.get('denied_pids') or [])} 个进程无权限",
                                           f"{res.get('stuck', 0)} handles stuck, "
                                           f"{len(res.get('denied_pids') or [])} processes denied")))
            # 厂家界面以管理员身份运行时，我们复制不了它的句柄 —— 说不清它占没占串口，要明说。
            denied = set(res.get("denied_pids") or []) | set(res.get("stuck_pids") or [])
            if denied:
                procs = self._processes() if procs is None else procs
                for p in procs:
                    nm = str(p.get("name") or "").lower()
                    if p.get("pid") in denied and nm in vendors and any(
                            e["enabled"] for es in serial.values() for e in es):
                        out.append(Finding(
                            kind="serial_port", resource=", ".join(sorted(serial)),
                            severity="warn", pid=p.get("pid"), process=p.get("name") or "",
                            detail=tr(f"{vendors[nm]}（pid {p.get('pid')}）的句柄查不了（以管理员身份"
                                      "运行？），无法确认它是否占着这些串口",
                                      f"Cannot inspect the handles of {vendors[nm]} (pid {p.get('pid')}) "
                                      "(running as administrator?); cannot confirm whether it holds "
                                      "these serial ports")))
            owners = res.get("owners") or {}
            for port, entries in serial.items():
                pids = [p for p in owners.get(port, []) if int(p) != self.own_pid]
                for e in entries:
                    e["owners"] = pids
                    e["mast_holds"] = self.own_pid in owners.get(port, [])
                    if port in (res.get("unmapped") or []):
                        e["present"] = False
                for pid in pids:
                    info = self._proc(pid)
                    enabled = any(e["enabled"] for e in entries)
                    ids = ", ".join(str(e["id"]) for e in entries)
                    out.append(Finding(
                        kind="serial_port", resource=f"{port}（{ids}）",
                        severity="conflict" if enabled else "info", pid=int(pid),
                        process=info.get("name") or "", exe=info.get("exe"),
                        cmdline=info.get("cmdline"),
                        detail=tr(f"{info.get('name') or '某进程'} 打开着 {port}，MAST 的 {ids} "
                                  f"{'打不开它' if enabled else '（这台设备在 MAST 里是关着的，只记录）'}",
                                  f"{info.get('name') or 'A process'} has {port} open, "
                                  + (f"so MAST cannot open {ids}" if enabled else
                                     "but this device is disabled in MAST (recorded only)"))))
        usb = [d for d in devices if str(d.get("type")) in _USB_VENDORS]
        if usb:
            procs = self._processes() if procs is None else procs
            running = {}
            for p in procs:
                running.setdefault(str(p.get("name") or "").lower(), []).append(p)
            for d in usb:
                for pname in _USB_VENDORS[str(d.get("type"))]:
                    for p in running.get(pname, []):
                        enabled = bool(d.get("enabled", True))
                        out.append(Finding(
                            kind="usb_device",
                            resource=tr(f"{d.get('id')}（序列号 {d.get('serial_no') or '?'}）",
                                        f"{d.get('id')} (serial no. {d.get('serial_no') or '?'})"),
                            severity="warn" if enabled else "info", pid=p.get("pid"),
                            process=p.get("name") or "", exe=p.get("exe"),
                            detail=tr(f"{vendors.get(pname, p.get('name'))} 在运行，可能占着 "
                                      f"{d.get('id')}（USB 设备没法不打开就查归属，只提醒）",
                                      f"{vendors.get(pname, p.get('name'))} is running and may hold "
                                      f"{d.get('id')} (USB device ownership cannot be checked without "
                                      "opening it; advisory only)")))
        # 厂家界面在跑：只记 info，给冲突定位用
        procs = self._processes() if procs is None else procs
        for p in procs:
            nm = str(p.get("name") or "").lower()
            if nm in vendors:
                out.append(Finding(kind="vendor_gui", resource=vendors[nm], severity="info",
                                   pid=p.get("pid"), process=p.get("name") or "", exe=p.get("exe"),
                                   detail=tr(f"{vendors[nm]} 在运行", f"{vendors[nm]} is running")))
        return out

    def _apply_allowlist(self, findings: list[Finding], gcfg: dict) -> None:
        rules = [r for r in (gcfg.get("allow") or []) if isinstance(r, dict)]
        for f in findings:
            if f.severity == "info":
                continue
            for r in rules:
                proc = str(r.get("process") or "").lower()
                rip = str(r.get("remote_ip") or "")
                ports = {int(x) for x in (r.get("ports") or []) if str(x).isdigit()}
                f_port = None
                if f.resource.startswith("tcp:"):
                    try:
                        f_port = int(f.resource[4:].split("(")[0])
                    except ValueError:
                        f_port = None
                if proc and proc != (f.process or "").lower():
                    continue
                if rip and not (f.remote or "").startswith(rip + ":"):
                    continue
                if ports and f_port not in ports:
                    continue
                if not proc and not rip and not ports:
                    continue
                f.severity = "info"
                f.allowed_by = str(r.get("note") or r)
                break

    def _alert(self, rep: PortGuardReport) -> None:
        keys = {f.key() for f in rep.conflicts}
        if keys == self._last_conflict_keys:
            return
        prev = self._last_conflict_keys
        self._last_conflict_keys = keys
        if keys:
            logger.warning("端口归属检查：%s", rep.message)
        elif prev:
            logger.info("端口归属冲突已解除")
        if not self.publish:
            return
        try:
            from mast.core.events import EventBus

            EventBus.get().publish_satellite("", "port_guard", conflicts=len(rep.conflicts),
                                             ok=rep.ok, summary=rep.message[:300])
        except Exception:  # noqa: BLE001
            logger.debug("port_guard EventBus publish failed", exc_info=True)
        try:
            from mast.live import get_live_hub

            live = get_live_hub()
            live.declare("mast/port_guard", "status", title=tr("端口归属", "Port ownership"),
                         source="mast")
            live.publish("mast/port_guard", {"t": time.time(), "text": rep.message[:500],
                                             "level": "warn" if keys else "info"})
        except Exception:  # noqa: BLE001
            logger.debug("port_guard LiveHub publish failed", exc_info=True)


# ── 连接失败时点名占用者（core/connection.py 调） ───────────────────────────

_HINT_CACHE: dict[tuple[str, int], tuple[float, str]] = {}
_HINT_CACHE_S = 2.0


def port_occupants(host: str, port: int, *, own_pid: int | None = None,
                   connections_fn: Callable[[], list] | None = None,
                   local_ips_fn: Callable[[], set[str]] | None = None,
                   process_info_fn: Callable[[int], dict] | None = None) -> list[dict]:
    """``host:port`` 上**不是本进程**的已建立 TCP 客户端：``[{pid, process, remote}]``。

    只用一次 ``psutil.net_connections``（几十毫秒），不做句柄枚举。Nanonis 在本机时也看
    服务端那一侧，别的机器连进来的报 ``remote``。永不抛：查不了就返回空表。
    """
    try:
        own = int(own_pid if own_pid is not None else os.getpid())
        conns = (connections_fn or _default_connections)()
        local_ips = (local_ips_fn or _default_local_ips)()
        info_fn = process_info_fn or _default_process_info
        try:
            host_ip = str(ipaddress.ip_address(str(host).strip()))
        except ValueError:
            host_ip = socket.gethostbyname(str(host).strip())
        host_local = host_ip in local_ips or host_ip.startswith("127.")
        port = int(port)

        def _ip(a):
            return str(getattr(a, "ip", a[0] if a else "")).split("%")[0]

        def _port(a):
            return int(getattr(a, "port", a[1])) if a else None

        def _local(ip: str) -> bool:
            return ip in local_ips or ip.startswith("127.") or ip == "::1"

        out: list[dict] = []
        seen: set = set()
        for c in conns:
            st = str(getattr(c, "status", "") or "")
            la, ra = getattr(c, "laddr", None), getattr(c, "raddr", None)
            if st not in _ACTIVE_STATES or not ra:
                continue
            # 客户端那一侧：本机进程连着 host:port
            if _port(ra) == port and (_ip(ra) == host_ip or (host_local and _local(_ip(ra)))):
                pid = getattr(c, "pid", None)
                if pid is None or int(pid) == own or ("pid", pid) in seen:
                    continue
                seen.add(("pid", pid))
                info = info_fn(int(pid)) or {}
                out.append({"pid": int(pid), "process": info.get("name") or "", "remote": None})
            # 服务端那一侧（Nanonis 在本机）：别的机器连进来的
            elif host_local and _port(la) == port and not _local(_ip(ra)):
                key = ("remote", _ip(ra), _port(ra))
                if key in seen:
                    continue
                seen.add(key)
                out.append({"pid": None, "process": "", "remote": f"{_ip(ra)}:{_port(ra)}"})
        return out
    except Exception:  # noqa: BLE001 — 只是一句提示，查不了就不提示
        return []


def occupant_hint(host: str, port: int, **kw) -> str:
    """给连接失败的日志/错误追加的一句话：「6501 已被 LabVIEW.exe（pid 1234）占用」。

    没发现别的客户端就返回空串（于是原来的错误信息原样不变）。按 (host, port) 缓存 2 s：
    重连风暴里不会每次都扫一遍连接表。永不抛。
    """
    try:
        key = (str(host), int(port))
        now = time.monotonic()
        hit = _HINT_CACHE.get(key)
        if hit is not None and now - hit[0] < _HINT_CACHE_S and not kw:
            return hit[1]
        occ = port_occupants(host, port, **kw)
        if not occ:
            text = ""
        else:
            who = tr("、", ", ").join(
                (tr(f"{o['process'] or '未知进程'}（pid {o['pid']}）",
                    f"{o['process'] or 'unknown process'} (pid {o['pid']})")
                 if o.get("pid") is not None
                 else tr(f"来自 {o['remote']} 的客户端", f"client from {o['remote']}")) for o in occ[:4])
            text = tr(f" —— {port} 已被 {who}占用：Nanonis 的这个口同时只服务一个控制方。"
                      "MAST 是控制枢纽，请先让那边断开（MAST 不会替你关它）。",
                      f" -- {port} is already held by {who}: this Nanonis port serves only one "
                      "controller at a time. MAST is the control hub; disconnect it there first "
                      "(MAST will not close it for you).")
        if not kw:
            _HINT_CACHE[key] = (now, text)
        return text
    except Exception:  # noqa: BLE001
        return ""


def serial_open_hint(port: str, *, processes_fn: Callable[[], list[dict]] | None = None,
                     guard_config_path: Path | None = None,
                     serial_owners_fn: Callable[[list[str]], dict] | None = None,
                     process_info_fn: Callable[[int], dict] | None = None,
                     own_pid: int | None = None) -> str:
    """MAST 自己打开配置的 COM 口**失败时**，给错误信息追加一句「可能被谁占着」。

    驱动的用法（``instruments/*`` 里把 ``SerialUnavailable`` 转成 ``InstrumentUnavailable`` 的地方）::

        raise InstrumentUnavailable(f"PZTC on {port}: {exc}{serial_open_hint(port)}")

    默认**不扫句柄**：只列出正在运行的、可能占串口的厂家程序（``serial_vendor_processes``，
    默认 LabVIEW、PI MikroMove）。``port_guard.json`` 设了 ``serial_handle_scan: true`` 时才用句柄
    扫描给确切占用者。永不抛；什么都没找到返回空串（原错误原样不变）。只在失败之后调用，所以
    不会为了检测去打开串口。
    """
    try:
        p = str(port or "").strip()
        if not p:
            return ""
        gcfg, _err = load_guard_config(guard_config_path)
        tail = tr("。MAST 是控制枢纽：请先在那边关掉这个串口（MAST 不会替你关它）。",
                  ". MAST is the control hub: close this serial port there first "
                  "(MAST will not close it for you).")
        if bool(gcfg.get("serial_handle_scan", False)):
            res = (serial_owners_fn or _default_serial_owners)([p.upper()])
            own = int(own_pid if own_pid is not None else os.getpid())
            pids = [x for x in (res.get("owners") or {}).get(p.upper(), []) if int(x) != own]
            if pids:
                info_fn = process_info_fn or _default_process_info
                who = tr("、", ", ").join(
                    tr(f"{(info_fn(int(x)) or {}).get('name') or '未知进程'}（pid {x}）",
                       f"{(info_fn(int(x)) or {}).get('name') or 'unknown process'} (pid {x})")
                    for x in pids[:4])
                return tr(f" —— {p} 正被 {who}打开着{tail}",
                          f" -- {p} is currently opened by {who}{tail}")
        sv = {str(x).lower() for x in
              (gcfg.get("serial_vendor_processes") or DEFAULT_SERIAL_VENDOR_PROCESSES)}
        procs = (processes_fn or _default_processes)()
        running = [x for x in procs if str(x.get("name") or "").lower() in sv]
        if not running:
            return ""
        who = tr("、", ", ").join(
            tr(f"{x.get('name')}（pid {x.get('pid')}）", f"{x.get('name')} (pid {x.get('pid')})")
            for x in running[:4])
        return tr(f" —— {p} 打不开，可能被占用：{who}在运行（没开句柄扫描，查不了确切归属）{tail}",
                  f" -- {p} cannot be opened and may be in use: {who} running "
                  f"(handle scan is off, so the exact owner is unknown){tail}")
    except Exception:  # noqa: BLE001 — 一句提示，绝不影响驱动的错误路径
        return ""


# ── 模块级入口 ──────────────────────────────────────────────────────────────

_guard: PortGuard | None = None
_guard_lock = threading.Lock()


def get_port_guard() -> PortGuard:
    global _guard
    if _guard is None:
        with _guard_lock:
            if _guard is None:
                _guard = PortGuard()
    return _guard


def set_port_guard(guard: PortGuard | None) -> None:
    global _guard
    with _guard_lock:
        _guard = guard


def configure(*, nanonis: Any = None) -> None:
    """runtime 启动时调：用它实际在用的 Nanonis 配置（主机与四个角色口）。"""
    g = get_port_guard()
    if nanonis is not None:
        g.nanonis = nanonis


def check(*, refresh: bool = False, reason: str = "") -> PortGuardReport:
    """组合技能开始前调：``report.ok`` 为假就拒绝开始，把 ``report.message`` 告诉操作员。
    结果缓存 5 s；``refresh=True`` 现查。永不抛（检查本身失败时 ``complete=False``、写进 errors）。"""
    try:
        return get_port_guard().check(refresh=refresh, reason=reason)
    except Exception as exc:  # noqa: BLE001
        return PortGuardReport(checked_ms=int(time.time() * 1000), ok=True, enforce=True,
                               complete=False, errors=[f"{type(exc).__name__}: {exc}"],
                               reason=reason)


def ensure_no_conflicts(purpose: str = "") -> PortGuardReport:
    """有阻断性冲突就抛 :class:`PortConflictError`（消息里点名冲突方），否则返回报告。"""
    rep = check(reason=purpose)
    if not rep.ok:
        raise PortConflictError(rep)
    return rep


def last_report() -> PortGuardReport | None:
    return get_port_guard().last_report()


def check_async(*, reason: str = "") -> threading.Thread:
    """后台查一次（启动时用，不挡启动）。"""
    t = threading.Thread(target=lambda: check(refresh=True, reason=reason),
                         name="port-guard-check", daemon=True)
    t.start()
    return t


__all__ = ["Finding", "PortGuardReport", "PortConflictError", "PortGuard", "get_port_guard",
           "set_port_guard", "configure", "check", "ensure_no_conflicts", "last_report",
           "check_async", "query_dos_device", "win_file_handle_owners", "load_guard_config",
           "port_occupants", "occupant_hint", "serial_open_hint", "DEFAULT_NANONIS_PORTS",
           "DEFAULT_VI_SERVER_PORT", "DEFAULT_SERIAL_VENDOR_PROCESSES"]
