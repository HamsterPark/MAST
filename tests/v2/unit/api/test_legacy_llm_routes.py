"""User-initiated internal-LLM routes refuse up front while legacy features are off.

With ``legacy_features_enabled`` off (the default) the internal LLM is disabled
(``mast/legacy_gate.py``). A route that would use it answers **403** with a detail
that starts with ``legacy_features_disabled:`` and says how to turn it on — instead
of a "degraded" body, an empty SSE stream, or a 500 from somewhere deep. The same
routes are NOT refused while the switch is on. The CLI ``--instruction`` path exits
with code 2 and the same text; the voice websocket sends one error frame and closes
with 1008.

Run::

    $env:PYTHONPATH = "$PWD\\MASTv2"
    .venv-v2-py313\\Scripts\\python.exe -m pytest tests/v2/unit/api/test_legacy_llm_routes.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path


def _find_mastv2_root() -> str:
    p = Path(__file__).resolve()
    while p.parent != p:
        candidate = p / "MASTv2"
        if candidate.is_dir():
            return str(candidate)
        p = p.parent
    raise RuntimeError("MASTv2 dir not found")


_MASTV2_ROOT = _find_mastv2_root()
if sys.path and sys.path[0] != _MASTV2_ROOT:
    while _MASTV2_ROOT in sys.path:
        sys.path.remove(_MASTV2_ROOT)
    sys.path.insert(0, _MASTV2_ROOT)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from mast import legacy_gate  # noqa: E402
from mast.api.app import create_app  # noqa: E402
from mast.legacy_gate import REFUSAL_CODE  # noqa: E402

#: (method, path, json body) — every user-initiated route that reaches the
#: internal LLM (or, for dreaming, the legacy memory feature).
LLM_ROUTES = [
    ("post", "/api/agents/instrument_control/chat",
     {"conversation_id": "c1", "user_text": "hello"}),
    ("post", "/api/agents/run-task", {"task": "summarise the last scan"}),
    ("post", "/api/agents/run-task/background",
     {"instruction": "survey Au(111) literature", "agents": ["literature"]}),
    ("post", "/api/qa", {"question": "what is the bias?"}),
    ("post", "/api/cognition/dream", {"namespace": "global"}),
    ("post", "/api/cognition/brainstorm", {"topic": "tip shaping"}),
    ("post", "/api/cognition/brainstorm/full", {"topic": "tip shaping"}),
    ("post", "/api/cognition/brainstorm/stream", {"topic": "tip shaping"}),
    ("get", "/api/cognition/dream/full", None),
    ("post", "/api/voice/transcribe", {"audio_url": "https://example.invalid/a.wav"}),
    ("post", "/api/voice/synthesize", {"text": "hello"}),
]


def _ids(routes):
    return [f"{m.upper()} {p}" for m, p, _ in routes]


@pytest.fixture(scope="module")
def client():
    """One app for the module: building it is ~3 s and the gate is read per request."""
    return TestClient(create_app())


@pytest.fixture()
def gate_closed():
    legacy_gate.set_override_for_test(False)
    try:
        yield
    finally:
        legacy_gate.set_override_for_test(True)


def _call(client, method, path, body):
    if method == "get":
        return client.get(path)
    return client.post(path, json=body)


@pytest.mark.parametrize("method,path,body", LLM_ROUTES, ids=_ids(LLM_ROUTES))
def test_refused_with_403_while_legacy_features_are_off(client, gate_closed,
                                                         method, path, body):
    r = _call(client, method, path, body)
    assert r.status_code == 403, (r.status_code, r.text[:300])
    detail = r.json()["detail"]
    assert detail.startswith(REFUSAL_CODE), detail
    assert "legacy_features_enabled" in detail, "the refusal must say how to turn it on"


@pytest.mark.parametrize("method,path,body", LLM_ROUTES, ids=_ids(LLM_ROUTES))
def test_not_refused_by_the_gate_while_on(client, method, path, body):
    """With the switch on these routes behave as before (here: degraded bodies or
    error frames, because the test app has no runtime wired)."""
    r = _call(client, method, path, body)
    assert not (r.status_code == 403 and REFUSAL_CODE in r.text), r.text[:300]


def test_the_voice_websocket_refuses_and_closes_while_off(client, gate_closed):
    with client.websocket_connect("/ws/voice") as ws:
        frame = ws.receive_json()
        assert frame["type"] == "error" and frame["code"] == REFUSAL_CODE
        with pytest.raises(WebSocketDisconnect) as ei:
            ws.receive_json()
        assert ei.value.code == 1008


def test_a_refusal_raised_deep_inside_a_route_is_a_403_not_a_500(gate_closed):
    """The app-level handler: any LegacyFeaturesDisabled that escapes a route."""
    from fastapi import APIRouter

    app = create_app()
    extra = APIRouter()

    @extra.get("/api/_test/deep-llm")
    def _deep():
        legacy_gate.require_internal_llm("deep call")

    app.include_router(extra)
    # When frontend/dist exists, create_app registers the SPA catch-all /{full_path:path},
    # which would match first and 404 the route added here. Put the test route in front.
    added = app.router.routes.pop()
    assert getattr(added, "path", None) == "/api/_test/deep-llm"
    app.router.routes.insert(0, added)
    r = TestClient(app).get("/api/_test/deep-llm")
    assert r.status_code == 403
    assert r.json()["detail"].startswith(REFUSAL_CODE)


def test_the_cli_instruction_refuses_with_exit_code_2(gate_closed, capsys):
    from mast.pipeline.main import main

    assert main(["--instruction", "scan 50 nm"]) == 2
    err = capsys.readouterr().err
    assert REFUSAL_CODE in err
