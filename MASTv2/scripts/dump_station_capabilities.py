"""Export the station capability table to ``frontend/src/lib/stationCapabilities.json``.

The frontend decides what to show from the capabilities the service reports
(``GET /api/health`` -> ``station_capabilities``); this file is the same table the backend
(``mast/station.py``) has, so the UI can order and close a capability set without a request.
Content: ``capabilities`` (table order), ``profiles`` (profile -> capabilities, table order),
``requires`` (capability -> what it needs, table order).

Usage::

    PYTHONPATH=MASTv2 .venv-v2-py313/Scripts/python.exe MASTv2/scripts/dump_station_capabilities.py

``--check`` compares only and exits 1 when the file is stale.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _default_out() -> Path:
    return Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib" / "stationCapabilities.json"


def build_table() -> dict:
    from mast import station
    return {
        "capabilities": list(station.CAPABILITIES),
        "profiles": {p: station.ordered(station.PROFILE_CAPABILITIES[p]) for p in station.PROFILES},
        "requires": {c: station.ordered(station.CAPABILITY_REQUIRES[c])
                     for c in station.CAPABILITIES if c in station.CAPABILITY_REQUIRES},
    }


def render(table: dict) -> str:
    return json.dumps(table, ensure_ascii=False, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-o", "--out", type=Path, default=None)
    ap.add_argument("--check", action="store_true", help="compare only; exit 1 when stale")
    args = ap.parse_args(argv)
    out = args.out or _default_out()
    text = render(build_table())
    if args.check:
        old = out.read_bytes().decode("utf-8").replace("\r\n", "\n") if out.is_file() else ""
        if old == text:
            print("stationCapabilities.json is up to date")
            return 0
        print(f"stationCapabilities.json is stale: run python {Path(__file__).name}", file=sys.stderr)
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(text.encode("utf-8"))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
