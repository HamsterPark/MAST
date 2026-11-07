#!/usr/bin/env python
"""Read-only probe of WITec Control's COM Automation (BUCS) — run it ON the WITec PC before anything else.

It answers, without changing anything on the instrument:

1. **Does a 64-bit Python reach WITec Control through COM at all?** (The BUCS interfaces and type library are
   registered for 32-bit clients only — docs/v2/design/optical_station_witec.md trap 6. If this probe fails with
   0x8002801D / "Interface not registered", the answer is a 32-bit bridge process, NOT a registry edit.)
2. Which WITec Control is it (``ProgramVersion``), is COM write access held (it must not be: the probe never asks),
   is the "COM Automation" button reachable (``AcceptsRemoteConnection``).
3. The **complete parameter table of this machine** (path + type, exactly what ``GetSubSystemsList`` returns),
   written to JSON and to a ``type:path`` text file; every enum with its labels (gratings, spectral units …).
4. A handful of **status reads** (laser, analyzer, stage, beam path, objective, current saved state …).
5. Which **driver features** this WITec Control supports (silent spectrum, centre wavelength, grating, analyzer
   series, …) — i.e. whether the paths the driver needs exist here.
6. **The laser-safety questions the driver cannot settle by itself** (questions Q-a / Q-b below): what the table says
   about shutters (``emission_facts``), the list of things a person has to try (``manual_checks``), and a **watch
   mode** (``--watch SECONDS``) that logs every change of the selected laser, its shutter and power, the sequencer,
   the beam path and the saved state while you do those things by hand in WITec Control — still read-only.

It never writes a parameter, never presses a trigger, never requests write access. That is structural, not a
promise: the probe talks to WITec through :class:`mast.instruments.witec.bucs.ReadOnlyBucs`, a view that does not
have ``set`` / ``trigger`` / ``request_write_access`` at all (and a test pins that this file contains no call to any
of them).

Prerequisites on the WITec PC
-----------------------------
* WITec Control is running (a person opened it). Do **not** press "COM Automation" for this probe.
* A Python with ``comtypes``. MAST ships its own x64 Python 3.13 (``C:\\MAST\\MASTv2\\pyruntime\\python.exe``) whose
  ``site-packages`` has only numpy. comtypes is pure Python, so do not ``pip install`` into MAST's runtime — give
  the probe a folder that contains it:

      # on any machine with internet:
      pip download comtypes --no-deps -d wheels
      python -m zipfile -e wheels\\comtypes-1.4.17-py3-none-any.whl D:\\comtypes_site
      # copy D:\\comtypes_site to the WITec PC

Run (PowerShell on the WITec PC, as the user that is logged on — session 1, not over SSH session 0)::

    C:\\MAST\\MASTv2\\pyruntime\\python.exe C:\\MAST\\MASTv2\\scripts\\witec\\probe_witec_com.py --mast-root C:\\MAST\\MASTv2 --comtypes-dir D:\\comtypes_site --out D:\\witec_probe

``--mast-root`` is the folder that contains the ``mast`` package (default: guessed from this file's location).
Results land in ``--out`` (default ``./witec_probe_<timestamp>``): ``witec_probe.json``, ``parameters.json``,
``parameters.txt``. Exit status: 0 ok; 2 WITec Control not reachable; 3 comtypes missing; 4 type library /
interface not registered for this Python (the 32-bit problem); 1 anything else.

Nothing here is documented behaviour to rely on: the point of the probe is to *replace guesses with the table of
this machine*. Hand ``witec_probe.json`` back to the integrator.

Watch mode — the safe way to answer "does WITec open its own shutter?" and "what does selecting a laser do?"
-----------------------------------------------------------------------------------------------------------
Start the probe with ``--watch 600`` (ten minutes) and, while it runs, do these by hand in WITec Control (the probe
only looks; it never presses anything). Leave the sample out of the beam or the laser area secured as usual:

* **Q-a** with the shutter CLOSED and the Raman beam path set, start one short spectrum from WITec's own GUI. Does the
  shutter line change during the exposure? (The driver treats "Raman path + closed shutter" as a dark spectrum that
  needs no emission gate, and it watches for the shutter opening; this tells us whether WITec ever does that.)
* **Q-a2** switch the beam path to Raman with the shutter closed. Does the shutter change by itself?
* **Q-b1** open the 532 shutter, then select the 633 laser. Does the shutter line say "closed" for the 633 (its own
  state), and what does it say after you select the 532 again — still open (a shutter that persists per laser) or
  closed (selecting closes the previous laser's shutter)? The driver does not assume either: a stop visits every laser.
* **Q-b2** note whether ``HasShutter`` is False for the 355 nm and the white light.
* **Q-c** load a saved state by hand. How long does the StateName line take to change (does setting it block)?

The log is written to ``watch.json`` and into ``witec_probe.json`` (``watch``); each line is also printed.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import struct
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

EXIT_OK, EXIT_OTHER, EXIT_NOT_RUNNING, EXIT_NO_COMTYPES, EXIT_TYPELIB = 0, 1, 2, 3, 4


def _guess_mast_root() -> Path | None:
    here = Path(__file__).resolve()
    for cand in (here.parents[2] / "MASTv2", Path(r"C:\MAST\MASTv2"), here.parents[1]):
        if (cand / "mast").is_dir():
            return cand
    return None


def _add_paths(mast_root: str | None, comtypes_dir: str | None) -> None:
    root = Path(mast_root) if mast_root else _guess_mast_root()
    if root is not None and str(root) not in sys.path:
        sys.path.insert(0, str(root))
    if comtypes_dir and comtypes_dir not in sys.path:
        sys.path.append(comtypes_dir)          # after MAST's own packages: it only has to supply comtypes


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _err(exc: BaseException) -> dict:
    return {"error": f"{type(exc).__name__}: {exc}"}


def run_probe(client: Any, *, include_enums: bool = True) -> dict:
    """Probe through *client* (wrapped read-only). Returns the report dict; raises only for a failed connect."""
    from mast.instruments.witec import params as P
    from mast.instruments.witec.bucs import BucsError, ReadOnlyBucs

    ro = ReadOnlyBucs(client)
    ro.connect()
    report: dict[str, Any] = {"connected": True}
    table = ro.list_parameters()
    report["parameter_count"] = len(table)
    report["parameters"] = table
    for key, path in (("program_version", P.PROGRAM_VERSION),):
        try:
            report[key] = ro.get(path)
        except BucsError as exc:
            report[key] = _err(exc)
    report["access"] = {"read": bool(ro.has_read_access()), "write_held": bool(ro.has_write_access()),
                        "note": "the probe never asks for write access; a person presses 'COM Automation' for that"}

    reads: dict[str, Any] = {}
    for path in P.PROBE_READ_PATHS:
        if path not in table:
            reads[path] = {"missing": True}
            continue
        try:
            info = ro.describe(path)
            if info.kind == "enum":
                idx, label = ro.get_enum(path)
                reads[path] = {"index": idx, "label": label}
            elif info.kind == "trigger":
                reads[path] = {"trigger": True}
            else:
                reads[path] = {"value": ro.get(path), "type": info.type}
        except BucsError as exc:
            reads[path] = _err(exc)
    report["reads"] = reads

    if include_enums:
        enums: dict[str, Any] = {}
        for path, typ in table.items():
            if typ == "enum":
                try:
                    enums[path] = {str(i): lab for i, lab in ro.enum_values(path).items()}
                except BucsError as exc:
                    enums[path] = _err(exc)
        report["enums"] = enums

    features = {name: all(p in table for p in paths) for name, paths in P.FEATURE_PATHS.items()}
    report["features"] = features
    report["missing_for_acquisition"] = [p for p in P.REQUIRED_FOR_ACQUISITION if p not in table]
    n_lasers = reads.get(P.LASER_COUNT, {}).get("value")
    selects = sorted(p for p in table if p.startswith(P.ROOT_MICROSCOPE + "Laser|SelectLaser"))
    report["lasers"] = {"NumberOfLasers": n_lasers, "SelectLaser_triggers": [p.rsplit("|", 1)[1] for p in selects],
                        "consistent": (n_lasers == len(selects)) if isinstance(n_lasers, int) else None}
    report["emission_facts"] = emission_facts(table, reads)
    report["manual_checks"] = list(MANUAL_CHECKS)
    report["guarded_groups_present"] = {          # what the driver's write guard refuses, as it exists on this machine
        g: sum(1 for p in table if f"|{g.casefold()}|" in f"|{p.casefold()}|")
        for g in ("ApplicationControl", "COMInterop")}
    return report


#: What a person has to try on the machine; the driver's safety does not depend on the answers (it assumes the worst)
#: but the gates can be relaxed or tightened once they are known. ``--watch`` is how to observe them read-only.
MANUAL_CHECKS: tuple[dict, ...] = (
    {"id": "Q-a", "question": "Does WITec open its own laser shutter while it acquires a spectrum (shutter closed, Raman path)?",
     "how": "run with --watch; start a short spectrum from WITec's GUI with the shutter closed; look for a Selected|Shutter change",
     "decides": "whether 'Raman path + closed shutter = dark spectrum, no emission gate' holds (the driver also watches for it)"},
    {"id": "Q-a2", "question": "Does switching the beam path to Raman open the shutter by itself?",
     "how": "run with --watch; switch the path by hand with the shutter closed",
     "decides": "whether SetBeamPath(raman) on a closed shutter is really dark (the driver closes it again and refuses if not allowed)"},
    {"id": "Q-b1", "question": "Does selecting a laser close the previously selected laser's shutter?",
     "how": "run with --watch; open the 532 shutter, select the 633, select the 532 again",
     "decides": "whether a stop has to visit every laser (the driver does, either way)"},
    {"id": "Q-b2", "question": "Is Laser|Selected|Shutter one shutter per laser, and does it persist across selections?",
     "how": "same as Q-b1; also read HasShutter for the 355 nm and white light",
     "decides": "same; and whether the 355 nm / white light can only be blocked physically"},
    {"id": "Q-c", "question": "Does setting StateName block until the state is loaded?",
     "how": "run with --watch; load a saved state by hand and time the StateName line",
     "decides": "how long a state that opens a shutter can be lit before the driver can look (it polls every <= 0.2 s once the call returns)"},
    {"id": "Q-d", "question": "After the driver hands write access back (ReleaseWITecWriteAccess), does RequestWriteAccess(True) succeed again without a person pressing COM Automation?",
     "how": "with the driver running: ReleaseWITecWriteAccess, then CloseWITecShutter (it asks once, silently)",
     "decides": "whether the stop / close paths keep working after a release (the report says 'NOT closed' if not)"},
)


def emission_facts(table: dict, reads: dict) -> dict:
    """What the parameter table and the status reads say about shutters and laser selection. Behaviour cannot be read —
    see :data:`MANUAL_CHECKS` and ``--watch``."""
    from mast.instruments.witec import params as P

    shutters = sorted(p for p in table if "shutter" in p.casefold())
    selected_handles = [p for p in shutters if p in (P.LASER_SHUTTER, P.LASER_HAS_SHUTTER)]
    others = [p for p in shutters if p not in selected_handles]

    def val(path: str) -> Any:
        r = reads.get(path, {})
        return r["value"] if "value" in r else (None if "missing" not in r else "<missing>")

    now = {"wavelength_nm": val(P.LASER_WAVELENGTH), "has_shutter": val(P.LASER_HAS_SHUTTER),
           "shutter_open": val(P.LASER_SHUTTER), "power_mw": val(P.LASER_POWER),
           "sequencer_active": val(P.IS_SEQUENCER_ACTIVE), "beam_state": val(P.BEAM_STATE)}
    notes: list[str] = []
    if others:
        notes.append("shutter-related paths beyond the selected laser's: " + ", ".join(others))
    else:
        notes.append("the table has only the SELECTED laser's shutter handle: another laser's shutter can be read "
                     "or closed only by selecting that laser first (what a stop does)")
    if P.LASER_SELECT_NONE not in table:
        notes.append("no SelectNoLaser trigger here (the driver does not rely on it either way)")
    if now["has_shutter"] == "<missing>":
        notes.append("HasShutter is missing (WITec Control < 5.2?): the driver treats an unreadable shutter as possibly open")
    return {"shutter_paths": shutters, "selected_laser_handles": selected_handles, "other_shutter_paths": others,
            "select_no_laser_present": P.LASER_SELECT_NONE in table, "now": now, "notes": notes}


def _short(path: str) -> str:
    return "|".join(path.split("|")[-2:])


def watch_changes(client: Any, seconds: float, interval: float = 0.2, *, clock: Callable[[], float] = time.monotonic,
                  sleep: Callable[[float], None] = time.sleep, emit: Callable[[str], None] = print) -> list[dict]:
    """Poll the laser / shutter / sequencer / beam-path / state values read-only for *seconds* and return (and print)
    every change as ``{t_s, path, from, to}``. A value that cannot be read is logged as ``<ErrorName>`` — an
    idle WITec has no value for some of them, and that too is information."""
    from mast.instruments.witec import params as P
    from mast.instruments.witec.bucs import BucsError, ReadOnlyBucs

    ro = client if isinstance(client, ReadOnlyBucs) else ReadOnlyBucs(client)
    paths = (P.LASER_WAVELENGTH, P.LASER_HAS_SHUTTER, P.LASER_SHUTTER, P.LASER_POWER, P.IS_SEQUENCER_ACTIVE,
             P.BEAM_STATE, P.STATE_NAME)
    last: dict[str, Any] = {}
    events: list[dict] = []
    t0 = clock()
    while True:
        t = clock() - t0
        for path in paths:
            try:
                value: Any = ro.get(path) if ro.exists(path) else "<missing>"
            except BucsError as exc:
                value = f"<{type(exc).__name__}>"
            if path not in last or last[path] != value:
                ev = {"t_s": round(t, 3), "path": _short(path), "from": last.get(path), "to": value,
                      "initial": path not in last}
                events.append(ev)
                emit(f"[{t:8.2f} s] {ev['path']}: {'(start)' if ev['initial'] else repr(ev['from'])} -> {value!r}")
            last[path] = value
        if t >= seconds:
            return events
        sleep(min(max(0.01, interval), max(0.0, seconds - t)))


def interpretation(report: dict) -> list[str]:
    """Plain-language findings, for the console."""
    out: list[str] = []
    f = report.get("features", {})
    out.append(f"WITec Control: {report.get('program_version')}")
    out.append(f"parameters on this configuration: {report.get('parameter_count')}")
    acc = report.get("access", {})
    out.append("write access held: " + ("YES (unexpected for a read-only probe)" if acc.get("write_held") else "no (correct)"))
    ar = report.get("reads", {}).get("Status|Software|COMAutomation|AcceptsRemoteConnection", {})
    if "value" in ar:
        out.append(f"COM Automation button pressed (AcceptsRemoteConnection): {ar['value']}")
    for feat, label in (("silent_spectrum", "spectra over COM (silent spectrum)"),
                        ("center_wavelength", "centre wavelength settable"), ("grating", "grating settable"),
                        ("analyzer", "analyzer angle"), ("analyzer_series", "analyzer series (Series Ana)"),
                        ("stage_xy", "XY stage"), ("stage_z", "Z stage"), ("lasers", "laser selection"),
                        ("laser_power", "laser power"), ("laser_shutter", "laser shutter"),
                        ("video_image", "video image"), ("autofocus", "autofocus"), ("states", "saved states"),
                        ("project_save", "project save")):
        out.append(f"  [{'x' if f.get(feat) else ' '}] {label}")
    miss = report.get("missing_for_acquisition")
    if miss:
        out.append("MISSING for acquisition: " + ", ".join(miss))
    las = report.get("lasers", {})
    if las.get("consistent") is False:
        out.append(f"laser count mismatch: NumberOfLasers={las.get('NumberOfLasers')} but "
                   f"{len(las.get('SelectLaser_triggers', []))} SelectLaser triggers")
    if not f.get("center_wavelength") or not f.get("grating"):
        out.append("grating/centre wavelength are not both settable here: use saved states (LoadWITecState)")
    facts = report.get("emission_facts", {})
    for note in facts.get("notes", []):
        out.append("shutters: " + note)
    now = facts.get("now", {})
    if now:
        out.append(f"selected laser now: {now.get('wavelength_nm')} nm, shutter_open={now.get('shutter_open')}, "
                   f"has_shutter={now.get('has_shutter')}, power={now.get('power_mw')} mW")
    out.append("still to be tried by hand on the machine (run with --watch to log the changes): "
               + ", ".join(c["id"] for c in report.get("manual_checks", [])))
    return out


def classify_failure(exc: BaseException) -> tuple[int, str]:
    from mast.instruments.witec.bucs import (BucsDependencyMissing, BucsNotRunning, BucsTypeLibMissing)

    if isinstance(exc, BucsDependencyMissing):
        return EXIT_NO_COMTYPES, "comtypes is missing — pass --comtypes-dir with a folder that contains it"
    if isinstance(exc, BucsTypeLibMissing):
        return EXIT_TYPELIB, ("this Python cannot reach the 32-bit-only BUCS registration. Do NOT edit the "
                              "registry; the fallback is a 32-bit bridge process (docs §11)")
    if isinstance(exc, BucsNotRunning):
        return EXIT_NOT_RUNNING, "WITec Control is not running or not reachable — a person has to open it first"
    return EXIT_OTHER, "unexpected failure"


def main(argv: list[str] | None = None, *, client_factory: Callable[[], Any] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Read-only probe of WITec Control's COM Automation.")
    ap.add_argument("--mast-root", help="folder that contains the 'mast' package")
    ap.add_argument("--comtypes-dir", help="folder that contains comtypes (pure Python; do not pip install into MAST)")
    ap.add_argument("--out", help="output folder (default ./witec_probe_<timestamp>)")
    ap.add_argument("--gen-dir", help="where comtypes may write its generated wrapper (default: MAST data root)")
    ap.add_argument("--machine", help="COM host name (default: this machine)")
    ap.add_argument("--no-enums", action="store_true", help="skip exporting enum labels")
    ap.add_argument("--watch", type=float, default=0.0, metavar="SECONDS",
                    help="after the probe, log changes of the laser / shutter / sequencer / beam path / state for "
                         "this long while a person tries the manual checks (read-only)")
    ap.add_argument("--watch-interval", type=float, default=0.2, metavar="SECONDS", help="poll interval for --watch")
    args = ap.parse_args(argv)
    _add_paths(args.mast_root, args.comtypes_dir)

    out_dir = Path(args.out) if args.out else Path.cwd() / f"witec_probe_{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    header = {"probe": "witec_com read-only", "time_utc": _stamp(), "host": socket.gethostname(),
              "python": sys.version, "python_bits": struct.calcsize("P") * 8, "platform": platform.platform(),
              "executable": sys.executable, "user": os.environ.get("USERNAME") or os.environ.get("USER"),
              "session_name": os.environ.get("SESSIONNAME")}
    try:
        from mast.instruments.witec.bucs import BucsError
        if client_factory is None:
            from mast.instruments.witec.comtypes_client import ComtypesBucsClient

            def client_factory() -> Any:        # noqa: F811 - the default
                return ComtypesBucsClient(machine=args.machine, gen_dir=args.gen_dir)
    except Exception as exc:  # noqa: BLE001 - mast not importable
        print(f"cannot import MAST ({exc}); pass --mast-root", file=sys.stderr)
        return EXIT_OTHER

    client = client_factory()
    try:
        report = run_probe(client, include_enums=not args.no_enums)
        if args.watch > 0:
            print(f"\nwatching for {args.watch:g} s (read-only) - do the manual checks now:")
            for c in MANUAL_CHECKS:
                print(f"  {c['id']}: {c['question']}\n        how: {c['how']}")
            report["watch"] = {"seconds": args.watch, "interval_s": args.watch_interval,
                               "events": watch_changes(client, args.watch, args.watch_interval)}
        code, msg = EXIT_OK, "ok"
    except BucsError as exc:
        code, msg = classify_failure(exc)
        report = {"connected": False, "error": f"{type(exc).__name__}: {exc}", "meaning": msg}
    except Exception as exc:  # noqa: BLE001
        code, msg = EXIT_OTHER, "unexpected failure"
        report = {"connected": False, "error": f"{type(exc).__name__}: {exc}", "meaning": msg}
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
    try:
        import comtypes  # noqa: F401 - only to report its version
        header["comtypes"] = getattr(sys.modules.get("comtypes"), "__version__", "imported")
    except Exception:  # noqa: BLE001
        header["comtypes"] = None

    full = {**header, "exit_code": code, "result": msg, **report}
    (out_dir / "witec_probe.json").write_text(json.dumps(full, indent=1, ensure_ascii=False, default=str),
                                              encoding="utf-8")
    if report.get("connected"):
        (out_dir / "parameters.json").write_text(
            json.dumps({"program_version": report.get("program_version"), "parameters": report["parameters"],
                        "enums": report.get("enums", {})}, indent=1, ensure_ascii=False), encoding="utf-8")
        (out_dir / "parameters.txt").write_text(
            "\n".join(f"{typ}:{path}" for path, typ in report["parameters"].items()) + "\n", encoding="utf-8")
        if "watch" in report:
            (out_dir / "watch.json").write_text(json.dumps(report["watch"], indent=1, ensure_ascii=False, default=str),
                                                encoding="utf-8")
        for line in interpretation(report):
            print(line)
        print(f"\nwritten to {out_dir}")
    else:
        print(f"PROBE FAILED ({msg})\n  {report['error']}\nwritten to {out_dir}")
    return code


if __name__ == "__main__":
    sys.exit(main())
