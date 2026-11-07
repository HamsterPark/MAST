"""Unattended Claude Code runs against MAST (contract ``docs/v2/architecture/mast7_vista.md`` §8).

Ported from VISTA-SPM (``claude_runner.py``, ``compact_hook.py``, ``prompts.py``), itself a port
of VISTA (MIT, github.com/joshhhhhan/VISTA). What changed in MAST: the model talks to MAST
through MAST's own MCP bridge (tools ``mast_*``), the server enforces one act per observation
(observation tokens), checkpoints and recovery observations come from the VISTA service, and the
compaction hooks reach the runner over MAST's HTTP API.

Layout::

    paths.py         where claude, the bridge, its python and the hook script are (dev / frozen)
    prompts.py       system prompt, task block, continue text, first messages of a segment
    compact_hook.py  the PreCompact/PostCompact/Stop hook script (standard library only)
    claude.py        ClaudeRunner: one run = a chain of `claude -p` stream-json sessions
    manager.py       RunnerManager: one active run per process, start/list/get/stop/hook

The HTTP routes are ``mast.api.ext.runner``. Nothing is imported here, so importing the
package stays cheap.
"""
