"""``POST /literature/search``：文献库语义检索，外部面的直通版。

检索逻辑**就是**主 API 的 ``POST /api/literature/search``（``mast/api/routes/literature.py``
的 ``search``）：这里直接调那个函数，一行检索逻辑都不复制 —— 同一个接线判据
（``ctx.library_registry`` / ``ctx.literature_wired``）、同一条 DashScope 语义检索与关键词降级、
同一个按库过滤（``library_id``）、同一个「永不 500，降级就说 degraded」。外部面只把 ``k``
收窄到 ≤ 50，并在 ``degraded`` 时用 ``note`` 说一句为什么。
"""

from __future__ import annotations

from mast.i18n import tr
import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from mast.api.ext.common import ctx_of

logger = logging.getLogger(__name__)

router = APIRouter(tags=["literature"])


class LiteratureSearchBody(BaseModel):
    query: str = Field(..., min_length=1, max_length=500, description="自然语言检索词")
    k: int = Field(8, ge=1, le=50, description="返回几条")
    material: str = Field("", max_length=120, description="材料过滤（可选），例如 Au(111)")
    library_id: str | None = Field(None, max_length=120,
                                   description="只在这个库（实验库 / 自建库）的成员里找（可选）")


def _degraded_note(wired: bool, hits: list[dict]) -> str:
    if not wired:
        return tr("文献库没有接线（这个 MAST 没有加载文献索引），没有结果", "Literature library not wired (this MAST has no literature index loaded); no results")
    if hits and all(h.get("retrieval") == "keyword" for h in hits):
        return tr("语义检索不可用（嵌入服务没响应），这些是关键词匹配的结果", "Semantic search unavailable (the embedding service did not respond); these are keyword matches")
    return tr("检索失败（详见服务端日志），没有结果或结果不完整", "Search failed (see the server log); no results or incomplete results")


@router.post("/literature/search")
def literature_search(body: LiteratureSearchBody, request: Request):
    """文献库检索。回答与 ``POST /api/literature/search`` 相同：``{hits, count, degraded}``；
    每条命中 ``{work_id, title, year, journal, doi, cited, score, source, abstract_excerpt,
    retrieval}``（``retrieval`` = semantic | keyword）。``degraded`` 时多一句 ``note``。"""
    from mast.api.routes import literature as main_literature
    from mast.api.schemas_literature import SearchRequest

    resp = main_literature.search(request, SearchRequest(
        query=body.query, k=body.k, material=body.material or "",
        library_id=body.library_id or None))
    out = resp.model_dump()
    if out.get("degraded"):
        out["note"] = _degraded_note(main_literature._is_wired(ctx_of(request)),
                                     list(out.get("hits") or []))
    return out


__all__ = ["LiteratureSearchBody", "router"]
