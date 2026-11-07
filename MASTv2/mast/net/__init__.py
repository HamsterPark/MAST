"""Networking helpers for MAST.

* :mod:`mast.net.tailscale` — Tailscale detection + cross-network remote-access URLs.
* :mod:`mast.net.addrs` — this machine's addresses; which path a client came in on.
* :mod:`mast.net.tls` — the LAN TLS certificate (re-signed with the same key) and
  SPKI pins for satellites.

Kept separate from :mod:`mast.core` so the launcher (which is not part of the
``mast.core`` runtime) can import it without pulling in the heavy runtime graph.
"""
