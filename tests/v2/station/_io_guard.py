"""No I/O beyond this machine's loopback from a probe child process (generic_station.md §5).

``install()`` is called first thing by ``_boot_probe.py`` and ``_invariants_probe.py``, which boot a
real ``CoreRuntime`` in a child process on the machine that runs the suite. It makes three things
impossible, and tells the parent (a ``BLOCKED_IO`` line on stderr, which ``assert_no_blocked_io``
turns into a test failure) when something tried:

* a socket connect / send to any address that is not loopback (satellites, vendor servers, the net);
* opening a serial port (``serial.Serial.open``) - nothing is ever written to a COM port of this
  machine; ``list_ports.comports`` answers an empty list instead of enumerating;
* nothing else is touched: loopback sockets (the probe's own) and AF_UNIX work as usual.

The guard only adds refusals; it changes no answer a probe gives when nothing is refused, so the
golden files recorded before it stay valid.
"""
from __future__ import annotations

import socket
import sys

_LOOPBACK = {"127.0.0.1", "localhost", "::1", "0.0.0.0", ""}


def _say(what: str) -> None:
    sys.stderr.write(f"BLOCKED_IO {what}\n")
    sys.stderr.flush()


def _is_loopback(address: object) -> bool:
    if not isinstance(address, tuple) or not address:
        return True                      # AF_UNIX path or something that is not a network address
    host = str(address[0]).strip().lower()
    return host in _LOOPBACK or host.startswith("127.")


def install() -> None:
    for name in ("connect", "connect_ex", "sendto"):
        orig = getattr(socket.socket, name)

        def guarded(self, *args, _orig=orig, _name=name):
            address = args[-1] if args else None
            if not _is_loopback(address):
                _say(f"socket.{_name} {address!r}")
                raise OSError(f"blocked by the probe I/O guard: {address!r}")
            return _orig(self, *args)

        setattr(socket.socket, name, guarded)
    try:
        import serial
        import serial.tools.list_ports as list_ports
    except Exception:  # noqa: BLE001 - no pyserial here: nothing to guard
        return

    def _no_open(self, *a, **k):
        _say(f"serial.Serial.open {getattr(self, 'port', None)!r}")
        raise serial.SerialException("blocked by the probe I/O guard")

    serial.Serial.open = _no_open

    def _no_ports(*a, **k):
        _say("list_ports.comports")
        return []

    list_ports.comports = _no_ports


def assert_no_blocked_io(stderr: str) -> None:
    lines = [ln for ln in (stderr or "").splitlines() if ln.startswith("BLOCKED_IO")]
    assert not lines, "a probe child process tried to reach beyond loopback / a COM port:\n" + "\n".join(lines)
