"""``MAST.exe --satellite-admin …``：装好的 MAST（冻结包，没有 ``python -m``）跑卫星管理命令的
入口（``mast2_launcher.main`` → ``mast.satellite.admin.launcher_main``）。

钉住三件事：

1. 数据根与 ``run_service`` 是同一个（``data_dir.txt`` 指的那个）。冻结包里 ``project_root()``
   默认是 exe 所在目录；少了入口里那句 ``setdefault``，令牌哈希会写进安装目录，跑着的服务
   读的却是数据根那一份 —— 「生成成功、卫星永远连不上」。
2. 令牌不进 launcher 日志：入口在 ``main()`` 接日志（tee）之前分发。
3. 没有标准输出（窗口程序被 PowerShell 的 ``&`` 直接起）时什么都不改、返回 3 ——
   尤其 ``--rotate``：旧令牌作废、新令牌没人看见，卫星就被锁在门外。

冻结包本身的行为（console=False 下 ``cmd /c`` 才有输出、退出码）在
一个小冻结包上实测过。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

from mast.satellite import hub_config as hc

_LAUNCHER_PATH = Path(__file__).resolve().parents[3] / "mast2_launcher.py"


@pytest.fixture(scope="module")
def launcher():
    spec = importlib.util.spec_from_file_location("mast2_launcher_admin_entry", _LAUNCHER_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """安装版布局：<install>/MAST.exe + data_dir.txt → <data>；环境里不预设任何根。"""
    install, data = tmp_path / "install", tmp_path / "data"
    install.mkdir()
    (install / "data_dir.txt").write_text(str(data), encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(install / "MAST.exe"))
    for var in ("MAST2_PROJECT_ROOT", "MAST_PROJECT_ROOT", "MAST2_USER_ROOT"):
        # 先 setenv 再 delenv：入口会 setdefault MAST2_PROJECT_ROOT，测试结束时要撤掉，
        # 不能漏给后面的测试（只 delenv 一个本来就没有的变量，monkeypatch 不会记下它）。
        monkeypatch.setenv(var, "unset-by-test")
        monkeypatch.delenv(var)
    return install, data


def _run(launcher, monkeypatch, *args: str) -> int:
    monkeypatch.setattr(sys, "argv", ["MAST.exe", "--satellite-admin", *args])
    return launcher.main()


def _files_containing(root: Path, needle: str) -> list[Path]:
    return [f for f in root.rglob("*") if f.is_file()
            and needle in f.read_text(encoding="utf-8", errors="replace")]


def test_new_token_lands_in_the_services_data_root_and_nowhere_else(launcher, installed,
                                                                     monkeypatch, capsys):
    install, data = installed
    assert _run(launcher, monkeypatch, "new-token", "rig1", "--note", "光谱电脑") == 0
    out = capsys.readouterr()
    token = out.out.strip()
    assert len(token) == 43 and "\n" not in token        # 标准输出上只有令牌这一行
    assert token not in out.err
    assert os.environ["MAST2_PROJECT_ROOT"] == str(data.resolve())
    cfg = hc.config_path(data)
    entry = hc.find(hc.load(cfg), "rig1")
    assert entry["token_sha256"] == hc.token_sha256(token) and entry["note"] == "光谱电脑"
    assert not (install / "config").exists(), "写进了安装目录：跑着的服务看不见"
    # 令牌只在调用者的标准输出上：没有 launcher 日志，任何文件里都没有它
    assert not (data / "experiments" / "logs").exists()
    assert _files_containing(install, token) == [] and _files_containing(data, token) == []


def test_list_and_show_pin_read_the_same_root_as_the_service(launcher, installed, monkeypatch,
                                                            capsys):
    from mast.net import tls as nt

    install, data = installed
    cert, key = nt.lan_cert_paths(data.resolve())         # run_service 用的同一个位置
    cert.parent.mkdir(parents=True)
    assert nt.ensure_lan_cert(cert, key, addresses=["127.0.0.1"], hostname="hubhost").ok
    assert _run(launcher, monkeypatch, "show-pin") == 0
    out = capsys.readouterr()
    assert out.out.strip() == nt.spki_sha256(cert)
    assert str(cert) in out.err

    assert _run(launcher, monkeypatch, "new-token", "rig1") == 0
    capsys.readouterr()
    assert _run(launcher, monkeypatch, "list", "--json") == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["config"] == str(hc.config_path(data.resolve()))
    assert [s["id"] for s in listing["satellites"]] == ["rig1"]
    # 已有令牌、不加 --rotate：拒绝，返回 2
    assert _run(launcher, monkeypatch, "new-token", "rig1") == 2


@pytest.mark.parametrize("args", [["new-token", "rig1"], ["new-token", "rig1", "--rotate"],
                                  ["disable", "rig1"]])
def test_without_stdout_nothing_changes(launcher, installed, monkeypatch, args):
    install, data = installed
    cfg = hc.config_path(data)
    hc.save(cfg, {"satellites": [{"id": "rig1", "enabled": True,
                                  "token_sha256": hc.token_sha256("old-token")}]})
    before = cfg.read_bytes()
    monkeypatch.setattr(sys, "stdout", None)              # 窗口程序被 PowerShell 的 & 直接起
    assert _run(launcher, monkeypatch, *args) == 3
    assert cfg.read_bytes() == before                     # 旧令牌还能用，什么都没动


def test_a_failed_save_voids_the_printed_token_and_keeps_the_old_one(tmp_path, monkeypatch,
                                                                     capsys):
    """先印后存：存不上时配置原样不动（旧令牌照常可用），刚印出的那枚作废并明说。"""
    from mast.satellite import admin

    cfg = hc.config_path(tmp_path)
    hc.save(cfg, {"satellites": [{"id": "rig1", "token_sha256": hc.token_sha256("old")}]})
    before = cfg.read_bytes()

    def _boom(path, data):
        raise PermissionError("拒绝访问")

    monkeypatch.setattr(hc, "save", _boom)
    assert admin.main(["--root", str(tmp_path), "new-token", "rig1", "--rotate"]) == 2
    out = capsys.readouterr()
    assert out.out.strip() and "作废" in out.err
    assert cfg.read_bytes() == before


class _FakeArchive:
    def __init__(self, toc):
        self.toc = toc

    def open_embedded_archive(self, name):
        return self


def test_pipes_get_utf8_so_remote_shells_can_read_the_messages(launcher, installed,
                                                               monkeypatch):
    """经 rps / ssh 时标准错误是管道：中文提示按 UTF-8 写（中文 Windows 默认 GBK 会乱码）。"""
    import io

    install, data = installed
    raw_out, raw_err = io.BytesIO(), io.BytesIO()
    out = io.TextIOWrapper(raw_out, encoding="cp936", errors="strict", write_through=True)
    err = io.TextIOWrapper(raw_err, encoding="cp936", errors="strict", write_through=True)
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    assert _run(launcher, monkeypatch, "new-token", "rig1") == 0
    out.flush()
    err.flush()
    assert "新令牌只显示这一次" in raw_err.getvalue().decode("utf-8")
    assert len(raw_out.getvalue().decode("ascii").strip()) == 43
