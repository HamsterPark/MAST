"""数据图库 —— thin relay onto ``mast.gallery``.


House rules this module holds:

* **Lazy imports, graceful degradation.** Every handler imports ``mast.gallery``
  inside a try and answers ``degraded=True`` + ``detail`` on any failure. Never 500.
* **GET is read-only.** No handler behind a GET creates the state directory or
  writes a file (trap T3): a contract test that walks every GET must not be able
  to leave a ``_gallery`` folder in the operator's real data root.
* **The build never runs on a request thread.** ``POST /gallery/build`` starts (or
  reports) the single background build and returns immediately.
* **Own prefix.** ``/gallery/*`` overlaps nothing; this repo has twice had a
  ``/{param}`` route capture a literal segment and answer 200 from the wrong
  handler (see the registration comments in ``api/app.py``).
"""

from __future__ import annotations

import logging
import os
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from mast.api.schemas_gallery import (
    GalleryAutoRequest,
    GalleryBuildRequest,
    GalleryBuildStatus,
    GalleryConfigResponse,
    GalleryConfigUpdate,
    GalleryIndex,
    GalleryMarksDoc,
    GalleryMarksImportRequest,
    GalleryMarksImportResult,
    GalleryMarksPatch,
    GalleryMarksPatchResult,
    GalleryRoot,
    GalleryRootSuggestion,
)

from mast.i18n import tr  # noqa: E402

logger = logging.getLogger(__name__)

router = APIRouter(tags=["gallery"])


# ── config ─────────────────────────────────────────────────────────────


def _config_response() -> GalleryConfigResponse:
    from mast.gallery import config as gcfg
    from mast.gallery import paths as gpaths

    cfg = gcfg.load_config()
    return GalleryConfigResponse(
        ok=True,
        state_dir=str(gpaths.state_dir()),
        roots=[
            GalleryRoot(name=r.name, path=r.path, enabled=r.enabled,
                        exists=os.path.isdir(r.path))
            for r in cfg.roots
        ],
        suggestions=[GalleryRootSuggestion(path=p, why=why)
                     for p, why in gcfg.suggest_roots()],
        workers=cfg.workers,
        auto_build=cfg.auto_build,
        auto_interval_min=cfg.auto_interval_min,
    )


@router.get("/gallery/config", response_model=GalleryConfigResponse)
def gallery_config() -> GalleryConfigResponse:
    try:
        return _config_response()
    except Exception as exc:  # noqa: BLE001 — degrade, never 500
        logger.warning("gallery config read failed: %s", exc)
        return GalleryConfigResponse(ok=False, degraded=True, detail=str(exc))


def _move_marks_of_merged_roots(old_cfg, merges) -> list[str]:
    """A data root folded into another changes its files' ids (``<old>/<x>`` becomes
    ``<into>/<rel>/<x>``). Move that root's marks along so none are left on ids nobody uses.
    Only a root that was saved before can carry marks; it is found by path. Best effort: the
    config is already saved, so a failure here is reported, not raised."""
    if not merges:
        return []
    from mast.gallery import config as gcfg
    from mast.gallery import marks as gmarks

    old_by_path = {gcfg._norm(r.path): r.name for r in old_cfg.roots}
    notes: list[str] = []
    for m in merges:
        old = old_by_path.get(gcfg._norm(m.path))
        if not old:
            continue
        old_prefix = old + "/"
        new_prefix = m.into + "/" + (m.rel + "/" if m.rel else "")
        if old_prefix == new_prefix:
            continue
        try:
            n = gmarks.rekey_prefix(old_prefix, new_prefix)
        except Exception as exc:  # noqa: BLE001
            logger.warning("gallery: marks of merged root %s not moved: %s", old, exc)
            notes.append(tr(f"「{old}」下的标记没能转过去（{exc}）",
                            f"The marks under '{old}' could not be moved ({exc})"))
            continue
        if n:
            notes.append(tr(f"「{old}」下的 {n} 条标记已转到「{m.into}」下",
                            f"{n} mark(s) under '{old}' moved to '{m.into}'"))
    return notes


@router.post("/gallery/config", response_model=GalleryConfigResponse)
def gallery_config_update(body: GalleryConfigUpdate) -> GalleryConfigResponse:
    """Replace the root list. Invalid input writes nothing and says why. Nested or repeated roots
    are merged into the outer / first one instead of refused; ``detail`` then says what was merged
    (with ``ok`` true)."""
    try:
        from mast.gallery import config as gcfg

        merges: list = []
        roots, errors = gcfg.normalise_roots([r.model_dump() for r in body.roots], merges=merges)
        if errors:
            current = _config_response()
            current.ok = False
            current.detail = "；".join(errors)
            return current
        cur = gcfg.load_config()
        gcfg.save_config(gcfg.GalleryConfig(
            roots=roots,
            workers=body.workers if body.workers is not None else cur.workers,
            auto_build=body.auto_build if body.auto_build is not None else cur.auto_build,
            auto_interval_min=(body.auto_interval_min if body.auto_interval_min is not None
                               else cur.auto_interval_min)))
        notes = [m.note for m in merges] + _move_marks_of_merged_roots(cur, merges)
        resp = _config_response()
        if notes:
            resp.detail = "；".join(notes)
        return resp
    except Exception as exc:  # noqa: BLE001
        logger.warning("gallery config write failed: %s", exc)
        return GalleryConfigResponse(ok=False, degraded=True, detail=str(exc))


# ── build ──────────────────────────────────────────────────────────────


@router.get("/gallery/status", response_model=GalleryBuildStatus)
def gallery_status() -> GalleryBuildStatus:
    try:
        from mast.gallery import service

        return GalleryBuildStatus(**service.get_status())
    except Exception as exc:  # noqa: BLE001
        return GalleryBuildStatus(degraded=True, detail=str(exc))


@router.post("/gallery/build", response_model=GalleryBuildStatus)
def gallery_build(body: GalleryBuildRequest) -> GalleryBuildStatus:
    """Start the background build, or report the one already running."""
    try:
        from mast.gallery import service

        return GalleryBuildStatus(**service.start_build(force=body.force))
    except Exception as exc:  # noqa: BLE001
        logger.warning("gallery build start failed: %s", exc)
        return GalleryBuildStatus(degraded=True, detail=str(exc))


@router.post("/gallery/auto", response_model=GalleryBuildStatus)
def gallery_auto(body: GalleryAutoRequest) -> GalleryBuildStatus:
    """The gallery page was opened: start an automatic incremental build if the server's rules
    allow it (switch on, a root enabled, nothing running, >= 1 min since the last build, past
    the start-up hold). Always answers with the current status; never waits."""
    try:
        from mast.gallery import service

        _started, status = service.start_auto(body.reason)
        return GalleryBuildStatus(**status)
    except Exception as exc:  # noqa: BLE001
        logger.warning("gallery auto build request failed: %s", exc)
        return GalleryBuildStatus(degraded=True, detail=str(exc))


@router.post("/gallery/build/cancel", response_model=GalleryBuildStatus)
def gallery_build_cancel() -> GalleryBuildStatus:
    try:
        from mast.gallery import service

        return GalleryBuildStatus(**service.cancel_build())
    except Exception as exc:  # noqa: BLE001
        return GalleryBuildStatus(degraded=True, detail=str(exc))


# ── index + thumbnails ─────────────────────────────────────────────────


@router.get("/gallery/index", response_model=GalleryIndex)
def gallery_index(request: Request):
    """The whole index in one response, served from disk (design D9).

    Declared as ``GalleryIndex`` so the frontend gets types; returned as the
    pre-serialised bytes so 5000+ items are not re-validated per page load. When
    the client accepts gzip and the build wrote ``index.json.gz``, those bytes go
    out with ``Content-Encoding: gzip``."""
    try:
        from mast.gallery import index as gindex

        accepts_gzip = "gzip" in (request.headers.get("accept-encoding") or "").lower()
        got = gindex.read_index_bytes(prefer_gzip=accepts_gzip)
        if got is None:
            return GalleryIndex(built=False)
        data, gzipped = got
        headers = {"Cache-Control": "no-store"}
        if gzipped:
            headers["Content-Encoding"] = "gzip"
        return Response(content=data, media_type="application/json", headers=headers)
    except Exception as exc:  # noqa: BLE001
        logger.warning("gallery index read failed: %s", exc)
        return GalleryIndex(degraded=True, detail=str(exc))


@router.get("/gallery/thumb/{path:path}")
def gallery_thumb(path: str, v: int | None = None):
    """One rendered thumbnail. ``v`` is a cache-buster the index puts on every URL,
    so a URL that carries it can be cached forever (design D10)."""
    try:
        from mast.gallery import paths as gpaths

        base = gpaths.thumbs_dir().resolve()
        target = (base / path).resolve()
        if not target.is_relative_to(base) or not target.is_file():
            return JSONResponse({"detail": "not found"}, status_code=404)
        cache = ("public, max-age=31536000, immutable" if v is not None else "no-cache")
        return FileResponse(target, headers={"Cache-Control": cache})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"detail": str(exc)}, status_code=404)


# ── marks ──────────────────────────────────────────────────────────────


@router.get("/gallery/marks", response_model=GalleryMarksDoc)
def gallery_marks():
    """The marks document as stored (design D11/D9: not re-serialised)."""
    try:
        from mast.gallery import marks as gmarks

        return JSONResponse(gmarks.load_marks(), headers={"Cache-Control": "no-store"})
    except Exception as exc:  # noqa: BLE001
        logger.warning("gallery marks read failed: %s", exc)
        return GalleryMarksDoc(degraded=True, detail=str(exc))


@router.post("/gallery/marks/patch", response_model=GalleryMarksPatchResult)
def gallery_marks_patch(body: GalleryMarksPatch) -> GalleryMarksPatchResult:
    """Apply one batch of edits. Only the keys the client sent are stored —
    ``exclude_unset`` keeps a mark from growing every default field."""
    try:
        from mast.gallery import marks as gmarks

        patch = body.model_dump(by_alias=True, exclude_unset=True)
        rev, updated = gmarks.patch_marks(patch)
        return GalleryMarksPatchResult(ok=True, rev=rev, updated=updated)
    except Exception as exc:  # noqa: BLE001
        logger.warning("gallery marks patch failed: %s", exc)
        return GalleryMarksPatchResult(ok=False, degraded=True, detail=str(exc))


@router.post("/gallery/marks/import", response_model=GalleryMarksImportResult)
def gallery_marks_import(body: GalleryMarksImportRequest) -> GalleryMarksImportResult:
    try:
        from mast.gallery import marks as gmarks

        res = gmarks.import_marks(body.doc, key_prefix=body.key_prefix)
        return GalleryMarksImportResult(ok=True, **res)
    except Exception as exc:  # noqa: BLE001
        logger.warning("gallery marks import failed: %s", exc)
        return GalleryMarksImportResult(ok=False, degraded=True, detail=str(exc))


@router.get("/gallery/marks/export/{fmt}")
def gallery_marks_export(fmt: Literal["json", "md", "csv", "series_csv"]):
    """Download one rendering of the marks. Read-only: generated on the fly when
    nothing has been saved yet."""
    try:
        from mast.gallery import marks as gmarks

        data, media_type, filename = gmarks.export_file(fmt)
        return Response(
            content=data, media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"',
                     "Cache-Control": "no-store"},
        )
    except Exception as exc:  # noqa: BLE001
        # 503 rather than a 200 JSON body: this is a download link, and a browser
        # would otherwise save the error message under the name marks.md.
        return JSONResponse({"degraded": True, "detail": str(exc)}, status_code=503)


# ── 环境标签（docs/v2/design/data_env_tags.md）──────────────────────────────
# 自成一段放在文件末尾。设置有自己的端点和文件（图库状态目录的 env_tags.json），与上面图库配置
# 那条路由互不相干：那条路由整条重写 config.json，设置放进去就得改它，不改又会被它重置成默认。

from mast.api.schemas_gallery import (  # noqa: E402
    GalleryEnvDetail,
    GalleryEnvTagStatus,
    GalleryEnvTagsConfig,
    GalleryEnvTagsUpdate,
)


def _env_tag_status() -> GalleryEnvTagStatus | None:
    """本进程里环境标签记录器的计数（服务进程里由 runtime 建；独立 API 进程里没有 ⇒ None）。"""
    try:
        from mast.envhistory.filetags.tagger import get_tagger

        tagger = get_tagger()
        if tagger is None:
            return None
        st = dict(tagger.stats())
        st.pop("ring", None)
        st["active"] = True
        return GalleryEnvTagStatus(**st)
    except Exception as exc:  # noqa: BLE001 — 状态读不到不该挡住设置页
        logger.debug("env tag status unavailable: %s", exc)
        return None


def _env_tags_response() -> GalleryEnvTagsConfig:
    from mast.envhistory.filetags import settings as envset

    enabled, max_age_s = envset.load()
    return GalleryEnvTagsConfig(enabled=enabled, max_age_s=max_age_s, status=_env_tag_status())


def _env_settings_changed() -> None:
    """设置改了：清掉读设置的 TTL 缓存，并让记录器马上重读（不等 5 s）。"""
    try:
        from mast.envhistory.filetags.settings import reset_cache
        from mast.envhistory.filetags.tagger import get_tagger

        reset_cache()
        tagger = get_tagger()
        if tagger is not None:
            tagger.refresh_settings()
    except Exception as exc:  # noqa: BLE001
        logger.debug("env tag settings refresh failed: %s", exc)


@router.get("/gallery/env-tags", response_model=GalleryEnvTagsConfig)
def gallery_env_tags() -> GalleryEnvTagsConfig:
    """环境标签的两个设置，加上本进程记录器的计数。只读，不创建任何东西。"""
    try:
        return _env_tags_response()
    except Exception as exc:  # noqa: BLE001
        logger.warning("gallery env-tag settings read failed: %s", exc)
        return GalleryEnvTagsConfig(ok=False, degraded=True, detail=str(exc))


@router.post("/gallery/env-tags", response_model=GalleryEnvTagsConfig)
def gallery_env_tags_update(body: GalleryEnvTagsUpdate) -> GalleryEnvTagsConfig:
    """改环境标签的设置，省略的键不变。只写状态目录的 ``env_tags.json``，不碰数据根。"""
    try:
        from mast.envhistory.filetags import settings as envset

        enabled, max_age_s = envset.load()
        envset.save(enabled if body.enabled is None else body.enabled,
                    max_age_s if body.max_age_s is None else body.max_age_s)
        _env_settings_changed()
        return _env_tags_response()
    except Exception as exc:  # noqa: BLE001
        logger.warning("gallery env-tag settings write failed: %s", exc)
        return GalleryEnvTagsConfig(ok=False, degraded=True, detail=str(exc))


@router.get("/gallery/env/{path:path}", response_model=GalleryEnvDetail)
def gallery_env(path: str) -> GalleryEnvDetail:
    """一个条目（``path`` = 条目 id）的完整环境记录：保存时写下的 sidecar、中央记录，或现推断。

    只读：读 sidecar、查环境历史库、必要时读数据文件头 64 KiB 核对身份；不写任何东西，也不
    新建环境历史库（docs/v2/design/data_env_tags.md）。条目不在索引里 ⇒ ``how="none"``。永不 500。"""
    try:
        from mast.gallery import envtags as genv

        return GalleryEnvDetail.model_validate(genv.detail(path))
    except Exception as exc:  # noqa: BLE001
        logger.warning("gallery env detail failed for %s: %s", path, exc)
        return GalleryEnvDetail(id=path, how="none", degraded=True, detail=str(exc))
