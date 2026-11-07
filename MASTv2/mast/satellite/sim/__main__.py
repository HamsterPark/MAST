"""命令行：``python -m mast.satellite.sim --hub <url> --id <id> --token <令牌>``。

标准输入上可以敲几条本地命令（模拟卫星窗口上的操作）::

    hold <设备> on|off      人工接管开关（契约 §5.3）
    manual                  模拟有人在 LightField 窗口里采一张谱
    status                  打印连接与缓冲状态
    quit                    退出
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import tempfile
import threading
from pathlib import Path

from mast.satellite.sim.client import SimConfig, SimSatellite
from mast.satellite.sim.devices import DEFAULT_DEVICES, ActionCtx, SimSpectrometer


def _parse(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="python -m mast.satellite.sim",
                                 description="MAST 卫星协议 v1 的 Python 参考卫星（模拟设备）")
    ap.add_argument("--hub", required=True,
                    help="枢纽地址，例如 wss://192.0.2.11:7862/ws/satellite/v1（站点的真实地址只写在"
                         "卫星配置里）")
    ap.add_argument("--id", required=True, dest="sat_id", help="卫星 id（satellites.json 里的 id）")
    tok = ap.add_mutually_exclusive_group(required=True)
    tok.add_argument("--token", help="令牌（python -m mast.satellite.admin new-token 打印的那个）")
    tok.add_argument("--token-file", type=Path, help="从文件读令牌（第一行）")
    ap.add_argument("--spki-sha256", default=None, help="wss:// 时必须给：枢纽证书公钥的 SHA-256")
    ap.add_argument("--allow-plain-loopback", action="store_true",
                    help="允许对 127.0.0.1 用明文 ws://（仅测试）")
    ap.add_argument("--state-dir", type=Path, default=None,
                    help="journal / 上传清单放哪（默认临时目录）")
    ap.add_argument("--watch-dir", type=Path, default=None,
                    help="文件监视目录；模拟光谱仪 save=true 时也把 .spe 写到这里")
    ap.add_argument("--devices", default=",".join(DEFAULT_DEVICES),
                    help=f"要模拟的设备，逗号分隔，默认 {','.join(DEFAULT_DEVICES)}")
    ap.add_argument("--speed", type=float, default=1.0, help="运动与曝光的时间倍率（>1 更快）")
    ap.add_argument("--manual-acq-every", type=float, default=None,
                    help="每隔多少秒模拟一次人工采集（manual=true 的谱）")
    ap.add_argument("--hold", default="", help="启动时就处于人工接管的设备，逗号分隔")
    ap.add_argument("--upload-bytes-per-s", type=int, default=None,
                    help="上传限速（默认用枢纽 welcome 给的值）")
    ap.add_argument("--chunk-bytes", type=int, default=256 * 1024)
    ap.add_argument("--exit-after", type=float, default=None,
                    help="跑这么多秒后自己正常退出（冒烟测试用，免得要从外面杀进程）")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap.parse_args(argv)


def build_config(ns: argparse.Namespace) -> SimConfig:
    token = ns.token
    if token is None:
        raw = ns.token_file.read_bytes()
        # PowerShell 5.1 的 `>` 写 UTF-16，Set-Content -Encoding utf8 写带 BOM 的 UTF-8：都认。
        text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8-sig")
        token = text.strip().splitlines()[0].strip()
    state_dir = ns.state_dir or Path(tempfile.mkdtemp(prefix=f"mast-sim-{ns.sat_id}-"))
    devices = {}
    for d in [x.strip() for x in ns.devices.split(",") if x.strip()]:
        if d not in DEFAULT_DEVICES:
            raise SystemExit(f"不认识的设备 {d!r}（可选 {list(DEFAULT_DEVICES)}）")
        devices[d] = DEFAULT_DEVICES[d]
    return SimConfig(
        hub_url=ns.hub, satellite_id=ns.sat_id, token=token, state_dir=state_dir,
        watch_dir=ns.watch_dir, devices=devices, spki_sha256=ns.spki_sha256,
        allow_plain_loopback=ns.allow_plain_loopback, chunk_bytes=ns.chunk_bytes,
        upload_bytes_per_s=ns.upload_bytes_per_s, speed=ns.speed,
        manual_acq_every_s=ns.manual_acq_every,
        hold=tuple(x.strip() for x in ns.hold.split(",") if x.strip()))


def _stdin_loop(sat: SimSatellite, stop: threading.Event) -> None:
    for line in sys.stdin:
        parts = line.strip().split()
        if not parts:
            continue
        try:
            if parts[0] == "quit":
                stop.set()
                sat.stop()
                if sat.loop is not None:
                    sat.loop.call_soon_threadsafe(lambda: [t.cancel() for t in asyncio.all_tasks(sat.loop)])
                return
            if parts[0] == "hold" and len(parts) == 3:
                sat.set_operator_hold(parts[1], parts[2].lower() in ("on", "1", "true"))
                print(f"{parts[1]} operator_hold={parts[2]}", flush=True)
            elif parts[0] == "manual":
                for d in sat.devices.values():
                    if isinstance(d, SimSpectrometer):
                        ctx = ActionCtx(None, "manual")
                        sat.emit(d.id, "manual_activity", {"what": "命令行模拟的人工采集"})
                        asyncio.run_coroutine_threadsafe(
                            d._run_job("acquire", d.act_acquire({"frames": 1}, ctx), ctx), sat.loop)
            elif parts[0] == "status":
                print(f"connected={sat.is_connected()} boot_id={sat.boot_id} evt_seq={sat.evt_seq} "
                      f"unacked={sat.unacked} dropped={sat.dropped_events} "
                      f"deadman_trips={sat.deadman_trips} last_close={sat.last_close}", flush=True)
            else:
                print("命令：hold <dev> on|off / manual / status / quit", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"出错：{exc}", flush=True)


def main(argv: list[str] | None = None) -> int:
    ns = _parse(argv)
    logging.basicConfig(level=logging.DEBUG if ns.verbose else logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    cfg = build_config(ns)
    sat = SimSatellite(cfg)
    logging.getLogger("mast.satellite.sim").info(
        "参考卫星 %s 启动：boot_id=%s，设备 %s，状态目录 %s", cfg.satellite_id, sat.boot_id,
        list(cfg.devices), cfg.state_dir)
    stop = threading.Event()
    if sys.stdin is not None and sys.stdin.isatty():
        threading.Thread(target=_stdin_loop, args=(sat, stop), daemon=True).start()

    async def _main() -> None:
        task = asyncio.create_task(sat.run())
        if ns.exit_after:
            await asyncio.sleep(ns.exit_after)
            sat.stop()
            await sat.drop_connection("exit-after")
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    try:
        asyncio.run(_main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    print(f"exit: connects={sat.connects} evt_seq={sat.evt_seq} boot_id={sat.boot_id}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
