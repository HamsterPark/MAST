"""Write spectrum files the way ``AcquireSpectrum`` does (``skills/builtins/optics_spectro.py``).

``spec_<stamp>_<device>.npy``: float64 ``(1 + frames, pixels)``, row 0 = wavelength in nm.
``spec_<stamp>_<device>.json``: the ``mast.spectrum/1`` sidecar. The real skill is exercised
against a simulated spectrometer in ``test_station_observe.py``; these synthetic ones give
peaks at known positions and a saturation level a test can set.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import numpy as np

#: two clear Stokes lines for a 532 nm laser: 939.9 and 1847.8 cm-1
PEAKS = ((560.0, 900.0), (590.0, 450.0))


def write_spectrum(folder: Path, stem: str = "spec_20261103-120000-000001_witec", *,
                   peaks=PEAKS, frames: int = 2, pixels: int = 900, lo: float = 530.0,
                   hi: float = 650.0, base: float = 100.0, noise: float = 2.0,
                   saturate: bool = False, laser_nm: float | None = 532.0,
                   exposure_ms: float | None = 1000.0, sidecar: bool = True,
                   nan_axis: bool = False, age_s: float | None = None,
                   extra_sidecar: dict | None = None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(7)
    x = np.linspace(lo, hi, pixels)
    y = np.full(pixels, base, dtype=float)
    for centre, height in peaks:
        y += height * np.exp(-0.5 * ((x - centre) / 1.2) ** 2)
    rows = [y + rng.normal(0.0, noise, pixels) for _ in range(frames)]
    if saturate:
        rows[0][400:404] = 65535.0
    axis = np.full(pixels, np.nan) if nan_axis else x
    path = folder / f"{stem}.npy"
    np.save(path, np.vstack([axis, *rows]))
    if sidecar:
        side = {
            "schema": "mast.spectrum/1",
            "device": {"id": "witec", "kind": "microscope"},
            "settings": {"requested": {"exposure_ms": exposure_ms, "frames": frames},
                         "before": {"exposure_ms": exposure_ms},
                         "laser_line_nm": laser_nm},
            "result": {"frames": frames},
            **(extra_sidecar or {}),
        }
        path.with_suffix(".json").write_text(json.dumps(side), encoding="utf-8")
    if age_s is not None:
        t = time.time() - age_s
        os.utime(path, (t, t))
    return path
