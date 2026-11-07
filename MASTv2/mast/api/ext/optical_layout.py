"""光路图（optical layout）—— 外部 agent 的只读面，外加评论。

用户在网页里画自己的光路图（器件的位置、方向、型号、是否电动、接到哪里……）。这里让 agent 读到
同一张图：图本身、二维追迹的结果、一份沿光束的文字叙述（``summary``，中英文按请求语言）、链接
指向的设备 / 手动光学状态槽位的当下状态，以及一张 PNG。agent 可以**发评论**（质疑、提醒、建议）。

**agent 不能改几何和注释，也不能解决评论**：光路图描述的是实物，台面上摆的是什么只有操作员知道
（同一原则见 ``api/ext/agent_tools.py`` 的 OPERATOR_ONLY、``api/routes/operator.py``）。
所以这个模块里没有任何写图的路由——写图只在网页端点（``api/routes/optical_layout.py``）；评论的
作者永远是调用方自己（``ext:<名字>``，取自 ``X-MAST-Actor``，不收请求体里的署名），评论的
``author`` 是 ``agent``，操作员在网页里看到它并由操作员解决。
``tests/v2/unit/optical_layout/test_layout_ext_readonly.py`` 钉着这件事。

* 读不会打开任何设备（``links_state`` 只读配置、已打开的设备和手动光学状态的快照）。
* 每个 agent 未解决的评论 ≤ 50，一张图的评论总数 ≤ 1000（满了先淘汰最老的已解决评论；全是未解决
  的才拒绝）。
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Query, Request, Response

from mast.api.body_limit import sized_route_class
from mast.api.ext.common import ExtError, caller_of
from mast.api.ext.schemas import LayoutCommentBody
from mast.i18n import tr
from mast.optical_layout import store as layout_store
from mast.optical_layout.links import links_state
from mast.optical_layout.summary import summarize
from mast.optical_layout.trace import trace_layout

logger = logging.getLogger(__name__)

#: unresolved comments one agent may have on one layout
MAX_OPEN_COMMENTS_PER_AGENT = 50
#: a comment is at most 2000 characters; whatever is above this (in bytes) is refused (413) without being read
MAX_COMMENT_REQUEST_BYTES = 64_000


def _refuse_body(status: int, code: str, detail: str) -> ExtError:
    return ExtError(status, code, detail)


# the only write of this module is the comment: its body must declare its length (411 when it does not,
# e.g. chunked) and not be too long (413), decided before the body is read (``mast/api/body_limit.py``)
router = APIRouter(tags=["optical-layout"], route_class=sized_route_class(MAX_COMMENT_REQUEST_BYTES, _refuse_body))


def _err(exc: layout_store.StoreError) -> ExtError:
    if isinstance(exc, layout_store.NotFound):
        return ExtError(404, "unknown_layout", str(exc))
    if isinstance(exc, layout_store.Invalid):
        return ExtError(422, "invalid_comment", str(exc))
    if isinstance(exc, layout_store.LimitReached):
        return ExtError(429, exc.code, str(exc), limit=exc.limit)
    if isinstance(exc, layout_store.Corrupt):
        return ExtError(500, "layout_unreadable", str(exc))
    if isinstance(exc, layout_store.SaveFailed):
        logger.error("ext optical layout: could not save: %s", exc.cause, exc_info=exc.cause)
        return ExtError(503, "save_failed", str(exc))
    if isinstance(exc, layout_store.Conflict):
        return ExtError(409, "conflict", str(exc))
    return ExtError(500, "layout_error", str(exc))


@router.get("/optical-layouts")
def list_layouts():
    """用户画过的光路图：``{layouts: [{id, title, revision, updated_at, updated_by, components,
    open_comments}]}``（最近保存的在前；读不了的文件多一个 ``error``）。没有图就是空列表。"""
    return {"layouts": layout_store.get_store().list_layouts()}


@router.get("/optical-layouts/{layout_id}")
def read_layout(layout_id: str):
    """一张光路图：``layout``（几何、器件、注释、链接，单位 mm / 度，原点在平台左下角，x 向右 y 向上，
    角度从 +x 逆时针量）、``trace``（每个光源每个波长的折线、命中点、偏离中心的距离 ``offset_mm``、
    入射角、累计路径长）、``summary``（沿光束顺序的文字叙述与问题清单，**先读它**）、``links_state``
    （器件链接的设备 / 槽位当下的状态）、``comments``（未解决的评论）。只读：你改不了这张图。"""
    store = layout_store.get_store()
    try:
        lay = store.load(layout_id)
        comments = [layout_store.public_comment(c) for c in store.comments(layout_id, open_only=True)]
    except layout_store.StoreError as exc:
        raise _err(exc) from None
    trace = trace_layout(lay)
    links = links_state(lay)
    return {"layout": lay.to_dict(), "trace": trace,
            "summary": summarize(lay, trace, links=links, comments=comments),
            "links_state": links, "comments": comments}


@router.get("/optical-layouts/{layout_id}/render.png", response_class=Response,
            responses={200: {"content": {"image/png": {}}, "description": "PNG"}})
def render_layout(layout_id: str, width: int = Query(1200, ge=200, le=3000, description="宽度（像素）"),
                  fit: Literal["table", "content"] = Query(
                      "content", description="table = 整个平台；content = 缩放到器件与光束所在处（坐标轴仍是平台坐标）")):
    """这张图的 PNG：器件是简单图形，光束按波长着色（红外 / 紫外虚线，粗细随强度），命中偏离中心超过
    口径 20% 的地方画黄圈，打在背面或险些没中的地方画红叉。"""
    from mast.optical_layout.render import render_png

    try:
        lay = layout_store.get_store().load(layout_id)
    except layout_store.StoreError as exc:
        raise _err(exc) from None
    try:
        png = render_png(lay, width_px=width, fit=fit)
    except Exception as exc:  # noqa: BLE001
        logger.exception("ext optical layout: render of %s failed", layout_id)
        raise ExtError(500, "render_failed", tr(f"画图失败：{type(exc).__name__}: {exc}",
                                                f"Rendering failed: {type(exc).__name__}: {exc}")) from None
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.post("/optical-layouts/{layout_id}/comments")
def post_comment(layout_id: str, body: LayoutCommentBody, request: Request):
    """在这张图上发一条评论（可以挂在某个器件上：``component_id``）。作者记成你的 agent id
    （``ext:<X-MAST-Actor>``）。操作员在网页里看到并由他解决；**你不能解决评论，也不能改图**。
    每个 agent 在一张图上没解决的评论 ≤ 50（429 ``too_many_open_comments``），一张图的评论总数
    ≤ 1000（429 ``layout_comments_full``）；``component_id`` 不在图里是 422 ``invalid_comment``。"""
    caller = caller_of(request)
    try:
        c = layout_store.get_store().add_comment(
            layout_id, text=body.text, by=caller.agent_id, author="agent", component_id=body.component_id,
            client={"via": "ext", "actor": caller.actor, "session": caller.session},
            max_open_for_author=MAX_OPEN_COMMENTS_PER_AGENT)
    except layout_store.StoreError as exc:
        raise _err(exc) from None
    logger.info("ext optical layout: comment %s on %s by %s", c["id"], layout_id, caller.agent_id)
    return {"ok": True, "comment": layout_store.public_comment(c)}


__all__ = ["MAX_COMMENT_REQUEST_BYTES", "MAX_OPEN_COMMENTS_PER_AGENT", "router"]
