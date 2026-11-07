"""End-to-end tests against the real MAST service running on the STM-Bench simulator.

The service runs in a CHILD process (``MASTv2/scripts/vista_sim.py serve``, through
``SimServiceProcess``): ``tests/conftest.py`` replaces ``nanonis_spm`` with a ``MagicMock`` in
this process, so a runtime built here would talk to the mock, not to the simulator. Every data
store of the child lives under the fixture's tmp directory (``vista_sim.isolation_env``).

Opt-in, like the repository's other live smokes, because it boots the whole service and a
simulated instrument (about a minute)::

    set MAST_E2E_SIM=1
    set PYTHONUTF8=1
    set PYTHONPATH=<repo>\\MASTv2;<STM-Bench checkout>
    .venv-v2-py313\\Scripts\\python.exe -m pytest tests/v2/e2e_sim -m e2e_sim -q

STM-Bench is not a dependency of this repository: put a checkout on ``PYTHONPATH`` (ahead of any
older installed copy). The tests skip when ``stmsim`` / ``stmbench`` cannot be imported.

``MAST_E2E_SIM_ROOT=<dir>`` keeps each service's data root (``service.log``, the simulator's
session dir, MAST's data) under ``<dir>`` instead of pytest's tmp dir: pytest keeps only the
last three tmp dirs of the user, and concurrent sessions rotate them away within minutes.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "MASTv2" / "scripts" / "vista_sim.py"
_OPT_IN = "MAST_E2E_SIM"
_ROOT_ENV = "MAST_E2E_SIM_ROOT"


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "e2e_sim: boots the real MAST service on the STM-Bench simulator "
        f"(opt-in: {_OPT_IN}=1, STM-Bench on PYTHONPATH)")


def load_vista_sim():
    """``MASTv2/scripts/vista_sim.py`` as a module (scripts/ is not a package)."""
    name = "_mast_vista_sim"
    mod = sys.modules.get(name)
    if mod is None:
        spec = importlib.util.spec_from_file_location(name, _SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod            # before exec: dataclasses / pickling look it up
        spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def sim_service(tmp_path_factory):
    """A running MAST service on a fresh simulator (seed 0, Au(111), tunnelling).

    Yields the started ``SimServiceProcess``: ``base_url``, ``ext_url``, ``session_dir`` (where
    the simulated controller saves .sxm / .dat) and ``info`` (the child's ready.json: wire ports,
    synced operator facts, initial bias, startup time ...). Stopped with
    ``POST /api/admin/shutdown`` at module teardown.
    """
    if os.environ.get(_OPT_IN) != "1":
        pytest.skip(f"simulator E2E: set {_OPT_IN}=1 (boots MAST + a simulated instrument, ~1 min)")
    vista_sim = load_vista_sim()
    missing = [n for n in ("stmsim", "stmbench") if vista_sim.package_parent(n) is None]
    if missing:
        pytest.skip(f"simulator E2E: {', '.join(missing)} not importable; put an STM-Bench "
                    "checkout on PYTHONPATH")
    keep = os.environ.get(_ROOT_ENV, "").strip()
    if keep:
        Path(keep).mkdir(parents=True, exist_ok=True)
        root = Path(tempfile.mkdtemp(prefix="sim-", dir=keep))
    else:
        root = tmp_path_factory.mktemp("sim")
    svc = vista_sim.SimServiceProcess(root, seed=0, material="Au(111)")
    svc.start()
    print(f"\nMAST on stmsim: {svc.base_url} (startup {svc.info.get('startup_s')} s, "
          f"stmsim from {svc.info.get('stmsim')}, log {svc.log_file})")
    try:
        yield svc
    finally:
        code = svc.stop()
        print(f"\nvista_sim serve exited with {code}")
