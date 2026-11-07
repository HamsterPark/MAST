"""Every global name the launcher's code reads must exist.

``_graceful_stop_proc`` used ``logger`` while the module never defined it. The
NameError was raised inside a ``try/except Exception: pass``, so the stop steps
of the main service were silently not logged. This walks every code object in
``mast2_launcher`` (functions, methods, nested helpers, lambdas) and checks each
``LOAD_GLOBAL`` against the module namespace and the builtins.
"""

from __future__ import annotations

import builtins
import dis
import types

import mast2_launcher


def _code_objects(code: types.CodeType):
    yield code
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            yield from _code_objects(const)


def _global_loads(code: types.CodeType) -> set[str]:
    return {ins.argval for ins in dis.get_instructions(code) if ins.opname == "LOAD_GLOBAL"}


def _module_code() -> types.CodeType:
    path = mast2_launcher.__file__
    with open(path, encoding="utf-8") as fh:
        return compile(fh.read(), path, "exec")


def test_every_global_the_launcher_reads_is_defined():
    known = set(vars(mast2_launcher)) | set(vars(builtins))
    missing: dict[str, set[str]] = {}
    for code in _code_objects(_module_code()):
        for name in _global_loads(code) - known:
            missing.setdefault(name, set()).add(code.co_qualname)
    assert not missing, f"undefined globals read by the launcher: {missing}"


def test_the_graceful_stop_fallback_logger_exists():
    import logging

    assert isinstance(mast2_launcher.logger, logging.Logger)


def test_the_scan_sees_a_missing_global():
    """Self-check: the walk must flag a name that is read but never defined."""
    code = compile("def f():\n    return undefined_name_for_the_scan\n", "<probe>", "exec")
    loads = set()
    for c in _code_objects(code):
        loads |= _global_loads(c)
    assert "undefined_name_for_the_scan" in loads
