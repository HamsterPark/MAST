"""The optical layout document (schema 1): validation and normalisation.

Contract: ``docs/v2/design/optical_layout.md`` §2-§4. One layout is the drawing of an optical
table: the table (size, hole pitch), the components on it (position, direction, aperture,
type parameters, annotations, links to MAST devices and manual optical-state slots), free text
labels and a note. Everything is in millimetres and degrees; the origin is the table's lower
left corner, x to the right, y up, angles counter-clockwise from +x.

The model is strict on purpose - it is the only door through which a layout reaches the disk:

* unknown keys are refused at every level (a typo is a 422, not a silently dropped field);
* numbers are finite and bounded, positions are rounded to 0.01 mm, angles are brought into
  [0, 360) and rounded to 0.01 degrees; text is cleaned of control characters and refused (never
  cut) when it is too long;
* ``props`` are checked against the component type (§4), unknown keys refused;
* ids are ``[A-Za-z0-9_-]{1,40}`` and unique in one layout; counts and the document size are
  bounded.

``revision``, ``updated_at`` and ``updated_by`` are written by the server (the store); a client
may send them back as it received them, they are overwritten.

Identity strings (component ``type``, ``props`` keys and values such as ``short`` / ``long``,
``Layout.id``) are never translated; the display names in :data:`TYPES` are (``tr`` at the point
of use).
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Optional, get_args

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from mast.i18n import tr

SCHEMA_VERSION = 1

ID_PATTERN = r"^[A-Za-z0-9_-]{1,40}$"
_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,40}")

MAX_COMPONENTS = 400
MAX_TEXTS = 100
MAX_SOURCES = 32
#: the whole document, serialised (UTF-8)
MAX_DOCUMENT_BYTES = 1_000_000
#: positions may lie outside the table (a laser on the next table) but not absurdly far
MAX_ABS_MM = 100_000.0
MAX_EXTRA_PAIRS = 20
MAX_LINE_CHARS = 200
MAX_NOTES_CHARS = 2000
MAX_LABEL_CHARS = 80

#: names Windows will not let a file have (``con.json`` is the console): a layout id is a file name
_WINDOWS_RESERVED = frozenset({"con", "prn", "aux", "nul"}
                              | {f"com{i}" for i in range(1, 10)}
                              | {f"lpt{i}" for i in range(1, 10)})

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


# ── component types (§4) ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class TypeInfo:
    """What the contract says about one component type.

    ``role`` is how the tracer treats it: ``source`` emits a ray; ``mirror`` reflects on its
    front and is blocked from behind; ``splitter`` / ``dichroic`` act on both faces;
    ``transmit`` lets everything through; ``filter`` / ``iris`` / ``shutter`` / ``sample`` /
    ``custom`` depend on ``props``; ``terminal`` ends the ray.
    """

    id: str
    zh: str
    en: str
    aperture_mm: float
    role: str


def _types(*rows: tuple) -> dict[str, TypeInfo]:
    return {r[0]: TypeInfo(*r) for r in rows}


TYPES: dict[str, TypeInfo] = _types(
    ("laser", "激光器", "laser", 2.0, "source"),
    ("fiber_out", "光纤输出", "fiber output", 2.0, "source"),
    ("lamp", "灯", "lamp", 5.0, "source"),
    ("led", "LED", "LED", 5.0, "source"),
    ("mirror", "反射镜", "mirror", 25.4, "mirror"),
    ("beam_splitter", "分束器", "beam splitter", 25.4, "splitter"),
    ("dichroic", "二向色镜", "dichroic mirror", 25.4, "dichroic"),
    ("grating", "光栅", "grating", 25.0, "mirror"),
    ("lens", "透镜", "lens", 25.4, "transmit"),
    ("objective", "物镜", "objective", 10.0, "transmit"),
    ("waveplate", "波片", "waveplate", 12.7, "transmit"),
    ("polarizer", "偏振片", "polarizer", 12.7, "transmit"),
    ("filter", "滤光片", "filter", 25.4, "filter"),
    ("window", "窗片", "window", 25.4, "transmit"),
    ("nd", "ND 滤光片", "ND filter", 25.4, "transmit"),
    ("aotf", "AOTF", "AOTF", 25.4, "transmit"),
    ("prism", "棱镜", "prism", 25.4, "transmit"),
    ("periscope", "潜望镜", "periscope", 25.4, "transmit"),
    ("iris", "光阑", "iris", 25.4, "iris"),
    ("shutter", "快门", "shutter", 12.7, "shutter"),
    ("sample", "样品", "sample", 10.0, "sample"),
    ("detector", "探测器", "detector", 10.0, "terminal"),
    ("camera", "相机", "camera", 10.0, "terminal"),
    ("spectrometer", "光谱仪", "spectrometer", 10.0, "terminal"),
    ("power_meter", "功率计", "power meter", 10.0, "terminal"),
    ("beam_dump", "光阱", "beam dump", 25.4, "terminal"),
    ("blocker", "挡板", "blocker", 25.4, "terminal"),
    ("fiber_in", "光纤耦合", "fiber coupler", 5.0, "terminal"),
    ("custom", "自定义", "custom", 25.4, "custom"),
)

ComponentType = Literal[
    "laser", "fiber_out", "lamp", "led", "mirror", "beam_splitter", "dichroic", "grating",
    "lens", "objective", "waveplate", "polarizer", "filter", "window", "nd", "aotf", "prism",
    "periscope", "iris", "shutter", "sample", "detector", "camera", "spectrometer",
    "power_meter", "beam_dump", "blocker", "fiber_in", "custom",
]

SOURCE_TYPES = frozenset(t for t, i in TYPES.items() if i.role == "source")

assert set(get_args(ComponentType)) == set(TYPES), "ComponentType and TYPES have drifted apart"


def type_name(type_id: str) -> str:
    """The display name of a component type in the active language (the type id if unknown)."""
    info = TYPES.get(type_id)
    return type_id if info is None else tr(info.zh, info.en)


# ── props (§4, "props" column) ─────────────────────────────────────────────────

@dataclass(frozen=True)
class Prop:
    kind: str                       # num | nums | enum | bool | id
    lo: Optional[float] = None
    hi: Optional[float] = None
    lo_open: bool = False           # ``lo`` itself is not allowed
    choices: tuple = ()
    max_items: int = 16
    decimals: int = 3


_WAVELENGTH = Prop("num", lo=0.0, hi=1e7, lo_open=True)        # nm; 1e7 nm = 10 mm covers THz
_WAVELENGTHS = Prop("nums", lo=0.0, hi=1e7, lo_open=True, max_items=16)
_POWER = Prop("num", lo=0.0, hi=1e7)                           # mW
_FRACTION = Prop("num", lo=0.0, hi=1.0, decimals=6)
_AXIS = Prop("num", lo=-360.0, hi=360.0, decimals=2)

PROPS: dict[str, dict[str, Prop]] = {
    "laser": {"wavelength_nm": _WAVELENGTH, "wavelengths_nm": _WAVELENGTHS, "power_mw": _POWER},
    "fiber_out": {"wavelength_nm": _WAVELENGTH, "wavelengths_nm": _WAVELENGTHS,
                  "power_mw": _POWER},
    "lamp": {"wavelength_nm": _WAVELENGTH},
    "led": {"wavelength_nm": _WAVELENGTH},
    "beam_splitter": {"reflect_fraction": _FRACTION},
    "dichroic": {"edge_nm": _WAVELENGTH, "reflects": Prop("enum", choices=("short", "long"))},
    "grating": {"lines_per_mm": Prop("num", lo=0.0, hi=1e5, lo_open=True)},
    "lens": {"focal_mm": Prop("num", lo=-1e5, hi=1e5)},
    "objective": {"focal_mm": Prop("num", lo=-1e5, hi=1e5)},
    "waveplate": {"retardance": Prop("enum", choices=("half", "quarter")),
                  "fast_axis_deg": _AXIS},
    "polarizer": {"axis_deg": _AXIS},
    "filter": {"pass_min_nm": _WAVELENGTH, "pass_max_nm": _WAVELENGTH},
    "nd": {"od": Prop("num", lo=0.0, hi=20.0)},
    "iris": {"open_mm": Prop("num", lo=0.0, hi=500.0, lo_open=True)},
    "shutter": {"closed": Prop("bool")},
    "sample": {"reflects": Prop("bool")},
    "fiber_in": {"continues_at": Prop("id")},
    "custom": {"behavior": Prop("enum", choices=("transmit", "reflect", "stop"))},
}

#: the props that must be there (``laser`` / ``fiber_out`` additionally need one of the two
#: wavelength forms, checked in :func:`validate_props`)
REQUIRED_PROPS: dict[str, tuple[str, ...]] = {"dichroic": ("edge_nm",)}


def _prop_value(type_id: str, key: str, spec: Prop, value: Any) -> Any:
    where = f"props.{key}"
    if spec.kind == "bool":
        if not isinstance(value, bool):
            raise ValueError(tr(f"{where} 必须是 true 或 false", f"{where} must be true or false"))
        return value
    if spec.kind == "enum":
        if value not in spec.choices:
            opts = " / ".join(spec.choices)
            raise ValueError(tr(f"{where} 必须是 {opts} 之一", f"{where} must be one of {opts}"))
        return value
    if spec.kind == "id":
        if not isinstance(value, str):
            raise ValueError(tr(f"{where} 必须是器件 id 字符串", f"{where} must be a component id"))
        value = value.strip()
        if value and not _ID_RE.fullmatch(value):
            raise ValueError(tr(f"{where} 不是合法的 id（只能用字母、数字、下划线、连字符，最长 40）",
                                f"{where} is not a valid id (letters, digits, _ and - only, up to 40)"))
        return value
    if spec.kind == "nums":
        if not isinstance(value, (list, tuple)) or not value:
            raise ValueError(tr(f"{where} 必须是非空的数字列表", f"{where} must be a non-empty list of numbers"))
        if len(value) > spec.max_items:
            raise ValueError(tr(f"{where} 最多 {spec.max_items} 项", f"{where} takes at most {spec.max_items} items"))
        out = [_prop_value(type_id, key, Prop("num", spec.lo, spec.hi, spec.lo_open,
                                              decimals=spec.decimals), v) for v in value]
        if len(set(out)) != len(out):
            raise ValueError(tr(f"{where} 里有重复的值", f"{where} has duplicate values"))
        return out
    # num
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(tr(f"{where} 必须是数字", f"{where} must be a number"))
    v = float(value)
    if not math.isfinite(v):
        raise ValueError(tr(f"{where} 必须是有限的数字", f"{where} must be a finite number"))
    if spec.lo is not None and (v <= spec.lo if spec.lo_open else v < spec.lo):
        bound = f"> {spec.lo:g}" if spec.lo_open else f">= {spec.lo:g}"
        raise ValueError(tr(f"{where} 必须 {bound}", f"{where} must be {bound}"))
    if spec.hi is not None and v > spec.hi:
        raise ValueError(tr(f"{where} 必须 <= {spec.hi:g}", f"{where} must be <= {spec.hi:g}"))
    return _zero(round(v, spec.decimals))


def validate_props(type_id: str, props: Any) -> dict[str, Any]:
    """The normalised ``props`` of a component of *type_id*: only the keys given (``None`` means
    "not given"), numbers rounded, unknown keys, wrong types and out-of-range values refused."""
    if not isinstance(props, dict):
        raise ValueError(tr("props 必须是对象", "props must be an object"))
    specs = PROPS.get(type_id, {})
    unknown = sorted(str(k) for k in props if k not in specs)
    if unknown:
        allowed = ", ".join(sorted(specs)) or tr("（无）", "(none)")
        raise ValueError(tr(f"{type_id} 没有这些 props：{', '.join(unknown)}（可用：{allowed}）",
                            f"{type_id} has no props {', '.join(unknown)} (allowed: {allowed})"))
    out: dict[str, Any] = {}
    for key, value in props.items():
        if value is None:
            continue
        out[key] = _prop_value(type_id, key, specs[key], value)
    # an empty ``continues_at`` is "not given"
    if out.get("continues_at") == "":
        del out["continues_at"]
    for key in REQUIRED_PROPS.get(type_id, ()):
        if key not in out:
            raise ValueError(tr(f"{type_id} 必须有 props.{key}", f"{type_id} needs props.{key}"))
    if type_id in ("laser", "fiber_out"):
        has_one, has_list = "wavelength_nm" in out, "wavelengths_nm" in out
        if has_one == has_list:
            raise ValueError(tr(f"{type_id} 要给 props.wavelength_nm 或 props.wavelengths_nm（二选一）",
                                f"{type_id} needs props.wavelength_nm or props.wavelengths_nm (exactly one)"))
    if type_id == "filter" and "pass_min_nm" in out and "pass_max_nm" in out \
            and out["pass_min_nm"] > out["pass_max_nm"]:
        raise ValueError(tr("props.pass_min_nm 不能大于 props.pass_max_nm",
                            "props.pass_min_nm must not exceed props.pass_max_nm"))
    return out


# ── field helpers ──────────────────────────────────────────────────────────────

def _zero(v: float) -> float:
    """``-0.0`` -> ``0.0`` (JSON would print ``-0.0``)."""
    return v + 0.0


def _no_bool(v: Any) -> Any:
    if isinstance(v, bool):
        raise ValueError(tr("必须是数字", "must be a number"))
    return v


def _clean_line(v: Any) -> Any:
    """One line of text: control characters removed, every run of whitespace one space."""
    return " ".join(_CONTROL.sub("", v).split()) if isinstance(v, str) else v


def _clean_text(v: Any) -> Any:
    """Free text: control characters removed, line breaks kept (as ``\\n``), ends trimmed."""
    if not isinstance(v, str):
        return v
    return _CONTROL.sub("", v.replace("\r\n", "\n").replace("\r", "\n")).strip()


Num = Annotated[float, BeforeValidator(_no_bool), Field(allow_inf_nan=False)]
Line = Annotated[str, BeforeValidator(_clean_line), Field(max_length=MAX_LINE_CHARS)]
LongText = Annotated[str, BeforeValidator(_clean_text), Field(max_length=MAX_NOTES_CHARS)]
Label = Annotated[str, BeforeValidator(_clean_line), Field(max_length=MAX_LABEL_CHARS)]
ItemId = Annotated[str, Field(pattern=ID_PATTERN)]
#: a reference to a device / slot of MAST: only a reference, it need not exist
RefId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.:-]{1,64}$")]


def _http_url(v: str) -> str:
    if v and not re.match(r"^https?://\S+$", v, re.IGNORECASE):
        raise ValueError(tr("datasheet_url 必须是 http:// 或 https:// 开头的网址（或留空）",
                            "datasheet_url must be an http:// or https:// address (or empty)"))
    return v


class _Strict(BaseModel):
    """Unknown keys are refused."""

    model_config = ConfigDict(extra="forbid")


# ── the document ───────────────────────────────────────────────────────────────

class ExtraPair(_Strict):
    key: Annotated[str, BeforeValidator(_clean_line), Field(min_length=1, max_length=40)]
    value: Line = ""


class Annotations(_Strict):
    """What the people who built the table know about a component (§3)."""

    model: Line = ""
    manufacturer: Line = ""
    function: Line = ""
    motorized: bool = False
    actuator: Line = Field("", description="motor / controller model (when motorized)")
    connection: Line = Field("", description="where it is connected: controller, port, channel, "
                                             "satellite, COM port ...")
    datasheet_url: Annotated[Line, AfterValidator(_http_url)] = ""
    notes: LongText = ""
    extra: list[ExtraPair] = Field(default_factory=list, max_length=MAX_EXTRA_PAIRS)


class Links(_Strict):
    """References into MAST. Only references: they are not required to exist when saved (the
    device may be disabled); the current state is attached when the layout is read."""

    device_id: Optional[RefId] = None
    slot: Optional[RefId] = None


class Component(_Strict):
    id: ItemId
    type: ComponentType
    label: Label = ""
    x_mm: Num = Field(description="position, mm, 0.01 resolution")
    y_mm: Num
    angle_deg: Num = Field(0.0, description="direction of the local +x axis, degrees counter-"
                                            "clockwise from +x, normalised to [0, 360)")
    aperture_mm: Optional[Num] = Field(None, description="length of the working surface; "
                                                         "0.1-500, default by type")
    props: dict[str, Any] = Field(default_factory=dict)
    annotations: Annotations = Field(default_factory=Annotations)
    links: Links = Field(default_factory=Links)
    locked: bool = False

    @field_validator("x_mm", "y_mm")
    @classmethod
    def _position(cls, v: float) -> float:
        if abs(v) > MAX_ABS_MM:
            raise ValueError(tr(f"位置不能超过 ±{MAX_ABS_MM:g} mm", f"a position must stay within ±{MAX_ABS_MM:g} mm"))
        return _zero(round(v, 2))

    @field_validator("angle_deg")
    @classmethod
    def _angle(cls, v: float) -> float:
        if abs(v) > 1e6:
            raise ValueError(tr("角度的绝对值不能超过 1e6", "an angle must not exceed 1e6 in absolute value"))
        a = round(v % 360.0, 2)
        return 0.0 if a >= 360.0 else _zero(a)

    @field_validator("aperture_mm")
    @classmethod
    def _aperture(cls, v: Optional[float]) -> Optional[float]:
        if v is None:
            return None
        if not 0.1 <= v <= 500.0:
            raise ValueError(tr("口径必须在 0.1 到 500 mm 之间", "the aperture must be between 0.1 and 500 mm"))
        return _zero(round(v, 2))

    @model_validator(mode="after")
    def _by_type(self) -> "Component":
        self.props = validate_props(self.type, self.props)
        if self.aperture_mm is None:
            self.aperture_mm = TYPES[self.type].aperture_mm
        return self


class TextLabel(_Strict):
    """A free text label on the drawing (a door, a warning, a name of a region)."""

    id: ItemId
    x_mm: Num
    y_mm: Num
    text: Line
    size: Num = Field(1.0, description="relative size, 0.1-20")

    @field_validator("x_mm", "y_mm")
    @classmethod
    def _position(cls, v: float) -> float:
        if abs(v) > MAX_ABS_MM:
            raise ValueError(tr(f"位置不能超过 ±{MAX_ABS_MM:g} mm", f"a position must stay within ±{MAX_ABS_MM:g} mm"))
        return _zero(round(v, 2))

    @field_validator("size")
    @classmethod
    def _size(cls, v: float) -> float:
        if not 0.1 <= v <= 20.0:
            raise ValueError(tr("size 必须在 0.1 到 20 之间", "size must be between 0.1 and 20"))
        return _zero(round(v, 2))


class Table(_Strict):
    width_mm: Num = 1200.0
    height_mm: Num = 900.0
    hole_pitch_mm: Num = 25.0
    hole_offset_mm: Num = 12.5
    grid: Literal["metric", "imperial", "none"] = "metric"

    @field_validator("width_mm", "height_mm")
    @classmethod
    def _size(cls, v: float) -> float:
        if not 10.0 <= v <= 20000.0:
            raise ValueError(tr("平台尺寸必须在 10 到 20000 mm 之间", "the table size must be between 10 and 20000 mm"))
        return _zero(round(v, 2))

    @field_validator("hole_pitch_mm")
    @classmethod
    def _pitch(cls, v: float) -> float:
        if not 1.0 <= v <= 500.0:
            raise ValueError(tr("孔距必须在 1 到 500 mm 之间", "the hole pitch must be between 1 and 500 mm"))
        return _zero(round(v, 3))

    @field_validator("hole_offset_mm")
    @classmethod
    def _offset(cls, v: float) -> float:
        if not 0.0 <= v <= 500.0:
            raise ValueError(tr("孔偏移必须在 0 到 500 mm 之间", "the hole offset must be between 0 and 500 mm"))
        return _zero(round(v, 3))


class Layout(_Strict):
    """One optical layout. ``schema`` is the document version (1)."""

    schema_: Literal[1] = Field(SCHEMA_VERSION, alias="schema")
    id: ItemId
    title: Line = ""
    revision: int = Field(0, ge=0, description="written by the server")
    updated_at: str = Field("", max_length=64, description="written by the server")
    updated_by: str = Field("", max_length=80, description="written by the server")
    table: Table = Field(default_factory=Table)
    components: list[Component] = Field(default_factory=list, max_length=MAX_COMPONENTS)
    texts: list[TextLabel] = Field(default_factory=list, max_length=MAX_TEXTS)
    notes: LongText = ""

    @field_validator("id")
    @classmethod
    def _file_name_safe(cls, v: str) -> str:
        if v.lower() in _WINDOWS_RESERVED:
            raise ValueError(tr(f"{v!r} 是 Windows 的保留名，不能做文件名", f"{v!r} is a reserved Windows name and cannot be a file name"))
        return v

    @model_validator(mode="after")
    def _whole(self) -> "Layout":
        seen: set[str] = set()
        for item in (*self.components, *self.texts):
            if item.id in seen:
                raise ValueError(tr(f"id {item.id!r} 在这张图里重复了", f"the id {item.id!r} is used twice in this layout"))
            seen.add(item.id)
        n_sources = sum(1 for c in self.components if c.type in SOURCE_TYPES)
        if n_sources > MAX_SOURCES:
            raise ValueError(tr(f"光源最多 {MAX_SOURCES} 个（现在 {n_sources}）", f"at most {MAX_SOURCES} sources (this layout has {n_sources})"))
        size = len(json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if size > MAX_DOCUMENT_BYTES:
            raise ValueError(tr(f"整张图序列化后 {size} 字节，超过上限 {MAX_DOCUMENT_BYTES}",
                                f"the layout is {size} bytes when serialised, over the limit of {MAX_DOCUMENT_BYTES}"))
        return self

    def to_dict(self) -> dict[str, Any]:
        """The canonical JSON form (``schema`` key, every field present, normalised)."""
        return self.model_dump(mode="json", by_alias=True)

    def component(self, component_id: str) -> Optional[Component]:
        return next((c for c in self.components if c.id == component_id), None)


def normalize_layout(raw: Any) -> Layout:
    """*raw* (a dict, as a client or a file gives it) as a validated :class:`Layout`; raises
    ``pydantic.ValidationError``."""
    return raw if isinstance(raw, Layout) else Layout.model_validate(raw)


def is_valid_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_ID_RE.fullmatch(value)) and value.lower() not in _WINDOWS_RESERVED


__all__ = [
    "Annotations", "Component", "ComponentType", "ExtraPair", "ID_PATTERN", "Layout", "Links",
    "MAX_COMPONENTS", "MAX_DOCUMENT_BYTES", "MAX_SOURCES", "MAX_TEXTS", "PROPS", "SCHEMA_VERSION",
    "SOURCE_TYPES", "TYPES", "Table", "TextLabel", "TypeInfo", "is_valid_id", "normalize_layout",
    "type_name", "validate_props",
]
