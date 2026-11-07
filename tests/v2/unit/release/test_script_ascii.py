"""PowerShell scripts that run under Windows PowerShell 5.1 stay ASCII-only.

5.1 reads a .ps1 without a BOM in the ANSI code page of the machine. Under cp936 a UTF-8 em dash
becomes garbage in a message; under cp1252 its third byte is a typographic double quote, which
PowerShell treats as a quote character: the string ends early and the whole script fails to
parse ("Unexpected token", "string is missing the terminator"). setup-ai-ssh.ps1 already says so
in its header and was fixed once for it; publish_github_release.ps1 had the same
defect (an em dash in a throw message, an arrow in a comment). Nothing enforced the rule, so this
does.

The Python side of publish_github_release prints its plan on a console whose encoding may not be
UTF-8 (a redirected stdout follows the ANSI code page), so the strings it prints are ASCII too;
only docstrings may use other characters.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[4]

#: Scripts that must stay pure ASCII (no BOM exemption: the rule is the one setup-ai-ssh.ps1
#: states in its header).
ASCII_ONLY_PS1 = (
    "setup-ai-ssh.ps1",
)
PUBLISH_PY = "scripts/release/publish_github_release.py"


def non_ascii_lines(data: bytes) -> list[str]:
    """``line N: <the offending text>`` for every line that has a byte above 127."""
    out = []
    for n, raw in enumerate(data.splitlines(), 1):
        if not raw.isascii():
            out.append(f"line {n}: {raw.decode('utf-8', 'replace')[:100]}")
    return out


def non_ascii_literals(source: str) -> list[str]:
    """Non-ASCII string literals that are not docstrings (those end up on screen or in files)."""
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if (isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and node.body and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)):
            docstrings.add(id(node.body[0].value))
    return [
        f"line {n.lineno}: {n.value[:80]!r}"
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
        and id(n) not in docstrings and not n.value.isascii()
    ]


@pytest.mark.parametrize("rel", ASCII_ONLY_PS1)
def test_powershell_script_is_pure_ascii(rel):
    data = (_REPO / rel).read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf"), f"{rel} has a BOM; keep it BOM-less ASCII"
    bad = non_ascii_lines(data)
    assert not bad, (
        f"{rel} has non-ASCII characters; Windows PowerShell 5.1 reads a BOM-less .ps1 in the "
        "ANSI code page and may fail to parse it:\n  " + "\n  ".join(bad))


def test_the_checks_catch_what_they_are_for():
    """Reverse self-check: the scanners must see an em dash, an arrow and a printed arrow."""
    assert non_ascii_lines(b'throw "Missing $py \xe2\x80\x94 create the venv"\r\nok\r\n') == [
        'line 1: throw "Missing $py — create the venv"']
    assert non_ascii_lines(b"# a -> b\r\n") == []
    src = '"""doc — fine"""\n\ndef f():\n    """also → fine"""\n    print("a → b")\n'
    assert non_ascii_literals(src) == ["line 5: 'a → b'"]
