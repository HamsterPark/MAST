"""Where a WITec project may be written: the shape rules, the order of the checks, the roots."""
from __future__ import annotations

import sys
from pathlib import Path

_MASTV2_ROOT = str(Path(__file__).resolve().parents[4] / "MASTv2")
if sys.path[0] != _MASTV2_ROOT:
    while _MASTV2_ROOT in sys.path:
        sys.path.remove(_MASTV2_ROOT)
    sys.path.insert(0, _MASTV2_ROOT)
for _name in list(sys.modules):
    if _name == "mast" or _name.startswith("mast."):
        _f = getattr(sys.modules[_name], "__file__", "") or ""
        if "MASTv2" not in _f.replace("\\", "/"):
            del sys.modules[_name]

import pytest

from mast.i18n import use_lang
from mast.instruments.microscope import MicroscopeLimitError
from mast.instruments.witec.paths import OutputDirRefused, allowed_output_roots, check_output_dir


@pytest.fixture
def roots(tmp_path, monkeypatch):
    (tmp_path / "root").mkdir()
    (tmp_path / "experiments").mkdir()
    monkeypatch.setenv("MAST2_PROJECT_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("MAST_EXPERIMENT_ROOT", str(tmp_path / "experiments"))
    with use_lang("en"):
        yield tmp_path


def test_the_roots_are_the_project_root_and_the_experiment_root(roots):
    assert allowed_output_roots() == [(roots / "root").resolve(), (roots / "experiments").resolve()]


def test_a_directory_below_either_root_is_accepted_whether_or_not_it_exists(roots):
    for d in (roots / "root" / "artifacts" / "x", roots / "experiments" / "e1" / "raw", roots / "root"):
        assert check_output_dir(d) == d.resolve()
        assert check_output_dir(str(d) + "/") == d.resolve()


def test_the_refusal_is_a_microscope_limit_and_names_the_roots(roots):
    with pytest.raises(OutputDirRefused, match="data root") as ei:
        check_output_dir(roots / "elsewhere")
    assert isinstance(ei.value, MicroscopeLimitError) and str(roots / "root") in str(ei.value)


@pytest.mark.parametrize("raw", ["", "   ", r"\\evil-host\share", "//evil-host/share/x", r"\\?\C:\Windows",
                                 r"\\.\pipe\x", "{root}/../x", "{root}/a/../../x", "{root}/a/..", "relative/dir", "./dir",
                                 "{root}/a:stream", "{root}/a<b", '{root}/a"b', "{root}/a|b", "{root}/a?b", "{root}/a*b"])
def test_shapes_that_are_never_accepted(roots, raw):
    with pytest.raises(OutputDirRefused):
        check_output_dir(raw.replace("{root}", str(roots / "root")))


def test_network_and_device_paths_are_refused_before_anything_is_resolved(roots, monkeypatch):
    """Path.resolve() on a UNC path opens it - an outbound SMB connection (NTLM). The shape check must come first."""
    def boom(self, *a, **k):
        raise AssertionError("resolve() was called on a path that must be refused by its shape")

    monkeypatch.setattr(Path, "resolve", boom)
    for raw in (r"\\evil-host\share\x", "//evil-host/share/x", r"\\?\C:\Windows", "{root}/a/../b"):
        with pytest.raises(OutputDirRefused):
            check_output_dir(raw.replace("{root}", str(roots / "root")))


def test_a_relative_path_is_refused_even_when_the_working_directory_would_put_it_inside_the_root(roots, monkeypatch):
    monkeypatch.chdir(roots / "root")                           # MAST normally runs with its install folder as cwd
    with pytest.raises(OutputDirRefused, match="not an absolute"):
        check_output_dir("artifacts/projects")


def test_explicit_roots_can_be_given(tmp_path):
    other = tmp_path / "only-here"
    assert check_output_dir(other / "x", roots=[other]) == (other / "x").resolve()
    with pytest.raises(OutputDirRefused):
        check_output_dir(tmp_path / "elsewhere", roots=[other])
