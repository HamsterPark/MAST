"""卫星管理命令行（契约 §5.1）::

    python -m mast.satellite.admin [--root <数据根>] new-token <id> [--note 备注] [--rotate]
    python -m mast.satellite.admin [--root <数据根>] list [--json]
    python -m mast.satellite.admin [--root <数据根>] show-pin [--cert <lan_cert.pem>]
    python -m mast.satellite.admin [--root <数据根>] enable <id> | disable <id>

装好的 MAST（冻结包，没有 ``python -m``）用同一套子命令，经 MAST.exe 的入口::

    cmd /c "C:\\MAST\\MAST.exe" --satellite-admin new-token <id>

（MAST.exe 是窗口程序：要经 ``cmd /c`` 起，标准输出才接得上；见 :func:`launcher_main`。）

- ``new-token``：生成 256 位令牌，**只打印这一次**（标准输出单独一行，便于管道）；
  ``<数据根>/config/satellites.json`` 里只写它的 SHA-256。已有令牌时要加 ``--rotate``
  （旧令牌立刻失效；正在连着的卫星下次重连时被拒）。运行中的 MAST 会在 2 s 内读到新配置。
- ``show-pin``：MAST LAN 证书公钥（SubjectPublicKeyInfo）的 SHA-256，填进卫星配置的
  ``hub.spki_sha256``。证书默认在 ``<数据根>/api key/lan_cert.pem``。

没有标准输出（窗口程序被直接起、pythonw）时什么都不做、返回 3：令牌印不出来就不能生成。

只用标准库（``show-pin`` 另需 cryptography）：也可以不经 mast 包、直接 ``python admin.py …`` 运行。
数据根：``--root`` > 环境变量 ``MAST2_PROJECT_ROOT`` > MAST 的默认数据根。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    # 当脚本直接运行：不 import mast 包（它会拉起一串依赖）。
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import hub_config as hc  # type: ignore[no-redef]
else:
    from mast.satellite import hub_config as hc

tr = hc.tr  # the same pair helper (script mode has no mast package; hub_config carries the fallback)


def _err(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _load(root) -> tuple[Path, dict]:
    p = hc.config_path(root)
    return p, hc.load(p)


def cmd_new_token(ns) -> int:
    try:
        sat_id = hc.validate_sat_id(ns.id)
    except hc.HubConfigError as exc:
        _err(str(exc))
        return 2
    p, data = _load(ns.root)
    entry = hc.find(data, sat_id)
    if entry is not None and entry.get("token_sha256") and not ns.rotate:
        _err(tr(f"卫星 {sat_id} 已有令牌。要轮换请加 --rotate（旧令牌立刻失效）。",
                f"Satellite {sat_id} already has a token. Add --rotate to rotate it "
                f"(the old token stops working immediately)."))
        return 2
    token = hc.new_token()
    entry = hc.set_token(data, sat_id, token, note=ns.note)
    if ns.heartbeat_s is not None:
        entry["heartbeat_s"] = float(ns.heartbeat_s)
    if ns.deadman_s is not None:
        entry["deadman_s"] = float(ns.deadman_s)
    # 先把令牌印出去、印成了再存：「存了哈希、令牌却没人看见」会把卫星锁在门外（--rotate 时
    # 旧令牌已经作废）；反过来「印了没存上」只是作废一枚令牌。
    _err(tr(f"卫星 {sat_id} 的新令牌只显示这一次（下一行），交给卫星保存（卫星那边用 DPAPI 加密）：",
            f"The new token of satellite {sat_id} is shown only this once (next line); hand it "
            f"to the satellite to store (the satellite encrypts it with DPAPI):"))
    print(token, flush=True)
    try:
        hc.save(p, data)
    except OSError as exc:
        _err(tr(f"没能写入 {p}：{exc}。上面这枚令牌作废（配置没改），修好后重跑。",
                f"Could not write {p}: {exc}. The token above is void (the config was not "
                f"changed); fix the problem and run again."))
        return 2
    _err(tr(f"已{'轮换' if ns.rotate else '生成'}：{p} 里只存了它的 SHA-256；运行中的 MAST 2 s 内生效。",
            f"{'Rotated' if ns.rotate else 'Generated'}: only its SHA-256 is stored in {p}; "
            f"a running MAST picks it up within 2 s."))
    return 0


def cmd_list(ns) -> int:
    p, data = _load(ns.root)
    rows = [hc.normalized_entry(e) for e in data.get("satellites") or [] if isinstance(e, dict)]
    if ns.json:
        out = [{k: (v[:8] + "…" if k == "token_sha256" and v else v) for k, v in r.items()}
               for r in rows]
        print(json.dumps({"config": str(p), "satellites": out}, ensure_ascii=False, indent=1))
        return 0
    print(tr(f"配置：{p}（{len(rows)} 颗）", f"Config: {p} ({len(rows)} satellite(s))"))
    for r in rows:
        tok = (r["token_sha256"][:8] + "…") if r["token_sha256"] else tr("（未设）", "(not set)")
        print(tr(f"  {r['id']:<16} {'启用' if r['enabled'] else '停用'}  令牌哈希 {tok}  "
                 f"心跳 {r['heartbeat_s']:g}s  死人开关 {r['deadman_s']:g}s  "
                 f"上传限速 {r['max_upload_bytes_per_s']} B/s  设备 {sorted(r['devices'])}  "
                 f"{r['note']}",
                 f"  {r['id']:<16} {'enabled' if r['enabled'] else 'disabled'}  token hash {tok}  "
                 f"heartbeat {r['heartbeat_s']:g}s  dead-man switch {r['deadman_s']:g}s  "
                 f"upload limit {r['max_upload_bytes_per_s']} B/s  devices {sorted(r['devices'])}  "
                 f"{r['note']}"))
    return 0


def _default_cert(root: Path) -> Path:
    """MAST LAN 证书的位置：与 run_service 同一个来源（``mast.net.tls.lan_cert_paths``）。"""
    try:
        from mast.net.tls import lan_cert_paths

        return Path(lan_cert_paths(root)[0])
    except Exception:  # noqa: BLE001 — 脚本模式或旧树：同一个字面量
        return root / "api key" / "lan_cert.pem"


def cmd_show_pin(ns) -> int:
    root = hc.data_root(ns.root)
    cert = Path(ns.cert) if ns.cert else _default_cert(root)
    if not cert.exists():
        _err(tr(f"找不到证书 {cert}（MAST 以 LAN 模式启动过一次才会生成；或用 --cert 指定）",
                f"Certificate {cert} not found (it is generated the first time MAST starts in "
                f"LAN mode; or point to one with --cert)"))
        return 2
    data = cert.read_bytes()
    pin = None
    try:
        from mast.net.tls import spki_sha256_of  # 与证书签发同一份实现

        pin = spki_sha256_of(data)
    except Exception:  # noqa: BLE001 — 脚本模式或旧树：自己算
        try:
            import hashlib

            from cryptography import x509
            from cryptography.hazmat.primitives import serialization

            c = (x509.load_pem_x509_certificate(data) if b"-----BEGIN" in data
                 else x509.load_der_x509_certificate(data))
            pin = hashlib.sha256(c.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo)).hexdigest()
        except ImportError:
            _err(tr("需要 cryptography 才能算钉值",
                    "The cryptography package is required to compute the pin"))
            return 2
    _err(tr(f"证书：{cert}", f"Certificate: {cert}"))
    _err(tr("卫星配置 hub.spki_sha256 填下一行（证书重签只要复用私钥，这个值不变）：",
            "Put the next line into the satellite config as hub.spki_sha256 (the value stays the "
            "same as long as a re-signed certificate reuses the private key):"))
    print(pin, flush=True)
    return 0


def cmd_enable(ns, on: bool) -> int:
    p, data = _load(ns.root)
    entry = hc.find(data, ns.id)
    if entry is None:
        _err(tr(f"没有卫星 {ns.id}", f"No such satellite: {ns.id}"))
        return 2
    entry["enabled"] = bool(on)
    hc.save(p, data)
    _err(tr(f"卫星 {ns.id} 已{'启用' if on else '停用'}（运行中的 MAST 2 s 内生效）",
            f"Satellite {ns.id} {'enabled' if on else 'disabled'} (a running MAST picks it up "
            f"within 2 s)"))
    return 0


#: 没有标准输出时的返回码（什么都没改）。
EXIT_NO_STDOUT = 3


def build_parser(prog: str = "python -m mast.satellite.admin") -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog=prog, description=tr("MAST 卫星令牌与钉值管理", "MAST satellite token and pin management"))
    ap.add_argument("--root", default=None,
                    help=tr("数据根（默认 MAST2_PROJECT_ROOT 或 MAST 默认）",
                            "Data root (default: MAST2_PROJECT_ROOT, else the MAST default)"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("new-token",
                       help=tr("生成（或 --rotate 轮换）一颗卫星的令牌",
                               "Generate (or, with --rotate, rotate) the token of a satellite"))
    a.add_argument("id")
    a.add_argument("--note", default=None)
    a.add_argument("--rotate", action="store_true")
    a.add_argument("--heartbeat-s", type=float, default=None)
    a.add_argument("--deadman-s", type=float, default=None)
    b = sub.add_parser("list", help=tr("列出已配置的卫星（不显示令牌）",
                                       "List the configured satellites (tokens are not shown)"))
    b.add_argument("--json", action="store_true")
    c = sub.add_parser("show-pin",
                       help=tr("MAST LAN 证书公钥的 SHA-256（卫星的 hub.spki_sha256）",
                               "SHA-256 of the MAST LAN certificate public key "
                               "(the satellite's hub.spki_sha256)"))
    c.add_argument("--cert", default=None)
    for name in ("enable", "disable"):
        s = sub.add_parser(name, help=tr(f"{'启用' if name == 'enable' else '停用'}一颗卫星",
                                         f"{'Enable' if name == 'enable' else 'Disable'} a satellite"))
        s.add_argument("id")
    return ap


def main(argv: list[str] | None = None, *, prog: str = "python -m mast.satellite.admin") -> int:
    ns = build_parser(prog).parse_args(argv)
    if sys.stdout is None:
        # 窗口程序（MAST.exe 被 PowerShell 的 & 直接起、pythonw）没有标准输出：令牌和钉值
        # 印不出来。什么都不改就退出 —— 尤其不能「存了新令牌的哈希、令牌却谁也没看见」：
        # 加了 --rotate 时旧令牌也跟着作废，卫星就再也连不上了。
        _err(tr("没有标准输出，什么都没做。在 PowerShell 里用 cmd /c \"…\\MAST.exe\" "
                "--satellite-admin … 运行。",
                "No standard output; nothing was done. In PowerShell run it as "
                "cmd /c \"...\\MAST.exe\" --satellite-admin ..."))
        return EXIT_NO_STDOUT
    try:
        if ns.cmd == "new-token":
            return cmd_new_token(ns)
        if ns.cmd == "list":
            return cmd_list(ns)
        if ns.cmd == "show-pin":
            return cmd_show_pin(ns)
        if ns.cmd in ("enable", "disable"):
            return cmd_enable(ns, ns.cmd == "enable")
    except hc.HubConfigError as exc:
        _err(str(exc))
        return 2
    return 2


def launcher_main(argv: list[str]) -> int:
    """``MAST.exe --satellite-admin …`` 的入口（``mast2_launcher`` 在接日志之前调它）。

    MAST.exe 是 PyInstaller 的窗口程序（console=False）：只有父进程把句柄交给它，才有标准
    输出。``cmd /c`` 会交（经 ssh 的 PowerShell 里也一样）；PowerShell 的 ``&`` 不交 ——
    那时 :func:`main` 什么都不做、返回 3。

    管道和文件里统一写 UTF-8（ssh 那边按 UTF-8 解码，中文提示不乱码）；直接写控制台时
    Python 本来就用宽字符接口，不受代码页影响，不动它。
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        try:
            if stream is not None and not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass
    return main(argv, prog="MAST.exe --satellite-admin")


if __name__ == "__main__":
    raise SystemExit(main())
