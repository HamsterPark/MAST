"""Shared by ``test_station_invariance.py`` and the golden files it compares against.

The golden files (``golden/<part>.json``) are the answers of ``_invariants_probe.py`` on
the last tree before the station capability table, normalised by :func:`normalize`.
Regenerating them is a deliberate act (a reviewed change of what a station does)::

    # PYTHONPATH must point at the tree whose answers become the golden files
    python tests/v2/station/_invariants.py write-golden <dir with <part>_<profile>.json files> <source>

Only the parts that do not move when a skill is added are pinned this way (``boot``,
``briefing``, ``act``). The tool face is pinned against a frozen copy of the old decision
(``_legacy_face_pre_capabilities.py``), which classifies whatever the live registry holds.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
PROBE = HERE / "_invariants_probe.py"
GOLDEN = HERE / "golden"

#: the parts pinned by golden files, and the profiles that existed before the capability table
PARTS = ("boot", "briefing", "act")
PROFILES = ("stm", "optical")

#: threads whose existence is a station decision (the rest of the boot is not this pin's business:
#: a new unrelated daemon must not turn it red)
STATION_THREADS = frozenset({"InstrumentStateRefresh", "SafetyWatchdog", "CurrentMonitorService",
                             "MapActivityWatcher", "mast-vision-warmup"})

_DROP = frozenset({"mast_file", "capabilities", "part", "profile", "traceback", "describe_before"})


def normalize(part: str, data: dict) -> dict:
    """The probe answer reduced to what the pin compares."""
    if data.get("probe_error"):
        raise AssertionError(f"the {part} probe failed: {data['probe_error']}\n{data.get('traceback', '')}")
    out = {k: v for k, v in data.items() if k not in _DROP}
    if part == "boot":
        out["threads"] = sorted(t for t in out.get("threads", []) if t in STATION_THREADS)
    if part == "briefing":
        text = out.pop("safety_text", None)
        out["safety_has_envelope"] = bool(text and text.lstrip().startswith("- Envelope in force"))
    return out


def _no_blocked_io(stderr: str) -> None:
    """Fail when the child's ``_io_guard`` refused a connect beyond loopback or a serial open."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("_io_guard", Path(__file__).with_name("_io_guard.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.assert_no_blocked_io(stderr)


def run_probe(part: str, profile: str, root: Path, *, tree: Path = REPO, timeout: float = 300) -> dict:
    """Run the probe for one part and profile against ``tree`` in a child process."""
    root.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONPATH": str(tree / "MASTv2"), "PYTHONIOENCODING": "utf-8"}
    env.pop("MAST_STATION_PROFILE", None)
    proc = subprocess.run([sys.executable, str(PROBE), part, profile, str(root)], cwd=str(tree),
                          capture_output=True, text=True, encoding="utf-8", timeout=timeout, env=env)
    _no_blocked_io(proc.stderr)
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("PROBE_JSON ")]
    assert lines, (f"the {part}/{profile} probe printed no result (rc {proc.returncode}):\n"
                   f"{proc.stdout[-1500:]}\n{proc.stderr[-2500:]}")
    return json.loads(lines[-1][len("PROBE_JSON "):])


def load_golden(part: str) -> dict:
    return json.loads((GOLDEN / f"{part}.json").read_text(encoding="utf-8"))


def write_golden(raw_dir: Path, source: str) -> None:
    GOLDEN.mkdir(exist_ok=True)
    for part in PARTS:
        doc = {"source": source, "probe": "tests/v2/station/_invariants_probe.py",
               "note": "answers of the probe on the source commit, normalised by _invariants.normalize"}
        for profile in PROFILES:
            raw = json.loads((raw_dir / f"{part}_{profile}.json").read_text(encoding="utf-8"))
            doc[profile] = normalize(part, raw)
        (GOLDEN / f"{part}.json").write_text(
            json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8",
            newline="\n")


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "write-golden":
        write_golden(Path(sys.argv[2]), sys.argv[3])
    else:
        sys.exit(__doc__)
