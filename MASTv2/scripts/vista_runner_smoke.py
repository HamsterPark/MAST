"""Runner smoke test: MAST's own unattended runner drives the simulator (Claude Code, subscription).

Starts the STM-Bench simulator and the real MAST service (``vista_sim.SimServiceProcess``), asks
MAST to start an unattended run (``POST /api/ext/v1/runner/start``) with a short task, polls the
run until it ends, optionally stops it as the operator after the first accepted act
(``--stop-after-act``), and writes ``<out>/summary.json`` with the run view and MAST's own records
(acts, frames, refusals). Exit code 0 when the run ended the way the test expects.

Costs one short Claude Code session on the user's subscription. Development and release tool;
not part of MAST at runtime.

Usage::

    set PYTHONUTF8=1
    set PYTHONPATH=<repo>\\MASTv2;<STM-Bench checkout>
    python MASTv2\\scripts\\vista_runner_smoke.py --out <dir> [--stop-after-act]
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve()
sys.path.insert(0, str(SCRIPT.parent))

from vista_sim import SimServiceProcess, http_json  # noqa: E402

TASK = ("Functional test of the instrument tools. Take one scan: action scan, center x_nm 0, y_nm 0, "
        "size_nm 20, pixels 64, line_time_s 0.2. Look at the image and describe it in one sentence. "
        "Measure the frame with mast_stats. Write WORKING.md with what you did and saw. Then end your "
        "reply with [DONE]. Do nothing else.")
H = {"X-MAST-Actor": "runner-smoke", "X-MAST-Session": "runner-smoke-operator",
     "Content-Type": "application/json"}


def _events(sim: SimServiceProcess) -> list[dict]:
    root = Path(sim.info.get("mast_root") or (sim.root / "mast_root"))
    rows: list[dict] = []
    for p in sorted(root.glob("experiments/vista/*/events.jsonl")):
        rows += [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="vista_runner_smoke", description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", default="claude-opus-5-5")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--timeout", type=float, default=1500.0)
    ap.add_argument("--stop-after-act", action="store_true",
                    help="operator stop as soon as the run's first act was accepted")
    a = ap.parse_args(argv)
    out = Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    sim = SimServiceProcess(out / "sim", seed=0, material="Au(111)")
    sim.start()
    summary: dict = {"base_url": sim.base_url}
    try:
        body = {"task": TASK, "client": "claude", "model": a.model, "effort": a.effort,
                "base_url": sim.base_url}
        status, run = http_json("POST", sim.ext_url + "/runner/start", body, headers=H, timeout=120)
        summary["start_status"] = status
        summary["start"] = run
        if status != 202:
            raise SystemExit(f"runner/start answered {status}: {run}")
        run_id = run["run_id"]
        t0 = time.monotonic()
        stopped = False
        view: dict = run
        while time.monotonic() - t0 < a.timeout:
            time.sleep(5)
            _, view = http_json("GET", sim.ext_url + f"/runner/{run_id}", headers=H, timeout=60)
            if a.stop_after_act and not stopped and any(e.get("kind") == "act" for e in _events(sim)):
                s, v = http_json("POST", sim.ext_url + f"/runner/{run_id}/stop",
                                 {"reason": "runner smoke: operator stop"}, headers=H, timeout=60)
                summary["stop_status"] = s
                stopped = True
            if view.get("state") in ("finished", "interrupted") or view.get("outcome"):
                break
        summary["elapsed_s"] = round(time.monotonic() - t0, 1)
        summary["run"] = view
        events = _events(sim)
        acts = [e for e in events if e.get("kind") == "act"]
        summary["acts"] = len(acts)
        summary["frames"] = sorted({f for e in acts for f in (e.get("new_frames") or [])})
        summary["refusals"] = [e.get("code") for e in events if e.get("kind") == "refusal"]
        outcome = str(view.get("outcome") or "")
        if a.stop_after_act:
            ok = outcome == "stopped_by_operator"
        else:
            ok = outcome == "done" and bool(summary["frames"])
        summary["passed"] = ok
    finally:
        sim.stop()
        for name in ("service.log",):
            try:
                shutil.copy2(sim.root / name, out / name)
            except OSError:
                pass
        (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str),
                                          encoding="utf-8")
    print(json.dumps({k: summary.get(k) for k in ("passed", "elapsed_s", "acts", "frames", "refusals")},
                     ensure_ascii=False))
    print("outcome:", (summary.get("run") or {}).get("outcome"), "state:", (summary.get("run") or {}).get("state"))
    return 0 if summary.get("passed") else 1


if __name__ == "__main__":
    sys.exit(main())
