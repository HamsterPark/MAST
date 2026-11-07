"""The internal-LLM gate: ``legacy_features_enabled`` is the one switch.

MAST is meant to be driven from an external agent (Claude Code + the MAST plugin).
The built-in LLM stack — agent chat, Agents, query assistant, multi-day conduct,
memory, and everything that calls a model on MAST's behalf — belongs to the legacy
feature set, which is OFF by default. Before this module only the UI and the vision
model honoured that switch; the backend could still call the model on its own (the
30-min dreaming pass could hit the provider during an unattended tip
approach). Now every path that can reach the internal LLM asks here.

Semantics
---------
* **One source of truth:** the ``legacy_features_enabled`` setting, read LIVE
  through :func:`mast.webui.settings_store.legacy_features_enabled` (the process
  store that ``POST /api/settings`` writes). Turning it off stops model calls at the
  next call; nothing has to restart for that direction.
* **Fail-safe OFF:** a missing key, an unreadable store or any exception means OFF.
* **Two layers:**
    1. choke points that every call passes through — :func:`make_gate_callback`
       is attached to every LangChain model ``make_chat_model`` builds (checked at
       CALL time, so a model built while the switch was on stops working the
       moment it is turned off), plus explicit checks in the legacy
       ``ClaudeClient``, the OCR transport and the voice transport;
    2. early exits where the work starts — automatic loops skip without doing any
       work, user-initiated routes refuse up front with :class:`LegacyFeaturesDisabled`
       (mapped to HTTP 403 by ``mast.api.app``) instead of failing somewhere deep.

:class:`LegacyFeaturesDisabled` subclasses ``RuntimeError`` on purpose: several
callers already treat "``make_chat_model`` raised ``RuntimeError``" as "no model
configured" (the conduct seats' "no seat ⇒ cannot decide" shape), and that is the
honest reading of a disabled LLM too.

Not gated here: embedding calls (literature search, the notes index). They are not
an LLM, the external-agent workflow's literature and notes search rely on them, and
switching the memory index's embedder off would make it wipe its vectors on the next
backend comparison.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from mast.i18n import tr

logger = logging.getLogger(__name__)

#: Stable marker at the start of every refusal text, so a client (or a test) can
#: recognise the refusal without parsing prose in two languages.
REFUSAL_CODE = "legacy_features_disabled"

_override_lock = threading.Lock()
_override: "bool | None" = None


class LegacyFeaturesDisabled(RuntimeError):
    """The internal LLM is off because ``legacy_features_enabled`` is off."""

    code = REFUSAL_CODE


def set_override_for_test(value: "bool | None") -> None:
    """Force the gate open/closed in tests; ``None`` = read the real setting.

    The test suite exercises the legacy agent stack with fakes and runs with the
    gate forced open (``tests/v2/conftest.py``); tests of the gate itself reset it.
    Production code never calls this.
    """
    global _override
    with _override_lock:
        _override = value


def internal_llm_enabled() -> bool:
    """Whether MAST may call its internal LLM right now. Live, fail-safe OFF."""
    ov = _override
    if ov is not None:
        return bool(ov)
    try:
        from mast.webui.settings_store import legacy_features_enabled

        return bool(legacy_features_enabled())
    except Exception:  # noqa: BLE001 — unreadable ⇒ off
        return False


def refusal_message(what: str = "") -> str:
    """The text a refused caller shows. Starts with :data:`REFUSAL_CODE`."""
    zh_what = f"（{what}）" if what else ""
    en_what = f" ({what})" if what else ""
    return f"{REFUSAL_CODE}: " + tr(
        f"内置 LLM 已关闭{zh_what}：旧版功能（legacy_features_enabled）没有开启。"
        "MAST 现在的推荐用法是从外部 agent 驱动；确实要用内置 LLM，请到 设置 → 常规 → 旧版功能 打开。",
        f"The internal LLM is off{en_what}: legacy features (legacy_features_enabled) "
        "are disabled. MAST is meant to be driven from an external agent; to use the "
        "internal LLM anyway, enable Settings → General → Legacy features.",
    )


def require_internal_llm(what: str = "") -> None:
    """Raise :class:`LegacyFeaturesDisabled` unless the internal LLM is allowed."""
    if not internal_llm_enabled():
        raise LegacyFeaturesDisabled(refusal_message(what))


_GATE_CLS: Any = None


def make_gate_callback(source: str = "") -> Any:
    """A LangChain callback that refuses every model run while the gate is closed.

    ``raise_error = True`` makes LangChain re-raise from ``on_chat_model_start`` /
    ``on_llm_start``, which run BEFORE the provider request — so a refused call
    never leaves the process. ``run_inline`` keeps it on the caller's thread for
    async runs. Attached by ``make_chat_model`` to every model it builds.
    """
    global _GATE_CLS
    if _GATE_CLS is None:
        from langchain_core.callbacks import BaseCallbackHandler

        class InternalLLMGateCallback(BaseCallbackHandler):
            raise_error = True
            run_inline = True

            def __init__(self, src: str = "") -> None:
                super().__init__()
                self.source = src

            def on_chat_model_start(self, serialized, messages, **kwargs):  # noqa: D401
                require_internal_llm(f"model call: {self.source}" if self.source else "model call")

            def on_llm_start(self, serialized, prompts, **kwargs):  # noqa: D401
                require_internal_llm(f"model call: {self.source}" if self.source else "model call")

        _GATE_CLS = InternalLLMGateCallback
    return _GATE_CLS(source)


__all__ = [
    "LegacyFeaturesDisabled", "REFUSAL_CODE", "internal_llm_enabled",
    "make_gate_callback", "refusal_message", "require_internal_llm",
    "set_override_for_test",
]
