"""MAST MCP server: lets Claude Code (or any MCP client) drive MAST through its
external agent API (``/api/ext/v1``).

Standard library only, on purpose: the server must start on MAST's bundled
embeddable Python, on the development venv, or on any Python >= 3.10, without
installing a single package.

Layout:

* ``stdio_rpc``  MCP over stdio (JSON-RPC framing and method dispatch). With
                 ``http_rpc`` the only modules that know the wire protocol;
                 swapping in an official SDK later means rewriting those two.
* ``http_rpc``   MCP over streamable HTTP, for an AI on another computer.
* ``config``     environment -> settings, including the loopback-only rule.
* ``client``     HTTP to MAST, with every failure turned into a sentence.
* ``tools``      the ``mast_*`` tools.
* ``vista_surface``  what the model reads about the VISTA tools (MAST 7.0): their
                 descriptions and input schemas, copied from ``mast.vista``.
* ``resources``  the bilingual operator guide as ``mast://guide/{lang}/{file}``.
* ``remote_server``  remote AI access, run by the MAST launcher: ``http_rpc`` on
                 the Tailscale address, with the ``mast_*`` tools, ``mast_service``
                 and the computer tools.
* ``computer``   commands, files and screenshots on this computer (remote AI
                 access only; ``take_screenshot`` needs Pillow, which MAST.exe has).
"""

__version__ = "0.2.0"

SERVER_NAME = "mast"
SERVER_TITLE = "MAST (instrument station)"
