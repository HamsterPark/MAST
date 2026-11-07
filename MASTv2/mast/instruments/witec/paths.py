"""Where WITec project files may be written: below the data roots only.

``SaveWITecProject`` and ``RunAnalyzerSeries`` take a ``directory`` from a caller that may be an external agent. WITec
Control (a different process, often a different user) then creates and fills that folder, so an unchecked path is a
write primitive for the whole machine - and a UNC path (``\\\\host\\share``) makes the WITec PC open an outbound SMB
connection (NTLM credentials leave the machine, project data too). The rule, applied by the driver and again by the
skills so the refusal comes before anything runs:

* absolute only (a relative path would be resolved against whatever the working directory happens to be);
* no UNC / device prefix (``\\\\host\\share``, ``//host/share``, ``\\\\?\\C:\\``), no ``..`` component, no ``:`` beyond
  the drive letter (alternate data streams), none of ``< > " | ? *``;
* after resolving links, the folder must lie below the MAST project root or the experiment root.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from mast.i18n import tr
from mast.instruments.microscope import MicroscopeLimitError

__all__ = ["OutputDirRefused", "allowed_output_roots", "check_output_dir"]


class OutputDirRefused(MicroscopeLimitError):
    """A project directory outside the data roots (or of a shape that is never accepted)."""


def allowed_output_roots() -> list[Path]:
    """The folders a project may be written below: the MAST project root and the experiment root (resolved)."""
    from mast._runtime_paths import project_root

    roots: list[Path] = [project_root()]
    try:
        from mast.core.experiment_paths import experiment_root

        roots.append(experiment_root())
    except Exception:  # noqa: BLE001 - the experiment root is an addition, the project root alone is still a rule
        pass
    out: list[Path] = []
    for r in roots:
        try:
            out.append(Path(r).resolve())
        except OSError:
            out.append(Path(r))
    return out


def check_output_dir(directory: str | Path, *, roots: Iterable[Path] | None = None) -> Path:
    """The resolved *directory* if it is an acceptable place for a project file, else :class:`OutputDirRefused`.
    Nothing is created or touched."""
    raw = str(directory if directory is not None else "").strip()
    flat = raw.replace("/", "\\")
    if not raw:
        raise OutputDirRefused(tr("项目目录是空的", "the project directory is empty"))
    if flat.startswith("\\\\"):
        raise OutputDirRefused(tr(
            f"项目目录 {raw!r} 是网络/设备路径（UNC 或 \\\\?\\）——不接受：WITec 所在电脑会向外发起连接",
            f"project directory {raw!r} is a network / device path (UNC or \\\\?\\) - refused: it would make the "
            "WITec PC open an outbound connection"))
    if any(part == ".." for part in flat.split("\\")):
        raise OutputDirRefused(tr(f"项目目录 {raw!r} 含有 '..'——不接受", f"project directory {raw!r} contains '..' - refused"))
    if raw.find(":", 2) != -1 or any(c in raw for c in '<>"|?*'):
        raise OutputDirRefused(tr(f"项目目录 {raw!r} 含有文件夹名不能有的字符", f"project directory {raw!r} contains characters a folder cannot have"))
    p = Path(raw)
    if not p.is_absolute():
        raise OutputDirRefused(tr(f"项目目录 {raw!r} 不是绝对路径", f"project directory {raw!r} is not an absolute path"))
    try:
        resolved = p.resolve()
    except (OSError, RuntimeError) as exc:
        raise OutputDirRefused(f"project directory {raw!r} cannot be resolved: {exc}") from exc
    base = [Path(r) for r in roots] if roots is not None else allowed_output_roots()
    for root in base:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    raise OutputDirRefused(tr(
        f"项目目录 {raw!r} 不在数据根之下（{', '.join(str(r) for r in base)}）——不接受",
        f"project directory {raw!r} is not below the data root ({', '.join(str(r) for r in base)}) - refused"))
