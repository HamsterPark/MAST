"""NanonisMotorGroupMove on the Nanonis Mimea simulator — opt-in, skipped by default.

Runs only with ``MAST_SIM_MOTOR_GROUPS=1`` (host/ports: the NanonisConfig
defaults, i.e. the local simulator). Hold the simulator lock while it runs and
release it afterwards::

    <acquire the simulator lock with your lock tool, waiting up to 1800 s>
    $env:MAST_SIM_MOTOR_GROUPS = "1"; python -m pytest tests/v2/instruments/test_motor_groups_simulator.py
    <release the simulator lock>

``tests/conftest.py`` swaps ``nanonis_spm`` for a MagicMock in this process, so
the simulator is driven from a child process (``_motor_groups_sim_child.py``)
and this module checks the JSON it writes. What it pins on the real controller
software, through a real ExecutionContext and pool (the unit tests use a
double): write → verify → chunked steps → restore → verify; verify_only never
writes; an abort between chunks lets Motor_StopMove through, refuses the
restore write and latches every coarse move (contract §10.3). The child is
never killed — a process holding Nanonis connections is only closed gracefully.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("MAST_SIM_MOTOR_GROUPS", "") != "1",
    reason="Nanonis simulator test: set MAST_SIM_MOTOR_GROUPS=1 and hold the simulator lock")

CHILD = Path(__file__).with_name("_motor_groups_sim_child.py")
WAIT_S = 300.0


@pytest.fixture(scope="module")
def sim(tmp_path_factory):
    root = tmp_path_factory.mktemp("mg_sim_root")
    out = root / "result.json"
    env = dict(os.environ, MAST2_PROJECT_ROOT=str(root),
               MAST_EXPERIMENT_ROOT=str(root / "experiments"))
    proc = subprocess.Popen([sys.executable, "-I", "-X", "utf8", str(CHILD), str(out)],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        log, _ = proc.communicate(timeout=WAIT_S)
    except subprocess.TimeoutExpired:
        pytest.fail(f"simulator child still running after {WAIT_S:.0f} s (pid {proc.pid}) — "
                    "left alone on purpose: it holds Nanonis connections and is only ever "
                    "closed gracefully")
    assert out.is_file(), log.decode("utf-8", "replace")[-3000:]
    res = json.loads(out.read_text(encoding="utf-8"))
    assert "fatal" not in res, res["fatal"]
    assert res["closed"] is True
    return res


def test_the_autonomous_path_never_writes_the_drive(sim):
    r = sim["w&r autonomous"]
    assert not r["success"] and "硬闸" in r["error"]
    assert "Motor_FreqAmpSet" not in [m for m, _ in r["calls"]]
    assert "Motor_StartMove" not in [m for m, _ in r["calls"]]
    assert sim["drive after autonomous"] == sim["base"]


def test_write_and_restore_round_trip(sim):
    base = sim["base"]
    f0, _a0 = base["0"]
    for d in ("+", "-"):
        r = sim[f"w&r {d}40"]
        assert r["success"], r["error"]
        assert r["steps_done"] == 40
        assert r["drive"]["after_write"] == [f0 + 1000.0, 25.0]
        assert r["drive"]["restored"]["verified"] is True
        assert sim[f"drive after w&r {d}40"] == base
        assert [m for m, _ in r["calls"]].count("Motor_StartMove") == 2      # 40 = 20 + 20


def test_verify_only_never_writes(sim):
    r = sim["verify_only"]
    assert not r["success"] and "verify_only" in r["error"]
    assert "Motor_FreqAmpSet" not in [m for m, _ in r["calls"]]
    assert "Motor_StartMove" not in [m for m, _ in r["calls"]]
    assert sim["drive after verify_only"] == sim["base"]


def test_abort_between_chunks_stops_refuses_the_restore_and_latches(sim):
    r = sim["abort"]
    f0, _a0 = sim["base"]["0"]
    assert not r["success"] and r["aborted"] and r["steps_done"] == 20
    assert "未还原" in r["error"]
    assert ["Motor_StopMove", False] in r["calls"]             # the stop passed the gate
    # the restore never reached the controller: ctx.run refuses sub-skills after an
    # abort, so the only FreqAmpSet sent is the configured-value write
    assert [m for m, _ in r["calls"]].count("Motor_FreqAmpSet") == 1
    assert sim["latch after abort"] is not None
    assert sim["drive after abort"]["0"] == [f0 + 1000.0, 25.0]
    assert sim["drive at end"] == sim["base"]                   # put back by the child


def test_the_manual_panel_buttons_run_as_the_frontend_builds_them(sim):
    """local_manual_devices' jog / stop for a group, executed with exactly the
    fixed + axis + direction + steps the panel sends (contract §13.1)."""
    assert sim["manual notes"] == [] and sim["manual ids"] == ["local/nanonis_group/3"]
    for key in ("manual jog +10", "manual jog -10"):
        r = sim[key]
        assert r["success"], r["error"]
        assert r["steps_done"] == 10
        assert "Motor_FreqAmpSet" not in [m for m, _ in r["calls"]]     # verify_only
    over = sim["manual jog 30"]
    assert not over["success"] and "单次上限" in over["error"]
    assert "Motor_StartMove" not in [m for m, _ in over["calls"]]
    assert sim["manual stop"]["success"]
    assert ["Motor_StopMove", False] in sim["manual stop"]["calls"]
    up = sim["manual readout +10"]["axes"][0]
    assert up["extra"]["odometer_steps"] == 10 and up["position"] is None
    assert sim["manual readout end"]["axes"][0]["extra"]["odometer_steps"] == 0
    assert sim["drive after manual"] == sim["base"]
