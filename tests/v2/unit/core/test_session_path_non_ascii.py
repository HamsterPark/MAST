"""The controller's save folder resolves even when its path is not ASCII.

MAST decodes Nanonis strings as latin-1 (one byte, one character; ``nanonis_patch`` explains why
that must not become utf-8). A session folder such as ``D:\\数据\\session`` therefore arrived as
mojibake, ``_resolve_session_dir`` named a folder that does not exist, and every consumer that
looks for scan files (the VISTA archive, GetLatestScanFile, the records page) found nothing.
The simulator sends UTF-8; a real controller (LabVIEW) sends the system ANSI code page (GBK on a
Chinese Windows).
"""
from __future__ import annotations

# ── path setup BEFORE any mast.* imports (canonical block for tests/v2/) ──
import sys
from pathlib import Path

_MASTV2_ROOT = str(Path(__file__).resolve().parents[4] / "MASTv2")
if sys.path and sys.path[0] != _MASTV2_ROOT:
    while _MASTV2_ROOT in sys.path:
        sys.path.remove(_MASTV2_ROOT)
    sys.path.insert(0, _MASTV2_ROOT)
for _n in list(sys.modules):
    if _n == "mast" or _n.startswith("mast."):
        _f = getattr(sys.modules[_n], "__file__", "") or ""
        if "MASTv2" not in _f.replace("\\", "/"):
            del sys.modules[_n]

from types import SimpleNamespace

import pytest

from mast.core.runtime import CoreRuntime, _repair_nanonis_path


def _as_received(path: Path, encoding: str) -> str:
    """What MAST's latin-1 Nanonis decoder hands over for a path sent in ``encoding``."""
    return str(path).encode(encoding).decode("latin-1")


@pytest.mark.parametrize("encoding", ["utf-8", "gbk"])
def test_a_non_ascii_session_folder_is_recovered(tmp_path, encoding):
    folder = tmp_path / "数据 2026" / "session"
    folder.mkdir(parents=True)
    received = _as_received(folder, encoding)
    assert received != str(folder)                      # the mojibake the resolver used to get
    assert _repair_nanonis_path(received) == str(folder)


def test_ascii_and_real_unicode_pass_through_unchanged(tmp_path):
    assert _repair_nanonis_path(str(tmp_path)) == str(tmp_path)
    assert _repair_nanonis_path("D:\\数据\\x") == "D:\\数据\\x"   # already real characters


def test_resolve_session_dir_uses_the_repaired_path(tmp_path, monkeypatch):
    import mast.core.scan_registry as sr

    monkeypatch.setattr(sr, "record_session_dir", lambda d: None)   # process-wide registry
    folder = tmp_path / "假期工作包" / "session"
    folder.mkdir(parents=True)
    received = _as_received(folder, "utf-8")
    raw = (str(folder).encode("utf-8"))
    rec = SimpleNamespace(error=None, return_value=("", b"\x00" + raw, [len(raw), received + "\x00"]))

    class Pool:
        def safe_call(self, method, role="main"):
            assert method == "Util_SessionPathGet"
            return rec

    rt = SimpleNamespace(_pool=Pool(), _session_dir_cache=None)
    got = CoreRuntime._resolve_session_dir(rt)
    assert got == str(folder)
