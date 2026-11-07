"""Start the MAST MCP server on stdio.

Claude Code starts it through the plugin's ``.mcp.json``; by hand it is just::

    python run_server.py

Settings come from environment variables (see ``mast_mcp/config.py``):
MAST_URL, MAST_USER, MAST_PASSWORD, MAST_VERIFY_TLS, MAST_ALLOW_REMOTE,
MAST_ACTOR, MAST_SESSION, MAST_FETCH_DIR. MAST_MCP_LOG sets the log level
(INFO); the log goes to stderr, and also to MAST_MCP_LOG_FILE (appended,
UTF-8) when that is set. stdout carries the protocol and nothing else.

This file stays importable by old Pythons on purpose, so that an interpreter
that is too old prints a clear message instead of a SyntaxError.
"""
import logging
import os
import sys

LOG_FORMAT = "mast-mcp %(levelname)s %(name)s: %(message)s"

# MAST's bundled runtime is an embeddable CPython whose ._pth file switches on
# isolated path mode: the script's own directory is NOT on sys.path there.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def _log_handlers(path):
    """stderr, plus the log file at ``path`` when given and writable."""
    handlers = [logging.StreamHandler(sys.stderr)]
    if not path:
        return handlers, None
    try:
        folder = os.path.dirname(os.path.abspath(path))
        if not os.path.isdir(folder):
            os.makedirs(folder)
        handler = logging.FileHandler(path, encoding="utf-8")
    except OSError as exc:
        return handlers, "cannot write the log file %s (%s); logging to stderr only" % (path, exc)
    handler.setFormatter(logging.Formatter("%(asctime)s " + LOG_FORMAT))
    handlers.append(handler)
    return handlers, None


def main():
    if sys.version_info < (3, 10):
        sys.stderr.write("mast-mcp: Python 3.10 or newer is required (this is %s). Point the "
                         "plugin option 'python' at MAST's bundled runtime "
                         "(MASTv2/pyruntime/python.exe) or at a newer Python.\n"
                         % sys.version.split()[0])
        return 2

    # The protocol owns the real stdout. Anything else that prints (a stray
    # print(), a noisy library) goes to stderr instead of corrupting it.
    proto_out = sys.stdout.buffer
    sys.stdout = sys.stderr

    from mast_mcp.config import env_value, load_config

    level = env_value(os.environ, "MAST_MCP_LOG").upper() or "INFO"
    handlers, log_problem = _log_handlers(env_value(os.environ, "MAST_MCP_LOG_FILE"))
    logging.basicConfig(handlers=handlers, level=getattr(logging, level, logging.INFO),
                        format=LOG_FORMAT)

    from mast_mcp import SERVER_NAME, SERVER_TITLE, __version__
    from mast_mcp.resources import GuideResources
    from mast_mcp.stdio_rpc import serve
    from mast_mcp.tools import INSTRUCTIONS, Toolbox

    config = load_config()
    log = logging.getLogger("mast_mcp")
    if log_problem:
        log.warning(log_problem)
    log.info("starting %s %s with %s", SERVER_NAME, __version__, config.describe())
    if config.problem:
        log.warning("tools will refuse to run: %s", config.problem)

    return serve(
        server_info={"name": SERVER_NAME, "title": SERVER_TITLE, "version": __version__},
        instructions=INSTRUCTIONS,
        tools=Toolbox(config),
        resources=GuideResources(),
        instream=sys.stdin.buffer,
        outstream=proto_out,
    )


if __name__ == "__main__":
    sys.exit(main())
