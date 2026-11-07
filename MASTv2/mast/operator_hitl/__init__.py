"""Operator-in-the-loop primitives for physical steps a person must perform.

Design contract: ``docs/v2/design/optical_station_witec.md`` §6.2, decisions D8/D9.

* :mod:`mast.operator_hitl.prompts` — structured operator prompts (confirm / choice /
  number / text / attest). Agents and skills may *post* and *wait*; only operator
  channels (operator web endpoints, launcher popup) may *answer*.
* :mod:`mast.operator_hitl.optical_state` — the manual optical-state registry (which λ/2
  is installed, analyzer angle, objective, …). Only operators write it; every
  acquisition sidecar carries a snapshot.

Both modules ship an in-memory default backend so skills and tests work without
the service; the persistent, notifying backends plug in through ``set_backend`` /
``set_store``:

* :mod:`mast.operator_hitl.store` — the persistent backends (JSON under
  ``<root>/artifacts/operator``; slot definitions from ``<root>/config/optical_station.json``);
* :mod:`mast.operator_hitl.service` — ``install()`` / ``uninstall()`` (service start-up calls
  it), the EventBus notifications, and the views the operator API serves;
* :mod:`mast.operator_hitl.human_node` — a composite ``human`` node (and ``AskOperator``)
  waiting for a real operator on the direct / external path.

Who may answer: only ``prompts.answer`` / ``prompts.dismiss`` and the ``OpticalStateStore``
write methods change what an operator attested, and the only callers allowed are the operator
routes (``mast/api/routes/operator.py``) and this package; ``tests/v2/unit/operator_hitl``
scans the tree to keep it so.
"""

from mast.operator_hitl import optical_state, prompts

__all__ = ["prompts", "optical_state"]
