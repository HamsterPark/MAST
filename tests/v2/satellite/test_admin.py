"""satellites.json 与管理命令行：令牌只打印一次、配置里只有哈希；SPKI 钉值。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mast.satellite import admin
from mast.satellite import hub_config as hc

_MASTV2 = Path(__file__).resolve().parents[3] / "MASTv2"
_ADMIN_FILE = _MASTV2 / "mast" / "satellite" / "admin.py"


def test_new_token_prints_once_and_stores_only_hash(tmp_path, capsys):
    root = tmp_path / "root"
    assert admin.main(["--root", str(root), "new-token", "rig1", "--note", "Spec"]) == 0
    out = capsys.readouterr()
    token = out.out.strip()
    assert len(token) >= 40 and "\n" not in token
    assert token not in out.err                          # 说明走 stderr，不含令牌
    raw = (root / "config" / "satellites.json").read_text(encoding="utf-8")
    assert token not in raw
    data = json.loads(raw)
    e = hc.find(data, "rig1")
    assert e["token_sha256"] == hc.token_sha256(token) and e["note"] == "Spec"
    assert hc.verify_token(hc.normalized_entry(e), token)
    assert not hc.verify_token(hc.normalized_entry(e), token + "x")
    assert not hc.verify_token(None, token)
    # 已有令牌：不加 --rotate 拒绝
    assert admin.main(["--root", str(root), "new-token", "rig1"]) == 2
    assert admin.main(["--root", str(root), "new-token", "rig1", "--rotate"]) == 0
    token2 = capsys.readouterr().out.strip()
    assert token2 != token
    e = hc.find(hc.load(root / "config" / "satellites.json"), "rig1")
    assert not hc.verify_token(hc.normalized_entry(e), token)
    assert hc.verify_token(hc.normalized_entry(e), token2)


@pytest.mark.parametrize("bad", ["files", "port-guard", "../x", "a b", "", "x" * 33])
def test_bad_or_reserved_ids_refused(tmp_path, bad, capsys):
    assert admin.main(["--root", str(tmp_path), "new-token", bad]) == 2


def test_list_never_shows_tokens_and_enable_disable(tmp_path, capsys):
    root = tmp_path
    admin.main(["--root", str(root), "new-token", "rig1"])
    token = capsys.readouterr().out.strip()
    assert admin.main(["--root", str(root), "list", "--json"]) == 0
    out = capsys.readouterr().out
    assert token not in out and hc.token_sha256(token) not in out
    data = json.loads(out)
    assert data["satellites"][0]["id"] == "rig1"
    assert admin.main(["--root", str(root), "disable", "rig1"]) == 0
    assert hc.find(hc.load(hc.config_path(root)), "rig1")["enabled"] is False
    assert admin.main(["--root", str(root), "enable", "nosuch"]) == 2


def test_cli_runs_as_module_and_as_plain_script(tmp_path):
    env = dict(os.environ, PYTHONPATH=str(_MASTV2), PYTHONUTF8="1")
    r = subprocess.run([sys.executable, "-m", "mast.satellite.admin", "--root", str(tmp_path),
                        "new-token", "sim1"], capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stderr
    tok = r.stdout.strip()
    assert hc.verify_token(hc.normalized_entry(hc.find(hc.load(hc.config_path(tmp_path)), "sim1")),
                           tok)
    # 不经 mast 包、直接跑文件（-I：不吃环境里的 PYTHONPATH）
    r2 = subprocess.run([sys.executable, "-I", str(_ADMIN_FILE), "--root", str(tmp_path), "list"],
                        capture_output=True, text=True, timeout=120,
                        env=dict(os.environ, PYTHONUTF8="1"))
    assert r2.returncode == 0, r2.stderr
    assert "sim1" in r2.stdout


def test_show_pin_matches_net_tls_and_survives_resign(tmp_path, capsys):
    from mast.net import tls as nt
    from mast.satellite.tls import spki_sha256, spki_sha256_local

    root = tmp_path
    cert, key = root / "api key" / "lan_cert.pem", root / "api key" / "lan_key.pem"
    cert.parent.mkdir(parents=True)
    r1 = nt.ensure_lan_cert(cert, key, addresses=["127.0.0.1"], hostname="hubhost")
    assert r1.ok
    assert admin.main(["--root", str(root), "show-pin"]) == 0
    pin = capsys.readouterr().out.strip()
    assert pin == r1.spki_sha256 == nt.spki_sha256(cert) == spki_sha256(cert)
    assert spki_sha256_local(cert.read_bytes()) == pin
    # 加一个新地址 → 用同一把私钥重签：钉值不变
    r2 = nt.ensure_lan_cert(cert, key, addresses=["127.0.0.1", "198.51.100.99"],
                            hostname="hubhost")
    assert r2.ok and r2.key_reused
    assert admin.main(["--root", str(root), "show-pin"]) == 0
    assert capsys.readouterr().out.strip() == pin
    assert admin.main(["--root", str(root / "nope"), "show-pin"]) == 2


def test_config_load_tolerates_missing_and_flags_corrupt(tmp_path):
    p = tmp_path / "config" / "satellites.json"
    assert hc.load(p) == {"satellites": []}
    p.parent.mkdir(parents=True)
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(hc.HubConfigError):
        hc.load(p)
    e = hc.normalized_entry({"id": "x", "heartbeat_s": "abc", "deadman_s": 0.01})
    assert e["heartbeat_s"] == 1.0 and e["deadman_s"] == 0.5


def test_hub_keeps_last_good_config_when_file_turns_corrupt(sat_root):
    from mast.satellite.hub import SatelliteHub

    from tests.v2.satellite.conftest import write_sat_config

    token = write_sat_config(sat_root, "rig1")
    h = SatelliteHub(data_root=sat_root, enabled_fn=lambda: True)
    assert h.authenticate("rig1", token) == (200, "ok")
    p = hc.config_path(sat_root)
    p.write_text("{broken", encoding="utf-8")
    os.utime(p, (p.stat().st_atime + 5, p.stat().st_mtime + 5))
    assert h.authenticate("rig1", token) == (200, "ok")   # 沿用上一次的好配置
    assert h.status()["config_error"]
