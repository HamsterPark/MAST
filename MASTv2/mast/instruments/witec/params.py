"""WITec Control COM Automation (BUCS) — parameter paths, interface ids, guard tables.

Every constant says where it comes from. Source tags:

``D3``          docs/v2/design/optical_station_witec.md §3 (read-only survey of a WITec PC)
``SDK:<file>``  WITecSDK 1.4.2, ``WITecSDK/Modules/<file>.py`` / ``WITecSDK/Parameters/…``
                (MIT, shipped with WITec Suite 7.0 under ``COM Automation\\Python``)
``CHM:<page>``  ``WITecSuiteHelp.chm`` (Suite 7.0): wccominterfaces, wccomsubsystems,
                wccomstatuscontainer, wccomremoteaccess, wcspectrograph, wcseriesslow, lasercontrol …
``R-api n/6``   survey notes on the WITec COM API, supplementary note n of 6
``R-hist``      survey notes on the WITec logs: the ``MCApp param`` paths listed in the start-up block
                of MicroscopeControl logs, and the ``COMInterface.SetValue(...)`` calls seen
                in them

**The paths here are what the documentation and the vendor SDK say. The authoritative list is the
parameter table of the machine itself** — export it with ``scripts/witec/probe_witec_com.py`` and
compare before trusting a path on a new configuration. Anything optional is looked up with
``client.exists(path)`` first; the driver never assumes a path exists.

Nothing in this module imports comtypes.
"""

from __future__ import annotations

__all__ = [
    "TYPELIB_ID", "TYPELIB_VERSION", "CLSID_CBUCSCORE", "APPID_BUCS", "PROGID",
    "INTERFACE_IIDS", "IID_KIND",
    "SEP", "ROOT_APPLICATION_CONTROL", "ROOT_USER", "ROOT_MICROSCOPE", "ROOT_STATUS",
    "PROGRAM_VERSION", "CURRENT_FILE_NAME", "COM_ACCEPTS_REMOTE",
    "IS_SEQUENCER_ACTIVE", "ACTIVE_SEQUENCER_NAME", "ACTIVE_SEQUENCER_ACTIVITY",
    "SEQUENCER_STATUS_OR_RESULT", "DATA_CHANNEL_PREFIX",
    "STOP_SEQUENCER", "SINGLE_SPECTRUM_INTEGRATION", "SINGLE_SPECTRUM_ACCUMULATIONS",
    "SINGLE_SPECTRUM_START", "MAX_Z_STEPPER_TRAVEL",
    "SERIES_INTEGRATION", "SERIES_ACCUMULATIONS", "ANALYZER_SERIES_STEPS",
    "ANALYZER_SERIES_START",
    "AUTOSAVE_PREFIX", "AUTOSAVE_STORE", "AUTOSAVE_START_DIR", "AUTOSAVE_FILE_NAME",
    "AUTOSAVE_FILE_NUMBER", "AUTOSAVE_DIR_MODE", "AUTOSAVE_STORE_MODE",
    "AUTOSAVE_OVERWRITE_MODE",
    "SILENT_PREFIX", "SILENT_PARAMETER", "SILENT_INFORMATION", "SILENT_DONE", "SILENT_TEXT",
    "SILENT_ERROR", "SILENT_START",
    "spectrograph_path", "SPECTROGRAPH_FIELDS",
    "BEAM_STATE", "BEAM_TRIGGERS", "ANALYZER", "ANALYZER_COUPLER", "FIELD_STOP", "APERTURE_STOP",
    "LASER_COUNT", "LASER_SELECT_NONE", "laser_select_path", "LASER_POWER", "LASER_WAVELENGTH",
    "LASER_SHUTTER", "LASER_HAS_SHUTTER", "LASER_POWER_CORRECTION",
    "STAGE_IS_VIRTUAL", "XY_STATE", "XY_DESIRED_X", "XY_DESIRED_Y", "XY_CURRENT_X",
    "XY_CURRENT_Y", "XY_MOVE", "XY_STOP", "XY_SPEED", "Z_STATE", "Z_DESIRED", "Z_CURRENT",
    "Z_MOVE", "Z_STOP", "Z_SPEED", "Z_IN_USER_SPACE",
    "STAGE_STATE_REACHED", "STAGE_STATE_IDLE", "STAGE_STATE_STOPPED", "STAGE_STATE_FAILED",
    "STAGE_STATE_IN_USE",
    "VIDEO_FILE_NAME", "VIDEO_TO_FILE", "VIDEO_ACQUIRE", "AUTOFOCUS_EXECUTE", "AUTOFOCUS_STATUS",
    "VIDEO_CAL_WIDTH_UM", "VIDEO_CAL_HEIGHT_UM", "VIDEO_CAL_ROTATION_DEG",
    "VIDEO_PROBE_X", "VIDEO_PROBE_Y",
    "OBJECTIVE_INFORMATION", "OBJECTIVE_MAGNIFICATION", "OBJECTIVE_FOCUS_DEPTH",
    "STATE_NAME", "DETECTOR_OUTPUT_CCD1",
    "DANGEROUS_GROUPS", "DANGEROUS_PATHS", "COORDINATE_REZERO_LEAVES",
    "PROBE_READ_PATHS", "FEATURE_PATHS", "REQUIRED_FOR_ACQUISITION",
]

SEP = "|"

# ── COM registration facts ───────────────────────────────────────────────────────

#: Type library of the BUCS COM server — registered for win32 only (D3, R-api 1/6).
#: ``comtypes.client.GetModule((TYPELIB_ID, 1, 0))`` generates ``comtypes.gen.BasicUniversalCOMServerLib``.
TYPELIB_ID = "{E84FEF4C-DD2F-4B8D-9C2F-D016FEBB01F4}"
TYPELIB_VERSION = (1, 0)
#: ``CBUCSCore`` — "Each client should create one entity of this class" (CHM:wccomclasses).
CLSID_CBUCSCORE = "{C45E77CE-3D66-489A-B5E2-159F443BD1AA}"
APPID_BUCS = "{9A78BD06-0BB2-4DAA-A8BA-389BFD6AB23C}"
PROGID = "WITec.COMAutomation"

#: Interface GUIDs (CHM:wccominterfaces, "Table of interface GUIDs").
INTERFACE_IIDS = {
    "IBUCSSubSystemsList": "512E8C62-F2C9-4840-8C5C-746E7FCE3B5B",
    "IBUCSCore": "78D9E3C8-7E0A-4788-B4D8-EF22365D3648",
    "IBUCSAccess": "594ACBE6-938A-411F-A62E-E06FE6DBD35C",
    "IBUCSSubSystemInfo": "A548CB11-959F-46C0-BACD-C813837CB9C4",
    "IBUCSSingleValue": "A75ED3DF-D774-412C-8DF3-B7640EAE0551",
    "IBUCSSingleValueHasLimits": "5C4AF664-B397-4D35-8D98-8E2AE222A8EB",
    "IBUCSInt": "EFAE9411-A8E0-461D-A5F4-4887595AA830",
    "IBUCSIntMinMaxValues": "67B7266A-9EC3-4417-A900-7FCE26FD3AD6",
    "IBUCSFloat": "3EBB7227-74F9-4A0D-9AC9-7E3327AB5221",
    "IBUCSFloatMinMaxValues": "3D750FA2-17AB-4D75-AAFA-8D6DD5717289",
    "IBUCSString": "90C0EA65-0483-46BB-80FD-B1B536D73FC4",
    "IBUCSBool": "08862E7F-BE29-4DCF-B2FC-55A3DFAE33F7",
    "IBUCSTrigger": "923FC802-04D3-4BEE-AE63-A349051FE2E8",
    "IBUCSEnum": "CC1BD98A-2D74-4242-B5F9-0288FC58E339",
    "IBUCSStatusContainer": "5CAF623C-976F-46DC-9624-2F685B00D293",
    "IBUCSFillDataArray": "EC6F2072-5998-4318-8B49-6F4E995407E6",
}

#: Default-interface id (as returned by ``GetSubSystemNameAndIId``, no braces) → parameter kind.
#: SDK:Parameters/__init__.py ``IIDToParamClass``.
IID_KIND = {
    INTERFACE_IIDS["IBUCSInt"]: "int",
    INTERFACE_IIDS["IBUCSBool"]: "bool",
    INTERFACE_IIDS["IBUCSFloat"]: "float",
    INTERFACE_IIDS["IBUCSTrigger"]: "trigger",
    INTERFACE_IIDS["IBUCSString"]: "string",
    INTERFACE_IIDS["IBUCSEnum"]: "enum",
    INTERFACE_IIDS["IBUCSStatusContainer"]: "status",
}

# ── roots (CHM:wccomsubsystems) ──────────────────────────────────────────────────

ROOT_APPLICATION_CONTROL = "ApplicationControl|"
ROOT_USER = "UserParameters|"
ROOT_MICROSCOPE = "MultiComm|MicroscopeControl|"      # the Video Control window / hardware
ROOT_STATUS = "Status|"

_MC = ROOT_MICROSCOPE
_UP = ROOT_USER

# ── status containers, read-only (CHM:wccomstatuscontainer) ──────────────────────

PROGRAM_VERSION = "Status|Software|Application|ProgramVersion"             # D3, SDK:WITecControlVersionTester
CURRENT_FILE_NAME = "Status|Software|Application|CurrentFileName"          # SDK:ProjectCreatorSaver (E_NOTIMPL before the first save)
COM_ACCEPTS_REMOTE = "Status|Software|COMAutomation|AcceptsRemoteConnection"   # "true if the server allows write access"
IS_SEQUENCER_ACTIVE = "Status|Software|Sequencers|IsASequencerActive"      # D3, SDK:ActiveSequencer — the busy flag
#: E_ACCESSDENIED while no sequencer is running (CHM:wccomstatuscontainer) → the driver treats it as "no value"
ACTIVE_SEQUENCER_NAME = "Status|Software|Sequencers|ActiveSequencer|Name"
ACTIVE_SEQUENCER_ACTIVITY = "Status|Software|Sequencers|ActiveSequencer|CurrentActivity"
#: "Running" / "Finished" / "Warning" / "Error" / "StoppedByUser" (SDK:ActiveSequencer60, WITec Control ≥ 6.0)
SEQUENCER_STATUS_OR_RESULT = "Status|Software|Sequencers|StatusOrResult"
DATA_CHANNEL_PREFIX = "Status|Hardware|Controller|DataChannels|"            # SDK:HelperStructs.datachannelpath

# ── user parameters (Control tree) ───────────────────────────────────────────────

STOP_SEQUENCER = _UP + "StopSequencer"                                     # D3, SDK:ActiveSequencer (trigger)
#: ``UserParameters|SequencerSingleSpectrum`` is the GUI's own single-spectrum sequencer (D3, SDK:SingleSpectrum).
#: The driver acquires through the *silent* sequencer below; these are read for provenance only.
SINGLE_SPECTRUM_INTEGRATION = _UP + "SequencerSingleSpectrum|IntegrationTime"       # seconds (CHM:wcsinglespectrum "Integration Time [s]")
SINGLE_SPECTRUM_ACCUMULATIONS = _UP + "SequencerSingleSpectrum|NrOfAccumulations"
SINGLE_SPECTRUM_START = _UP + "SequencerSingleSpectrum|Start"                       # trigger — NOT used by the driver
#: ± range of the software Z axis is this value / 2 in µm (SDK:ZAxis ``SoftwareZConstraints``; default 200 → ±100 µm)
MAX_Z_STEPPER_TRAVEL = _UP + "HardwareConstraints|MaximumZStepperTravel"

# Slow series — the Analyzer series is a full 0–360° rotation in N steps (CHM:wcseriesslow; SDK:Polarization)
SERIES_INTEGRATION = _UP + "SequencerTimeSeriesSlow|SpectrumAcquisition|IntegrationTime"   # seconds
SERIES_ACCUMULATIONS = _UP + "SequencerTimeSeriesSlow|SpectrumAcquisition|Accumulations"
ANALYZER_SERIES_STEPS = _UP + "SequencerTimeSeriesSlow|AnalyzerSeries|NumberOfAnalyzerValues"
ANALYZER_SERIES_START = _UP + "SequencerTimeSeriesSlow|AnalyzerSeries|StartAnalyzerSeries"  # trigger

# Auto-save of the WITec project (.wip) — SDK:ProjectCreatorSaver
AUTOSAVE_PREFIX = _UP + "AutoSaveProject|"
AUTOSAVE_STORE = AUTOSAVE_PREFIX + "StoreProject"            # trigger
AUTOSAVE_START_DIR = AUTOSAVE_PREFIX + "StartDirectory"      # string
AUTOSAVE_FILE_NAME = AUTOSAVE_PREFIX + "FileName"            # string, without ".wip"
AUTOSAVE_FILE_NUMBER = AUTOSAVE_PREFIX + "FileNumber"        # string
AUTOSAVE_DIR_MODE = AUTOSAVE_PREFIX + "DirectoryMode"        # enum: 0 = no sub-directory … 4 (SDK ``SubDirectory``)
AUTOSAVE_STORE_MODE = AUTOSAVE_PREFIX + "StoreMode"          # enum: 1 = clear the project after storing (SDK ``ClearAfterStore``)
AUTOSAVE_OVERWRITE_MODE = AUTOSAVE_PREFIX + "OverwriteMode"  # enum: 1 = overwrite existing (SDK ``OverwriteExisting``)

# Silent spectrum — a spectrum straight back over COM as WITEC_TRUEMATCH_ASCII text (Control ≥ 5.2).
# NB the root is ``MultiComm|SequencerSingleSpectrum|``, not ``MultiComm|MicroscopeControl|``. D3, SDK:SilentSpectrum
SILENT_PREFIX = "MultiComm|SequencerSingleSpectrum|"
SILENT_PARAMETER = SILENT_PREFIX + "SilentSpectrumAcquisitionParameter"   # string: "IntegrationTime <s>\n NumberOfAccumulations <n>\n"
SILENT_INFORMATION = SILENT_PREFIX + "SilentSpectrumAcquisitionInformation"
SILENT_DONE = SILENT_PREFIX + "SilentSpectrumSequenceDone"                # bool — the spectrum is available (the sequencer may still be cleaning up)
SILENT_TEXT = SILENT_PREFIX + "SilentSpectrumAsText"                      # string — WITEC_TRUEMATCH_ASCII
SILENT_ERROR = SILENT_PREFIX + "SilentSpectrumError"                      # string — "Ok" on success
SILENT_START = SILENT_PREFIX + "StartSilentSpectrumAcquisition"           # trigger

# Spectrograph n (1–3): ``Grating`` is "Only available on special systems" (CHM:wcspectrograph) — often absent
SPECTROGRAPH_FIELDS = ("Grating", "CenterWavelength", "SpectralCenter", "SpectralUnit")   # SDK:Spectrograph


def spectrograph_path(n: int, field: str) -> str:
    """``UserParameters|Spectrograph<n>|<field>`` (SDK:Spectrograph). Units: nm / nm / see SpectralUnit / enum."""
    if field not in SPECTROGRAPH_FIELDS:
        raise ValueError(f"unknown spectrograph field {field!r}; known: {SPECTROGRAPH_FIELDS}")
    return f"{_UP}Spectrograph{int(n)}{SEP}{field}"


# ── video-control window: optics (MultiComm|MicroscopeControl|…) ──────────────────

BEAM_STATE = _MC + "BeamPath|State"                          # string (SDK:BeamPath)
#: skill-level beam-path name → trigger (SDK:BeamPath, SDK:BeamPath51; R-hist 88-parameter list)
BEAM_TRIGGERS = {
    "raman": _MC + "BeamPath|SetStateRaman",
    "video": _MC + "BeamPath|SetStateVideo",
    "all_off": _MC + "BeamPath|SetStateAllOff",
    "confocal": _MC + "BeamPath|SetStateConfocal",
    "white_light": _MC + "BeamPath|SetStateWhiteLightMeasurement",
}
ANALYZER = _MC + "OpticControl|Analyzer"                     # float, degrees; D3, SDK:Polarization, R-hist
ANALYZER_COUPLER = _MC + "OpticControl|AnalyzerCoupler"      # bool: analyzer in the beam path
FIELD_STOP = _MC + "OpticControl|FieldStop"                  # float, % (SDK:ApertureFieldStop)
APERTURE_STOP = _MC + "OpticControl|ApertureStop"            # float, %

# ── lasers ─────────────────────────────────────────────────────────────────────────

LASER_COUNT = _MC + "Laser|NumberOfLasers"                   # int (SDK:LaserManager)
LASER_SELECT_NONE = _MC + "Laser|SelectNoLaser"              # trigger


def laser_select_path(index: int) -> str:
    """``…|Laser|SelectLaser<index>`` trigger (D3: ``SelectLaser0..4`` on the WITec PC)."""
    return f"{_MC}Laser{SEP}SelectLaser{int(index)}"


LASER_POWER = _MC + "Laser|Selected|Power"                   # float, mW at the sample ("Laser Power [mW]"); "not available" for the 355/1064 lines (R-hist)
LASER_WAVELENGTH = _MC + "Laser|Selected|Wavelength"         # float, nm
LASER_SHUTTER = _MC + "Laser|Selected|Shutter"               # bool, True = open
LASER_HAS_SHUTTER = _MC + "Laser|Selected|HasShutter"        # bool (WITec Control ≥ 5.2)
LASER_POWER_CORRECTION = _MC + "Laser|Selected|PowerCorrectionFactor"   # float (≥ 6.1): power at sample / power in fibre

# ── motorised XYZ stage (µm) ─────────────────────────────────────────────────────

_XY = _MC + "MotorizedXYZ|XYAxes|"
_Z = _MC + "MotorizedXYZ|ZAxis|"
STAGE_IS_VIRTUAL = _MC + "MotorizedXYZ|IsVirtual"            # R-hist
XY_STATE = _XY + "State"                                     # string, see STAGE_STATE_* (SDK:XYAxes)
XY_DESIRED_X = _XY + "DesiredSamplePositionX"
XY_DESIRED_Y = _XY + "DesiredSamplePositionY"
XY_CURRENT_X = _XY + "CurrentSamplePositionX"
XY_CURRENT_Y = _XY + "CurrentSamplePositionY"
XY_MOVE = _XY + "MoveToDesiredSamplePosition"                # trigger
XY_STOP = _XY + "Stop"                                       # trigger
XY_SPEED = _XY + "Speed"                                     # µm/s
Z_STATE = _Z + "State"
Z_DESIRED = _Z + "DesiredSamplePosition"                     # "software" position — the one the ± travel limit applies to (SDK:ZAxis)
Z_CURRENT = _Z + "CurrentSamplePosition"
Z_MOVE = _Z + "MoveToDesiredSamplePosition"                  # trigger
Z_STOP = _Z + "Stop"                                         # trigger
Z_SPEED = _Z + "Speed"
Z_IN_USER_SPACE = _Z + "ZAxisInUserSpace"                    # bool; never written by this driver

#: axis ``State`` strings (SDK:XYAxes ``waitForMovingFinished`` / SDK:ZAxis)
STAGE_STATE_REACHED = "Desired Position Reached"
STAGE_STATE_IDLE = ""                                        # SDK treats the empty state as "finished"
STAGE_STATE_STOPPED = "Manually Stopped"
STAGE_STATE_FAILED = "Position not Reached"                  # e.g. an end switch was hit
STAGE_STATE_IN_USE = "Axis In Use"

# ── video / focus / objective ───────────────────────────────────────────────────

_V = _MC + "Video|"
VIDEO_FILE_NAME = _V + "VideoImageFileName"                  # string; SDK:VideoControl51 (WITec Control ≥ 5.1)
VIDEO_TO_FILE = _V + "AcquireVideoImageToFile"               # trigger
VIDEO_ACQUIRE = _V + "AcquireVideoImage"                     # trigger (puts the image into the project)
AUTOFOCUS_EXECUTE = _V + "AutoFocus|Execute"                 # trigger (D3, SDK:AutoFocus)
AUTOFOCUS_STATUS = _V + "AutoFocus|Status"                   # enum: 0 running, 1 last succeeded, 2 last failed (≥ 6.1; SDK:AutoFocus61)
VIDEO_CAL_WIDTH_UM = _V + "Calibration|ImageWidthMicrons"    # SDK:VideoControl — image scale
VIDEO_CAL_HEIGHT_UM = _V + "Calibration|ImageHeightMicrons"
VIDEO_CAL_ROTATION_DEG = _V + "Calibration|RotationDegrees"
VIDEO_PROBE_X = _V + "ProbePosition|RelativeX"               # laser spot in the video image (0–1)
VIDEO_PROBE_Y = _V + "ProbePosition|RelativeY"
#: the objective turret is manual — these are read-only registration info (R-api 5/6, correction)
OBJECTIVE_INFORMATION = _MC + "Objective|SelectedTop|Information"
OBJECTIVE_MAGNIFICATION = _MC + "Objective|SelectedTop|Magnification"
OBJECTIVE_FOCUS_DEPTH = _MC + "Objective|SelectedTop|FocusDepth"

STATE_NAME = _MC + "StateManager|StateName"                  # string: reads/loads a saved microscope state (preset) — D3, SDK:StateManager
DETECTOR_OUTPUT_CCD1 = _MC + "DetectorOutput|CCD1"           # trigger (R-hist); not used by the driver

# ── what no automation may write or trigger ────────────────────────────────────────

#: Whole groups refused by the client's write guard (``BucsClient.set`` / ``trigger``); reads stay allowed.
#:
#: * ``ApplicationControl|`` — main-menu actions: ``NewProject`` (deletes the current project), ``ExitApplication``
#:   (closes WITec Control; COM cannot start it again), ``LoadConfiguration`` (swaps the whole GUI configuration),
#:   ``UpdateHardware``, ``FileNameToAppendToProject`` (CHM:wccomsubsystems, R-api 5/6 §7). The first three are the
#:   design contract's blacklist (docs §1 trap 5); the group is refused wholesale.
#: * ``COMInterop`` — ``LocalLockoutLicensed`` is the "COM Automation" button itself, ``LocalLockout`` /
#:   ``AllowReadAccess`` / ``RevokeWriteAccess`` its siblings (R-api 1/6 §2). Write access is handed over by a person
#:   pressing that button (docs D5); software must never press it.
#: * ``UserParameters|HardwareConstraints|`` — ``SetExtendedZStepperTravel`` & co. widen the software Z travel
#:   limit (SDK:ZAxis ``UseExtendedZRange``) that protects the objective.
DANGEROUS_GROUPS = (
    "ApplicationControl",
    "COMInterop",
    "UserParameters|HardwareConstraints",
)

#: The contract's three, spelt out. All of them are also covered by the ``ApplicationControl`` group; the test pins both.
DANGEROUS_PATHS = (
    "ApplicationControl|NewProject",
    "ApplicationControl|ExitApplication",
    "ApplicationControl|LoadConfiguration",
)

#: Triggers that re-define the stage coordinate origin. Moving the zero silently shifts every stored position and the
#: software Z travel window the hub-side limit is written for. Refused anywhere under ``MotorizedXYZ`` (SDK:XYAxes,
#: SDK:ZAxis, R-hist). ``MoveToCalibrationPosition`` goes to a position nobody told us.
COORDINATE_REZERO_LEAVES = (
    "SetSamplePositionToZero",
    "SetSamplePositionZToZero",
    "SetHardwarePositionZToZero",
    "ResetCoordinateSystem",
    "MoveToCalibrationPosition",
)

# ── read-only probe / capability tables ──────────────────────────────────────────

#: Candidate status reads for ``probe_witec_com.py`` and ``WITecMicroscope.capabilities`` (all reads).
PROBE_READ_PATHS = (
    PROGRAM_VERSION, COM_ACCEPTS_REMOTE, CURRENT_FILE_NAME, IS_SEQUENCER_ACTIVE,
    ACTIVE_SEQUENCER_NAME, ACTIVE_SEQUENCER_ACTIVITY, SEQUENCER_STATUS_OR_RESULT,
    SINGLE_SPECTRUM_INTEGRATION, SINGLE_SPECTRUM_ACCUMULATIONS, MAX_Z_STEPPER_TRAVEL,
    spectrograph_path(1, "Grating"), spectrograph_path(1, "CenterWavelength"),
    spectrograph_path(1, "SpectralCenter"), spectrograph_path(1, "SpectralUnit"),
    BEAM_STATE, ANALYZER, ANALYZER_COUPLER, FIELD_STOP, APERTURE_STOP,
    LASER_COUNT, LASER_WAVELENGTH, LASER_POWER, LASER_SHUTTER, LASER_HAS_SHUTTER,
    LASER_POWER_CORRECTION,
    STAGE_IS_VIRTUAL, XY_STATE, XY_CURRENT_X, XY_CURRENT_Y, Z_STATE, Z_CURRENT, Z_IN_USER_SPACE,
    VIDEO_CAL_WIDTH_UM, VIDEO_CAL_HEIGHT_UM, VIDEO_CAL_ROTATION_DEG, VIDEO_PROBE_X, VIDEO_PROBE_Y,
    OBJECTIVE_INFORMATION, OBJECTIVE_MAGNIFICATION, OBJECTIVE_FOCUS_DEPTH, STATE_NAME,
    SILENT_ERROR, SILENT_DONE,
)

#: Paths without which the driver cannot acquire a spectrum at all.
REQUIRED_FOR_ACQUISITION = (
    IS_SEQUENCER_ACTIVE, STOP_SEQUENCER, SILENT_PARAMETER, SILENT_DONE, SILENT_TEXT, SILENT_ERROR,
    SILENT_START, BEAM_TRIGGERS["raman"], BEAM_STATE,
)

#: Feature → the paths it needs. ``WITecMicroscope.capabilities()`` reports a feature as available only if *all* exist
#: on the connected WITec Control; every optional feature is looked up here before use (docs §1 trap 10).
FEATURE_PATHS: dict[str, tuple[str, ...]] = {
    "silent_spectrum": REQUIRED_FOR_ACQUISITION,
    "center_wavelength": (spectrograph_path(1, "CenterWavelength"),),
    "grating": (spectrograph_path(1, "Grating"),),
    "analyzer": (ANALYZER,),
    "analyzer_coupler": (ANALYZER_COUPLER,),
    "analyzer_series": (ANALYZER_SERIES_STEPS, ANALYZER_SERIES_START, SERIES_INTEGRATION,
                        SERIES_ACCUMULATIONS),
    "beam_path": (BEAM_STATE,),
    "lasers": (LASER_COUNT, LASER_WAVELENGTH),
    "laser_power": (LASER_POWER,),
    "laser_shutter": (LASER_SHUTTER,),
    "stage_xy": (XY_DESIRED_X, XY_DESIRED_Y, XY_CURRENT_X, XY_CURRENT_Y, XY_MOVE, XY_STATE, XY_STOP),
    "stage_z": (Z_DESIRED, Z_CURRENT, Z_MOVE, Z_STATE, Z_STOP),
    "states": (STATE_NAME,),
    "video_image": (VIDEO_FILE_NAME, VIDEO_TO_FILE),
    "autofocus": (AUTOFOCUS_EXECUTE,),
    "project_save": (AUTOSAVE_STORE, AUTOSAVE_START_DIR, AUTOSAVE_FILE_NAME),
}
