"""7.0.1：仪器 agent 的工具对外部 agent 开放 —— 检索、卡片、作业、入账、外部面多守的几道门，以及
不开放的那几个（把活交给内部 agent；替操作员陈述仪器旁的物理事件 —— 换针、手动粗动）。

起因：简报写着「一律调 get_map_analysis」「要登记请调 register_tip」，而外部面上没有这些
工具；因此定为「外部 agent 不分角色地可以访问所有能力，但不提供交任务给内部 agent」。
"""
from __future__ import annotations

import threading

import numpy as np
import pytest

from _ext_world import RAN, H, wait_terminal, write_sxm

from mast.api.ext import agent_tools as AT

SPAWNED: list = []


def _provider(world, plan_store=None):
    """与 ``CoreRuntime._meta_tool_context`` 同形状的 provider（含 spawn_background —— 要证明它被剥掉）。"""
    def spawn(*a, **k):
        SPAWNED.append((a, k))
        return {"ok": True, "run_id": "r_should_never_happen", "agents": ["literature"]}

    return {"experiment_log": world.log, "plan_store": plan_store, "storage": world.st,
            "map_analysis_cfg": None, "coarse_map_cfg": None,
            "experiments_dir": str(world.tmp), "session_path": None, "session_dir": None,
            "spawn_background": spawn}


@pytest.fixture()
def tw(world):
    from mast.planning.plan_store import PlanStore

    SPAWNED.clear()
    ps = PlanStore(world.tmp / "plans.db")
    world.plans = ps
    world.rt._meta_tool_context = lambda: _provider(world, ps)
    return world


def _run(c, skill, params=None, *, session="s1"):
    r = c.post("/jobs", json={"skill": skill, "params": params or {}}, headers=H(session=session))
    assert r.status_code in (200, 202), r.text
    return wait_terminal(c, r.json()["job_id"])


# ── 目录本身 ────────────────────────────────────────────────────────────────

def test_every_built_tool_is_either_offered_or_excluded():
    """新加一个 meta tool 而没决定开不开放 ⇒ 红（清单说有、实物没有，或者反过来，都红）。"""
    built = AT.build(None, "ext:test")
    assert set(built) == set(AT.OFFERED) | set(AT.EXCLUDED)
    assert not set(AT.OFFERED) & set(AT.EXCLUDED)
    assert all(AT.OFFERED[n][0] == built[n][0] for n in AT.OFFERED)
    assert set(AT.describe()) == set(AT.OFFERED)


def test_nothing_that_hands_work_to_internal_agents_is_offered():
    assert {"spawn_background_task", "request_activation", "conduct_create",
            "conduct_approve"} <= set(AT.EXCLUDED)
    assert all(op not in AT.CONDUCT_OPS_OFFERED for op in ("start", "resume"))


def test_spawn_background_is_unreachable_even_if_the_tool_is_called_directly(tw):
    tool = AT.build(tw.rt, "ext:tester")["spawn_background_task"][1]
    out = tool.invoke({"args": {"instruction": "survey Au(111)"}, "name": "spawn_background_task",
                       "type": "tool_call", "id": "t1"})
    assert "不可用" in AT._text_of(out) and SPAWNED == []


# ── 检索与卡片 ──────────────────────────────────────────────────────────────

def test_search_lists_instrument_agent_tools_with_their_origin(tw):
    r = tw.client.get("/skills/search", params={"q": "map analysis"}, headers=H())
    hits = {h["name"]: h for h in r.json()["results"]}
    hit = hits.get("get_map_analysis")
    assert hit is not None, list(hits)
    assert hit["origin"] == AT.ORIGIN_LABEL and hit["origin_code"] == AT.ORIGIN_CODE
    assert hit["official"] is False and hit["footprint"] == AT.READ and "name" in hit["matched_on"]
    spawn = tw.client.get("/skills/search", params={"q": "spawn_background_task"}, headers=H()).json()
    assert "spawn_background_task" not in {h["name"] for h in spawn["results"]}


def test_the_card_of_an_agent_tool_has_the_skill_card_shape(tw):
    card = tw.client.get("/skills/mark_area_used", headers=H()).json()
    assert card["origin"] == AT.ORIGIN_LABEL and card["family"] == "meta"
    assert card["takes_instrument_token"] is False and card["requires_sample"] is False
    assert card["footprint"] == AT.WRITE and card["verbs"] == ["Util_SessionPathGet"]
    assert [p["name"] for p in card["parameters"]] == ["x_m", "y_m", "forbidden", "radius_m"]
    assert card["si_params"] == {"x_m": "prefix_optional", "y_m": "prefix_optional",
                                 "radius_m": "prefix_optional"}
    assert "duration" in card and card["tool_face"]["disabled"] is False
    doc = tw.client.get("/skills/list_documents", headers=H()).json()
    assert doc["family"] == "document" and doc["verbs"] == []


@pytest.mark.parametrize("name", ["spawn_background_task", "request_activation", "conduct_create",
                                  "conduct_approve"])
def test_tools_that_hand_work_to_internal_agents_are_refused_with_the_reason(tw, name):
    card = tw.client.get(f"/skills/{name}", headers=H())
    assert card.status_code == 403 and card.json()["error"] == "not_offered"
    assert "交" in card.json()["detail"]
    job = tw.client.post("/jobs", json={"skill": name, "params": {}}, headers=H())
    assert job.status_code == 403 and job.json()["error"] == "not_offered"


# ── 作为作业执行 ────────────────────────────────────────────────────────────

def test_list_experiments_runs_as_a_tool_job_and_is_recorded(tw):
    v = _run(tw.client, "list_experiments")
    assert v["state"] == "succeeded", v
    assert v["kind"] == "tool" and v["result"]["nanonis_calls"] == 0
    names = [e["name"] for e in v["result"]["data"]["json"]["experiments"]]
    assert "外部网关测试" in names
    assert v["recorded"]["v1"] is True and v["recorded"]["v2_action_id"]
    row = tw.st.recent_actions(tw.eid)[0]
    assert row["skill_name"] == "list_experiments" and row["context"] == "ext:tester/s1"
    assert RAN == [] and tw.pool.calls == []        # 没有技能跑、没有 Nanonis 命令


def test_a_scope_changing_tool_is_refused_while_the_instrument_is_busy(tw):
    from mast.core.instrument_lock import instrument_lock

    held, release = threading.Event(), threading.Event()

    def _holder():
        with instrument_lock().hold(owner="别的入口", skill="LongScan"):
            held.set()
            release.wait(30)

    t = threading.Thread(target=_holder, daemon=True)
    t.start()
    assert held.wait(5)
    try:
        v = _run(tw.client, "start_experiment", {"name": "被占用时不许切"})
    finally:
        release.set()
        t.join(10)
    assert v["state"] == "failed" and v["refused_by"] == "instrument_busy"
    assert "mast_scope(force=true)" in v["result"]["error"]
    assert tw.log.current_experiment_id == tw.eid      # 作用域没动
    ok = _run(tw.client, "start_experiment", {"name": "空闲时可以切"})
    assert ok["state"] == "succeeded" and tw.log.current_experiment_id != tw.eid


@pytest.mark.parametrize("name", sorted(AT.OPERATOR_ONLY))
def test_physical_events_at_the_instrument_are_the_operators_to_record(tw, name):
    """补记手动粗动会清空同一点撞针两次之后的封锁表；登记 / 注销针尖会清掉 qPlus
    撞针判据的振幅基线，修针的缺省剂量也跟着登记走。远端的 agent 看不见这些事有没有发生 —— 卡片与提交
    都 403，理由指向操作员。"""
    assert name in AT.EXCLUDED and name not in AT.OFFERED
    card = tw.client.get(f"/skills/{name}", headers=H())
    assert card.status_code == 403 and card.json()["error"] == "not_offered"
    assert "操作员" in card.json()["detail"] and "mast_ask_operator" in card.json()["detail"]
    job = tw.client.post("/jobs", json={"skill": name, "params": {}}, headers=H())
    assert job.status_code == 403 and job.json()["error"] == "not_offered"


def test_a_refused_coarse_move_backfill_leaves_the_crash_block_in_place(tw):
    """被拒的那一次什么都没做：同一点撞两次之后的封锁还在（补记粗动会把它清空）。"""
    from mast.core.tip_crash_tracker import get_tip_crash_tracker, reset_tip_crash_tracker

    reset_tip_crash_tracker()
    tracker = get_tip_crash_tracker()
    try:
        for _ in range(2):
            tracker.record_crash(1e-9, 2e-9)
        assert tracker.is_blocked(1e-9, 2e-9), "test is vacuous — two crashes must block the spot"
        r = tw.client.post("/jobs", json={"skill": "record_coarse_move", "params": {}}, headers=H())
        assert r.status_code == 403
        assert tracker.is_blocked(1e-9, 2e-9)
    finally:
        reset_tip_crash_tracker()


def test_the_tip_the_operator_registered_is_readable(tw):
    from mast.core import tip_state
    from mast.logging import tip_registry as tr

    try:
        assert tr.register_tip(tw.st, material="W", fabrication="etched", form="stm_wire",
                               name="W-操作员登记")["ok"]
        cur = _run(tw.client, "get_current_tip")
        tip = cur["result"]["data"]["json"]
        assert tip["registered"] is True and tip["tip"]["name"] == "W-操作员登记"
    finally:
        # 登记会刷新进程级的当前针尖 holder；不复位，这根钨针会漏给同一进程里后面的测试
        # （否则 test_tipshape_refuses_hardcoded_3v… 会因此从方案表拿到 4.0 V）。
        tip_state.set_current_tip(None)


def test_plans_are_created_approved_and_advanced_by_the_external_agent(tw):
    v = _run(tw.client, "create_plan", {"name": "三张图", "goal": "看台阶",
                                        "phases": [{"name": "survey"}, {"name": "zoom"}]})
    assert v["state"] == "succeeded", v
    plan_id = v["result"]["data"]["json"]["plan_id"]
    assert _run(tw.client, "approve_plan", {"plan_id": plan_id})["state"] == "succeeded"
    prog = _run(tw.client, "get_plan_progress", {"plan_id": plan_id})["result"]["data"]["json"]
    assert prog["status"] == "approved" and prog["total_phases"] == 2


def test_the_conduct_post_op_card_says_that_abort_retracts(tw):
    """卡片原来写着「不发控制器命令」，而 abort 会让指挥线程执行 SafeRetract。"""
    card = tw.client.get("/skills/conduct_post_op", headers=H()).json()
    assert card["sub_skills"] == ["SafeRetract"] and "SafeRetract" in card["runs_as"]
    assert "SafeRetract" in card["description"]
    assert any("SafeRetract" in r for r in card["footprint_reasons"])
    plain = tw.client.get("/skills/conduct_status", headers=H()).json()
    assert plain["sub_skills"] == [] and "never drives the instrument" in plain["runs_as"]


def test_conduct_compile_from_plan_reaches_the_compiler(tw):
    """此前 conduct 工具裸调编译器、不给 plan 库，编译类工具对每个 agent 都必然失败
    （「需要显式 plan_store」）。现在经 runtime 的 _conduct_tool_context 拿到活的 plan 库。"""
    tw.rt._conduct_tool_context = lambda: {"plan_store": tw.plans, "registry": None}
    v = _run(tw.client, "create_plan", {"name": "编译一下", "goal": "看台阶", "phases": [{"name": "survey"}]})
    plan_id = v["result"]["data"]["json"]["plan_id"]
    assert _run(tw.client, "approve_plan", {"plan_id": plan_id})["state"] == "succeeded"
    out = _run(tw.client, "conduct_compile_from_plan", {"plan_id": plan_id})
    body = out["result"]["data"]["json"]
    assert "plan_store" not in str(body.get("reason", "")), body
    # 编译器真的读了这份方案：它没有 conduct 绑定块，结论是「编不动」，而不是「没能编」
    assert body["ok"] is False and "TEMPLATE_UNRESOLVED" in {e["code"] for e in body["errors"]}, body


def test_without_a_plan_store_the_compile_tool_says_it_is_not_wired(tw):
    tw.rt._conduct_tool_context = lambda: {}
    out = _run(tw.client, "conduct_compile_from_plan", {"plan_id": "p_nope"})
    assert out["state"] == "failed" and "plan 库没接上" in out["result"]["data"]["json"]["reason"]


def test_conduct_post_op_only_takes_the_stop_and_takeover_ops(tw):
    start = _run(tw.client, "conduct_post_op", {"conduct_id": "c1", "op": "start"})
    assert start["state"] == "failed" and start["refused_by"] == "not_offered"
    pause = _run(tw.client, "conduct_post_op", {"conduct_id": "c1", "op": "pause"})
    # 过了门：指挥线程在测试里没起，工具自己如实说「没在跑」
    assert pause["refused_by"] is None and pause["state"] == "failed"
    assert "指挥线程" in pause["result"]["error"]


def test_load_scan_file_reads_only_inside_the_data_roots(tw, documents_root):
    z = np.zeros((8, 8), dtype=np.float32)
    outside = tw.tmp / "elsewhere"
    outside.mkdir()
    bad = write_sxm(outside / "x.sxm", z, z)
    v = _run(tw.client, "load_scan_file", {"path": str(bad)})
    assert v["state"] == "failed" and v["refused_by"] == "path_not_allowed"
    inside = write_sxm(documents_root / "inside.sxm", z, z)
    ok = _run(tw.client, "load_scan_file", {"path": str(inside)})
    assert ok["state"] == "succeeded", ok
    assert "Z" in ok["result"]["data"]["json"]["channels"]


def test_invalid_params_are_refused_by_the_tools_own_schema(tw):
    v = _run(tw.client, "list_experiments", {"limit": "many"})
    assert v["state"] == "failed" and v["refused_by"] == "invalid_params"


def test_an_agent_tool_through_vista_act_runs_as_a_plain_job_without_a_token(tw):
    c = tw.client
    assert c.post("/vista/hello", json={}, headers=H()).status_code == 200    # 会话已接入一动一观
    r = c.post("/vista/act", json={"skill": "list_experiments", "skill_params": {}, "wait_s": 10},
               headers=H())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "job" and body["job"]["kind"] == "tool"
    # /jobs 对已接入的会话也照常放行（它不驱动仪器）
    j = c.post("/jobs", json={"skill": "list_plans", "params": {}}, headers=H())
    assert j.status_code == 202, j.text


# ── 简报的改写、成败判据 ─────────────────────────────────────────────────────

def test_the_briefing_points_external_agents_at_mast_run():
    from mast.api.ext.overview import _ext_tool_calls

    text = ("往哪走：调 `get_coarse_map`；换区用 `RelocateCoarseXY`。要登记请调 `register_tip`。"
            "`spawn_background_task` 不在外部面上。")
    out = _ext_tool_calls(text)
    assert '`mast_run("get_coarse_map")`' in out
    assert "`RelocateCoarseXY`" in out                  # 技能名原样（本来就是 mast_run 的写法）
    assert "`spawn_background_task`" in out and "spawn_background_task`（" not in out   # 不开放的不改写
    # 操作员才能做的：不改写成 mast_run，原名后面加注「请操作员做」
    assert '`mast_run("register_tip")`' not in out
    assert "`register_tip`（外部面不开放" in out and "`mast_ask_operator`" in out


def test_the_tip_section_of_the_briefing_sends_registration_to_the_operator():
    """真文字（未登记针尖时的那一段）走一遍：外部 agent 读到的不是「请调 register_tip」。"""
    from mast.api.ext.overview import _ext_tool_calls
    from mast.core.tip_state import format_tip_block

    out = _ext_tool_calls(format_tip_block({}, {}))
    assert "register_tip" in format_tip_block({}, {}), "test is vacuous — the source no longer names it"
    assert '`mast_run("register_tip")`' not in out and "请操作员" in out


@pytest.mark.parametrize("text, ok", [
    ('{"success": false, "error": "无匹配"}', False),
    ('{"ok": false, "reason": "conduct 库打不开"}', False),
    ('{"success": true, "experiments": []}', True),
    ("[]", True),
    ("实验记录不可用", False),
    ("load_document failed: KeyError: x", False),
    ("list_documents: 读不到文档清单（OSError: x）。", False),
    ("list_documents: 本实验目前没有文档。（这是确认过的空，不是查询失败。）", True),
    ("提交推荐失败：重复", False),
    ("本实验现有资料:\n  ⚠️ 草稿 —— 读不到(x)", True),
])
def test_the_verdict_reads_the_tools_own_words(text, ok):
    assert AT._verdict(text)[0] is ok
