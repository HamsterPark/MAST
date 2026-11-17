"""Save a proven procedure as a LOCAL MAST skill (and check it).

    python scripts/skill_scaffold.py new MySkill --summary "one line" --evidence "what validated it"
    python scripts/skill_scaffold.py check MySkill

``new`` writes ``config/custom_skills/MySkill.py`` (gitignored, never auto-enabled);
``check`` runs the loader/contribution checks and prints what would stop it from loading.
Exit code 0 = ok, 1 = problems, 2 = bad arguments. See docs/external/en/06-contributing-skills.md.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "MASTv2"))

try:
    import nanonis_spm  # noqa: F401
except ImportError:
    from unittest.mock import MagicMock
    _m = MagicMock()
    sys.modules["nanonis_spm"] = _m
    sys.modules["nanonis_spm.Nanonis"] = _m.Nanonis


def main(argv=None) -> int:
    from mast.skills.scaffold import EVIDENCE_PLACEHOLDER, check_local_skill, scaffold_local_skill

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new")
    n.add_argument("name")
    n.add_argument("--summary", default="What this skill does, in one line.")
    n.add_argument("--evidence", default=EVIDENCE_PLACEHOLDER + " (data / instrument run that proved it)")
    n.add_argument("--dir", type=Path, default=None)
    c = sub.add_parser("check")
    c.add_argument("name")
    c.add_argument("--dir", type=Path, default=None)
    a = ap.parse_args(argv)
    if a.cmd == "new":
        try:
            p = scaffold_local_skill(a.name, a.summary, a.evidence, directory=a.dir)
        except (ValueError, FileExistsError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"wrote {p}\nnext: edit it, run `check {a.name}`, tell the user; a person enables it in enabled.json.")
        return 0
    r = check_local_skill(a.name, a.dir)
    for k in ("problems", "warnings"):
        for line in r[k]:
            print(f"{k[:-1].upper()}: {line}")
    print("OK" if r["ok"] else "NOT OK", r["path"])
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
