"""VISTA inside MAST: the visual harness an external agent uses to look at the instrument.

Ported from VISTA-SPM, itself a port of VISTA (Han et al., arXiv 2610.02200, MIT-licensed
code at github.com/joshhhhhan/VISTA). The design contract is
``docs/v2/architecture/mast7_vista.md``.

Layout::

    protocol.py   shared types: ToolReply / ImageBlock / ToolDef, frame ids, display caps
    frames.py     load .sxm / .dat (MAST's readers), orient, flatten, render, display <-> scan nm
    archive.py    the lossless frame archive: every frame addressable by id, reopened on restart
    viewtools.py  inspect (look again at any region) and read_values (exact archived values)
    measure.py    line_profile, fft_peaks, detect_blobs, diff, stats: numbers, never verdicts
    notes.py      GUIDE.md / WORKING.md, the agent's own notes
    actions.py    the act vocabulary: short fixed sequences of MAST skills via ExecutionContext

The server side (observation token, workspaces, frame watcher, detectors, runner) lives in
sibling modules of this package. Nothing is imported here: ``import mast.vista`` stays
cheap, and numpy, scipy, PIL and matplotlib load only with the modules that need them.
"""
