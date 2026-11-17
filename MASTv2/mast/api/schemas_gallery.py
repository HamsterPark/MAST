"""Pydantic models for the 数据图库 slice.


* **incremental preprocessing** (``mast.gallery.build``): inventory the configured
  data roots → run the calibrated judges on new frames → render thumbnails →
  write ``index.json``;
* **a marks store** (``mast.gallery.marks``): ``marks.json`` is the single source
  of truth for ratings / tags / notes / series / spectrum→frame anchors, and every
  write also refreshes ``marks.md`` / ``marks.csv`` / ``marks_series.csv``;
* **a single-page UI** (``frontend/src/components/gallery``).

This file is the single source of types for the slice. Two responses are
DOCUMENTED here but served straight from disk (``GalleryIndex`` and
``GalleryMarksDoc``): validating 5000+ index items through Pydantic on every page
load buys nothing, and re-serialising a marks document would pad every stored mark
with default keys it never had. The models still land in ``openapi.json`` so the
frontend gets real types.

``GalleryItem`` keys are SHORT on purpose. They are the keys of the standalone
gallery's ``data.js`` (plus a few additions marked below), so the two can be
diffed field by field when checking the port against the original.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# ── config ─────────────────────────────────────────────────────────────


class GalleryRoot(BaseModel):
    """One data root the gallery indexes.

    ``name`` is the stable half of every item id (``<name>/<relative path>``):
    when the data moves, edit ``path`` and keep ``name`` and every mark follows."""

    name: str = Field(..., description="stable root name; the first segment of every item id")
    path: str = Field(..., description="absolute directory path")
    enabled: bool = True
    exists: bool = Field(
        False, description="filled on responses: whether the directory exists right now"
    )


class GalleryRootSuggestion(BaseModel):
    """A directory worth offering as a root. Offered, never added implicitly."""

    path: str
    why: str


class GalleryEnvTagStatus(BaseModel):
    """The env-tag recorder in THIS process (``mast.envhistory.filetags.tagger``). Counters are
    since the service started. ``active=false``: no recorder here (standalone API process)."""

    model_config = ConfigDict(extra="allow")

    active: bool = False
    enabled: bool = True
    max_age_s: float = 30.0
    pending: int = 0
    worker_alive: bool = False
    written: int = Field(0, description="sidecars written")
    superseded: int = Field(0, description="sidecars written after moving a stale one aside")
    same: int = Field(0, description="files whose sidecar already described them")
    reused: int = Field(0, description="copies given the original's record")
    computed: int = Field(0, description="records made from the cached readings")
    empty: int = Field(0, description="files saved while nothing was known (no record written)")
    failed_write: int = Field(0, description="sidecar writes that failed (central record kept)")
    cooldown_skipped: int = Field(0, description="files in a directory cooling down after a failure")
    central_written: int = 0
    central_failed: int = Field(0, description="central records that could not be written")
    dropped_full: int = Field(0, description="files dropped because the queue was full")
    dropped_disabled: int = Field(0, description="files dropped because recording was switched off")
    errors: int = Field(0, description="unexpected errors inside the recorder (see last_error)")
    cooling_dirs: int = 0
    last_error: Optional[str] = None


class GalleryEnvTagsConfig(BaseModel):
    """Environment tags on saved data files (docs/v2/design/data_env_tags.md): the two
    settings and the recorder in this process. Stored in the gallery state directory
    (``env_tags.json``), not in the gallery ``config.json``: saving the data roots rewrites that
    file and must not reset these."""

    ok: bool = True
    enabled: bool = Field(
        True, description="record the known environment beside every newly saved data file "
                          "(.mast/envtags/<file>.json)")
    max_age_s: float = Field(
        30.0, description="a cached reading counts as current when it is at most this many seconds "
                          "from the save (5-600); older ones are recorded with their age")
    status: Optional[GalleryEnvTagStatus] = Field(
        None, description="the recorder in this process; null in a process without one")
    degraded: bool = False
    detail: Optional[str] = None


class GalleryEnvTagsUpdate(BaseModel):
    """Change the env-tag settings. An omitted key is unchanged; the data roots are never touched."""

    enabled: Optional[bool] = Field(default=None, description="omitted ⇒ unchanged")
    max_age_s: Optional[float] = Field(default=None, ge=5, le=600, description="omitted ⇒ unchanged")


class GalleryConfigResponse(BaseModel):
    ok: bool = True
    state_dir: str = Field("", description="where caches, thumbnails, index and marks live")
    roots: list[GalleryRoot] = Field(default_factory=list)
    suggestions: list[GalleryRootSuggestion] = Field(default_factory=list)
    workers: int = 2
    auto_build: bool = Field(
        True, description="incremental build in the background every auto_interval_min minutes "
                          "and when the gallery page is opened")
    auto_interval_min: int = Field(10, description="minutes between automatic incremental builds (2-720)")
    degraded: bool = False
    detail: Optional[str] = None


class GalleryRootIn(BaseModel):
    name: Optional[str] = Field(
        None, description="omitted ⇒ derived from the directory name (made unique)"
    )
    path: str
    enabled: bool = True


class GalleryConfigUpdate(BaseModel):
    """Replace the root list (and optionally the worker count). Validation errors
    come back as ``ok=false`` + ``detail`` and nothing is written."""

    roots: list[GalleryRootIn] = Field(default_factory=list)
    workers: Optional[int] = Field(default=None, ge=1, le=16)
    auto_build: Optional[bool] = Field(default=None, description="omitted ⇒ unchanged")
    auto_interval_min: Optional[int] = Field(default=None, ge=2, le=720, description="omitted ⇒ unchanged")


# ── build ──────────────────────────────────────────────────────────────


class GalleryBuildRequest(BaseModel):
    force: bool = Field(
        False,
        description="re-render and re-judge everything; the inventory cache is still "
                    "keyed on (size, mtime_ns), so unchanged headers are not re-read",
    )


class GalleryAutoRequest(BaseModel):
    """Ask for an automatic incremental build. The body is required (a bodyless POST must not be
    accepted: trap on optional request bodies as a CSRF door). The server decides whether one
    actually starts (switch on, a root enabled, nothing running, >= 1 min since the last build)."""

    reason: Literal["open"] = Field(..., description="the gallery page was opened / became visible")


class GalleryBuildError(BaseModel):
    id: str
    why: str


GalleryPhase = Literal["idle", "inventory", "render", "dups", "index", "done", "cancelled", "error"]


class GalleryBuildStatus(BaseModel):
    """State of the (single, process-wide) background build."""

    running: bool = False
    phase: GalleryPhase = "idle"
    started: Optional[str] = None
    finished: Optional[str] = None
    done: int = Field(0, description="render/analysis tasks finished in this build")
    total: int = Field(0, description="render/analysis tasks queued in this build")
    n_files: int = Field(0, description="index entries after copy-collapse")
    n_new: int = 0
    n_changed: int = 0
    n_render: int = 0
    n_analysis: int = 0
    n_failed: int = 0
    errors: list[GalleryBuildError] = Field(default_factory=list)
    log: list[str] = Field(default_factory=list, description="most recent log lines")
    message: str = ""
    degraded: bool = False
    detail: Optional[str] = None
    trigger: Literal["manual", "auto"] = Field(
        "manual", description="who started this build: the operator's click, or the automatic updater")
    n_removed: int = Field(0, description="entries dropped because their files are gone")
    n_deferred: int = Field(
        0, description="files written in the last minute, left for the next round (not read, not cached)")
    n_env: int = Field(
        0, description="entries whose environment tags changed in this build (a sidecar appeared "
                       "after the file was first indexed counts) — the index changed even if no file did")


# ── index ──────────────────────────────────────────────────────────────


class GalleryEnvValue(BaseModel):
    """One primary value in an item's environment summary (card + filters)."""

    v: Optional[float] = Field(None, description="value in the sensor's own unit (4 significant digits)")
    u: str = Field("", description="the sensor's own unit, shown on the card as is")
    c: Optional[float] = Field(None, description="canonical value for filters: K / T / Pa / % / cm")
    s: Literal["l", "h", "o", "i"] = Field(
        "l", description="l = a cached reading near the save; h = the env-history bucket holding the "
                         "save; o = stale (the last reading before the save, see a); i = inferred later")
    a: Optional[int] = Field(None, description="stale only: how many seconds before the save it was read")


class GalleryEnvSummary(BaseModel):
    """``env`` on an index item. Present only when something is known."""

    s: Literal["r", "i"] = Field(
        "r", description="r = recorded when the file was saved (sidecar or central record); "
                         "i = inferred by the gallery from the environment history")
    T: Optional[GalleryEnvValue] = Field(None, description="temperature")
    B: Optional[GalleryEnvValue] = Field(None, description="magnetic field")
    P: Optional[GalleryEnvValue] = Field(None, description="pressure")
    L: Optional[GalleryEnvValue] = Field(None, description="cryogen level")
    wT: Optional[list[float]] = Field(None, description="[min, max] during the acquisition, same unit as T.u")
    wB: Optional[list[float]] = Field(None, description="[min, max] during the acquisition, same unit as B.u")
    wP: Optional[list[float]] = Field(None, description="[min, max] during the acquisition, same unit as P.u")
    wL: Optional[list[float]] = Field(None, description="[min, max] during the acquisition, same unit as L.u")
    wr: Optional[list[str]] = Field(
        None, description="keys (T/B/P/L) whose window came from individual readings, so it covers "
                          "exactly the acquisition; the other windows come from 1-minute buckets and "
                          "are wider. Only these mark a card as changed during the acquisition")


class GalleryItem(BaseModel):
    """One frame (``k='f'``), point spectrum (``'s'``) or grid spectrum (``'g'``).

    Field meanings are tabulated in the design doc §4.1. Additions over the
    standalone gallery's ``data.js``: ``p pf cp cpl r0 r1 ar hl seg`` (and ``hn``
    on grids). ``sm`` (a sample kind hard-coded from file names) is deliberately
    absent — see the doc, trap T10."""

    model_config = ConfigDict(extra="allow")

    id: str = Field(..., description="<root name>/<path relative to the root>")
    k: Literal["f", "s", "g"]
    d: str = Field(..., description="directory key: <root name>/<relative parent dir>")
    fn: str
    p: str = Field("", description="absolute path of the representative file")
    pf: str = Field("", description="file-name prefix (stem minus the trailing counter)")
    t: Optional[float] = Field(None, description="start, epoch s (frame: REC time; spectrum/grid: Start time)")
    mt: Optional[float] = Field(None, description="file mtime, epoch s (= when the instrument saved it)")
    ad: str = Field("", description="batch in which the file was first inventoried")
    th: str = Field("", description="thumbnail URL")
    cp: Optional[int] = Field(None, description="byte-identical copies folded into this entry (>1 only)")
    cpl: Optional[list[str]] = Field(None, description="absolute paths of every copy (>1 only)")

    # frame (and grid) geometry / working point
    w: Optional[float] = Field(None, description="frame width, nm")
    hn: Optional[float] = Field(None, description="frame height, nm")
    b: Optional[float] = Field(None, description="bias, V")
    sp: Optional[float] = Field(None, description="setpoint, pA")
    nx: Optional[int] = None
    ny: Optional[int] = None
    ang: Optional[float] = Field(None, description="scan angle, deg; positive = frame rotated clockwise")
    cx: Optional[float] = Field(None, description="centre x, nm")
    cy: Optional[float] = Field(None, description="centre y, nm")
    sd: Optional[Literal["u", "d"]] = Field(None, description="scan direction")
    acq: Optional[float] = Field(None, description="ACQ_TIME, s")
    rows: Optional[int] = Field(None, description="rows that are entirely finite")
    rall: Optional[int] = Field(None, description="rows in the frame")
    r0: Optional[int] = Field(None, description="thumbnail shows oriented rows [r0, r1)")
    r1: Optional[int] = None
    li: Optional[str] = Field(None, description="lock-in channel thumbnail URL, '' when none")
    at: Optional[float] = Field(None, description="atomic-phase angular concentration; 0 = not passed")
    ar: Optional[str] = Field(None, description="why the atomic judge could not decide, '' otherwise")
    hf: Optional[float] = Field(None, description="superstructure candidate/control ratio; 0 = none")
    hl: Optional[str] = Field(None, description="superstructure fraction label, e.g. '(0.5,0)'")
    dup: Optional[str] = Field(None, description="id of the frame this one is a repeat save of")
    seg: Optional[int] = Field(None, description="distinct files from the same acquisition (>=2 only)")

    # spectrum (and grid)
    n: Optional[int] = Field(None, description="points per sweep")
    v0: Optional[float] = None
    v1: Optional[float] = None
    zo: Optional[float] = Field(None, description="Z offset, pm")
    x: Optional[float] = Field(None, description="spectrum position x, nm")
    y: Optional[float] = Field(None, description="spectrum position y, nm")
    sw: Optional[int] = Field(None, description="sweeps")
    lic: Optional[int] = Field(None, description="1 = lower panel is a lock-in channel")
    dn: Optional[int] = Field(None, description="1 = lower panel is a numerical derivative of I(V)")
    ex: Optional[str] = Field(None, description="Experiment name when this is not a bias spectrum")

    # grid
    t1: Optional[float] = Field(None, description="end, epoch s")
    gx: Optional[int] = None
    gy: Optional[int] = None
    have: Optional[int] = Field(None, description="grid points completed")

    # environment (docs/v2/design/data_env_tags.md)
    env: Optional[GalleryEnvSummary] = Field(
        None, description="known environment when the file was saved (or inferred); absent = nothing known")


class GalleryIndex(BaseModel):
    version: int = 1
    generated: str = ""
    built: bool = Field(False, description="false until the first build has written an index")
    roots: list[GalleryRoot] = Field(default_factory=list)
    n_frames: int = 0
    n_spectra: int = 0
    n_grids: int = 0
    last_batch: str = ""
    items: list[GalleryItem] = Field(default_factory=list)
    degraded: bool = False
    detail: Optional[str] = None


# ── environment detail (GET /gallery/env/{id}) ─────────────────────────
# The record is the sidecar's own schema (``mast.envtags/1``,
# docs/v2/design/data_env_tags.md §4); these models only give the frontend types.


class GalleryEnvValueDetail(BaseModel):
    model_config = ConfigDict(extra="allow")

    sensor: str = Field("", description="sensor name at the time (provenance only)")
    driver: str = ""
    kind: str = Field("", description="temperature / field / pressure / level / magnet_current / other")
    value: Optional[float] = None
    unit: str = ""
    canonical: Optional[float] = None
    canonical_unit: str = ""
    status: str = ""
    source: str = Field("", description="live / history / inferred")
    fresh: bool = True
    t: Optional[float] = Field(None, description="when MAST got the reading (bucket start for history), epoch s")
    iso: str = ""
    span_s: Optional[float] = Field(None, description="0 for a reading; the bucket width for history")
    dt_s: Optional[float] = Field(None, description="reading time minus save time, s (negative = before)")
    bucket: Optional[dict[str, Any]] = None
    primary: bool = False


class GalleryEnvWindowDetail(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    sensor: str = ""
    kind: str = ""
    unit: str = ""
    min: Optional[float] = None
    max: Optional[float] = None
    canonical_min: Optional[float] = None
    canonical_max: Optional[float] = None
    canonical_unit: str = ""
    n: int = 0
    from_: Optional[float] = Field(None, alias="from", description="start of the interval actually covered")
    to: Optional[float] = Field(None, description="end of the interval actually covered")
    method: str = Field("", description="readings / buckets / buckets+readings")
    complete: bool = False


class GalleryEnvMissing(BaseModel):
    model_config = ConfigDict(extra="allow")

    sensor: str = ""
    kind: Optional[str] = None
    why: str = Field("", description="no_reading / invalid")
    status: Optional[str] = None


class GalleryEnvAcquisition(BaseModel):
    model_config = ConfigDict(extra="allow")

    start: Optional[float] = None
    start_iso: str = ""
    start_from: str = ""
    end: Optional[float] = None
    end_iso: str = ""
    end_from: str = ""
    duration_s: Optional[float] = None


class GalleryEnvRecord(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    schema_: str = Field("", alias="schema")
    file: dict[str, Any] = Field(default_factory=dict)
    saved_at: dict[str, Any] = Field(default_factory=dict)
    acquisition: Optional[GalleryEnvAcquisition] = None
    values: list[GalleryEnvValueDetail] = Field(default_factory=list)
    window: list[GalleryEnvWindowDetail] = Field(default_factory=list)
    missing: list[GalleryEnvMissing] = Field(default_factory=list)
    tagged: dict[str, Any] = Field(default_factory=dict)
    inferred: bool = False


class GalleryEnvDetail(BaseModel):
    id: str
    how: Literal["sidecar", "central", "inferred", "none"] = Field(
        "none", description="sidecar / central record (both made when the file was saved) / "
                            "inferred from the environment history / nothing known")
    sidecar_path: str = ""
    record: Optional[GalleryEnvRecord] = None
    degraded: bool = False
    detail: Optional[str] = None


# ── marks ──────────────────────────────────────────────────────────────


class GalleryAnchor(BaseModel):
    """Where a spectrum (or a series of them) was taken, pinned to a frame."""

    model_config = ConfigDict(extra="allow")

    id: str = Field(..., description="frame item id")
    fn: str = ""
    rel: str = Field("", description="'prev' or 'next' relative to the spectrum")
    dt: Optional[float] = Field(None, description="seconds between spectrum and frame")
    desc: str = ""
    u: Optional[float] = Field(None, description="fraction across the frame from the left")
    v: Optional[float] = Field(None, description="fraction down the frame from the top")
    inside: Optional[bool] = None


class GalleryMark(BaseModel):
    """One item's mark. In a patch, ``{"del": true, "ts": …}`` removes it."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    r: int = Field(0, description="2 = 重点, 1 = 可用, -1 = 排除, 0 = none")
    tags: list[str] = Field(default_factory=list)
    note: str = ""
    t: str = Field("", description="local time of the last edit")
    ts: int = Field(0, description="ms stamp; the server drops edits older than what it holds")
    tt: Optional[float] = Field(None, description="the item's own start time (for sorting)")
    k: str = ""
    meta: str = ""
    anchor: Optional[GalleryAnchor] = None
    deleted: bool = Field(False, alias="del")


class GallerySeries(BaseModel):
    """A named run of consecutive items. ``{"del": true}`` or empty ``ids`` removes it."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    name: str = ""
    k: str = Field("", description="f / s / g / mix")
    ids: list[str] = Field(default_factory=list)
    r: int = 0
    tags: list[str] = Field(default_factory=list)
    note: str = ""
    t: str = ""
    ts: int = 0
    anchor: Optional[GalleryAnchor] = None
    deleted: bool = Field(False, alias="del")


class GalleryDirMark(BaseModel):
    """Per-directory state. Stored under ``days`` for file compatibility with the
    standalone gallery, whose directories were dates."""

    model_config = ConfigDict(extra="allow")

    done: bool = False
    note: str = ""
    ts: int = 0


class GalleryMarksDoc(BaseModel):
    model_config = ConfigDict(extra="allow")

    version: int = 1
    rev: int = 0
    updated: str = ""
    tags: list[str] = Field(default_factory=list)
    items: dict[str, GalleryMark] = Field(default_factory=dict)
    series: dict[str, GallerySeries] = Field(default_factory=dict)
    days: dict[str, GalleryDirMark] = Field(default_factory=dict)
    tomb: dict[str, int] = Field(default_factory=dict)
    stomb: dict[str, int] = Field(default_factory=dict)
    degraded: bool = False
    detail: Optional[str] = None


class GalleryMarksPatch(BaseModel):
    items: dict[str, GalleryMark] = Field(default_factory=dict)
    series: dict[str, GallerySeries] = Field(default_factory=dict)
    days: dict[str, GalleryDirMark] = Field(default_factory=dict)
    tags: Optional[list[str]] = Field(None, description="the whole tag table, when it changed")


class GalleryMarksPatchResult(BaseModel):
    ok: bool = False
    rev: int = 0
    updated: str = ""
    degraded: bool = False
    detail: Optional[str] = None


class GalleryMarksImportRequest(BaseModel):
    """Merge a ``marks.json`` (e.g. the standalone gallery's) into this one."""

    doc: dict[str, Any]
    key_prefix: str = Field(
        "",
        description="prepended to item keys, series members and anchor ids — the "
                    "standalone gallery keyed paths relative to its RAW root, so "
                    "importing it needs the root name here (e.g. 'SPM/')",
    )


class GalleryMarksImportResult(BaseModel):
    ok: bool = False
    items: int = 0
    series: int = 0
    days: int = 0
    tags_added: int = 0
    unmatched: int = Field(
        0, description="imported keys with no item in the current index (imported anyway)"
    )
    rev: int = 0
    updated: str = ""
    degraded: bool = False
    detail: Optional[str] = None
