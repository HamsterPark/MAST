# -*- coding: utf-8 -*-
"""采集窗口：.dat 只读文件头，不解析数据块。

模块的 docstring 一直写着「只读文件头，不读数据块」，而 .dat 原来走的是 ``read_dat`` —— 整份
解析（几万行数据也全读进来、转成数组）。现在有自己的头部读取，规则与 ``read_dat`` 的头部分相同。
"""
from __future__ import annotations

import os
from pathlib import Path

from mast.envhistory.filetags.acq import DAT_HEADER_MAX_LINES, acquisition_window, dat_header, parse_local
from mast.io.nanonis_files import read_dat

_HEADER = ("Experiment\tbias spectroscopy\t\n"
           "Start time\t10.09.2001 15:07:12\t\n"
           "Saved Date\t10.09.2001 15:08:40\t\n"
           "\n"
           "Comment01\tone\ttwo\t\n"                 # 多个 tab：第一个之后的全并进值
           "User\t\n"                                # 只有键
           "\tno key here\n"
           "Bias Spectroscopy>Sweep Start (V)\t-1.000000E+0\t\n")


def _dat(path: Path, rows: int = 50) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = _HEADER + "\n[DATA]\nBias calc (V)\tCurrent (A)\n" + "".join(f"{i}\t{i * 1e-12}\n" for i in range(rows))
    path.write_bytes(body.replace("\n", "\r\n").encode("ascii"))
    t = parse_local("10.09.2001 15:08:40")
    os.utime(path, (t, t))
    return path


def test_the_header_is_the_one_read_dat_sees(tmp_path):
    p = _dat(tmp_path / "w" / "Bias-Spectroscopy001.dat")
    assert dat_header(str(p)) == read_dat(str(p))["header"]
    assert dat_header(str(p))["Comment01"] == "one\ttwo"


def test_the_window_never_parses_the_data_block(tmp_path, monkeypatch):
    p = _dat(tmp_path / "w" / "Bias-Spectroscopy001.dat", rows=20000)

    def boom(*_a, **_k):
        raise AssertionError("read_dat was called for an acquisition window")

    monkeypatch.setattr("mast.io.nanonis_files.read_dat", boom)
    w = acquisition_window(str(p))
    assert w is not None and w.start == parse_local("10.09.2001 15:07:12") and w.start_from == "Start time"


def test_a_file_without_a_data_marker_is_read_up_to_the_line_cap(tmp_path):
    p = tmp_path / "w" / "odd.dat"
    p.parent.mkdir(parents=True)
    lines = ["Start time\t10.09.2001 15:07:12"] + [f"k{i}\tv" for i in range(DAT_HEADER_MAX_LINES + 50)]
    p.write_text("\n".join(lines) + "\n", encoding="ascii")
    h = dat_header(str(p))
    assert h["Start time"] == "10.09.2001 15:07:12"
    assert len(h) == DAT_HEADER_MAX_LINES                # 第一行 + 后面 1999 行，到上限为止
