"""Auto-detection + assembly of environment sensors from config and COM scan.

Top-level entry point is :func:`build_environment_sensors`, called from the GUI
``setup()``. It is the single place that decides which concrete sensors the
:class:`~mast.environment.monitor.EnvironmentMonitor` watches:

1. Build every sensor declared in ``environment_sensors.json`` (pinned port,
   user name, alarm thresholds).
2. If ``autodetect`` is on, scan the remaining COM ports: identity queries
   first (``*IDN?`` — Lake Shore temperature instruments, the Lake Shore Model 625
   magnet supply, the Cryomagnetics LM-510 level monitor; see
   :func:`identify_port`), and the binary DL-7 Modbus probe only on a port that
   stayed silent through all of them.
3. Always append the helium / noise placeholders, and a vacuum / temperature
   placeholder for any kind not found — so the Lab Console Environment panel
   keeps showing every row honestly (``N/A`` when no hardware), and the program
   runs with nothing connected.

Nothing here raises: a bad config entry is logged and skipped, a probe failure
is swallowed, and the worst case still returns the placeholder set.
"""

from __future__ import annotations

import logging
from typing import Iterable

from mast.environment.alarm import AlarmSpec
from mast.environment.base import EnvironmentSensor
from mast.environment.config import autodetect_policy, load_config, normalise_sensor
from mast.environment.cryomag_lm510 import (
    LM510LevelSensor,
    LM510Link,
    probe_lm510,
)
from mast.environment.cryomag_lm510 import SUPPORTED_BAUD as LM510_BAUD
from mast.environment.dl7_vacuum import DL7VacuumSensor, probe_dl7
from mast.environment.lakeshore_625 import (
    Model625Link,
    Model625Sensor,
    probe_model625,
)
from mast.environment.lakeshore_625 import SUPPORTED_BAUD as MODEL625_BAUD
from mast.environment.lakeshore_temp import (
    HEALTH_CLEAN,
    HEALTH_UNKNOWN,
    LakeshoreTemperatureSensor,
    default_channels,
    discover_lakeshore,
    is_temperature_model,
    probe_lakeshore,
)
from mast.environment.placeholders import (
    HeliumLevelSensor,
    NoiseSensor,
    TemperatureSensor,
    VacuumSensor,
)
from mast.environment.serial_transport import (
    SerialSettings,
    list_serial_ports,
    open_transport,
)

logger = logging.getLogger(__name__)


def _shared_transport(transports: dict, settings) -> "object":
    """One transport per physical PORT, shared by every sensor on it.

    A multi-input instrument is modelled as several sensors (a Lakeshore 335
    with inputs A and B is two :class:`LakeshoreTemperatureSensor` objects), but
    they are one device on one cable. Windows opens COM ports EXCLUSIVELY, so
    letting each sensor lazily open its own handle means the first one wins and
    every other input is stuck reporting ``unavailable`` forever. Keyed by port
    only — a physical port has exactly one line configuration, so a second entry
    asking for a different baud is a config error, not a second connection.
    """
    key = str(settings.port).upper()
    existing = transports.get(key)
    if existing is not None:
        prev = getattr(existing, "settings", None)
        if prev is not None and prev.label() != settings.label():
            logger.warning(
                "environment: %s already configured as %s; ignoring conflicting "
                "line settings %s (one port, one configuration)",
                key, prev.label(), settings.label())
        return existing
    transport = open_transport(settings)   # lazy — does not touch the port yet
    transports[key] = transport
    return transport


def _shared_link(transports: dict, settings, factory) -> "object":
    """One instrument link (``Model625Link`` / ``LM510Link``) per port.

    The link owns the identity check and the command spacing for the whole
    instrument, so every sensor reading that instrument must go through the SAME
    link — two links on one transport would each verify identity and interleave
    commands without honouring each other's spacing. Stored in the same pool as
    the transports under ``"<PORT>#link"``.
    """
    transport = _shared_transport(transports, settings)
    key = f"{str(settings.port).upper()}#link"
    link = transports.get(key)
    if link is None or getattr(link, "transport", None) is not transport:
        link = factory(transport)
        transports[key] = link
    return link


def _magnet625_settings(port: str, baudrate: int = 9600) -> SerialSettings:
    # The character format is fixed by the instrument (7O1); only the baud is a choice.
    return SerialSettings(port=port, baudrate=int(baudrate), bytesize=7, parity="O",
                          stopbits=1, timeout=0.6)


def _lm510_settings(port: str, baudrate: int = 9600) -> SerialSettings:
    return SerialSettings(port=port, baudrate=int(baudrate), bytesize=8, parity="N",
                          stopbits=1, timeout=0.8)


def _magnet625_sensors(port: str, settings, transports: dict) -> list[EnvironmentSensor]:
    """Field + current readings of a Model 625 (one shared, identity-checked link)."""
    link = _shared_link(transports, settings, lambda t: Model625Link(t, port=port))
    return [Model625Sensor(link, name="magnet_field", quantity="field"),
            Model625Sensor(link, name="magnet_current", quantity="current")]


def _lm510_sensors(port: str, settings, transports: dict) -> list[EnvironmentSensor]:
    """Channel-1 level of an LM-510 (single-channel units are the common case)."""
    link = _shared_link(transports, settings, lambda t: LM510Link(t, port=port))
    return [LM510LevelSensor(link, name="helium_level", channel=1)]


def _build_one(entry: dict, transports: dict | None = None) -> EnvironmentSensor | None:
    """Construct one configured sensor. Returns None on unknown/invalid type."""
    stype = entry.get("type")
    name = entry.get("name") or entry.get("id") or stype or "sensor"
    alarm = AlarmSpec.from_dict(entry.get("alarm"))
    pool = transports if transports is not None else {}
    try:
        if stype == "dl7_vacuum":
            from mast.environment.dl7_vacuum import DL7_BAUD

            port = entry.get("port")
            transport = None
            if port:
                transport = _shared_transport(pool, SerialSettings(
                    port=port, baudrate=DL7_BAUD, bytesize=8, parity="N",
                    stopbits=1, timeout=float(entry.get("timeout", 0.4)),
                ))
            return DL7VacuumSensor(
                name=name,
                port=port,
                address=int(entry.get("address", 7)),
                unit=entry.get("unit", "Pa"),
                alarm=alarm,
                transport=transport,
            )
        if stype == "lakeshore_temp":
            settings = None
            transport = None
            if entry.get("port"):
                settings = SerialSettings(
                    port=entry["port"],
                    baudrate=int(entry.get("baudrate", 57600)),
                    bytesize=int(entry.get("bytesize", 7)),
                    parity=entry.get("parity", "O"),
                    stopbits=float(entry.get("stopbits", 1)),
                    timeout=float(entry.get("timeout", 0.5)),
                )
                transport = _shared_transport(pool, settings)
            return LakeshoreTemperatureSensor(
                name=name,
                settings=settings,
                transport=transport,
                channel=str(entry.get("channel", "A")),
                unit=entry.get("unit", "K"),
                alarm=alarm,
                command=entry.get("command", "KRDG?"),
                model=str(entry.get("model") or ""),
            )
        if stype == "lakeshore_625":
            # READ-ONLY magnet supply. Only the port, the baud and which quantity
            # to report come from the config; nothing in the entry can reach the
            # wire (the driver builds every byte from its own whitelist).
            port = str(entry.get("port") or "").strip()
            baud = int(entry.get("baudrate", 9600))
            if not port or baud not in MODEL625_BAUD:
                logger.warning("environment: %r (lakeshore_625) needs a port and a "
                               "baud in %s — skipped", name, sorted(MODEL625_BAUD))
                return None
            link = _shared_link(pool, _magnet625_settings(port, baud),
                                lambda t: Model625Link(t, port=port))
            return Model625Sensor(link, name=name,
                                  quantity=str(entry.get("quantity", "field")),
                                  alarm=alarm)
        if stype == "cryomag_lm510":
            port = str(entry.get("port") or "").strip()
            baud = int(entry.get("baudrate", 9600))
            channel = int(entry.get("channel", 1))
            if not port or baud not in LM510_BAUD or channel not in (1, 2):
                logger.warning("environment: %r (cryomag_lm510) needs a port, a baud in "
                               "%s and channel 1|2 — skipped", name, sorted(LM510_BAUD))
                return None
            link = _shared_link(pool, _lm510_settings(port, baud),
                                lambda t: LM510Link(t, port=port))
            return LM510LevelSensor(link, name=name, channel=channel, alarm=alarm,
                                    min_interval_s=float(entry.get("min_interval_s", 10.0)))
        if stype == "nanonis_signal":
            # Owned by core.runtime, not by this module: it needs a live
            # ConnectionPool, and environment/ must not import core/. See
            # CoreRuntime._build_nanonis_env_sensors. Skipped SILENTLY — a
            # warning here would fire on every boot of a perfectly valid config.
            return None
    except Exception as exc:
        logger.warning("environment: failed to build sensor %r: %s", name, exc)
        return None
    logger.warning("environment: unknown sensor type %r — skipped", stype)
    return None


def build_sensors_from_config(cfg: dict | None = None,
                              transports: dict | None = None,
                              ) -> tuple[list[EnvironmentSensor], set[str]]:
    """Build all configured sensors. Returns (sensors, used_ports).

    ``transports`` is the per-port pool (see :func:`_shared_transport`). The
    caller may pass its own so configured and auto-detected sensors on the same
    physical port end up on ONE handle.
    """
    cfg = cfg if cfg is not None else load_config()
    sensors: list[EnvironmentSensor] = []
    used_ports: set[str] = set()
    if transports is None:
        transports = {}   # port -> shared transport (see _shared_transport)
    for i, raw in enumerate(cfg.get("sensors") or []):
        entry = normalise_sensor(raw, i)
        s = _build_one(entry, transports)
        if s is not None:
            sensors.append(s)
            if entry.get("port"):
                used_ports.add(str(entry["port"]).upper())
    return sensors, used_ports


def _lakeshore_sensors(port: str, lsci,
                       transports: dict | None = None) -> list[EnvironmentSensor]:
    """Build sensors for healthy controller inputs using their configured labels.

    Enumerate inputs supported by the model, then read labels and health from
    the controller snapshot. Do not invent roles from channel letters or create
    rows for inputs that did not report a healthy measurement.

    If the snapshot cannot be read, fall back to the first model channel and
    its model label. Sensors on one controller share the serial transport, so
    multiple inputs do not compete for exclusive ownership of the same port.
    """
    model = getattr(lsci, "model", "") or "Lakeshore"
    pool = transports if transports is not None else {}
    settings = lsci.settings
    # Lazy — registering the shared transport does not touch the port, so the
    # discover_lakeshore() probe below still gets the bus to itself.
    shared = _shared_transport(pool, settings) if settings is not None else None
    fallback = [LakeshoreTemperatureSensor(
        name=f"{model} ({port})", settings=settings, transport=shared,
        channel=default_channels(model)[0], model=getattr(lsci, "model", "") or "")]
    try:
        from mast.environment.lakeshore_temp import discover_lakeshore

        snap = discover_lakeshore(port)
        # TWO questions, asked separately — they have different answers and
        # different failure modes.
        #
        # 1. Did this input report a reading?  `kelvin > 0`. An input with
        #    nothing wired to it still ANSWERS — a 335 returns +000.000E+00 —
        #    so "not None" would give the panel a permanent 0.00 K row next to
        #    the real one. Zero kelvin is not a measurement.
        # 2. Does the instrument BACK that reading?  `RDGST?` says so, and its
        #    answer is three-state. Only HEALTH_CLEAN — asked, and clean — earns
        #    a row. The test used to be `not c.faults`, which passes HEALTH_
        #    UNKNOWN as well, and HEALTH_UNKNOWN was manufactured out of a
        #    timeout by `int("" or 0)`: an open-circuit / over-range input whose
        #    status query went unanswered was registered as a healthy
        #    thermometer, and fed a conduct's 换样品 temperature gate.
        reported = [c for c in (getattr(snap, "channels", None) or [])
                    if c.kelvin is not None and c.kelvin > 0]
        live = [c for c in reported if c.health == HEALTH_CLEAN]
        unconfirmed = [c for c in reported if c.health == HEALTH_UNKNOWN]
        if unconfirmed:
            # WARNING, not debug. This is an input MAST can see, and is choosing
            # not to vouch for; dropping it silently is how the operator ends up
            # hunting a row that simply stopped appearing. The link is what needs
            # fixing, and only a human can fix it.
            logger.warning(
                "autodetect: %s on %s — %s reported a temperature but its "
                "RDGST? status query never answered, so nothing backs that "
                "number; NOT registering it. Fix the link, or pin the channel "
                "in environment_sensors.json to monitor it regardless.",
                model, port,
                ", ".join(f"{c.channel}={c.kelvin}" for c in unconfirmed))
        if not live:
            # Two different reasons to fall back, and they send the operator to
            # two different places — do not print one when the other is true.
            logger.info(
                "autodetect: %s on %s — %s, using the default input only",
                model, port,
                "no channel confirmed a healthy reading" if reported
                else "no channel reported a reading")
            return fallback
        out: list[EnvironmentSensor] = []
        for c in live:
            label = (c.label or "").strip()
            out.append(LakeshoreTemperatureSensor(
                # The operator's own name wins. Without one, the channel letter
                # is still more use than the model name repeated N times.
                name=(label or f"{model} {c.channel}") + f" ({port})",
                settings=settings, transport=shared, channel=c.channel,
                model=getattr(lsci, "model", "") or ""))
        logger.info("autodetect: %s on %s → %d input(s): %s", model, port,
                    len(out), ", ".join(
                        f"{c.channel}={(c.label or '?').strip()}" for c in live))
        return out
    except Exception as exc:  # noqa: BLE001 — probing is best-effort
        logger.debug("autodetect: %s channel enumeration on %s failed: %s",
                     model, port, exc)
        return fallback


#: Identification results of :func:`identify_port`.
KIND_LAKESHORE_TEMP = "lakeshore_temp"
KIND_LAKESHORE_625 = "lakeshore_625"
KIND_LAKESHORE_OTHER = "lakeshore_other"
KIND_LM510 = "cryomag_lm510"


def identify_port(port: str, *, timeout: float = 0.5) -> tuple[str, object, bool]:
    """``(kind, info, heard)`` for one serial port. Writes only identity queries.

    Order and what each step may write:

    1. ``*IDN?`` CR LF at 57600 / 9600 / 115200, 7O1 (Lake Shore family). An
       ``LSCI`` answer is then classified by MODEL: a temperature instrument
       (``lakeshore_temp``), the Model 625 magnet supply (``lakeshore_625``), or
       anything else (``lakeshore_other`` — reported, never polled).
    2. CR, then ``*IDN?`` CR at the LM-510's 8N1 bauds (``cryomag_lm510``).
    3. ``*IDN?`` CR LF at 19200 / 38400 7O1 — a 625 set to a non-default baud.

    ``heard`` is True if ANY step got bytes back. A port that talked is not a
    Modbus-only DL-7 gauge, so the caller must not send it the binary DL-7 frame
    (probing an instrument's command parser with binary is how a "harmless"
    probe stops being harmless). ``kind`` is ``""`` when nothing was identified.
    """
    heard: list[bytes] = []

    def _rec(b: bytes) -> None:
        if b:
            heard.append(b)

    lsci = probe_lakeshore(port, on_reply=_rec)
    if lsci is not None:
        model = getattr(lsci, "model", "") or ""
        if "625" in model.upper():
            return KIND_LAKESHORE_625, lsci, True
        if is_temperature_model(model):
            return KIND_LAKESHORE_TEMP, lsci, True
        return KIND_LAKESHORE_OTHER, lsci, True
    lm = probe_lm510(port, on_reply=_rec)
    if lm is not None:
        return KIND_LM510, lm, True
    m625 = probe_model625(port, candidates=((19200, 7, "O", 1), (38400, 7, "O", 1)),
                          on_reply=_rec)
    if m625 is not None:
        return KIND_LAKESHORE_625, m625, True
    if heard:
        # What came back, so "answered but was not recognised" can be told apart next time:
        # a truncated "LSCI,M…", wrong-baud junk, or another instrument's answer. A past
        # start-up miss left only the INFO line, not the bytes.
        logger.info("autodetect: %s replies heard but not identified (first %d of %d): %s",
                    port, min(4, len(heard)), len(heard),
                    ", ".join(repr(b[:40]) for b in heard[:4]))
    return "", None, bool(heard)


def autodetect_sensors(skip_ports: set[str] | None = None,
                       transports: dict | None = None) -> list[EnvironmentSensor]:
    """Scan COM ports and return sensors for any known instrument found.

    Recognised: Lake Shore temperature instruments, the Lake Shore Model 625
    magnet supply (read-only field/current), the Cryomagnetics LM-510 level
    monitor (read-only level) and DL-7 vacuum gauges.

    ``skip_ports`` (upper-cased device names) are ports already claimed by
    configured sensors — they are not probed again. ``transports`` is the
    per-port handle pool; every sensor built here joins it so a multi-input
    instrument reads all of its inputs. Never raises.
    """
    skip = {p.upper() for p in (skip_ports or set())}
    pool = transports if transports is not None else {}
    found: list[EnvironmentSensor] = []
    try:
        ports = list_serial_ports()
    except Exception as exc:  # pragma: no cover
        logger.debug("autodetect: port enumeration failed: %s", exc)
        return found
    for info in ports:
        dev = (info.device or "").upper()
        if not dev or dev in skip:
            continue
        # One INFO line per port we are about to write to. Previously these
        # probes left no trace at all (a failed one logged nothing), so nobody could
        # tell from the log that MAST had sent ``*IDN?`` and a Modbus frame to a
        # laser or a temperature controller on that port.
        logger.info("autodetect: probing %s (%s)", info.device,
                    (info.description or "no description").strip() or "no description")
        try:
            # Identity queries FIRST, deliberately. Identifying an ASCII
            # instrument costs one "*IDN?", which a Modbus gauge ignores on a CRC
            # mismatch. probe_dl7 instead writes a raw Modbus frame, and probing
            # an instrument with that puts binary bytes into its command parser —
            # a temperature controller's, or a magnet supply's. So the DL-7
            # frame only goes to a port that stayed SILENT through every identity
            # query.
            kind, ident, heard = identify_port(info.device)
            if kind == KIND_LAKESHORE_TEMP:
                found.extend(_lakeshore_sensors(info.device, ident, pool))
                skip.add(dev)
                continue
            if kind == KIND_LAKESHORE_625:
                found_at = ident[0] if isinstance(ident, tuple) else ident.settings
                logger.info("autodetect: Lake Shore Model 625 magnet supply on %s "
                            "(read-only: field + current)", info.device)
                found.extend(_magnet625_sensors(
                    info.device, _magnet625_settings(info.device, found_at.baudrate), pool))
                skip.add(dev)
                continue
            if kind == KIND_LM510:
                logger.info("autodetect: Cryomagnetics LM-510 level monitor on %s "
                            "(read-only, never starts a measurement)", info.device)
                found.extend(_lm510_sensors(
                    info.device, _lm510_settings(info.device, ident[0].baudrate), pool))
                skip.add(dev)
                continue
            if kind == KIND_LAKESHORE_OTHER:
                logger.warning("autodetect: %s answered as Lake Shore %s, which MAST has "
                               "no driver for — not polled", info.device,
                               getattr(ident, "model", "?"))
                skip.add(dev)
                continue
            if heard:
                logger.info("autodetect: %s answered but was not recognised; the DL-7 "
                            "Modbus probe is skipped for a port that talks", info.device)
                continue
            if probe_dl7(info.device):
                from mast.environment.dl7_vacuum import DL7_BAUD

                logger.info("autodetect: DL-7 vacuum gauge on %s", info.device)
                found.append(DL7VacuumSensor(
                    name=f"DL-7 真空计 ({info.device})", port=info.device,
                    transport=_shared_transport(pool, SerialSettings(
                        port=info.device, baudrate=DL7_BAUD, bytesize=8,
                        parity="N", stopbits=1, timeout=0.4))))
                skip.add(dev)
            else:
                logger.debug("autodetect: %s answered neither as a Lakeshore nor as a DL-7",
                             info.device)
        except Exception as exc:  # pragma: no cover - probing is best-effort
            logger.debug("autodetect: probe of %s failed: %s", info.device, exc)
    return found


def build_environment_sensors(*, include_placeholders: bool = True,
                              cfg: dict | None = None,
                              reserved_names: Iterable[str] | None = None,
                              ) -> list[EnvironmentSensor]:
    """Full sensor list for the EnvironmentMonitor. Never raises.

    Configured sensors + auto-detected sensors + placeholders for kinds not
    found. With nothing connected this returns the four placeholders (all
    ``status="unavailable"``) and the program runs normally.

    ``reserved_names`` are sensor names the CALLER is going to append after this
    returns (core.runtime's Nanonis-backed sensors, which need a ConnectionPool
    this module must not know about). No placeholder is emitted for them.
    Without this the dedup below would keep both, renaming the real one to
    ``helium_level#2`` — the panel would show the placeholder's permanent "N/A"
    under the expected name and hide the working gauge behind a name nobody
    queries.
    """
    reserved = {str(n) for n in (reserved_names or ())}
    try:
        cfg = cfg if cfg is not None else load_config()
    except Exception as exc:  # pragma: no cover
        logger.warning("environment: config load failed, using placeholders: %s", exc)
        cfg = {"autodetect": False, "sensors": []}

    sensors: list[EnvironmentSensor] = []
    # ONE handle pool for the whole assembly, config and autodetect alike: a
    # port has one cable, and every sensor reachable through it must share the
    # one handle Windows will grant.
    transports: dict = {}
    try:
        configured, used_ports = build_sensors_from_config(cfg, transports)
        sensors.extend(configured)
        scan, why = autodetect_policy(cfg)
        if scan:
            sensors.extend(autodetect_sensors(skip_ports=used_ports,
                                              transports=transports))
        else:
            logger.info("autodetect: serial scan skipped - %s", why)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("environment: sensor assembly failed: %s", exc)
        sensors = []

    if include_placeholders:
        has_vacuum = any(isinstance(s, (DL7VacuumSensor, VacuumSensor)) for s in sensors)
        has_temp = any(isinstance(s, (LakeshoreTemperatureSensor, TemperatureSensor))
                       for s in sensors)
        has_helium = any(isinstance(s, (LM510LevelSensor, HeliumLevelSensor)) for s in sensors)
        if not has_vacuum:
            sensors.append(VacuumSensor())
        if not has_temp:
            sensors.append(TemperatureSensor())
        # Helium has a driver now (Cryomagnetics LM-510); noise still has none.
        # A placeholder is added only for a kind nothing real covers — and not
        # when the caller is about to supply a real one (a level meter wired into
        # a Nanonis analogue input arrives as a ``nanonis_signal`` sensor built by
        # core.runtime). Adding the placeholder next to a real LM-510 named
        # "helium_level" would make the dedup below rename one of them.
        if not has_helium and HeliumLevelSensor().name() not in reserved:
            sensors.append(HeliumLevelSensor())
        if NoiseSensor().name() not in reserved:
            sensors.append(NoiseSensor())

    # Guard against name collisions (monitor keys by name()).
    seen: set[str] = set()
    deduped: list[EnvironmentSensor] = []
    for s in sensors:
        try:
            nm = s.name()
        except Exception:  # pragma: no cover
            continue
        base = nm
        n = 2
        while nm in seen:
            nm = f"{base}#{n}"
            n += 1
        if nm != base:
            # rare; wrap to keep a stable unique name without mutating driver
            s = _RenamedSensor(s, nm)
        seen.add(nm)
        deduped.append(s)
    return deduped


def _lakeshore_suggestions(snap, port: str) -> list[dict]:
    """Ready-to-persist config entries for every input of a discovered unit.

    Names come from the instrument's own ``INNAME?`` labels (the user already
    typed "SPM" / "Magnet" on the front panel) rather than a generated string —
    adoption should not throw away naming the user has already done. The line
    parameters that actually answered are pinned so later boots skip the
    auto-baud search.
    """
    s = getattr(snap, "settings", None)
    line = {
        "baudrate": getattr(s, "baudrate", 57600),
        "bytesize": getattr(s, "bytesize", 7),
        "parity": getattr(s, "parity", "O"),
        "stopbits": getattr(s, "stopbits", 1),
    }
    model = snap.model or "Lakeshore"
    out: list[dict] = []
    for ch in snap.channels:
        out.append({
            "id": f"lakeshore_{port}_{ch.channel}".lower(),
            "name": ch.label.strip() or f"{model} {ch.channel}",
            "type": "lakeshore_temp",
            "port": port,
            "channel": ch.channel,
            "unit": "K",
            # Kept so a later boot knows which instrument this is without asking it:
            # temperature control is offered only for a Model 335.
            "model": snap.model or "",
            **line,
        })
    return out


def _describe_magnet625(port: str, ident) -> dict:
    """Discovery entry for a Model 625: identity, full read-only state, suggestions."""
    found_at = ident[0] if isinstance(ident, tuple) else ident.settings
    settings = _magnet625_settings(port, found_at.baudrate)
    out: dict = {"kind": KIND_LAKESHORE_625, "identified": True,
                 "model": "MODEL625", "idn": getattr(ident, "idn", "")}
    link = Model625Link(open_transport(settings), port=port)
    try:
        st = link.read_state()
        out.update({"model": st.model or "MODEL625", "idn": st.idn,
                    "firmware": st.firmware, "serial_number": st.serial_number,
                    "magnet": st.as_dict()})
    except Exception as exc:  # noqa: BLE001 - identity alone is still worth showing
        logger.debug("discover: 625 state read on %s failed: %s", port, exc)
    finally:
        link.close()
    line = {"baudrate": settings.baudrate}
    out["suggested_sensors"] = [
        {"id": f"magnet625_{port}_field".lower(), "name": "magnet_field",
         "type": KIND_LAKESHORE_625, "port": port, "quantity": "field", **line},
        {"id": f"magnet625_{port}_current".lower(), "name": "magnet_current",
         "type": KIND_LAKESHORE_625, "port": port, "quantity": "current", "unit": "A",
         **line},
    ]
    return out


def _describe_lm510(port: str, ident) -> dict:
    """Discovery entry for an LM-510: identity, channel-1 state, suggestion."""
    settings = _lm510_settings(port, ident[0].baudrate)
    fields = ident[1] if isinstance(ident, tuple) else {}
    out: dict = {"kind": KIND_LM510, "identified": True,
                 "model": fields.get("model", "LM-510"), "idn": fields.get("idn", ""),
                 "firmware": fields.get("firmware", ""),
                 "serial_number": fields.get("serial_number", "")}
    link = LM510Link(open_transport(settings), port=port)
    try:
        out["level_meter"] = link.read_state(1).as_dict()
    except Exception as exc:  # noqa: BLE001
        logger.debug("discover: LM-510 state read on %s failed: %s", port, exc)
    finally:
        link.close()
    out["suggested_sensors"] = [
        {"id": f"lm510_{port}_1".lower(), "name": "helium_level",
         "type": KIND_LM510, "port": port, "channel": 1,
         "baudrate": settings.baudrate},
    ]
    return out


def discover_devices(*, timeout: float = 0.5) -> list[dict]:
    """Probe every serial port and DESCRIBE what answered. Persists nothing.

    This is the read-only half of the "扫描设备接口" flow: it identifies each
    port and reads enough live state that the confirmation dialog can show the
    user what was found (model, per-input label + temperature, heater state)
    before anything is written to the config. Adoption is a separate, explicit
    step — discovery never changes what MAST monitors.

    The caller must have freed the serial bus first (see the admin route): a
    port the live monitor still holds cannot be opened here.
    """
    try:
        ports = list_serial_ports()
    except Exception as exc:  # pragma: no cover - OS quirk
        logger.debug("discover: port enumeration failed: %s", exc)
        return []

    try:
        configured = {
            str(s.get("port", "")).upper()
            for s in (load_config().get("sensors") or []) if isinstance(s, dict)
        }
    except Exception:  # pragma: no cover - config is best-effort
        configured = set()

    results: list[dict] = []
    for info in ports:
        port = info.device or ""
        if not port:
            continue
        # An operator-started scan, but the same writes to the port as the boot-time
        # auto-detection: it gets the same trace.
        logger.info("autodetect: probing %s (manual device scan)", port)
        entry: dict = {
            "port": port,
            "description": info.description or "",
            "hwid": info.hwid or "",
            "kind": "",
            "identified": False,
            "already_configured": port.upper() in configured,
            "suggested_sensors": [],
        }
        try:
            # Same ordering rule as autodetect_sensors: identity queries first,
            # the binary DL-7 frame only to a port that stayed silent.
            kind, ident, heard = identify_port(port, timeout=timeout)
            snap = (discover_lakeshore(port, timeout=timeout)
                    if kind == KIND_LAKESHORE_TEMP else None)
            if kind == KIND_LAKESHORE_625:
                entry.update(_describe_magnet625(port, ident))
            elif kind == KIND_LM510:
                entry.update(_describe_lm510(port, ident))
            elif kind == KIND_LAKESHORE_OTHER:
                entry.update({"kind": KIND_LAKESHORE_OTHER, "identified": True,
                              "model": getattr(ident, "model", ""),
                              "idn": getattr(ident, "idn", "")})
            elif snap is not None:
                entry.update({
                    "kind": "lakeshore_temp",
                    "identified": True,
                    "model": snap.model,
                    "idn": snap.idn,
                    "firmware": snap.firmware,
                    "serial_number": snap.serial_number,
                    "any_heater_on": snap.any_heater_on,
                    "channels": [c.as_dict() for c in snap.channels],
                    "outputs": [o.as_dict() for o in snap.outputs],
                    "suggested_sensors": _lakeshore_suggestions(snap, port),
                })
            elif not heard and probe_dl7(port, timeout=timeout):
                entry.update({
                    "kind": "dl7_vacuum",
                    "identified": True,
                    "model": "DL-7",
                    "suggested_sensors": [{
                        "id": f"dl7_{port}".lower(),
                        "name": f"DL-7 真空计 ({port})",
                        "type": "dl7_vacuum", "port": port,
                        "address": 7, "unit": "Pa",
                    }],
                })
        except Exception as exc:  # noqa: BLE001 - one bad port can't kill the scan
            logger.debug("discover: probe of %s failed: %s", port, exc)
        results.append(entry)
    return results


def is_real_sensor(sensor) -> bool:
    """True if THIS sensor is a real driver rather than a placeholder.

    Split out of :func:`has_real_sensors` (2026-08-14) because a second caller
    needs the same judgement per-sensor rather than per-set: the public
    temperature port has to answer "does this machine have a thermometer at
    all" (``no_sensor`` — waiting will never succeed) separately from "the
    thermometer is not answering right now" (``unavailable`` — freeing the COM
    port fixes it), and a placeholder is exactly the first case. Deriving both
    from one predicate keeps that verdict identical to the one that decides
    whether the archive loop runs — two different answers to "is there real
    hardware here" would be worse than either answer alone.

    ``None`` (no sensor object to inspect) is NOT "placeholder" — it is
    "unknown", and callers that care must keep those apart themselves.
    """
    if sensor is None:
        return False
    inner = getattr(sensor, "_inner", sensor)  # unwrap _RenamedSensor
    if isinstance(inner, (DL7VacuumSensor, LakeshoreTemperatureSensor)):
        return True
    # Nanonis-backed sensors (tunnelling current mirrored from the
    # InstrumentState cache, or a gauge wired into an analogue input) are
    # real readings too. Without this a machine whose only instrumentation
    # is the Nanonis controller — no serial gauges at all — would be judged
    # "nothing connected" and the archive loop would never start, so not
    # even the readings it CAN take would be recorded. Declared by an
    # attribute rather than an isinstance list so environment/ does not have
    # to import every future backend.
    return bool(getattr(inner, "counts_as_real", False))


def has_real_sensors(sensors) -> bool:
    """True if any sensor is a real serial driver rather than a placeholder.

    This predicate decides whether the :class:`EnvironmentMonitor` archive +
    alarm loop runs at all, so the boot path (``core.runtime``) and the live
    rescan path (``api.routes.admin``) must agree on it — hence one definition
    here instead of a copy in each caller.
    """
    return any(is_real_sensor(s) for s in (sensors or []))


class _RenamedSensor(EnvironmentSensor):
    """Adapter giving a sensor a unique name on collision (defensive only)."""

    def __init__(self, inner: EnvironmentSensor, name: str):
        self._inner = inner
        self._name = name

    def name(self) -> str:
        return self._name

    def read(self):
        return self._inner.read()


__all__ = [
    "build_environment_sensors",
    "build_sensors_from_config",
    "autodetect_sensors",
    "identify_port",
    "discover_devices",
    "has_real_sensors",
    "is_real_sensor",
]
