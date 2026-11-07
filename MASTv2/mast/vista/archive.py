"""A workspace's lossless visual memory: every frame the instrument produced, addressable forever.

README
------
One :class:`Archive` per workspace (:mod:`mast.vista.workspace`, one per MAST experiment),
rooted in a directory::

    <root>/index.jsonl        one JSON record per frame, append-only (crash recovery)
    <root>/t0007.s0.npz       every channel x direction, float32, SI units, NaN = not acquired
    <root>/t0007.s0.png       the default rendering (what the model saw as "current")
    <root>/t0007.d0.dat       a spectrum keeps the original file, plus .npz and .png
    <root>/t0007.m0.png       a derived image a measurement tool produced (+ .npz if it has values)

Frame ids are ``protocol.frame_id(turn, kind, index)``: ``s`` saved scan, ``p`` partial
snapshot of a scan in progress, ``d`` spectrum, ``m`` derived image. ``index`` counts the
frames of that kind within the turn.

* ``add_scan`` / ``add_partial`` / ``add_spectrum`` / ``add_derived`` store a frame and
  return its :class:`FrameEntry`; files are written first and the index line last, so an
  index line always has its files.
* ``render`` re-renders any frame from its archived values (channel, direction, flatten,
  contrast, exact region); ``observation`` returns the stored default image with its label;
  ``array`` returns archived values; ``text_grid`` writes a frame out as numbers (VISTA-SPM's
  text-only observation).
* Re-opening an existing root restores the index; a damaged index line is skipped and
  reported in :attr:`Archive.load_warnings`. Frame ids continue after the restored ones; the
  id of a record skipped for missing files is never handed out again, and a torn last line
  is ended before the next record is appended.
* Thread-safe: one lock guards frame-id allocation, file writes, the index and the cache.
"""
from __future__ import annotations

import io
import json
import math
import os
import shutil
import threading
from collections import OrderedDict
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

import numpy as np

from . import frames as F
from .protocol import DISPLAY_MAX_DEFAULT, FRAME_KINDS, ImageBlock, frame_id, parse_frame_id

INDEX_NAME = "index.jsonl"
_KIND_WORD = {"s": "scan", "p": "partial scan", "d": "spectrum", "m": "derived image"}
#: ``kind`` as the model reads it (summaries and tool replies); the letter stays in the frame id
#: and in ``FrameEntry.kind``.
KIND_NAMES = {"s": "scan", "p": "partial", "d": "spectrum", "m": "derived"}
_CACHE_FRAMES = 8


@dataclass
class FrameEntry:
    """One archived frame. ``geometry`` is ``{cx_nm, cy_nm, w_nm, h_nm, angle_deg, nx, ny}``
    for scans (and derived maps that have values), empty otherwise. ``display_scale`` is the
    ``k`` of ``protocol.DISPLAY_CONVENTION`` (``0.5`` = block-mean by 2); ``display_size``
    the full display image ``[W, H]``. ``units`` maps channel (or spectrum column) to its SI
    unit. ``extra`` holds kind-specific facts (per-channel directions, default render
    attributes, spectrum columns, the producing tool of a derived image, ...)."""

    fid: str
    turn: int
    kind: str
    index: int
    source_name: str = ""
    channels: list[str] = field(default_factory=list)
    directions: list[str] = field(default_factory=list)
    nx: int = 0
    ny: int = 0
    geometry: dict = field(default_factory=dict)
    nm_per_px: float | None = None
    display_scale: float = 1.0
    display_size: list[int] = field(default_factory=lambda: [0, 0])
    bias_v: float | None = None
    setpoint_a: float | None = None
    scan_dir: str | None = None
    acquired_rows: int | None = None
    rec_time: str = ""
    sim_s: float | None = None
    units: dict[str, str] = field(default_factory=dict)
    default_channel: str | None = None
    extra: dict = field(default_factory=dict)

    # ── derived facts ───────────────────────────────────────────────────────
    @property
    def kind_word(self) -> str:
        return _KIND_WORD.get(self.kind, self.kind)

    @property
    def kind_name(self) -> str:
        """``scan`` | ``partial`` | ``spectrum`` | ``derived``: the model-facing kind."""
        return KIND_NAMES.get(self.kind, self.kind)

    @property
    def is_image(self) -> bool:
        """Has a 2-D scan grid (scan, partial, or a derived map with values)."""
        return bool(self.geometry) and self.kind in ("s", "p", "m") and bool(self.channels)

    @property
    def scale_pair(self) -> tuple[int, int]:
        k = float(self.display_scale)
        return (int(round(k)), 1) if k >= 1 else (1, int(round(1.0 / k)))

    def channel_directions(self, channel: str) -> list[str]:
        cd = self.extra.get("channel_directions") or {}
        return list(cd.get(channel, self.directions))

    def summary(self) -> dict:
        """Compact, JSON-safe description the model reads (history, recovery, tool replies).

        Keys: ``frame``, ``turn``, ``kind`` (scan, partial, spectrum, derived), and ``origin``
        when the service tagged what caused the frame (``act``, ``external``, ...); scans add
        ``source, channels, directions, size_px, field_nm, center_nm, angle_deg, nm_per_px,
        display_scale`` (a number; plus ``display_scale_frac`` such as ``"1/2"`` when it is not an
        integer), ``display_size_px, default_channel``, ``rows_acquired`` and ``rows_total`` (only
        when rows are missing) and ``feedback``; spectra add ``source, columns, n_points,
        position_nm, experiment``;
        derived images add ``tool, frames, description, image_size_px`` (and ``channels``);
        any kind may add ``bias_v``, ``setpoint_pa`` (``setpoint_hz`` for frequency feedback),
        ``scan_dir``, ``rec_time``, ``sim_s``. Units travel in the key suffix."""
        out: dict[str, Any] = {"frame": self.fid, "turn": self.turn, "kind": self.kind_name}
        if self.extra.get("origin"):
            out["origin"] = self.extra["origin"]
        if self.kind in ("s", "p"):
            g = self.geometry
            out["source"] = self.source_name
            out["channels"] = list(self.channels)
            out["directions"] = list(self.directions)
            out["size_px"] = [self.nx, self.ny]
            out["field_nm"] = [_r(g.get("w_nm"), 4), _r(g.get("h_nm"), 4)]
            out["center_nm"] = [_r(g.get("cx_nm"), 4), _r(g.get("cy_nm"), 4)]
            out["angle_deg"] = _r(g.get("angle_deg"), 3)
            out["nm_per_px"] = _r(self.nm_per_px, 5)
            num, den = self.scale_pair
            out["display_scale"] = num if den == 1 else _r(num / den, 6)
            if den != 1:
                out["display_scale_frac"] = F.scale_text(num, den)
            out["display_size_px"] = list(self.display_size)
            out["default_channel"] = self.default_channel
            if self.acquired_rows is not None and self.acquired_rows < self.ny:
                out["rows_acquired"] = int(self.acquired_rows)
                out["rows_total"] = int(self.ny)
            if self.extra.get("feedback"):
                out["feedback"] = self.extra["feedback"]
        elif self.kind == "d":
            out["source"] = self.source_name
            out["columns"] = list(self.channels)
            out["n_points"] = self.nx
            pos = self.extra.get("position_nm")
            if pos:
                out["position_nm"] = [_r(pos[0], 4), _r(pos[1], 4)]
            if self.extra.get("experiment"):
                out["experiment"] = self.extra["experiment"]
        else:
            out["tool"] = self.extra.get("tool")
            if self.extra.get("sources"):
                out["frames"] = list(self.extra["sources"])
            if self.extra.get("description"):
                out["description"] = self.extra["description"]
            out["image_size_px"] = list((self.extra.get("render") or {}).get("image_size") or self.display_size)
            if self.channels:
                out["channels"] = list(self.channels)
        if self.bias_v is not None:
            out["bias_v"] = _r(self.bias_v, 6)
        if self.setpoint_a is not None:
            factor, unit = F.display_unit(self.extra.get("setpoint_unit") or "A")
            out[F.unit_key("setpoint", unit)] = float(f"{self.setpoint_a * factor:.6g}")
        if self.scan_dir:
            out["scan_dir"] = self.scan_dir
        if self.rec_time:
            out["rec_time"] = self.rec_time
        if self.sim_s is not None:
            out["sim_s"] = _r(self.sim_s, 3)
        return out

    def to_record(self) -> dict:
        return _jsonable(asdict(self))

    @classmethod
    def from_record(cls, rec: dict) -> "FrameEntry":
        names = {f.name for f in fields(cls)}
        kw = {k: v for k, v in rec.items() if k in names}
        missing = [k for k in ("fid", "turn", "kind", "index") if k not in kw]
        if missing:
            raise ValueError(f"index record lacks {missing}")
        return cls(**kw)


@dataclass(frozen=True)
class ChannelFormat:
    """How one channel of one frame is reported to the model: the model unit, the factor from
    SI, and the rounding (from the channel's value span, :func:`frames.decimals_for`)."""

    factor: float
    unit: str
    decimals: int

    def key(self, name: str) -> str:
        """``fmt.key("height") == "height_pm"`` for a Z channel."""
        return F.unit_key(name, self.unit)

    def value(self, v_si: Any) -> float | None:
        return F.round_to(None if v_si is None else float(v_si) * self.factor, self.decimals)

    def values(self, a_si: Any) -> list:
        """Nested lists of rounded model-unit values (same shape as ``a_si``); NaN -> ``None``."""
        a = np.round(np.asarray(a_si, dtype=float) * self.factor, self.decimals) + 0.0
        return _nan_to_none(a.tolist())


def _nan_to_none(x: Any) -> Any:
    if isinstance(x, list):
        return [_nan_to_none(v) for v in x]
    return None if (isinstance(x, float) and not math.isfinite(x)) else x


def _r(v: Any, nd: int) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, nd) if math.isfinite(f) else None


def _jsonable(o: Any) -> Any:
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, (np.floating, float)):
        f = float(o)
        return f if math.isfinite(f) else None
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, np.ndarray):
        return _jsonable(o.tolist())
    return o


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _region_tuple(region: Any) -> tuple[int, int, int, int] | None:
    if region is None:
        return None
    if isinstance(region, dict):
        try:
            return (int(region["x"]), int(region["y"]), int(region["width"]), int(region["height"]))
        except KeyError as exc:
            raise ValueError(f"region needs x, y, width and height (missing {exc.args[0]})") from None
    vals = list(region)
    if len(vals) != 4:
        raise ValueError("region must be (x, y, width, height)")
    return tuple(int(v) for v in vals)  # type: ignore[return-value]


class Archive:
    """Per-workspace lossless frame store (see the module docstring)."""

    def __init__(self, root: Path | str, *, display_max: int = DISPLAY_MAX_DEFAULT, grid: bool = False):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.display_max = int(display_max)
        if self.display_max < 64:
            raise ValueError("display_max must be at least 64")
        self.grid = bool(grid)              # draw the metric grid on scan images (frames.view_grid)
        self._lock = threading.RLock()
        self._entries: dict[str, FrameEntry] = {}
        self._order: list[str] = []
        self._cache: OrderedDict[str, dict[str, np.ndarray]] = OrderedDict()
        self._turn = 0
        self._reserved: set[tuple[int, str, int]] = set()   # (turn, kind, index) of skipped records
        self._torn_tail = False             # the index ends in a torn line: start the next record anew
        self.load_warnings: list[str] = []
        self._restore()

    # ── bookkeeping ─────────────────────────────────────────────────────────
    @property
    def index_path(self) -> Path:
        return self.root / INDEX_NAME

    @property
    def current_turn(self) -> int:
        """The latest turn seen (or set by the workspace); derived images are filed under it."""
        return self._turn

    def set_turn(self, turn: int) -> None:
        with self._lock:
            self._turn = max(0, int(turn))

    def _restore(self) -> None:
        p = self.index_path
        if not p.exists():
            return
        with self._lock:
            text = p.read_text(encoding="utf-8", errors="replace")
            # a torn last line (the writer died mid-line): appended to it, the next record would be
            # lost at the next reopen (_commit starts it on a line of its own)
            self._torn_tail = bool(text) and not text.endswith("\n")
            # split at "\n" only: splitlines() also splits at U+0085 / U+2028, which a string may hold
            for n, line in enumerate(text.split("\n"), 1):
                if not line.strip():
                    continue
                try:
                    e = FrameEntry.from_record(json.loads(line))
                    turn, kind, index = parse_frame_id(e.fid)
                except (ValueError, TypeError, AttributeError) as exc:     # AttributeError: not an object
                    self.load_warnings.append(f"index line {n} skipped: {exc}")
                    continue
                missing = [x for x in self._files_of(e) if not x.exists()]
                if missing:
                    # the agent may have seen this id: it is never handed out again, and its turn counts
                    self.load_warnings.append(f"{e.fid}: missing {[m.name for m in missing]}; skipped")
                    self._reserved.add((turn, kind, index))
                    self._turn = max(self._turn, turn)
                    continue
                if e.fid in self._entries:
                    self.load_warnings.append(f"index line {n}: duplicate {e.fid}; later record kept")
                    self._order.remove(e.fid)
                self._entries[e.fid] = e
                self._order.append(e.fid)
                self._turn = max(self._turn, e.turn)

    def _files_of(self, e: FrameEntry) -> list[Path]:
        out = [self.root / f"{e.fid}.png"]
        if e.kind in ("s", "p", "d") or e.extra.get("array_keys"):
            out.append(self.root / f"{e.fid}.npz")
        return out

    def _allocate(self, turn: int, kind: str) -> tuple[str, int]:
        if kind not in FRAME_KINDS:
            raise ValueError(f"kind {kind!r} not in {FRAME_KINDS}")
        turn = int(turn)
        if turn < 0:
            raise ValueError("turn must be >= 0")
        used = [e.index for e in self._entries.values() if e.turn == turn and e.kind == kind]
        used += [i for t, k, i in self._reserved if t == turn and k == kind]
        index = (max(used) + 1) if used else 0
        return frame_id(turn, kind, index), index

    def _commit(self, e: FrameEntry) -> FrameEntry:
        line = json.dumps(e.to_record(), ensure_ascii=False, separators=(",", ":"))
        with open(self.index_path, "a", encoding="utf-8") as fh:
            fh.write(("\n" if self._torn_tail else "") + line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self._torn_tail = False
        self._entries[e.fid] = e
        self._order.append(e.fid)
        self._turn = max(self._turn, e.turn)
        return e

    def _save_arrays(self, fid: str, arrays: dict[str, np.ndarray]) -> list[str]:
        keys = list(arrays)
        tmp = self.root / f"{fid}.npz.tmp"
        with open(tmp, "wb") as fh:
            np.savez_compressed(fh, **{f"a{i}": np.asarray(arrays[k]) for i, k in enumerate(keys)})
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.root / f"{fid}.npz")
        return keys

    # ── adding frames ───────────────────────────────────────────────────────
    def add_scan(self, turn: int, sxm_path: str | Path, *, sim_s: float | None = None,
                 origin: str | None = None) -> FrameEntry:
        """Archive a saved ``.sxm`` (every channel and direction). ``origin`` tags what caused
        the frame (``"act"``, ``"external"``, ...): kept in ``extra["origin"]`` and in the summary."""
        data = F.load_sxm(sxm_path)
        return self._add_scan_data(turn, "s", data, sim_s=sim_s, source_path=str(Path(sxm_path)),
                                   origin=origin)

    def add_partial(self, turn: int, arrays: dict[str, np.ndarray], *, geometry: dict,
                    meta: dict, sim_s: float | None = None) -> FrameEntry:
        """Archive a snapshot of a scan in progress. ``arrays``: ``{"Z/forward": 2-D, ...}``
        oriented (row 0 = top edge, backward un-mirrored), SI units, NaN rows not yet
        acquired. ``meta`` may carry ``bias_v, setpoint_a, scan_dir, rec_time, units,
        acquired_rows, feedback, source_name``."""
        data = F.scan_from_arrays(arrays, geometry=geometry, meta=meta)
        return self._add_scan_data(turn, "p", data, sim_s=sim_s, source_path=None)

    def add_partial_scan(self, turn: int, sxm_path: str | Path, *,
                         origin: str | None = None) -> FrameEntry:
        """Archive a saved ``.sxm`` of a scan that stopped before its frame was complete as a
        partial frame (``p``): the loader and orientation of :meth:`add_scan`, rows the scan
        never reached NaN (``acquired_rows`` counts the others). Detectors run on saved scans
        only, so they skip it."""
        data = F.load_sxm(sxm_path)
        return self._add_scan_data(turn, "p", data, sim_s=None, source_path=str(Path(sxm_path)),
                                   origin=origin)

    def _add_scan_data(self, turn: int, kind: str, data: F.ScanData, *, sim_s: float | None,
                       source_path: str | None, origin: str | None = None) -> FrameEntry:
        g = data.geometry
        nx, ny = int(g["nx"]), int(g["ny"])
        num, den = F.scale_pair(nx, ny, self.display_max)
        W, H = F.display_size(nx, ny, self.display_max)
        chan_dirs = {c: data.directions(c) for c in data.channels}
        dirs = [d for d in F.DIRECTIONS if any(d in v for v in chan_dirs.values())]
        span = {c: F.robust_span(*[data.arrays[f"{c}/{d}"] for d in chan_dirs[c]]) for c in data.channels}
        with self._lock:
            fid, index = self._allocate(turn, kind)
            keys = self._save_arrays(fid, data.arrays)
            e = FrameEntry(
                fid=fid, turn=int(turn), kind=kind, index=index, source_name=data.source_name,
                channels=list(data.channels), directions=dirs, nx=nx, ny=ny, geometry=dict(g),
                nm_per_px=float(data.nm_per_px), display_scale=num / den, display_size=[W, H],
                bias_v=data.bias_v, setpoint_a=data.setpoint_a, scan_dir=data.scan_dir,
                acquired_rows=int(data.acquired_rows), rec_time=data.rec_time,
                sim_s=None if sim_s is None else float(sim_s), units=dict(data.units),
                default_channel=F.default_channel(data.channels, data.arrays),
                extra={"array_keys": keys, "channel_directions": chan_dirs, "span": span,
                       "nm_per_px_y": float(data.nm_per_px_y), "feedback": data.feedback,
                       "setpoint_unit": data.setpoint_unit, "source_path": source_path})
            if origin:
                e.extra["origin"] = str(origin)
            self._cache_put(fid, data.arrays)
            png, meta = self._render_scan(e, None, "forward", "plane", F.CLIP_PCT_DEFAULT, None,
                                          self.display_max, F.HIGHPASS_NM_DEFAULT, "current", None)
            _atomic_write(self.root / f"{fid}.png", png)
            e.extra["render"] = {"label_attrs": meta["label_attrs"], "image_size": meta["image_size_px"],
                                 "unit": meta["unit"], "black": meta["black"], "white": meta["white"]}
            return self._commit(e)

    def add_spectrum(self, turn: int, dat_path: str | Path, *, sim_s: float | None = None,
                     origin: str | None = None) -> FrameEntry:
        """Archive a saved spectrum (.dat): original file copy, parsed columns, default plot.
        ``origin`` as for :meth:`add_scan`."""
        data = F.load_dat(dat_path)
        names = list(data.columns)
        with self._lock:
            fid, index = self._allocate(turn, "d")
            shutil.copyfile(dat_path, self.root / f"{fid}.dat")
            keys = self._save_arrays(fid, data.columns)
            n = int(len(next(iter(data.columns.values()))))
            e = FrameEntry(
                fid=fid, turn=int(turn), kind="d", index=index, source_name=data.source_name,
                channels=names, directions=[], nx=n, ny=1, geometry={}, nm_per_px=None,
                display_scale=1.0, display_size=[0, 0], bias_v=data.bias_v, setpoint_a=data.setpoint_a,
                scan_dir=None, acquired_rows=None, rec_time=data.rec_time,
                sim_s=None if sim_s is None else float(sim_s), units=dict(data.units),
                default_channel=names[1] if len(names) > 1 else names[0],
                extra={"array_keys": keys, "experiment": data.experiment,
                       "position_nm": None if data.x_nm is None else [data.x_nm, data.y_nm],
                       "span": {c: F.robust_span(v) for c, v in data.columns.items()},
                       "setpoint_unit": data.setpoint_unit, "source_path": str(Path(dat_path))})
            if origin:
                e.extra["origin"] = str(origin)
            self._cache_put(fid, data.columns)
            png, meta = self._render_spectrum(e, None, None, None, self.display_max, "current", None)
            _atomic_write(self.root / f"{fid}.png", png)
            e.display_size = list(meta["image_size_px"])
            e.extra["render"] = {"label_attrs": meta["label_attrs"], "image_size": meta["image_size_px"]}
            return self._commit(e)

    def add_derived(self, turn: int, png: bytes, *, meta: dict,
                    arrays: dict[str, np.ndarray] | None = None) -> FrameEntry:
        """Archive an image a measurement tool produced. ``meta``: ``tool`` (name),
        ``sources`` (frame ids), ``description``; with ``arrays`` (``{"diff/forward": 2-D}``,
        SI) also ``geometry`` and ``units`` so the values can be read back."""
        meta = dict(meta or {})
        if arrays and not meta.get("geometry"):
            raise ValueError("add_derived with arrays needs meta['geometry'] ({cx_nm, cy_nm, w_nm, h_nm, angle_deg})")
        W, H = F.png_size(png)
        with self._lock:
            fid, index = self._allocate(turn, "m")
            extra = {"tool": meta.get("tool"), "sources": list(meta.get("sources") or []),
                     "description": meta.get("description"), "label_attrs": meta.get("label_attrs") or {}}
            entry_kw: dict[str, Any] = {"nx": W, "ny": H, "display_size": [W, H], "display_scale": 1.0}
            if arrays:
                data = F.scan_from_arrays(arrays, geometry=meta["geometry"], meta={"units": meta.get("units")})
                g = data.geometry
                num, den = F.scale_pair(g["nx"], g["ny"], self.display_max)
                keys = self._save_arrays(fid, data.arrays)
                chan_dirs = {c: data.directions(c) for c in data.channels}
                extra.update({"array_keys": keys, "channel_directions": chan_dirs,
                              "nm_per_px_y": data.nm_per_px_y,
                              "span": {c: F.robust_span(*[data.arrays[f"{c}/{d}"] for d in chan_dirs[c]])
                                       for c in data.channels}})
                entry_kw.update(nx=g["nx"], ny=g["ny"], geometry=dict(g), nm_per_px=data.nm_per_px,
                                display_scale=num / den, channels=list(data.channels),
                                directions=sorted({d for v in chan_dirs.values() for d in v}),
                                units=dict(data.units), default_channel=data.channels[0])
                self._cache_put(fid, data.arrays)
            _atomic_write(self.root / f"{fid}.png", png)
            e = FrameEntry(fid=fid, turn=int(turn), kind="m", index=index,
                           source_name=str(meta.get("tool") or "derived"), extra=extra, **entry_kw)
            e.extra["render"] = {"label_attrs": self._derived_label_attrs(e), "image_size": [W, H]}
            return self._commit(e)

    # ── lookup ──────────────────────────────────────────────────────────────
    def get(self, fid: str) -> FrameEntry:
        key = str(fid or "").strip()
        with self._lock:
            e = self._entries.get(key)
            if e is not None:
                return e
            raise KeyError(self._unknown_message(key))

    def _unknown_message(self, key: str) -> str:
        try:
            turn, _, _ = parse_frame_id(key)
        except ValueError:
            return (f"{key!r} is not a frame id (expected like 't0007.s0'); "
                    f"latest frames: {self._order[-6:]}")
        same = [f for f in self._order if self._entries[f].turn == turn]
        if same:
            return f"no frame {key!r}; turn {turn} has {same}"
        return (f"no frame {key!r}; turns 0..{self._turn} exist; latest frames: {self._order[-6:]}"
                if self._order else f"no frame {key!r}; the archive is empty")

    def frames(self, turn: int | None = None, kind: str | None = None) -> list[FrameEntry]:
        with self._lock:
            out = [self._entries[f] for f in self._order]
        if turn is not None:
            out = [e for e in out if e.turn == int(turn)]
        if kind is not None:
            out = [e for e in out if e.kind == kind]
        return out

    def latest(self, kind: str = "s") -> FrameEntry | None:
        with self._lock:
            for f in reversed(self._order):
                if self._entries[f].kind == kind:
                    return self._entries[f]
        return None

    def index(self) -> list[dict]:
        return [e.summary() for e in self.frames()]

    # ── values ──────────────────────────────────────────────────────────────
    def _cache_put(self, fid: str, arrays: dict[str, np.ndarray]) -> None:
        ro = {}
        for k, v in arrays.items():
            a = np.asarray(v)
            a = a.copy() if a.flags.writeable else a
            a.flags.writeable = False
            ro[k] = a
        with self._lock:
            self._cache[fid] = ro
            self._cache.move_to_end(fid)
            while len(self._cache) > _CACHE_FRAMES:
                self._cache.popitem(last=False)

    def _arrays(self, e: FrameEntry) -> dict[str, np.ndarray]:
        with self._lock:
            got = self._cache.get(e.fid)
            if got is not None:
                self._cache.move_to_end(e.fid)
                return got
        keys = list(e.extra.get("array_keys") or [])
        if not keys:
            raise ValueError(f"{e.fid} is a {e.kind_word} without archived values")
        with np.load(self.root / f"{e.fid}.npz", allow_pickle=False) as z:
            arrays = {k: z[f"a{i}"] for i, k in enumerate(keys)}
        self._cache_put(e.fid, arrays)
        return self._cache[e.fid]

    def resolve_channel(self, e: FrameEntry, channel: str | None) -> str:
        if channel is None or str(channel).strip() == "":
            if not e.default_channel:
                raise KeyError(f"{e.fid} is a {e.kind_word} without channels")
            return e.default_channel
        return F.resolve_channel(str(channel), list(e.channels), e.units)

    def array(self, fid: str, channel: str, direction: str = "forward") -> np.ndarray:
        """Archived values (read-only, SI units) of one channel and direction. For a
        spectrum, ``channel`` is a column name and ``direction`` is ignored."""
        e = self.get(fid)
        return self.values(e, self.resolve_channel(e, channel), direction)

    def values(self, e: FrameEntry, ch: str, direction: str = "forward") -> np.ndarray:
        """Like :meth:`array` for an entry and an already resolved channel name."""
        arrays = self._arrays(e)
        if e.kind == "d":
            return arrays[ch]
        if direction not in F.DIRECTIONS:
            raise ValueError(f"direction must be 'forward' or 'backward', not {direction!r}")
        key = f"{ch}/{direction}"
        if key not in arrays:
            raise KeyError(f"{e.fid} channel {ch!r} has no {direction} pass; it has "
                           f"{e.channel_directions(ch)}")
        return arrays[key]

    def fmt(self, e: FrameEntry, channel: str) -> ChannelFormat:
        """Model unit, SI factor and rounding of one channel (or spectrum column) of a frame."""
        factor, unit = F.display_unit(e.units.get(channel, ""))
        span = (e.extra.get("span") or {}).get(channel)
        if span is None and e.extra.get("array_keys"):
            arrays = self._arrays(e)
            span = F.robust_span(*[a for k, a in arrays.items() if k == channel or k.startswith(channel + "/")])
        return ChannelFormat(factor, unit, F.decimals_for(None if span is None else span * factor))

    # ── rendering ───────────────────────────────────────────────────────────
    def render(self, fid: str, *, channel: str | None = None, direction: str = "forward",
               flatten: str = "plane", clip_pct: float = F.CLIP_PCT_DEFAULT, region: dict | None = None,
               display_max: int | None = None, highpass_nm: float = F.HIGHPASS_NM_DEFAULT,
               x_col: str | None = None, y_col: str | list[str] | None = None,
               x_range: tuple[float, float] | None = None, label_kind: str = "current",
               label_attrs: dict | None = None) -> tuple[bytes, dict]:
        """Re-render a frame from its archived values. Scan / partial: ``channel``
        (default: the frame's default channel), ``direction``, ``flatten``, ``clip_pct``,
        ``region`` (display px of the full frame image, ``{x, y, width, height}``). Spectrum:
        ``x_col``, ``y_col`` (one name or a list; default every column), ``x_range`` (in the x
        column's display unit). Derived image: ``region`` crops the stored image. Returns
        ``(png, meta)``; ``meta["label"]`` is the text block that goes before the image."""
        e = self.get(fid)
        dm = int(display_max or self.display_max)
        if e.kind in ("s", "p"):
            return self._render_scan(e, channel, direction, flatten, clip_pct, _region_tuple(region),
                                     dm, highpass_nm, label_kind, label_attrs)
        if e.kind == "d":
            if region is not None:
                raise ValueError(f"{e.fid} is a spectrum: use x_range instead of region")
            ycols = [y_col] if isinstance(y_col, str) else (list(y_col) if y_col else None)
            return self._render_spectrum(e, x_col, ycols, x_range, dm, label_kind, label_attrs)
        return self._render_derived(e, _region_tuple(region), dm, label_kind, label_attrs)

    def _render_scan(self, e: FrameEntry, channel: str | None, direction: str, flatten: str,
                     clip_pct: float, region: tuple | None, display_max: int, highpass_nm: float,
                     label_kind: str, label_attrs: dict | None) -> tuple[bytes, dict]:
        ch = self.resolve_channel(e, channel)
        z = self.values(e, ch, direction)
        nm_y = float(e.extra.get("nm_per_px_y") or e.nm_per_px or 0)
        zf = F.flatten(z, flatten, nm_per_px=math.sqrt(float(e.nm_per_px) * nm_y) if nm_y else e.nm_per_px,
                       highpass_nm=highpass_nm)
        num, den = e.scale_pair
        view = F.plan_view(e.nx, e.ny, num, den, region, display_max)
        nmx = float(e.nm_per_px)
        grid = F.view_grid(view, nmx, nm_y or nmx) if self.grid and nmx > 0 else None
        png, lo, hi = F.render_values(zf, view, clip_pct=clip_pct, grid=grid)
        fmt = self.fmt(e, ch)
        i0, j0, i1, j1 = view.native
        crop = zf[j0:j1, i0:i1]
        fin = crop[np.isfinite(crop)]
        x, y, w, h = view.region_px
        attrs: dict[str, Any] = dict(label_attrs or {})
        attrs.update({"frame": e.fid, "channel": ch, "direction": direction, "flatten": flatten})
        if flatten == "highpass":
            attrs["highpass_nm"] = f"{highpass_nm:g}"
        attrs.update({"scale": F.scale_text(num, den), "size": f"{view.image_size[0]}x{view.image_size[1]}",
                      "region": f"{x},{y},{w},{h}",
                      "field_nm": f"{(i1 - i0) * nmx:.4g}x{(j1 - j0) * nm_y:.4g}"})
        if region is not None:
            attrs["magnification"] = f"{view.magnification:.4g}"
        if grid is not None:
            attrs["grid_nm"] = f"{grid.spacing_nm:g}"
        attrs[F.range_attr(ch, fmt.unit)] = (f"{F.fmt_num(lo * fmt.factor, fmt.decimals)}.."
                                             f"{F.fmt_num(hi * fmt.factor, fmt.decimals)}")
        if e.acquired_rows is not None and e.acquired_rows < e.ny:
            attrs["rows_acquired"] = f"{e.acquired_rows}/{e.ny}"
        meta = {
            "frame": e.fid, "kind": e.kind_name, "channel": ch, "direction": direction, "flatten": flatten,
            "highpass_nm": float(highpass_nm) if flatten == "highpass" else None,
            "region_px": [x, y, w, h], "native_px": [i0, j0, i1, j1],
            "image_size_px": list(view.image_size), "magnification": round(view.magnification, 4),
            "image_px_per_scan_px": view.up if view.down == 1 else round(1.0 / view.down, 6),
            "nm_per_image_px": round(nmx * view.down / view.up, 6),
            "corners_nm": F.view_corners_nm(e.geometry, view),
            "grid_nm": None if grid is None else grid.spacing_nm,
            "unit": fmt.unit, "fmt": fmt,
            "black": fmt.value(lo), "white": fmt.value(hi),
            "min": fmt.value(fin.min()) if fin.size else None, "max": fmt.value(fin.max()) if fin.size else None,
            "label_attrs": attrs, "label": F.visual_label(label_kind, **attrs),
        }
        return png, meta

    def _render_spectrum(self, e: FrameEntry, x_col: str | None, y_cols: list[str] | None,
                         x_range: tuple | None, display_max: int, label_kind: str,
                         label_attrs: dict | None) -> tuple[bytes, dict]:
        cols = self._arrays(e)
        names = list(e.channels)
        xc = names[0] if not x_col else F.resolve_channel(x_col, names, e.units)
        if y_cols:
            ycs = [F.resolve_channel(c, names, e.units) for c in y_cols]
        else:
            ycs = [c for c in names if c != xc][:4] or [xc]
        png, pm = F.render_spectrum(cols, e.units, x_col=xc, y_cols=ycs, x_range=x_range,
                                    width_px=max(600, display_max))
        W, H = F.png_size(png)
        attrs: dict[str, Any] = dict(label_attrs or {})
        attrs.update({"frame": e.fid, "plot": "spectrum", "x": f"{F._strip_unit(xc)} [{pm['x_unit']}]",
                      "y": "; ".join(f"{F._strip_unit(d['column'])} [{d['unit']}]" for d in pm["y"]),
                      "points": pm["n_points"], "size": f"{W}x{H}"})
        if x_range is not None:
            attrs["x_range"] = f"{pm['x_shown'][0]:.6g}..{pm['x_shown'][1]:.6g}"
        meta = {"frame": e.fid, "kind": e.kind_name, "x_col": xc, "x_unit": pm["x_unit"],
                "y": pm["y"], "n_points": pm["n_points"], "x_shown": pm["x_shown"],
                "image_size_px": [W, H], "label_attrs": attrs, "label": F.visual_label(label_kind, **attrs)}
        return png, meta

    def _derived_label_attrs(self, e: FrameEntry) -> dict:
        attrs = {"frame": e.fid, "derived": e.extra.get("tool")}
        if e.extra.get("sources"):
            attrs["frames"] = ",".join(e.extra["sources"])
        attrs.update(e.extra.get("label_attrs") or {})
        attrs["size"] = f"{e.display_size[0]}x{e.display_size[1]}"
        return attrs

    def _render_derived(self, e: FrameEntry, region: tuple | None, display_max: int,
                        label_kind: str, label_attrs: dict | None) -> tuple[bytes, dict]:
        png = (self.root / f"{e.fid}.png").read_bytes()
        W, H = e.display_size
        attrs: dict[str, Any] = dict(label_attrs or {})
        attrs.update(self._derived_label_attrs(e))
        meta: dict[str, Any] = {"frame": e.fid, "kind": e.kind_name, "tool": e.extra.get("tool"),
                                "region_px": [0, 0, W, H], "image_size_px": [W, H], "magnification": 1}
        if region is not None:
            from PIL import Image

            x, y, w, h = region
            if w < 1 or h < 1 or x < 0 or y < 0 or x + w > W or y + h > H:
                raise ValueError(f"region x={x}, y={y}, width={w}, height={h} leaves the "
                                 f"{W}x{H} derived image")
            with Image.open(io.BytesIO(png)) as im:
                a = np.asarray(im.convert("RGB"))[y:y + h, x:x + w]
            m = max(1, display_max // max(w, h))
            img = F.upscale(a, m)
            png = F.encode_png(img)
            attrs.update({"region": f"{x},{y},{w},{h}", "magnification": str(m),
                          "size": f"{img.shape[1]}x{img.shape[0]}"})
            meta.update({"region_px": [x, y, w, h], "image_size_px": [img.shape[1], img.shape[0]],
                         "magnification": m})
        meta["label_attrs"] = attrs
        meta["label"] = F.visual_label(label_kind if label_kind != "current" else "derived", **attrs)
        return png, meta

    def observation(self, fid: str, *, kind: str = "current") -> list[ImageBlock]:
        """The frame's stored default image with its label (no re-rendering)."""
        e = self.get(fid)
        png = (self.root / f"{e.fid}.png").read_bytes()
        attrs = dict((e.extra.get("render") or {}).get("label_attrs") or {})
        label_kind = kind if e.kind != "m" else ("derived" if kind == "current" else kind)
        return [ImageBlock(png=png, label=F.visual_label(label_kind, **attrs))]

    # ── text observation ────────────────────────────────────────────────────
    def text_grid(self, fid: str, n: int = 64) -> str:
        """The frame as ``n`` x ``n`` numbers sampled at cell centers (VISTA-SPM's text-only
        observation): Z plane-flattened, as integers in pm; other channels unflattened in their
        model unit, with the channel's decimals."""
        e = self.get(fid)
        n = int(n)
        if not 1 <= n <= 256:
            raise ValueError("n must be within 1..256")
        if e.kind == "d":
            return self._spectrum_text(e, n)
        if not e.is_image:
            raise ValueError(f"{e.fid} is a {e.kind_word} without values")
        ch = e.default_channel or e.channels[0]
        z = self.values(e, ch, "forward")
        fmt = self.fmt(e, ch)
        mode = "plane" if e.units.get(ch) == "m" else "none"
        dec = 0 if mode == "plane" else fmt.decimals
        zf = F.flatten(z, mode)
        num, den = e.scale_pair
        W, H = e.display_size
        _, cols = sample_axis(0, W, n, num, den, e.nx)
        _, rows = sample_axis(0, H, n, num, den, e.ny)
        sub = zf[np.ix_(rows, cols)] * fmt.factor
        g = e.geometry
        head = F.visual_label(
            "text_grid", frame=e.fid, channel=ch, direction="forward", flatten=mode, unit=fmt.unit,
            decimals=dec, rows=n, columns=n, field_nm=f"{g['w_nm']:.4g}x{g['h_nm']:.4g}",
            display_size=f"{W}x{H}", rows_acquired=(f"{e.acquired_rows}/{e.ny}"
                                                    if e.acquired_rows is not None and e.acquired_rows < e.ny else None))
        what = f"an integer in {fmt.unit}" if dec == 0 else f"a number in {fmt.unit} with {dec} decimals"
        lines = [head,
                 f"Each value is the archived pixel at the center of one cell, as {what}; "
                 f"row 0 is the frame's top edge, column 0 its left edge; '.' = never acquired. "
                 f"Cell (r, c) is display pixel x = ((2c+1)*{W})//{2 * n}, y = ((2r+1)*{H})//{2 * n} "
                 f"of the {W}x{H} display image."]
        for r in range(n):
            vals = ["." if not np.isfinite(v) else (str(int(np.rint(v))) if dec == 0 else F.fmt_num(float(v), dec))
                    for v in sub[r]]
            lines.append(" ".join(vals))
        return "\n".join(lines)

    def _spectrum_text(self, e: FrameEntry, n: int) -> str:
        cols = self._arrays(e)
        names = list(e.channels)
        N = e.nx
        idx = [min(N - 1, ((2 * i + 1) * N) // (2 * n)) for i in range(min(n, N))]
        fmts = {c: self.fmt(e, c) for c in names}
        head = F.visual_label("text_spectrum", frame=e.fid, points=N, samples=len(idx),
                              columns="; ".join(f"{F._strip_unit(c)} [{fmts[c].unit}]" for c in names))
        lines = [head, "\t".join(names)]
        for i in idx:
            lines.append("\t".join(
                "." if not np.isfinite(cols[c][i]) else F.fmt_num(float(cols[c][i]) * fmts[c].factor, fmts[c].decimals)
                for c in names))
        return "\n".join(lines)


def sample_axis(x0: int, w: int, n: int, num: int, den: int, n_native: int) -> tuple[np.ndarray, np.ndarray]:
    """Bin-center sampling along one axis (VISTA's read_pixels rule).

    Returns ``(display_px, native_idx)``: the display pixel ``x0 + ((2c+1)*w)//(2n)`` of
    every cell and the archived (native) pixel read there. When the display is a block mean
    (``den > 1``) the native pixel is the one under the cell's exact center."""
    c = np.arange(int(n))
    disp = int(x0) + ((2 * c + 1) * int(w)) // (2 * int(n))
    if den == 1:
        nat = disp // int(num)
    else:
        center = int(x0) + (2 * c + 1) * int(w) / (2.0 * int(n))
        nat = np.floor(center * int(den)).astype(int)
    nat = np.clip(nat, 0, int(n_native) - 1)
    return disp.astype(int), nat.astype(int)
