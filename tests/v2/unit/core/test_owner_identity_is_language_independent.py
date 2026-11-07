"""An instrument-token owner is an identity, not display text.

``approach._chain_scope`` builds the scope of an approach refusal from
``context.owner``, and only the same scope may clear it. If an owner were built
with ``tr()``, the same chain would get a different identity in a Chinese and an
English request, and a refusal recorded by one could never be cleared by the
other. The frontend also matches some owners exactly (``DIRECT_EXEC_OWNER``).

So owners are written once, as fixed strings, and translated only where people
read them: ``instrument_lock.owner_label``.
"""
from __future__ import annotations

import ast
from pathlib import Path

from mast.core.instrument_lock import InstrumentBusy, owner_label
from mast.i18n import use_lang

_TR_NAMES = {"tr", "_tr_lang", "_tr"}


def _mast_root() -> Path:
    p = Path(__file__).resolve()
    while p.parent != p:
        if (p / "MASTv2" / "mast").is_dir():
            return p / "MASTv2" / "mast"
        p = p.parent
    raise RuntimeError("MASTv2/mast not found")


def _is_tr_call(node: ast.AST) -> bool:
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in _TR_NAMES)


def test_no_owner_is_built_with_tr():
    offenders = []
    for path in _mast_root().rglob("*.py"):
        source = path.read_text(encoding="utf-8-sig")   # a few generated files carry a BOM
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg == "owner" and _is_tr_call(kw.value):
                    offenders.append(f"{path.name}:{node.lineno} owner=tr(...)")
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name == "hold_for_skill" and len(node.args) >= 3 and _is_tr_call(node.args[2]):
                offenders.append(f"{path.name}:{node.lineno} hold_for_skill(..., tr(...))")
            if (isinstance(func, ast.Name) and func.id == "str" and node.args
                    and isinstance(node.args[0], ast.BoolOp)
                    and any(_is_tr_call(v) for v in node.args[0].values)):
                # e.g. self.owner = str(owner or tr(...)) — a default identity
                parent_src = ast.get_source_segment(source, node) or ""
                if "owner" in parent_src:
                    offenders.append(f"{path.name}:{node.lineno} str(owner or tr(...))")
    assert not offenders, "owners must not depend on the language:\n" + "\n".join(offenders)


def test_owner_label_translates_only_for_display():
    with use_lang("zh"):
        assert owner_label("主聊天/私聊") == "主聊天/私聊"
        assert owner_label("群聊任务 r42") == "群聊任务 r42"
    with use_lang("en"):
        assert owner_label("主聊天/私聊") == "main chat / private chat"
        assert owner_label("群聊任务 r42") == "group-chat task r42"
        assert owner_label("群聊任务 (未命名)") == "group-chat task (unnamed)"
        assert owner_label("某个第三方入口") == "某个第三方入口"   # unknown: passed through
        assert owner_label(None) == ""


def test_busy_message_names_the_holder_in_the_reader_language():
    holder = {"owner": "信号采集 API", "skill": "Scan_Action", "held_s": 3.0}
    with use_lang("en"):
        msg = InstrumentBusy(holder, "Bias_Set", 5.0).message()
        assert "signal acquisition API" in msg and "信号采集" not in msg
    with use_lang("zh"):
        assert "信号采集 API" in InstrumentBusy(holder, "Bias_Set", 5.0).message()
