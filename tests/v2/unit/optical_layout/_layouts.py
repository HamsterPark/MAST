"""Small builders for layouts in tests (plain dicts, as a client would send them)."""
from __future__ import annotations

from typing import Any


def comp(cid: str, typ: str, x: float, y: float, angle: float = 0.0, *, label: str | None = None,
         aperture: float | None = None, props: dict | None = None, **annotations: Any) -> dict:
    c: dict[str, Any] = {"id": cid, "type": typ, "label": cid.upper() if label is None else label,
                         "x_mm": x, "y_mm": y, "angle_deg": angle, "props": props or {}}
    if aperture is not None:
        c["aperture_mm"] = aperture
    if annotations:
        c["annotations"] = annotations
    return c


def laser(cid: str, x: float, y: float, angle: float = 0.0, wl: Any = 532, **kw: Any) -> dict:
    props = {"wavelengths_nm": wl} if isinstance(wl, list) else {"wavelength_nm": wl}
    return comp(cid, "laser", x, y, angle, props=props, **kw)


def mirror(cid: str, x: float, y: float, angle: float, **kw: Any) -> dict:
    return comp(cid, "mirror", x, y, angle, **kw)


def det(cid: str, x: float, y: float, angle: float, aperture: float = 10.0, **kw: Any) -> dict:
    return comp(cid, "detector", x, y, angle, aperture=aperture, **kw)


def layout(components: list[dict], *, lid: str = "t", title: str = "", width: float = 1200.0,
           height: float = 900.0, **extra: Any) -> dict:
    return {"schema": 1, "id": lid, "title": title,
            "table": {"width_mm": width, "height_mm": height}, "components": components, **extra}


def validated(lay: dict, **raw: float):
    """The :class:`Layout` of *lay*, with some numbers overwritten **unrounded**
    (``las1__y_mm=112.70000000050001``): the tolerance tests need geometry finer than the 0.01 mm
    and 0.01 degree the model rounds to."""
    from mast.optical_layout.model import Layout

    obj = Layout.model_validate(lay)
    for key, value in raw.items():
        cid, attr = key.split("__")
        setattr(obj.component(cid), attr, value)
    return obj


def budget_layout() -> dict:
    """Two sources. L1: BS1 splits; the reflected branch goes to M1 and D_R, the transmitted one to
    D_T (4 events). L2 goes straight to D_2 (1 event). 5 events in all (the shared budget fixtures)."""
    return layout([
        laser("las1", 50, 62.5, 0, 532),
        laser("las2", 50, 400, 0, 633),
        comp("bs1", "beam_splitter", 150, 62.5, 135),
        mirror("m1", 150, 150, 315),
        det("det_r", 300, 150, 180),
        det("det_t", 300, 62.5, 180),
        det("det_2", 300, 400, 180),
    ])


def dense_field() -> dict:
    """A worst case: 361 beam splitters on a 3 mm grid at 45 degrees and 32
    lasers of 16 wavelengths each firing into them - inside every limit of the model, 60 KB."""
    comps = []
    for i in range(19):
        for j in range(19):
            comps.append(comp(f"bs{i}_{j}", "beam_splitter", 50.0 + i * 3.0, 50.0 + j * 3.0, 45.0, aperture=25.0,
                              props={"reflect_fraction": 0.5}))
    wls = [400.0 + 10 * n for n in range(16)]
    for s in range(32):
        comps.append(laser(f"L{s}", 1.0, 50.0 + s * 0.5, 0.0, wls, aperture=5.0))
    return layout(comps, width=300.0, height=300.0)
