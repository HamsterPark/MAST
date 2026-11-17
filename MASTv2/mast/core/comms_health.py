"""Communication health circuit breaker for the Nanonis TCP link.

WHY (field trace, s82–130): when the Nanonis TCP link goes down, every
hardware read/write times out ON ITS OWN (5 s each) before the agent gives up.
The trace shows GetBias → GetCurrent → GetZPosition → SetBias → ZControllerOnOff
each raising ``TimeoutError`` — nineteen separate ~5 s stalls, one tool at a time,
before the agent finally handed off. That is ~90 s of the operator watching the
system bang its head against a dead socket, and it hammers the *fragile* Nanonis
port (force-kill / repeated abrupt reconnects can wedge it until Nanonis restarts).

This module is the circuit breaker that stops that march. It observes only
TCP-level failures (timeouts / connection errors / failed reconnects — NOT a
Nanonis application error string, which means the round-trip SUCCEEDED). After
``fail_threshold`` consecutive TCP failures it OPENS: subsequent calls are
short-circuited *without touching the socket* for a cooldown, so the caller gets
an immediate, clear "comms down" verdict instead of a fresh 5 s stall, and the
port is left alone. After the cooldown one probe is allowed (HALF-OPEN); its
result either CLOSES the breaker (Nanonis recovered) or re-OPENS it.

SAFETY: the breaker never issues a command. It only decides whether a ROUTINE
command is allowed to be *attempted*. Its whole job is to make the system stop
sending and report, which is the safe thing to do with routine traffic on a dead
link.

Emergency traffic is NOT gated (a behaviour change): anything on the
dedicated emergency port and anything sent through ``ConnectionPool.urgent_call``
(the E-STOP and the watchdog's SafeRetract, main-port withdraw fallback included)
is always attempted, and still reported. The original reason for gating it too —
"a stop / withdraw cannot reach a Nanonis that is not answering TCP anyway, so
failing fast beats a 5 s hang" — assumes the verdict is about the socket the stop
would use, and is current. It is neither. One breaker serves four independent
sockets, so timeouts on main / monitor / data refuse the emergency port, whose
whole point is not to depend on them. And the verdict lags: OPEN holds for the
full cooldown after the last failure however soon the link came back, and
HALF_OPEN refuses everyone for as long as one probe is out. Refusing a withdraw
on that basis trades a crashed tip for a few seconds; attempting it costs at
most one recv timeout per role (later calls on a role whose socket was retired
fail fast through the reconnect backoff).

The gate also did something its comment never said. When it was written
role locks were unbounded and a dead socket could hold a call for
~1000 s (a timeout nanonis_spm leaked onto the socket) or forever (a busy loop
on EOF): an emergency call on a dead link would hang holding its role lock, and
every emergency call after it would queue behind it. Later fixes
bounded both (patched send, bounded role locks, ``urgent_call``). The part
still left — a plain ``safe_call`` on the emergency role waits up to 30 s for
its lock — is closed in ``ConnectionPool.safe_call``: an emergency call let past
a breaker that would have refused it waits at most ``EMERGENCY_LOCK_TIMEOUT_S``
(2 s) for the role lock and never queues.

Process-level, thread-safe, holds only primitives (no sockets / tensors) — safe
to import anywhere in the core layer. One breaker lives on each
:class:`~mast.core.connection.ConnectionPool`.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from mast.i18n import tr

logger = logging.getLogger(__name__)

# Defaults (module constants so the whole system agrees; overridable per-instance
# for tests). Kept conservative on the Nanonis-port-fragility side: a short
# threshold trips fast, and the cooldown is long enough that we are not
# re-probing a dead port every few hundred ms.
_FAIL_THRESHOLD = 3          # consecutive TCP failures that OPEN the breaker
_OPEN_COOLDOWN_S = 20.0      # how long the breaker stays open before a probe
_STREAK_WINDOW_S = 30.0      # a failure older than this starts a fresh streak
#: How long a probe may stay out before it counts as abandoned, when the caller
#: does not say (``admit(lease_s=...)``). Defence in depth: every exit of
#: ``ConnectionPool.safe_call`` returns its probe, and safe_call always passes its
#: OWN budget (``ConnectionPool._probe_lease_s``). This default is the longest an
#: ordinary call stays on the wire without asking for more: two tries at the
#: module-open grace (30 s, ``connection._MODULE_OPEN_RECV_S``) plus one graceful
#: reconnect (~2.3 s) between them, rounded up. One fixed cap for every call was
#: rejected: the longest legitimate call is 2 x 900 s, so a cap that never cuts a
#: live probe short would let a leak refuse everything for ~30 min, and one short
#: enough to matter would cut live long probes short.
_PROBE_LEASE_S = 75.0

# States
CLOSED = "closed"        # healthy — calls flow normally
OPEN = "open"            # tripped — calls short-circuited, port left alone
HALF_OPEN = "half_open"  # cooldown elapsed — exactly one probe in flight


class CommsCircuitBreaker:
    """Consecutive-TCP-failure circuit breaker.

    The pool asks :meth:`would_admit` before queueing for a role lock (a refusal
    there costs nothing), then :meth:`admit` once it holds the lock, and reports
    the outcome via :meth:`record_success` / :meth:`record_failure`. ``record_*``
    is driven ONLY by TCP-level outcomes — a Nanonis app-error string is a
    successful round-trip and counts as a success, so a run that legitimately
    pokes an unavailable module does not trip the link breaker.

    THE PROBE MUST COME BACK. In HALF_OPEN, :meth:`admit` hands out exactly one
    probe, identified by a number, and refuses everyone else until that probe
    reports. A holder that leaves without reporting (method not found, an
    exception, ...) must return it with :meth:`abandon_probe`, or the breaker
    refuses every call on every role for good — the emergency withdraw
    included — while it still reads ``half_open``. The number makes the return exact: once the probe has reported,
    expired or been replaced, returning it is a no-op, so the return can sit in a
    ``finally`` without ever freeing somebody else's probe.

    A LEASE backs that up. Each probe carries one (``admit(lease_s=...)``, by
    default :data:`_PROBE_LEASE_S`); a probe still out past it counts as
    abandoned and the next :meth:`admit` issues a new one. So if some future path
    forgets to report, the breaker refuses for at most one lease, not forever.
    """

    def __init__(
        self,
        *,
        fail_threshold: int = _FAIL_THRESHOLD,
        open_cooldown_s: float = _OPEN_COOLDOWN_S,
        streak_window_s: float = _STREAK_WINDOW_S,
        probe_lease_s: float = _PROBE_LEASE_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fail_threshold = max(1, int(fail_threshold))
        self._open_cooldown_s = float(open_cooldown_s)
        self._streak_window_s = float(streak_window_s)
        self._probe_lease_s = max(0.0, float(probe_lease_s))
        self._clock = clock
        self._lock = threading.RLock()
        # State
        self._streak = 0                # consecutive TCP failures
        self._last_fail_at = 0.0
        self._open_until = 0.0           # monotonic deadline while OPEN
        self._probe_id = 0               # HALF-OPEN: number of the probe out, 0 = none
        self._probe_seq = 0              # last probe number handed out
        self._probe_issued_at = 0.0      # when the probe out was handed out
        self._probe_deadline = 0.0       # past this the probe out counts as abandoned
        self._probes_abandoned = 0       # probes returned without a verdict (telemetry)
        self._probes_expired = 0         # probes whose lease ran out (telemetry)
        self._tripped_total = 0          # how many times we have OPENed (telemetry)
        self._last_reason = ""

    # ── state introspection ──────────────────────────────────────────────
    def _probe_live_locked(self, now: float) -> bool:
        """A probe is out and its lease has not run out."""
        return bool(self._probe_id) and now < self._probe_deadline

    def _state_locked(self) -> str:
        if self._streak < self._fail_threshold:
            return CLOSED
        now = self._clock()
        if self._probe_live_locked(now):
            return HALF_OPEN
        if now >= self._open_until:
            return HALF_OPEN  # cooldown elapsed — the next admit() probes
        return OPEN

    def state(self) -> str:
        with self._lock:
            return self._state_locked()

    def is_open(self) -> bool:
        with self._lock:
            return self._state_locked() == OPEN

    # ── the gate ─────────────────────────────────────────────────────────
    def would_admit(self, *, may_probe: bool = True) -> bool:
        """Would :meth:`admit` let a call through right now? Changes nothing.

        The pool asks this BEFORE queueing for a role lock, so an OPEN breaker
        still refuses instantly; the probe itself is handed out by
        :meth:`admit` once the lock is held. ``may_probe=False`` (a caller that
        will not report its outcome) is admitted only while CLOSED."""
        with self._lock:
            if self._streak < self._fail_threshold:
                return True  # CLOSED
            now = self._clock()
            if not may_probe or self._probe_live_locked(now):
                return False
            return now >= self._open_until

    def admit(self, *, may_probe: bool = True,
              lease_s: float | None = None) -> tuple[bool, int]:
        """``(admitted, probe_id)`` for a call about to be attempted.

        CLOSED → ``(True, 0)``. OPEN → ``(False, 0)`` until the cooldown
        elapses; then exactly ONE caller gets ``(True, n)`` with ``n > 0``: it
        holds the HALF-OPEN probe, and everyone else is refused until the probe
        reports via :meth:`record_success` / :meth:`record_failure`, is returned
        with :meth:`abandon_probe`, or outlives its lease (``lease_s``, default
        :data:`_PROBE_LEASE_S`: the longest the holder can legitimately take).
        ``may_probe=False`` never takes the probe — a caller that will not
        report must not hold it."""
        expired = None
        with self._lock:
            if self._streak < self._fail_threshold:
                return True, 0  # CLOSED
            if not may_probe:
                return False, 0  # will not report: never the probe
            now = self._clock()
            if self._probe_id:
                if self._probe_live_locked(now):
                    return False, 0  # a probe is out — hold everyone else
                # Its lease ran out and it never reported: count it abandoned.
                expired = (self._probe_id, now - self._probe_issued_at,
                           self._probe_deadline - self._probe_issued_at)
                self._probes_expired += 1
                self._probe_id = 0
            if now < self._open_until:
                return False, 0  # still OPEN, inside cooldown
            # Cooldown elapsed: release ONE probe.
            lease = self._probe_lease_s if lease_s is None else max(0.0, float(lease_s))
            self._probe_seq += 1
            self._probe_id = self._probe_seq
            self._probe_issued_at = now
            self._probe_deadline = now + lease
            issued = self._probe_id
        if expired is not None:
            logger.warning(
                "comms breaker: probe #%d was out %.1f s, past its %.1f s lease, and "
                "never reported — treated as abandoned; probe #%d issued",
                expired[0], expired[1], expired[2], issued)
        return True, issued

    def allow(self) -> bool:
        """``admit()[0]``: True if a call may be ATTEMPTED right now.

        Takes the probe when one is available but gives the caller no number to
        return it with. Only for callers that report every outcome; a caller
        that can leave without reporting must use :meth:`admit` and
        :meth:`abandon_probe`."""
        return self.admit()[0]

    def abandon_probe(self, probe_id: int, why: str = "") -> bool:
        """Return probe ``probe_id`` WITHOUT a verdict; the next caller probes.

        For a holder that leaves without reporting. The cooldown is not
        re-armed — nothing has been learnt about the link. A no-op (False) when
        that probe already reported, or another probe has replaced it, so it is
        safe in a ``finally``."""
        with self._lock:
            if not probe_id or probe_id != self._probe_id:
                return False
            self._probe_id = 0
            self._probes_abandoned += 1
        logger.warning(
            "comms breaker: probe #%d ended without a verdict%s — returned; the "
            "next counted call probes the link", probe_id, f" ({why})" if why else "")
        return True

    def rearm_probe(self, why: str = "") -> bool:
        """End the cooldown NOW and forget any probe out: the next counted call
        probes. For an operator's explicit reconnect. CLOSED: no-op (False).

        Deliberately NOT a close. A reconnect proves only that a TCP handshake
        on one role went through; a Nanonis whose port is wedged accepts the
        connection and then answers nothing (seen in a field trace).
        Closing would release every waiting caller onto an unverified link at
        once — the burst the single probe exists to prevent — and if the link is
        still bad it takes three more timeouts to re-open. One probe gets the
        verdict with one call. The streak is kept, so a failed probe re-opens at
        once with a fresh cooldown."""
        with self._lock:
            if self._streak < self._fail_threshold:
                return False
            now = self._clock()
            self._open_until = min(self._open_until, now)
            forgot = self._probe_id
            self._probe_id = 0
        logger.warning(
            "comms breaker: cooldown ended by request%s — the next counted call "
            "probes the link%s", f" ({why})" if why else "",
            f" (probe #{forgot} still out is no longer waited for)" if forgot else "")
        return True

    def record_success(self) -> None:
        """A TCP round-trip completed (even if Nanonis returned an app error).
        Resets the streak and closes the breaker."""
        with self._lock:
            self._streak = 0
            self._last_fail_at = 0.0
            self._open_until = 0.0
            self._probe_id = 0
            self._last_reason = ""

    def record_failure(self, reason: str = "") -> None:
        """A TCP-level failure (timeout / connection error / failed reconnect).

        Extends the consecutive-failure streak (a stale failure older than the
        window starts a fresh streak rather than counting toward an unrelated
        one) and, at/over the threshold, (re)arms the OPEN cooldown."""
        with self._lock:
            now = self._clock()
            if self._streak and (now - self._last_fail_at) > self._streak_window_s:
                # The previous failures are too old to be "consecutive" with this
                # one — start the streak over so a slow drip never trips it.
                self._streak = 1
            else:
                self._streak += 1
            self._last_fail_at = now
            self._last_reason = str(reason or "")[:200]
            was_probe = bool(self._probe_id)
            self._probe_id = 0
            if self._streak >= self._fail_threshold:
                # (Re)open. A failed HALF-OPEN probe re-arms the full cooldown.
                if self._open_until <= now or was_probe:
                    self._tripped_total += 1
                self._open_until = now + self._open_cooldown_s

    def cooldown_remaining_s(self) -> float:
        with self._lock:
            if self._streak < self._fail_threshold:
                return 0.0
            return max(0.0, self._open_until - self._clock())

    def snapshot(self) -> dict:
        """Plain-dict telemetry (JSON-safe) for diagnostics / GUI."""
        with self._lock:
            now = self._clock()
            live = self._probe_live_locked(now)
            return {
                "state": self._state_locked(),
                "streak": self._streak,
                "fail_threshold": self._fail_threshold,
                "cooldown_remaining_s": round(
                    max(0.0, self._open_until - now), 2)
                if self._streak >= self._fail_threshold else 0.0,
                "tripped_total": self._tripped_total,
                "last_reason": self._last_reason,
                "probe_in_flight": live,
                "probe_age_s": round(now - self._probe_issued_at, 2) if live else 0.0,
                "probe_lease_s": (round(self._probe_deadline - self._probe_issued_at, 2)
                                  if live else 0.0),
                "probes_issued": self._probe_seq,
                "probes_abandoned": self._probes_abandoned,
                "probes_expired": self._probes_expired,
            }


# Operator-facing message returned in a short-circuited call's record.error. The
# agent reads this on the tool path — it must say plainly "stop retrying, hand
# off", because the whole point is to end the one-tool-at-a-time march.
COMMS_DOWN_MESSAGE = tr(
    "comms_circuit_open: Nanonis TCP 通信连续超时(≥{n} 次)已熔断,暂停发命令 "
    "~{cd:.0f}s 以免反复重连撞坏脆弱的 Nanonis 端口。通信已判定中断——请勿逐个工具"
    "重试,应立即停止硬件操作、向用户/上层报告并 handoff,等待连接恢复后再继续。",
    "comms_circuit_open: Nanonis TCP communication timed out repeatedly (>= {n} times) and the "
    "circuit breaker has opened; commands are paused for ~{cd:.0f}s so that repeated reconnects "
    "do not damage the fragile Nanonis port. Communication is judged down -- do not retry tool by "
    "tool; stop hardware operations immediately, report to the user / upstream and hand off, and "
    "continue only after the connection recovers.",
)


def format_comms_down(breaker: "CommsCircuitBreaker") -> str:
    snap = breaker.snapshot()
    return COMMS_DOWN_MESSAGE.format(
        n=snap.get("fail_threshold", _FAIL_THRESHOLD),
        cd=snap.get("cooldown_remaining_s", _OPEN_COOLDOWN_S) or _OPEN_COOLDOWN_S,
    )


__all__ = [
    "CommsCircuitBreaker",
    "COMMS_DOWN_MESSAGE",
    "format_comms_down",
    "CLOSED",
    "OPEN",
    "HALF_OPEN",
]
