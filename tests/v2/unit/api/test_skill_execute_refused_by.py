"""POST /api/skills/{name}/execute 的 ``refused_by``：「门口就被拒、根本没执行」与「执行了但失败」分得开。

手动面板松手补发 stop 时靠它判断（契约 §13.1「补发 stop 的前提」）。这里走的是真的
``build_context`` → ``ExecutionContext`` → ``direct_exec.run_and_record``，只把技能换成几个会记账的
小替身，所以每条都能同时断言「技能体到底跑没跑」：

* sample_gate：产数据的技能、有记录系统但没选样品 → 门口拒，技能体一次没跑；
* busy：仪器令牌被另一个线程拿着 → ExecutionContext 的仲裁拒，技能体一次没跑；
* 正常执行 → 空串；
* 执行了但失败 → 也是空串（成败看 success，原因看 error）。
"""
from __future__ import annotations

import sys
from pathlib import Path

_MASTV2_ROOT = str(Path(__file__).resolve().parents[4] / "MASTv2")
if sys.path[0] != _MASTV2_ROOT:
    while _MASTV2_ROOT in sys.path:
        sys.path.remove(_MASTV2_ROOT)
    sys.path.insert(0, _MASTV2_ROOT)

import threading  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import mast.core.instrument_lock as il  # noqa: E402
from mast.api.routes.skill_exec import router  # noqa: E402
from mast.core.registry import SkillRegistry  # noqa: E402
from mast.core.types import SafetyLevel, SkillCategory, SkillMetadata, SkillResult  # noqa: E402
from mast.logging import experiment_log as el  # noqa: E402
from mast.skills.base import BaseSkill  # noqa: E402

RAN: list[str] = []


class _Probe(BaseSkill):
    NAME = ""
    TAGS: tuple[str, ...] = ("write",)
    OK = True

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(name=self.NAME, version="1.0.0", category=SkillCategory.WRITE,
                             safety_level=SafetyLevel.AUTO, description="refused_by 测试替身",
                             parameters=[], estimated_duration_s=0.0, composition_level=0,
                             tags=list(self.TAGS))

    def execute(self, context, params: dict) -> SkillResult:
        RAN.append(self.NAME)
        return SkillResult(skill_name=self.NAME, success=self.OK, data={"ran": True},
                           error="" if self.OK else "设备回了一个错")


class ProbeScan(_Probe):          # 带产数据标签 → 要样品
    NAME = "ProbeScanForRefusal"
    TAGS = ("scan", "write")


class ProbeWrite(_Probe):         # WRITE、不在任何豁免表 → 要仪器令牌
    NAME = "ProbeWriteForRefusal"


class ProbeFail(_Probe):
    NAME = "ProbeFailForRefusal"
    OK = False


@pytest.fixture
def client():
    reg = SkillRegistry()
    for cls in (ProbeScan, ProbeWrite, ProbeFail):
        reg.register(cls)
    api = FastAPI()
    # 真的 build_context：连接池 / 状态 / 注册表三样都在；替身技能碰不到连接池。
    api.state.ctx = SimpleNamespace(connection_pool=object(), state=SimpleNamespace(snapshot=lambda: None),
                                    skill_registry=reg, live_app=None, app=None)
    api.include_router(router, prefix="/api")
    RAN.clear()
    return TestClient(api)


def _execute(client, name: str) -> dict:
    r = client.post(f"/api/skills/{name}/execute", json={"params": {}})
    assert r.status_code == 200
    return r.json()


def test_sample_gate_refusal_is_marked_and_nothing_ran(client):
    prev = el.get_active_log()
    el.set_active_log(SimpleNamespace(current_experiment_id=None, current_sample_id=None))
    try:
        b = _execute(client, ProbeScan.NAME)
    finally:
        el.set_active_log(prev)
    assert b["ok"] is True and b["success"] is False
    assert b["refused_by"] == "sample_gate" and "[sample_gate]" in b["error"]
    assert RAN == []


def test_busy_refusal_is_marked_and_nothing_ran(client, monkeypatch):
    orig = il.hold_for_skill
    # 真的令牌、真的仲裁，只是不等满 5 s。
    monkeypatch.setattr(il, "hold_for_skill",
                        lambda meta, name, owner, timeout_s=None: orig(meta, name, owner, timeout_s=0.2))
    held, release = threading.Event(), threading.Event()

    def holder() -> None:
        with il.instrument_lock().hold(owner="TERS 组合技能", skill="TERSGridMap"):
            held.set()
            release.wait(10)

    th = threading.Thread(target=holder, name="token-holder", daemon=True)
    th.start()
    assert held.wait(5)
    try:
        b = _execute(client, ProbeWrite.NAME)
    finally:
        release.set()
        th.join(5)
    assert b["ok"] is True and b["success"] is False
    assert b["refused_by"] == "busy" and b["data"].get("instrument_busy") is True
    assert RAN == []


def test_a_normal_run_has_no_refusal(client):
    b = _execute(client, ProbeWrite.NAME)
    assert b["ok"] is True and b["success"] is True and b["refused_by"] == ""
    assert RAN == [ProbeWrite.NAME]
    props = client.app.openapi()["components"]["schemas"]["SkillExecuteResponse"]["properties"]
    assert props["refused_by"]["type"] == "string" and props["refused_by"]["default"] == ""


def test_a_skill_that_ran_and_failed_has_no_refusal(client):
    b = _execute(client, ProbeFail.NAME)
    assert b["ok"] is True and b["success"] is False and b["refused_by"] == ""
    assert b["error"] == "设备回了一个错"
    assert RAN == [ProbeFail.NAME]
