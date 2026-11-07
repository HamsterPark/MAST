"""The ``rig`` fixture of the runner tests (the kit is ``vista_runner_kit.py``)."""
from __future__ import annotations

import pytest
from vista_runner_kit import Rig


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("MAST2_PROJECT_ROOT", str(tmp_path / "proj"))
    r = Rig(tmp_path)
    r.start_server()
    yield r
    mgr = r.manager
    if mgr is not None:
        mgr.shutdown(timeout_s=20.0)
    r.stop_server()
