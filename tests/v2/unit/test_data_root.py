"""运行时写的用户数据都从 ``mast._runtime_paths.data_root()`` 走。

``data_root()`` = ``MAST_DATA_DIR`` > ``project_root()``。这些写入点原先各自直接取
``project_root()``,而大多数测试不改 project_root,于是测试数据一直落在检出里 —— 开发机上
那正是真实计费账本、park board、电流监控库所在的地方(2026-11-09:账本 5,549 条里 2,792 条
是测试写的)。``tests/v2/conftest.py`` 现在给每条测试设 ``MAST_DATA_DIR``;这里钉住的是
「每个写入点真的跟着它走」—— 将来谁在某处又写回 ``project_root()``,这里先红,
而不是等到检出里又攒出一堆测试数据。
"""
from __future__ import annotations

import sys
from pathlib import Path

_MASTV2_ROOT = str(Path(__file__).resolve().parents[3] / "MASTv2")
if sys.path[0] != _MASTV2_ROOT:
    while _MASTV2_ROOT in sys.path:
        sys.path.remove(_MASTV2_ROOT)
    sys.path.insert(0, _MASTV2_ROOT)
for _n in list(sys.modules):
    if _n == "mast" or _n.startswith("mast."):
        _f = getattr(sys.modules[_n], "__file__", "") or ""
        if "MASTv2" not in _f.replace("\\", "/"):
            del sys.modules[_n]

import pytest  # noqa: E402

from mast._runtime_paths import data_root, project_root  # noqa: E402


def test_without_the_override_it_is_the_project_root(tmp_path, monkeypatch):
    monkeypatch.delenv("MAST_DATA_DIR", raising=False)
    monkeypatch.setenv("MAST2_PROJECT_ROOT", str(tmp_path))
    assert data_root() == project_root() == tmp_path.resolve()


def test_the_override_wins_and_blank_means_unset(tmp_path, monkeypatch):
    monkeypatch.setenv("MAST2_PROJECT_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("MAST_DATA_DIR", str(tmp_path / "data"))
    assert data_root() == tmp_path / "data"
    monkeypatch.setenv("MAST_DATA_DIR", "   ")
    assert data_root() == (tmp_path / "root").resolve()


def _writer_paths() -> dict[str, Path]:
    """每个写入点「现在会写到哪」。有几个函数顺手 mkdir —— 在 tmp 里,无妨。"""
    from mast.agents._shared import data_paths
    from mast.billing import ledger
    from mast.config import MASTConfig, Paths
    from mast.core import park_board
    from mast.envhistory import store as envhistory_store
    from mast.monitoring import export as monitor_export
    from mast.monitoring import store as monitor_store
    from mast.skills.builtins import best_frame, scan_frame
    from mast.skills.composite import graph_executor
    from mast.vision.scan_monitor import vision_frames_dir

    return {
        "usage ledger": ledger._default_path(),
        "current monitor": monitor_store._default_dir(),
        "vision buffer (monitor export)": monitor_export._vision_wal_path(),
        "current corpus": monitor_export._default_out_root(),
        "env history": envhistory_store._default_path(),
        "composite progress": graph_executor._sidecar_dir(),
        "frames": scan_frame._frames_dir(),
        "best frames": best_frame._store_path("t"),
        "park board": park_board._board_path(),
        "vision frames": vision_frames_dir(),
        "v1 records (data_paths)": data_paths.experiment_db_path(),
        "v1 records (MASTConfig)": Path(MASTConfig().db_path),
        "v1 records (Paths)": Path(Paths().experiments_db),
        "v2 records": data_paths.v2_experiment_db_path(),
        "drafts": data_paths.drafts_dir(),
        "reviews": data_paths.reviews_dir(),
        "reports": data_paths.reports_dir(),
        "figures": data_paths.figures_dir(create=False),
    }


def test_every_runtime_writer_follows_the_data_root(tmp_path, monkeypatch):
    for var in ("MAST_PARK_BOARD_DIR", "MAST_VISION_FRAMES_DIR", "MAST_EXPERIMENT_DB",
                "MAST_DRAFTS_DIR", "MAST_REVIEWS_DIR", "MAST_REPORTS_DIR", "MAST_FIGURES_DIR"):
        monkeypatch.delenv(var, raising=False)
    root = tmp_path / "root"
    data = tmp_path / "data"
    monkeypatch.setenv("MAST2_PROJECT_ROOT", str(root))
    monkeypatch.setenv("MAST_DATA_DIR", str(data))

    strays = {name: p for name, p in _writer_paths().items() if data not in Path(p).parents}
    assert not strays, f"这些写入点没有跟着 data_root() 走:{strays}"


def test_the_numpy_sandbox_allows_the_data_roots_dirs(tmp_path, monkeypatch):
    """npy_load 的白名单要包含 MAST 自己写数据的地方;测试要能往 tmp 里放 .npy,
    而不是往检出的 data/ 里放(2026-11-09 以前就是这么放的)。"""
    from mast.agents.data_processing.tools import _allowed_npy_roots

    data = tmp_path / "data"
    monkeypatch.setenv("MAST2_PROJECT_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("MAST_DATA_DIR", str(data))
    roots = _allowed_npy_roots()
    assert data / "experiments" in roots and data / "data" in roots, roots


def test_overflowing_tool_returns_land_under_the_data_root(tmp_path, monkeypatch):
    from mast.agents._shared.skill_adapter import _offload_long_summary

    monkeypatch.setenv("MAST_DATA_DIR", str(tmp_path / "data"))
    out = _offload_long_summary("MySkill", "y" * 50, cap=10)
    saved = list((tmp_path / "data" / "artifacts" / "tool_returns").glob("MySkill_*.txt"))
    assert len(saved) == 1 and saved[0].read_text(encoding="utf-8") == "y" * 50
    assert str(saved[0]) in out


@pytest.mark.parametrize("var", ["MAST_PARK_BOARD_DIR", "MAST_VISION_FRAMES_DIR",
                                 "MAST_EXPERIMENT_DB"])
def test_a_specific_override_still_beats_the_data_root(tmp_path, monkeypatch, var):
    """先于 data_root 存在的专用变量照旧优先 —— 设过它们的部署不该因此搬家。"""
    from mast.agents._shared import data_paths
    from mast.core import park_board
    from mast.vision.scan_monitor import vision_frames_dir

    monkeypatch.setenv("MAST_DATA_DIR", str(tmp_path / "data"))
    special = tmp_path / "special"
    monkeypatch.setenv(var, str(special))
    got = {"MAST_PARK_BOARD_DIR": park_board._board_path,
           "MAST_VISION_FRAMES_DIR": vision_frames_dir,
           "MAST_EXPERIMENT_DB": data_paths.experiment_db_path}[var]()
    assert Path(got) == special or special in Path(got).parents
