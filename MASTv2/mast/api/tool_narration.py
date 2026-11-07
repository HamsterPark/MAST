"""Turn a tool call / tool return into ONE honest line of Chinese.

The group-chat panel used to show raw tool calls and returns, which was too
noisy; they are rewritten as natural language with parameters folded. What the
panel actually showed:

    show_plan_on_map(steps=[{'kind': 'scan', 'label': '+1V 50nm', 'w_m': 5.0, , )
    {"seqno": 1036, "progress": {"seqno": 1036, "scan_id": "…", "line_idx": 2, …}}

Two different defects wearing the same clothes:

  * the CALL was rendered by ``f"{name}({', '.join(f'{k}={v}')[:120]})"`` — the
    join was cut at 120 chars and the ``)`` pasted on afterwards, which is where
    the ``, )`` comes from. It also silently dropped every argument past the
    fourth. A reader cannot tell that from a call that really had two arguments;
  * the RETURN was the raw JSON the tool handed the model. That text is written
    for an LLM, not for a person watching a run.

THE RULE THIS MODULE IS BUILT ON — a summary may only say things that are
mechanically derivable from the call. It may name the tool, count and echo
arguments, and use a curated Chinese phrase for a tool we have actually read.
It may **never** guess what an unknown tool does. An unknown tool gets its own
name and an argument count, which is a true sentence about any tool that ever
exists. The failure mode this module exists to avoid is looking complete when it wasn't ("看起来完成了其实没有"); a prettier line that quietly asserts something false is worse than the raw
JSON it replaced.

The curated map covers the tools that actually flood a run (planning, lifecycle,
buffer, memory, figures, the paper loop, literature). It is NOT a second registry
of what tools exist — ``tests/v2/unit/api/test_tool_narration.py`` asserts every
key here is a REAL tool name taken from the agents' live tool lists, so a
renamed tool breaks the test instead of silently falling back forever.
"""

from __future__ import annotations

import json
from mast.i18n import tr
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

__all__ = [
    "NARRATORS",
    "narrate_tool_call",
    "narrate_tool_result",
    "summarize_args",
]

#: Serialized arguments longer than this are clipped — WITH a marker. Silent
#: truncation is the defect this module exists to remove, so it is never done
#: quietly anywhere in here.
_ARGS_JSON_MAX = 20_000
_VALUE_CHARS = 60


def _s(v: Any, limit: int = _VALUE_CHARS) -> str:
    """One short, faithful rendering of a value — never a truncated fragment
    that could pass for the whole thing."""
    if isinstance(v, str):
        text = v
    elif isinstance(v, bool):
        # JSON spelling, not Python's. This digest claims every token in it
        # appears verbatim in the payload; "False" would not.
        return "true" if v else "false"
    elif isinstance(v, (list, tuple, set)):
        return tr(f"{len(v)} 项", f"{len(v)} items")
    elif isinstance(v, dict):
        return tr(f"{len(v)} 个字段", f"{len(v)} fields")
    else:
        text = str(v)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _n(args: dict, key: str) -> int | None:
    """Length of a list-valued argument, or None when it isn't one."""
    v = args.get(key)
    return len(v) if isinstance(v, (list, tuple)) else None


def _steps(args: dict) -> str:
    n = _n(args, "steps")
    return tr(f"（{n} 步）", f" ({n} steps)") if n else ""


# ── the curated phrases ──────────────────────────────────────────────────────
#
# Each entry maps a REAL tool name to a function of its arguments. Anything that
# reads an argument must tolerate the argument being absent: an LLM omits
# optional keys constantly, and a summary that says "写入记忆 · None" is the
# kind of small lie this module refuses to tell.
NARRATORS: dict[str, Callable[[dict], str]] = {
    # ── planning ──
    "create_plan": lambda a: tr(f"创建实验计划{_steps(a)}", f"Create experiment plan{_steps(a)}")
                             + (f" · {_s(a['title'])}" if a.get("title") else ""),
    "approve_plan": lambda a: tr("批准实验计划", "Approve experiment plan"),
    "advance_plan": lambda a: tr("推进实验计划到下一步", "Advance experiment plan to the next step"),
    "pause_plan": lambda a: tr("暂停实验计划", "Pause experiment plan"),
    "resume_plan": lambda a: tr("恢复实验计划", "Resume experiment plan"),
    "list_plans": lambda a: tr("查询实验计划列表", "List experiment plans"),
    "get_plan_progress": lambda a: tr("查询实验计划进度", "Check experiment plan progress"),
    "show_plan_on_map": lambda a: tr(f"在扫描地图上预览计划路线{_steps(a)}", f"Preview the planned route on the scan map{_steps(a)}"),
    "clear_plan_on_map": lambda a: tr("清除扫描地图上的计划路线", "Clear the planned route on the scan map"),
    # ── experiment / sample lifecycle ──
    "start_experiment": lambda a: tr("开始实验", "Start experiment")
                                  + (f" · {_s(a['name'])}" if a.get("name") else ""),
    "end_experiment": lambda a: tr("结束实验", "End experiment"),
    "rename_experiment": lambda a: tr("重命名实验", "Rename experiment")
                                   + (f" → {_s(a['name'])}" if a.get("name") else ""),
    "start_sample": lambda a: tr("开始新样品", "Start new sample")
                              + (f" · {_s(a['name'])}" if a.get("name") else ""),
    "end_sample": lambda a: tr("结束当前样品", "End current sample"),
    "rename_sample": lambda a: tr("重命名样品", "Rename sample")
                               + (f" → {_s(a['name'])}" if a.get("name") else ""),
    # ── vision buffer (read-only) ──
    "read_latest_tip_status": lambda a: tr("读取最新针尖状态", "Read latest tip status"),
    "get_scan_progress": lambda a: tr("读取扫描进度", "Read scan progress"),
    "get_tip_history_since": lambda a: tr("读取针尖状态历史", "Read tip status history"),
    # ── long-term memory ──
    "memory_write": lambda a: tr("写入长期记忆", "Write long-term memory")
                              + (f" · {_s(a['path'])}" if a.get("path") else ""),
    "memory_read": lambda a: tr("读取长期记忆", "Read long-term memory")
                             + (f" · {_s(a['path'])}" if a.get("path") else ""),
    "memory_list": lambda a: tr("列出长期记忆", "List long-term memory"),
    "memory_search": lambda a: tr("检索长期记忆", "Search long-term memory")
                               + (f" · {_s(a['query'])}" if a.get("query") else ""),
    # ── scans ──
    "get_latest_scan_info": lambda a: tr("查询最近一次扫描的信息", "Look up info on the latest scan"),
    "get_latest_scan_file": lambda a: tr("查询最近一次扫描的文件", "Look up the file of the latest scan"),
    "load_scan_file": lambda a: tr("载入扫描文件", "Load scan file")
                                + (f" · {_s(a['path'])}" if a.get("path") else ""),
    "load_scan": lambda a: tr("载入扫描数据", "Load scan data")
                           + (f" · {_s(a['path'])}" if a.get("path") else ""),
    "get_next_scan_position": lambda a: tr("计算下一个扫描位置", "Compute the next scan position"),
    "mark_area_used": lambda a: tr("标记该区域已扫过", "Mark this area as scanned"),
    # ── knowledge / reference lookups ──
    "query_knowledge": lambda a: tr("查询知识库", "Query knowledge base")
                                 + (f" · {_s(a['query'])}" if a.get("query") else ""),
    "get_workflow_advice": lambda a: tr("查询工作流建议", "Look up workflow advice"),
    "get_skill_guidance": lambda a: tr("查询技能使用指导", "Look up skill usage guidance")
                                    + (f" · {_s(a['skill_name'])}"
                                       if a.get("skill_name") else ""),
    "get_literature_parameters": lambda a: tr("查询文献参数", "Look up literature parameters"),
    "get_fault_diagnosis": lambda a: tr("查询故障诊断参考", "Look up fault-diagnosis reference"),
    "get_noise_reference": lambda a: tr("查询噪声参考", "Look up noise reference"),
    "get_measurement_template": lambda a: tr("查询测量模板", "Look up measurement template"),
    "search_deep_reference": lambda a: tr("深度检索参考资料", "Deep search of reference material")
                                       + (f" · {_s(a['query'])}"
                                          if a.get("query") else ""),
    "read_reference_section": lambda a: tr("阅读参考资料章节", "Read a reference section"),
    # ── background ──
    "spawn_background_task": lambda a: tr("转入后台运行一个任务", "Move a task to run in the background"),
    # ── figures ──
    "plot_scan": lambda a: tr("绘制扫描图", "Plot scan image"),
    "plot_spectrum": lambda a: tr("绘制谱图", "Plot spectrum"),
    "mosaic_scans": lambda a: tr("拼接扫描大图", "Stitch scans into a large mosaic"),
    "embed_figure": lambda a: tr("把图插入稿件", "Insert figure into the manuscript"),
    "list_figures": lambda a: tr("列出已渲染的图", "List rendered figures"),
    # ── the paper loop ──
    "save_draft": lambda a: tr("保存论文草稿", "Save paper draft")
                            + (f" · {_s(a['title'])}" if a.get("title") else ""),
    "load_draft": lambda a: tr("读取论文草稿", "Load paper draft"),
    "draft_section": lambda a: tr("撰写稿件章节", "Draft manuscript section")
                               + (f" · {_s(a['section'])}" if a.get("section") else ""),
    "save_review": lambda a: tr("保存评审报告", "Save review report"),
    "load_review": lambda a: tr("读取评审报告", "Load review report"),
    "produce_review": lambda a: tr("生成评审报告", "Produce review report"),
    "check_citations": lambda a: tr("核对引文", "Check citations"),
    "check_methodology": lambda a: tr("核查方法学", "Check methodology"),
    "check_data_reasoning": lambda a: tr("核查数据推理", "Check data reasoning"),
    # ── literature ──
    "search_papers": lambda a: tr("检索文献", "Search literature")
                               + (f" · {_s(a['query'])}" if a.get("query") else ""),
    "search_local_corpus": lambda a: tr("检索本地文献语料", "Search local literature corpus")
                                     + (f" · {_s(a['query'])}"
                                        if a.get("query") else ""),
    "propose_citations": lambda a: tr("推荐可引用文献", "Suggest citable papers"),
    "lookup_citation": lambda a: tr("查证一条引文", "Verify a citation"),
    "lib_create": lambda a: tr("新建文献库", "Create library"),
    "lib_add": lambda a: tr("把文献加入文献库", "Add paper to library"),
    "lib_remove": lambda a: tr("从文献库移除文献", "Remove paper from library"),
    "lib_switch": lambda a: tr("切换当前文献库", "Switch current library"),
    "lib_list": lambda a: tr("列出文献库", "List libraries"),
    "lib_search": lambda a: tr("在文献库内检索", "Search within library"),
    # ── experiment records ──
    "query_experiment_records": lambda a: tr("查询实验记录", "Query experiment records"),
    "query_past_experiments": lambda a: tr("查询历史实验", "Query past experiments"),
    "lookup_sample": lambda a: tr("查询样品信息", "Look up sample info"),
}


def narrate_tool_call(name: str, args: Any) -> str:
    """One line of Chinese describing a tool CALL. Never invents behaviour.

    A tool in :data:`NARRATORS` gets its curated phrase. Everything else — and
    this system has ~490 tools, so "everything else" is the common case — gets
    ``<name> · N 个参数``: the tool's own name plus a count, which is true of any
    tool whether or not anyone has ever read it.
    """
    name = str(name or "tool")
    args = args if isinstance(args, dict) else {}
    fn = NARRATORS.get(name)
    if fn is not None:
        try:
            text = fn(args)
            if text:
                return text
        except Exception as exc:  # noqa: BLE001 — fall through to the honest form
            logger.debug("tool narration failed for %s: %s", name, exc)
    return tr(f"{name} · {len(args)} 个参数", f"{name} · {len(args)} args") if args else name


def summarize_args(args: Any) -> tuple[str, bool]:
    """``(pretty JSON, clipped)`` for the collapsed 参数 panel.

    Returns the FULL arguments — the panel is where the detail the summary
    dropped has to remain available, otherwise this whole change is just hiding
    information. Only a genuinely huge payload is clipped, and the flag says so
    out loud so the UI can label it rather than letting it read as complete.
    """
    if not isinstance(args, dict) or not args:
        return "", False
    try:
        text = json.dumps(args, ensure_ascii=False, indent=2, default=str)
    except Exception as exc:  # noqa: BLE001
        logger.debug("tool args serialisation failed: %s", exc)
        return repr(args)[:_ARGS_JSON_MAX], len(repr(args)) > _ARGS_JSON_MAX
    if len(text) <= _ARGS_JSON_MAX:
        return text, False
    return text[:_ARGS_JSON_MAX], True


def narrate_tool_result(text: str) -> str:
    """A one-line digest of a tool RETURN, or "" when the raw text is fine.

    JSON returns are what make the feed unreadable — ``{"seqno": 1036,
    "progress": {"seqno": 1036, "scan_id": …}}`` says nothing to a person at a
    glance. The digest is a MECHANICAL transcription of the top-level scalar
    fields (``seqno=1036 · line_idx=2 · advancing=false``), not an
    interpretation: every token in it appears verbatim in the payload.

    Returns "" for plain prose, for a handoff note, and for JSON with no scalar
    fields worth a line — in those cases the caller shows the text itself, which
    was already readable.
    """
    raw = (text or "").strip()
    if not raw or not raw.startswith(("{", "[")):
        return ""
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return ""
    if isinstance(obj, list):
        return tr(f"返回 {len(obj)} 项", f"Returned {len(obj)} items")
    if not isinstance(obj, dict):
        return ""
    parts: list[str] = []
    nested = 0
    for k, v in obj.items():
        if isinstance(v, (dict, list, tuple)):
            nested += 1
            continue
        if v is None:
            continue
        parts.append(f"{k}={_s(v, 40)}")
    if not parts:
        # All-nested payload (the get_scan_progress shape: everything hides one
        # level down). Say what is there rather than pretending to summarise it.
        return tr(f"返回 {len(obj)} 个字段", f"Returned {len(obj)} fields") if obj else ""
    head = " · ".join(parts[:6])
    # Fields this line does NOT show — the ones past the sixth PLUS every nested
    # object skipped above. Counting only the overflow (and letting the nested
    # ones vanish) is how "{"seqno": 1036, "progress": {…}}" would summarise to a
    # bare "seqno=1036", quietly losing the entire payload.
    rest = max(0, len(parts) - 6) + nested
    return head + (tr(f" · 另有 {rest} 项", f" · {rest} more") if rest > 0 else "")
