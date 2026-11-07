"""Operator endpoints: answer structured prompts, register the manual optical state.

* ``GET  /api/operator/prompts?status=open|all&limit=``  — prompts with their answers, plus ``seq``
* ``POST /api/operator/prompts/{id}/answer``             — answer one (``by``, ``choice`` / ``value``, ``note``)
* ``POST /api/operator/prompts/{id}/dismiss``            — close one without answering
* ``GET  /api/operator/optical-state``                   — slot definitions + current values
* ``PUT  /api/operator/optical-state/{slot_id}``         — register what is in the beam path
* ``POST /api/operator/optical-state/{slot_id}/clear``   — forget a slot's value
* ``GET  /api/operator/optical-state/history?limit=``    — every write, oldest first

These are for **people** (the web UI and the launcher popup) and are not part of
``/api/ext/v1`` nor of the MCP tools: an external agent may post prompts, read them and wait for
them (``api/ext/collab.py``), but whether a laser area is safe, which λ/2 is installed or whether
the operator confirmed a step is the operator's word alone
(``api/ext/agent_tools.py`` OPERATOR_ONLY is the precedent).
``tests/v2/unit/operator_hitl/test_operator_only.py`` keeps it that way.

Write requests (three layers, outermost first — a write endpoint with an
optional body can be pressed by a ``no-cors`` empty POST):

1. the main app's ``AccessGate``: LAN credentials, and browser writes must be same-origin;
2. :func:`write_refusal`, here: ``Content-Type: application/json`` (415 — not relying on a
   FastAPI version's ``strict_content_type``); for a browser (``Origin`` / ``Sec-Fetch-*``
   present) ``Sec-Fetch-Site`` same-origin, ``Origin`` equal to the service (the same judgement
   as the gate, dev origins included), and a ``Host`` that is a loopback name or an IP literal
   — a foreign host name pointing at this machine is the shape of DNS rebinding — unless LAN
   credentials are on (a rebinding page has none);
3. the body is mandatory (no handler takes an optional body) and unknown fields are refused.

Programmatic clients (the launcher's urllib, scripts) send none of the browser headers and
only need the JSON content type and a body.

Provenance - who really sent a write. These checks tell a browser from a program; they cannot
tell the operator from another process on the same machine that can reach the port (a shell the
operator gave an AI, a script): on a loopback service without LAN credentials nothing technical
can. So every write records, next to the operator's name ``by`` and the client's own claim
``source`` ("web" / "launcher": **self-reported**, worth nothing as evidence), a ``client``
record made *by the server from the request itself* (:func:`client_record`): peer address,
User-Agent, whether a web page sent it (Origin / Sec-Fetch-*), the Host, and which credential
came with it (never the credential). It is stored with the answer / slot value and in the
history, returned in the responses, and logged (INFO, one line per write). A ``client`` field in
a request body is refused like any unknown field. An audit goes by ``client``, not ``source``.
It is a record, not a lock.

The ``peer`` is the address of the TCP connection and nothing else. A process on this machine
can send any ``X-Forwarded-For`` / ``Forwarded`` it likes, so those headers never replace it:
they are recorded next to it as what the client *claims* (``claimed_forwarding``). That only
holds if the ASGI server does not rewrite the connection address from them: uvicorn does by
default for a loopback client, so MAST's servers are started with ``proxy_headers=False``
(``mast2_launcher.py``, ``mast/api/__main__.py``; ``tests/v2/unit/operator_hitl`` reads both),
and a rewritten address (port 0) is never recorded as the peer anyway.
"""

from __future__ import annotations

import contextlib
import functools
import ipaddress
import json
import logging
import re
from typing import Any, Iterator, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from mast.api.access import AccessGate
from mast.api.schemas_operator import (
    AnswerBody,
    AnswerOut,
    ClearSlotBody,
    ClearSlotResponse,
    DismissBody,
    OpticalHistoryResponse,
    OpticalStateResponse,
    PromptsResponse,
    SetSlotBody,
    SlotValueOut,
)
from mast.i18n import tr
from mast.operator_hitl import optical_state, prompts, service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["operator"])

_LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})


# ── write guard ────────────────────────────────────────────────────────────────


def _host_part(netloc: str) -> str:
    """``[::1]:7862`` → ``::1``; ``127.0.0.1:7862`` → ``127.0.0.1``; ``rig`` → ``rig``."""
    n = (netloc or "").strip().lower()
    if n.startswith("["):
        return n[1:n.find("]")] if "]" in n else n
    return n.rsplit(":", 1)[0] if n.count(":") == 1 else n


def _is_ip_or_loopback(host: str) -> bool:
    if host in _LOOPBACK_NAMES:
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _gate_settings(request: Request) -> tuple[bool, tuple[str, ...]]:
    """``(LAN credentials are on, dev origins)`` of the app's ``AccessGate``; ``(False, ())``
    when the app has none (a bare app in a test)."""
    for m in getattr(request.app, "user_middleware", None) or ():
        if getattr(m, "cls", None) is AccessGate:
            kw = getattr(m, "kwargs", None) or {}
            return kw.get("credentials") is not None, tuple(kw.get("dev_origins") or ())
    return False, ()


@functools.lru_cache(maxsize=8)
def _origin_judge(dev_origins: tuple[str, ...]) -> AccessGate:
    # only ``origin_allowed`` is used: the very judgement the gate applies to browser writes
    return AccessGate(None, credentials=None, dev_origins=dev_origins)  # type: ignore[arg-type]


def write_refusal(request: Request) -> tuple[int, str] | None:
    """``(status, reason)`` if this write must be refused, else ``None``."""
    h = request.headers
    ctype = (h.get("content-type") or "").split(";", 1)[0].strip().lower()
    if not (ctype == "application/json" or (ctype.startswith("application/") and ctype.endswith("+json"))):
        return 415, tr("写请求必须带 Content-Type: application/json 与一个 JSON 请求体",
                       "Write requests must carry Content-Type: application/json and a JSON body")
    if not ("origin" in h or any(k.lower().startswith("sec-fetch-") for k in h.keys())):
        return None                     # a programmatic client (launcher, script)
    lan_auth, dev_origins = _gate_settings(request)
    site = (h.get("sec-fetch-site") or "").strip().lower()
    if site and site not in ("same-origin", "none"):
        return 403, tr(f"跨站写请求被拒（Sec-Fetch-Site: {site[:40]}）",
                       f"Cross-site write refused (Sec-Fetch-Site: {site[:40]})")
    origin = h.get("origin")
    if origin is not None and not _origin_judge(dev_origins).origin_allowed(origin, h, request.scope):
        return 403, tr(f"跨站写请求被拒（Origin: {origin[:120]}）",
                       f"Cross-site write refused (Origin: {origin[:120]})")
    if not lan_auth and not _is_ip_or_loopback(_host_part(h.get("host") or "")):
        return 403, tr("浏览器经主机名写入被拒（DNS 重绑定的形状）；请用 IP 地址或 127.0.0.1 打开 MAST",
                       "A browser write by host name was refused (the shape of DNS rebinding); "
                       "open MAST by IP address or 127.0.0.1")
    return None


def write_guard(request: Request) -> None:
    """Dependency of every write route: runs before the body is validated, so a forged request
    learns nothing about the body's shape."""
    refusal = write_refusal(request)
    if refusal is not None:
        status, detail = refusal
        logger.warning("operator API: refused %s %s (HTTP %s) peer=%r origin=%r host=%r "
                       "sec-fetch-site=%r", request.method, request.url.path, status,
                       request.client.host if request.client else None,
                       request.headers.get("origin"), request.headers.get("host"),
                       request.headers.get("sec-fetch-site"))
        raise HTTPException(status_code=status, detail=detail)


# -- provenance ---------------------------------------------------------------

_BASIC = re.compile(r"basic(\s|$)", re.IGNORECASE)


def _clean(value: Any, limit: int) -> str | None:
    """A header value for the record: printable characters only (no control characters to forge
    a log line with), cut to *limit*; ``None`` when the header was absent."""
    if value is None:
        return None
    return "".join(ch for ch in str(value) if ch.isprintable()).strip()[:limit]


#: headers a client (or a proxy) may send to say where a request "really" came from. Recorded
#: as claims, never believed
_FORWARDING_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-forwarded-port",
                       "x-real-ip", "forwarded", "via")


def _tcp_peer(request: Request) -> str | None:
    """The address of the TCP connection, as the socket reports it - never a header.

    ``None`` when there is no usable one: no client at all, or an address that was not the
    socket's. uvicorn's proxy-headers layer replaces ``scope["client"]`` by an ``X-Forwarded-For``
    entry and gives it port 0; a real connection never has port 0, so port 0 means "this came
    from a header" and is not recorded as the peer (the header itself is, as a claim)."""
    client = request.client
    if client is None or not client.port:
        return None
    return _clean(client.host, 80)


def _claimed_forwarding(request: Request) -> dict[str, str] | None:
    """The forwarding headers the request carried, verbatim (printable characters, each cut to
    200), or ``None``. What the client says, recorded beside the peer and never instead of it."""
    claimed: dict[str, str] = {}
    for name in _FORWARDING_HEADERS:
        values = request.headers.getlist(name)
        if values:
            claimed[name] = _clean(", ".join(values), 200) or ""
    return claimed or None


def _has_session_cookie(request: Request) -> bool:
    """Whether the request carries the access gate's session cookie (``mast_session`` or
    ``mast_session_<port>``). Only cookie *names* are looked at."""
    for raw in request.headers.getlist("cookie"):
        for part in raw.split(";"):
            name = part.split("=", 1)[0].strip()
            if name == "mast_session" or name.startswith("mast_session_"):
                return True
    return False


def client_record(request: Request) -> dict[str, Any]:
    """What the server saw of *request*, for the audit trail of a write. Made here from the
    request itself - nothing in it comes from the body, and a client cannot change it (apart
    from the headers it sends, which is why ``auth_enforced`` and ``peer`` are there).

    ``peer`` the address of the TCP connection (:func:`_tcp_peer`; never read from a header,
    ``None`` when the server has none it can trust); ``claimed_forwarding`` the forwarding
    headers the client sent (X-Forwarded-For / -Host / -Proto / -Port, X-Real-IP, Forwarded,
    Via), verbatim and cut to 200 characters each, or ``None`` - a claim, not evidence;
    ``user_agent`` (at most 200 chars), ``origin`` / ``host``
    (at most 120): header values, control characters removed; ``browser`` an Origin or any
    Sec-Fetch-* header was present, i.e. a web page sent it; ``sec_fetch_site`` /
    ``sec_fetch_mode``; ``referer`` only whether the header was there; ``auth`` ``basic`` (an
    Authorization header starting with Basic), ``session_cookie`` (the gate's cookie) or
    ``none``; ``auth_enforced`` whether the service requires LAN credentials at all (only then
    was ``auth`` checked by the access gate: on a loopback service anybody can send "Basic").
    No credential, cookie value or header other than these is recorded."""
    h = request.headers
    if _BASIC.match((h.get("authorization") or "").lstrip()):
        auth = "basic"
    elif _has_session_cookie(request):
        auth = "session_cookie"
    else:
        auth = "none"
    return {
        "peer": _tcp_peer(request),
        "user_agent": _clean(h.get("user-agent"), 200),
        "browser": "origin" in h or any(k.lower().startswith("sec-fetch-") for k in h.keys()),
        "origin": _clean(h.get("origin"), 120),
        "sec_fetch_site": _clean(h.get("sec-fetch-site"), 40),
        "sec_fetch_mode": _clean(h.get("sec-fetch-mode"), 40),
        "referer": "referer" in h,
        "auth": auth,
        "auth_enforced": _gate_settings(request)[0],
        "host": _clean(h.get("host"), 120),
        "claimed_forwarding": _claimed_forwarding(request),
    }


@contextlib.contextmanager
def _audited(action: str, target: str, by: str, client: dict[str, Any]) -> Iterator[None]:
    """One log line per write that reaches a handler - action, prompt or slot id, the operator's
    name, the outcome and the ``client`` record. INFO when it was done (``ok``) or refused for
    what was asked (``refused_4xx``: an attempt that did not work is as much part of the trail as
    one that did); WARNING when MAST could not do it (``failed_5xx``, ``error``). Values are
    JSON-encoded: a header or a name cannot start a fake log line."""
    outcome = "error"
    try:
        yield
        outcome = "ok"
    except HTTPException as exc:
        outcome = f"refused_{exc.status_code}" if exc.status_code < 500 else f"failed_{exc.status_code}"
        raise
    finally:
        level = logging.INFO if outcome == "ok" or outcome.startswith("refused_") else logging.WARNING
        logger.log(level, "operator write: action=%s target=%s by=%s outcome=%s client=%s", action,
                   json.dumps(target, ensure_ascii=False), json.dumps(by, ensure_ascii=False),
                   outcome, json.dumps(client, ensure_ascii=False, sort_keys=True))


def _save_failed(exc: OSError) -> HTTPException:
    logger.error("operator API: could not save: %s", exc, exc_info=True)
    return HTTPException(
        status_code=503,
        detail=tr(f"没能把这次操作存盘（{type(exc).__name__}: {exc}）。操作没有生效，请检查数据目录后重试。",
                  f"Could not save this (the {type(exc).__name__}: {exc}). Nothing was changed; "
                  "check the data folder and try again."))


# ── prompts ────────────────────────────────────────────────────────────────────


@router.get("/operator/prompts", response_model=PromptsResponse)
def list_prompts(status: Literal["open", "all"] = Query("open", description="open | all"),
                 limit: int = Query(100, ge=1, le=500)) -> dict:
    """Operator prompts, each with its ``answer`` (``null`` while open). Open ones first, oldest
    first; with ``status=all`` the closed ones follow, newest first. ``seq`` changes on every post /
    answer / close — poll every ~2 s and refetch when it differs."""
    return service.prompts_view(status, limit)


@router.post("/operator/prompts/{prompt_id}/answer", response_model=AnswerOut,
             dependencies=[Depends(write_guard)])
def answer_prompt(prompt_id: str, body: AnswerBody, request: Request) -> dict:
    """Answer an open prompt. confirm: ``choice`` ``yes`` | ``no``; choice (and attest with
    options): ``choice`` = an option id; number: ``value`` within the prompt's range; text and
    plain attest: ``value`` (a text longer than the limit is refused, never cut). An attest
    answer also sets the prompt's optical-state slots, in the same step. 404 unknown prompt, 409
    no longer open, 422 invalid answer, 503 could not save. ``source`` is the client's own claim
    (self-reported); the response's ``client`` is what the server saw - audit by that."""
    client = client_record(request)
    with _audited("answer", prompt_id, body.by, client):
        try:
            ans = prompts.answer(prompt_id, by=body.by, source=body.source, choice=body.choice,
                                 value=body.value, note=body.note, client=client)
        except prompts.PromptNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except prompts.PromptClosed as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except (prompts.PromptError, optical_state.OpticalStateError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except OSError as exc:
            raise _save_failed(exc) from None
    return ans.to_dict()


@router.post("/operator/prompts/{prompt_id}/dismiss", response_model=AnswerOut,
             dependencies=[Depends(write_guard)])
def dismiss_prompt(prompt_id: str, body: DismissBody, request: Request) -> dict:
    """Close an open prompt without answering it; whoever waits for it gets ``dismissed``.
    Nothing is attested. 404 unknown prompt, 409 no longer open. ``source`` self-reported,
    ``client`` seen by the server (as for answer)."""
    client = client_record(request)
    with _audited("dismiss", prompt_id, body.by, client):
        try:
            prompts.dismiss(prompt_id, by=body.by, source=body.source, reason=body.reason,
                            client=client)
        except prompts.PromptNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except prompts.PromptClosed as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except prompts.PromptError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
    ans = prompts.get_backend().get_answer(prompt_id)
    return ans.to_dict() if ans is not None else {"prompt_id": prompt_id, "status": "dismissed"}


# ── optical state ──────────────────────────────────────────────────────────────


@router.get("/operator/optical-state", response_model=OpticalStateResponse)
def get_optical_state() -> dict:
    """Slot definitions (``slots``) and what is registered now (``values``)."""
    return service.optical_state_view()


# declared before the {slot_id} routes: "history" must not be read as a slot id
@router.get("/operator/optical-state/history", response_model=OpticalHistoryResponse)
def get_optical_history(limit: int = Query(200, ge=1, le=2000)) -> dict:
    """The newest ``limit`` writes (set and clear), oldest first."""
    return {"records": service.optical_history(limit)}


@router.put("/operator/optical-state/{slot_id}", response_model=SlotValueOut,
            dependencies=[Depends(write_guard)])
def set_slot(slot_id: str, body: SetSlotBody, request: Request) -> dict:
    """Register what is physically installed / set. The value must fit the slot (a choice
    option id, a number in range, text of at most 500 characters - longer is refused, never
    cut - true / false). 404 unknown slot, 422 does not fit, 503 could not save (then nothing
    changed). ``source`` self-reported, ``client`` seen by the server (as for answer)."""
    store = optical_state.get_store()
    client = client_record(request)
    with _audited("set_slot", slot_id, body.by, client):
        try:
            sv = store.set_by_operator(slot_id, body.value, by=body.by, source=body.source,
                                       note=body.note, client=client)
        except optical_state.UnknownSlot as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except optical_state.OpticalStateError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except OSError as exc:
            raise _save_failed(exc) from None
    return sv.to_dict()


@router.post("/operator/optical-state/{slot_id}/clear", response_model=ClearSlotResponse,
             dependencies=[Depends(write_guard)])
def clear_slot(slot_id: str, body: ClearSlotBody, request: Request) -> dict:
    """Forget a slot's value (the history keeps it, with the writer's ``client``). 404 unknown
    slot, 503 could not save."""
    store = optical_state.get_store()
    client = client_record(request)
    with _audited("clear_slot", slot_id, body.by, client):
        try:
            store.clear_by_operator(slot_id, by=body.by, source=body.source, client=client)
        except optical_state.UnknownSlot as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except optical_state.OpticalStateError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except OSError as exc:
            raise _save_failed(exc) from None
    return {"slot": slot_id, "cleared": True, "seq": int(getattr(store, "seq", 0))}


__all__ = ["client_record", "router", "write_guard", "write_refusal"]
