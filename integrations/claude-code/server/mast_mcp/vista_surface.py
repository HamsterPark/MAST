# Notes tool texts derived from VISTA (MIT), github.com/joshhhhhan/VISTA, core/tools.py
"""What the model reads about the VISTA tools: descriptions and input schemas.

MAST 7.0 runs VISTA's visual harness inside the MAST server (``MASTv2/mast/vista/``):
the frame archive, the magnifier, exact readings, the rulers and the notes. This server
imports nothing from MASTv2 (it runs on a bare embedded Python), so the model-facing
text of those tools is copied here from the VISTA modules (``actions.py``,
``viewtools.py``, ``measure.py``, ``notes.py``; ``history`` from VISTA-SPM's host).

Copied verbatim except where MAST changed the contract:

* tool names carry the ``mast_`` prefix (``act`` is ``mast_act`` and so on);
* an act needs the observation token (``after``), runs as a job, and may still be running
  when the call returns (then ``mast_wait``); Codex Code Mode shows an image only after
  ``image()``;
* there is no scoring channel (``report_result``) and no instrument-time budget;
* the notes belong to MAST's workspaces: ``GUIDE.md`` is shared by every experiment,
  ``WORKING.md`` belongs to the active experiment.

``tests/v2/unit/integrations/test_mast_mcp_vista_surface.py`` compares these copies with
``mast.vista``; ``test_mast_mcp_surface.py`` pins the whole tool list.
"""
from __future__ import annotations

#: Longest side of a rendered frame (VISTA's ``DISPLAY_MAX_DEFAULT``).
DISPLAY_MAX = 768
#: MAST's workspaces build their archive with ``grid=True`` (``mast.vista.workspace``): scan
#: images carry VISTA's metric grid, and inspect and act describe it.
GRID = True
GRID_COLOUR_NAME = "amber"

MAX_INSPECT_VIEWS = 16
MAX_READ_VIEWS = 64
MAX_READ_SAMPLES = 4096
MAX_QUESTION_CHARS = 1024
MAX_LABEL_CHARS = 128
MAX_GUIDE_CHARS = 64 * 1024
MAX_WORKING_CHARS = 16 * 1024
MAX_BLOBS = 50

DIRECTIONS = ("forward", "backward")
FLATTEN_MODES = ("none", "plane", "line", "poly2", "highpass")
FRAME_ID_PATTERN = r"^t\d{4,}\.[spdm]\d+$"

#: Conventions every key follows (measure tools).
UNIT_KEYS = ("Keys carry their unit: _nm, _deg, _px, and for channel values _pm (Z), _pa (current), "
             "_hz (frequency shift), _v (voltage).")

CODEX_IMAGE = "In Codex Code Mode, call image() on the result to see its images."


# ── shared schema parts (viewtools.py, measure.py) ──────────────────────────
def region_schema(display_max: int = DISPLAY_MAX) -> dict:
    return {"type": "object", "additionalProperties": False, "required": ["x", "y", "width", "height"],
            "description": ("Rectangle in display pixels of the frame's full image: origin at the top-left "
                            "corner, x to the right, y down (the coordinates mast_act accepts with a frame id)."),
            "properties": {"x": {"type": "integer", "minimum": 0, "maximum": display_max - 1},
                           "y": {"type": "integer", "minimum": 0, "maximum": display_max - 1},
                           "width": {"type": "integer", "minimum": 1, "maximum": display_max},
                           "height": {"type": "integer", "minimum": 1, "maximum": display_max}}}


def _common_view_props() -> dict:
    return {
        "label": {"type": "string", "minLength": 1, "maxLength": MAX_LABEL_CHARS,
                  "description": "A short name for this view."},
        "frame": {"type": "string", "pattern": FRAME_ID_PATTERN,
                  "description": "Archived frame id, e.g. 't0007.s0' (scan), 't0007.p0' (partial scan), "
                                 "'t0007.d0' (spectrum), 't0007.m0' (derived image)."},
        "channel": {"type": "string", "minLength": 1, "maxLength": 64,
                    "description": "Scan channel, e.g. 'Z' or 'Current'. Default: the frame's default "
                                   "channel (Z; Current for a constant-height frame)."},
        "direction": {"type": "string", "enum": list(DIRECTIONS),
                      "description": "Scan pass. Default forward."},
    }


QUESTION_SCHEMA = {"type": "string", "minLength": 1, "maxLength": MAX_QUESTION_CHARS,
                   "description": "The visual question these views should answer."}


def _point_schema() -> dict:
    return {"type": "object", "additionalProperties": False,
            "description": ("A point: either {x, y} in display pixels of the frame's full image, or "
                            "{x_nm, y_nm} in scan-frame nm."),
            "properties": {"x": {"type": "number"}, "y": {"type": "number"},
                           "x_nm": {"type": "number"}, "y_nm": {"type": "number"}}}


def _frame_schema(desc: str = "Archived scan frame id, e.g. 't0007.s0'.") -> dict:
    return {"type": "string", "pattern": FRAME_ID_PATTERN, "description": desc}


_CHANNEL_SCHEMA = {"type": "string", "minLength": 1, "maxLength": 64,
                   "description": "Channel, e.g. 'Z' or 'Current'. Default: the frame's default channel."}
_DIRECTION_SCHEMA = {"type": "string", "enum": list(DIRECTIONS), "description": "Scan pass. Default forward."}
_FLATTEN_SCHEMA = {"type": "string", "enum": list(FLATTEN_MODES),
                   "description": "Background removed over the whole frame first (default plane)."}
_HIGHPASS_SCHEMA = {"type": "number", "exclusiveMinimum": 0, "maximum": 10000,
                    "description": "Background width in nm for flatten='highpass'. Default 3."}


# ── inspect, read_values (viewtools.py) ─────────────────────────────────────
def inspect_schema(display_max: int = DISPLAY_MAX) -> dict:
    view = {"type": "object", "additionalProperties": False, "required": ["label", "frame"],
            "properties": {
                **_common_view_props(),
                "region": region_schema(display_max),
                "flatten": {"type": "string", "enum": list(FLATTEN_MODES),
                            "description": "Background removed before display, over the whole frame: none; "
                                           "plane (default); line (plane, then each row's median offset); "
                                           "poly2 (2nd-order surface); highpass (median background of "
                                           "highpass_nm)."},
                "highpass_nm": {"type": "number", "exclusiveMinimum": 0, "maximum": 10000,
                                "description": "Background width in nm for flatten='highpass'. Default 3."},
                "clip_pct": {"type": "number", "minimum": 0, "maximum": 25,
                             "description": "Contrast: black and white at this percentile and 100 minus it "
                                            "of the selected region. Default 0.5."},
                "x_col": {"type": "string", "minLength": 1, "maxLength": 64,
                          "description": "Spectrum only: x column (default the sweep column)."},
                "y_col": {"type": "string", "minLength": 1, "maxLength": 64,
                          "description": "Spectrum only: one y column (default: the first four columns other than x, "
                                         "one panel each)."},
                "x_range": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2,
                            "description": "Spectrum only: [low, high] of the x axis, in the unit the plot "
                                           "shows (bias in V, Z in pm)."},
            }}
    return {"type": "object", "additionalProperties": False, "required": ["question", "views"],
            "properties": {"question": QUESTION_SCHEMA,
                           "views": {"type": "array", "minItems": 1, "maxItems": MAX_INSPECT_VIEWS,
                                     "items": view,
                                     "description": f"1 to {MAX_INSPECT_VIEWS} views, rendered in this order."}}}


def inspect_description(display_max: int = DISPLAY_MAX, grid: bool = GRID) -> str:
    dm = int(display_max)
    drawn = ("dark blue; nothing is drawn on them." if not grid else
             f"dark blue. Faint {GRID_COLOUR_NAME} lines draw a square grid every grid_nm nm, counted from "
             "the top-left corner of the frame's full image along its own x and y, finer when you zoom in; "
             "nothing else is drawn on them.")
    spacing = "" if not grid else " the grid spacing (grid_nm),"
    return (
        "Look again at archived frames without changing the instrument. Each view re-renders one frame "
        "from its archived values: a saved scan, a partial scan, a spectrum or a derived image. For a scan, "
        "choose the channel, the direction (forward or backward), the flattening and the contrast. Omit "
        "region to see the whole frame, or select a rectangle in display pixels of that frame's full image "
        "(origin at the top-left corner, x to the right, y down: the coordinates mast_act accepts with a frame "
        "id). The region is cropped exactly, widened to whole scan pixels, and enlarged by the largest whole "
        f"number of image pixels per scan pixel that fits {dm}x{dm}, without smoothing. Images are gray "
        "(black = low, white = high, contrast from the selected region); pixels never acquired are flat "
        f"{drawn} A spectrum view is a line plot (x_col, y_col, x_range instead "
        "of region); a derived image is cropped as stored. State the visual question these views should "
        "answer and give each view a short label. The views are returned in request order, one image each; "
        "the reply text gives each view's region in display pixels (region_px), the scan-frame nm of its "
        f"corners (corners_nm), the magnification, nm per image pixel,{spacing} and the values shown as black "
        "and white with the unit in the key (black_pm / white_pm for Z, black_pa / white_pa for current). "
        "Your question and labels remain in this call. " + CODEX_IMAGE
    )


def read_values_schema(display_max: int = DISPLAY_MAX) -> dict:
    view = {"type": "object", "additionalProperties": False, "required": ["label", "frame"],
            "properties": {
                **_common_view_props(),
                "region": region_schema(display_max),
                "rows": {"type": "integer", "minimum": 1, "maximum": MAX_READ_SAMPLES,
                         "description": "Scan frames: number of equal rows the region is divided into."},
                "columns": {"type": "integer", "minimum": 1, "maximum": MAX_READ_SAMPLES,
                            "description": "Scan frames: number of equal columns the region is divided into."},
                "x_col": {"type": "string", "minLength": 1, "maxLength": 64,
                          "description": "Spectrum only: x column (default the sweep column)."},
                "y_cols": {"type": "array", "minItems": 1, "maxItems": 8,
                           "items": {"type": "string", "minLength": 1, "maxLength": 64},
                           "description": "Spectrum only: columns to return besides x (default all)."},
                "x_range": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2,
                            "description": "Spectrum only: [low, high] in the x column's unit (bias in V)."},
                "n": {"type": "integer", "minimum": 1, "maximum": MAX_READ_SAMPLES,
                      "description": "Spectrum only: number of samples at bin centers (default: every point)."},
            }}
    return {"type": "object", "additionalProperties": False, "required": ["question", "views"],
            "properties": {"question": QUESTION_SCHEMA,
                           "views": {"type": "array", "minItems": 1, "maxItems": MAX_READ_VIEWS,
                                     "items": view,
                                     "description": f"1 to {MAX_READ_VIEWS} views, read in this order."}}}


READ_VALUES_DESCRIPTION = (
    "Read exact archived values from one or more frames without changing the instrument. For a scan "
    "view, the region (display pixels of that frame's full image; omit it for the whole frame) is "
    "divided into equal rows and columns and the archived pixel at the center of every cell is read: "
    "display pixel x + ((2c+1)*width)//(2*columns), likewise for y. Values are physical and unflattened, "
    "in one unit per quantity carried by the key: values_pm for Z, values_pa for current, values_hz for "
    "frequency shift, values_v for voltages; rounded to the view's decimals (about 1/100 of the "
    "channel's value range); null where nothing was acquired. The reply gives the sampled display "
    "pixels and the scan-frame nm of the first and last sample. For a spectrum view, give x_range (in "
    "the x column's unit), n and y_cols instead; values come back per column with column_units. At most "
    f"{MAX_READ_VIEWS} views and {MAX_READ_SAMPLES} samples per call. This tool only reads values; it "
    "does not flatten, align, compare or interpret them. State the question and give each view a short "
    "label; those remain in this call."
)


# ── measure tools (measure.py) ──────────────────────────────────────────────
def line_profile_schema(display_max: int = DISPLAY_MAX) -> dict:
    return {"type": "object", "additionalProperties": False, "required": ["frame", "p1", "p2"],
            "properties": {
                "frame": _frame_schema(), "p1": _point_schema(), "p2": _point_schema(),
                "channel": _CHANNEL_SCHEMA, "direction": _DIRECTION_SCHEMA,
                "width_px": {"type": "number", "minimum": 1, "maximum": int(display_max),
                             "description": "Band width in display pixels averaged across the line. Default 1."},
                "flatten": _FLATTEN_SCHEMA, "highpass_nm": _HIGHPASS_SCHEMA}}


LINE_PROFILE_DESCRIPTION = (
    "Measure values along a straight line in one archived scan frame without changing the instrument. "
    "p1 and p2 are either {x, y} in display pixels of that frame's full image or {x_nm, y_nm} in "
    "scan-frame nm. The flattened frame (default plane) is interpolated bilinearly at up to 400 evenly "
    "spaced samples, at most one scan pixel apart on a line shorter than 400 scan pixels (spacing_nm "
    "gives the spacing), and averaged across width_px. The reply gives distance_nm from p1 and the values "
    "(values_pm for Z, values_pa for current) per sample; the line's length and direction in the scan "
    "frame; the straight segments of a piecewise-linear fit (from_nm, to_nm, slope, mean); and step "
    "candidates where the segments on either side of a short transition differ by at least 4 noise "
    "sigmas: position_nm along the line, the scan-frame x_nm and y_nm, the height (right side minus left "
    "side, e.g. height_pm) and transition_nm. A bump wider than the transition window appears as a rise "
    "and a fall, and a rough or zigzag step edge that the line crosses more than once appears as several "
    "steps; the height between two terraces is better taken as the difference of the means of the segments "
    "on either side. One plot of the profile is returned and archived as a derived frame (plot.frame). It "
    "measures; it does not decide what a step or a feature is. " + UNIT_KEYS
)


def fft_peaks_schema(display_max: int = DISPLAY_MAX) -> dict:
    return {"type": "object", "additionalProperties": False, "required": ["frame"],
            "properties": {
                "frame": _frame_schema(), "region": region_schema(int(display_max)),
                "channel": _CHANNEL_SCHEMA, "direction": _DIRECTION_SCHEMA,
                "n_peaks": {"type": "integer", "minimum": 1, "maximum": 20,
                            "description": "Maximum number of peaks listed. Default 6."},
                "flatten": _FLATTEN_SCHEMA, "highpass_nm": _HIGHPASS_SCHEMA}}


FFT_PEAKS_DESCRIPTION = (
    "Compute the 2-D Fourier spectrum of one archived scan frame, or of a region of it (display pixels "
    "of the frame's full image), without changing the instrument, and list its strongest peaks. The "
    "region is flattened (default plane over the whole frame, then a plane over the region), "
    "Hann-windowed and zero-padded; the central DC lobe (2/field along each axis) is excluded. Each peak "
    "gives the spatial frequency k = 1/period in cycles per nm (k_per_nm, no 2 pi), the real-space "
    "period_nm, the direction of k in the scan frame (angle_deg, from +x toward +y, 0..180; stripes or "
    "atom rows with this period run perpendicular to it), its components and a strength: log-magnitude "
    "above the spectrum's 30th percentile at the same |k|, in robust units of its lower spread; and "
    "nyquist_fraction, its larger component along the frame's own pixel axes as a fraction of that axis's "
    "Nyquist frequency (0.5 / pixel size), with near_nyquist true from 0.8: such a peak may be an alias of "
    "a period finer than two pixels, and a rescan with smaller pixels tells the two apart. Only one "
    "peak of each +k/-k pair is listed; peaks weaker than 3 are not. One image of the log-magnitude "
    "spectrum with the listed peaks numbered is returned (center = k 0, right = +x and up = +y of the "
    "frame's own axes) and archived as a derived frame (image.frame). The spectrum is measured; whether "
    "a peak is a lattice, a reconstruction or noise is your judgement."
)


def detect_blobs_schema(display_max: int = DISPLAY_MAX) -> dict:
    return {"type": "object", "additionalProperties": False, "required": ["frame"],
            "properties": {
                "frame": _frame_schema(), "region": region_schema(int(display_max)),
                "channel": _CHANNEL_SCHEMA, "direction": _DIRECTION_SCHEMA,
                "polarity": {"type": "string", "enum": ["bright", "dark"],
                             "description": "Look for protrusions (bright, default) or depressions (dark)."},
                "min_height": {"type": "number", "minimum": 0,
                               "description": "Only report blobs at least this high above (dark: this deep "
                                              "below) their local background, in the channel's unit (pm for "
                                              "Z, pA for current)."},
                "min_sep_nm": {"type": "number", "exclusiveMinimum": 0, "maximum": 100,
                               "description": "Maxima closer than this are one candidate. Default 0.3 nm."},
                "background_nm": {"type": "number", "exclusiveMinimum": 0, "maximum": 1000,
                                  "description": "Width of the median background removed first. Default 3 nm; "
                                                 "use a larger value for larger features."},
                "max_n": {"type": "integer", "minimum": 1, "maximum": MAX_BLOBS,
                          "description": "At most this many blobs (up to 50), the highest. Default 50."}}}


DETECT_BLOBS_DESCRIPTION = (
    "Find compact bright (or dark) features in one archived scan frame, or in a region of it (display "
    "pixels of the frame's full image), without changing the instrument. The frame is plane-flattened, a "
    "median background of background_nm is subtracted, the residual is lightly smoothed, and local maxima "
    "above 6 noise sigmas are kept (the noise is measured on the lower part of the residual, which features "
    "do not reach, so a dense lattice does not raise it; where that lower part is a flat floor or deep gaps "
    "between touching features, the noise is capped at 8 times the pixel-to-pixel noise from second "
    "differences, and detection.noise_method says which applied); maxima not separated by a saddle are merged; each "
    "feature is fitted with an elliptical Gaussian. Per blob: id, centroid in display pixels of the full frame "
    "(x_px, y_px) and in scan-frame nm (x_nm, y_nm), height relative to the local background (height_pm "
    "for Z, height_pa for current; negative for dark blobs), FWHM along the frame's x and y and along its "
    "major/minor axes (nm), elongation (major/minor), the major axis angle (axis_angle_deg, scan frame, "
    "from +x toward +y; null when the elongation is below 1.1, where noise decides the axis) and whether "
    "the centroid comes from the Gaussian fit or from intensity moments "
    "(fit). Blobs are numbered in reading order (top to bottom, left to right). One image of the frame "
    "or region with numbered rings is returned and archived as a derived frame (image.frame); the marks "
    "exist only on that image. It counts and locates; it does not identify species or decide what is an "
    "atom. " + UNIT_KEYS
)


def diff_schema(display_max: int = DISPLAY_MAX) -> dict:
    return {"type": "object", "additionalProperties": False, "required": ["frame_a", "frame_b"],
            "properties": {
                "frame_a": _frame_schema("The earlier (reference) scan frame id."),
                "frame_b": _frame_schema("The later scan frame id."),
                "region": region_schema(int(display_max)),
                "channel": _CHANNEL_SCHEMA, "direction": _DIRECTION_SCHEMA,
                "flatten": {"type": "string", "enum": list(FLATTEN_MODES),
                            "description": "Background removed from both frames before registering and "
                                           "subtracting. Default highpass (median background of highpass_nm), "
                                           "so steps and the bow of piezo creep do not dominate the "
                                           "correlation; use plane to compare large features."},
                "highpass_nm": {"type": "number", "exclusiveMinimum": 0, "maximum": 10000,
                                "description": "Background width in nm for flatten='highpass'. Default 3."},
                "align": {"type": "boolean", "description": "Register b to a before subtracting. Default true."}}}


DIFF_DESCRIPTION = (
    "Compare two archived scan frames without changing the instrument. Both are flattened the same way "
    "(default highpass: a median background of highpass_nm = 3 nm is removed, so steps and the bow of "
    "piezo creep do not dominate; plane keeps large features); frames "
    "with different geometry are resampled onto their overlap in scan-frame nm (at the coarser pixel "
    "size), and region (display pixels of frame_a) restricts the comparison. With align=true (default) "
    "the content of frame_b is registered to frame_a by sub-pixel normalized cross-correlation: "
    "registration.shift_nm is the displacement of the image content from frame_a to frame_b in "
    "scan-frame nm (shift_px in grid pixels), with the correlation peak and confidence = main peak / the "
    "best correlation farther away than twice the image's correlation length (close to 1 means "
    "ambiguous, e.g. a bare periodic lattice or parallel stripes; peak_at_search_limit means the shift "
    "was not found); second_peak_shift_nm (second_peak_shift_px, whole grid pixels) is where that best "
    "alternative lies, a displacement like shift_nm: on a repeating pattern the two differ by one "
    "repeat. The aligned difference (b minus shifted a, median offset and plane removed) is "
    "returned as a derived image (red = higher in b, blue = lower in b, white = unchanged, gray = outside "
    "the overlap) and archived with its values (difference.frame), so mast_read_values and mast_inspect "
    "work on it. Changed regions above 5 robust noise sigmas are numbered with centroid (x_nm, y_nm), sign, "
    "area_nm2 and peak change (peak_change_pm for Z). It measures change; it does not say why something "
    "changed. " + UNIT_KEYS
)


def stats_schema(display_max: int = DISPLAY_MAX) -> dict:
    return {"type": "object", "additionalProperties": False, "required": ["frame"],
            "properties": {"frame": _frame_schema(), "region": region_schema(int(display_max)),
                           "channel": _CHANNEL_SCHEMA, "direction": _DIRECTION_SCHEMA}}


STATS_DESCRIPTION = (
    "Summary statistics of one archived scan frame, or of a region of it (display pixels of the frame's "
    "full image), without changing the instrument: mean, standard deviation, min, max and percentiles of "
    "the raw values; RMS roughness, mean absolute deviation and peak-to-valley after subtracting a plane "
    "fitted to the region; the correlation and RMS difference between the forward and backward passes "
    "(both plane-flattened) when both exist; a line-noise index (median absolute offset between "
    "consecutive row medians divided by the plane-flattened standard deviation); and the fraction of "
    "rows acquired. " + UNIT_KEYS
)

#: ``mast_<tool>`` -> (title, description, schema builder, idempotent), as in ``measure.MEASURE_TOOLS``.
MEASURE_TOOLS = {
    "line_profile": ("Line profile", LINE_PROFILE_DESCRIPTION, line_profile_schema, False),
    "fft_peaks": ("FFT peaks", FFT_PEAKS_DESCRIPTION, fft_peaks_schema, False),
    "detect_blobs": ("Detect blobs", DETECT_BLOBS_DESCRIPTION, detect_blobs_schema, False),
    "diff": ("Difference of two frames", DIFF_DESCRIPTION, diff_schema, False),
    "stats": ("Frame statistics", STATS_DESCRIPTION, stats_schema, True),
}


# ── notes (notes.py) ────────────────────────────────────────────────────────
def no_args_schema() -> dict:
    return {"type": "object", "additionalProperties": False, "properties": {}}


def content_schema(limit: int) -> dict:
    return {"type": "object", "additionalProperties": False, "required": ["content"],
            "properties": {"content": {"type": "string", "minLength": 1, "maxLength": limit,
                                       "description": "The complete new file (Markdown)."}}}


READ_GUIDE_DESCRIPTION = (
    "Read `GUIDE.md`: your durable notes about the instrument, the samples and the tip. It is shared by "
    "every MAST experiment; read it at the start of a session.")
WRITE_GUIDE_DESCRIPTION = (
    "Write the complete contents of `GUIDE.md`, replacing its previous contents. Keep in it concise, "
    "durable, revisable understanding of the instrument, the sample and the tip, and revise it when new "
    "evidence changes what is supported. Every MAST experiment and every later session reads it. "
    f"At most {MAX_GUIDE_CHARS} characters.")
READ_WORKING_DESCRIPTION = (
    "Read `WORKING.md`: your temporary state for the active MAST experiment — the current plan and "
    "where you are in it.")
WRITE_WORKING_DESCRIPTION = (
    "Replace the contents of `WORKING.md`, your temporary state for the active MAST experiment: the "
    "active plan, the next expected observation, unresolved contradictions and the frame ids you still "
    "need. It persists until you replace it and survives context replacement: a fresh session starts "
    "from GUIDE.md, WORKING.md and the current observation. When MAST asks for a checkpoint "
    "(checkpoint_requested), only the notes tools run until it is saved. "
    f"At most {MAX_WORKING_CHARS} characters.")


# ── history (VISTA-SPM host.py) ─────────────────────────────────────────────
HISTORY_DESCRIPTION = (
    "Read objective records of the active MAST experiment without changing anything. `events` returns "
    "the exact records by turn, of these kinds: act (parameters as resolved, controller result, "
    "instrument state afterwards, new frame ids, time used), external_frame (a frame that came from "
    "outside your acts), detector_hit, detector_disabled and checkpoint; use start_turn, end_turn and "
    "limit to select a range. `attempts` summarises the experiment so far: actions by type and how "
    "many failed, frames and spectra.")


def history_schema() -> dict:
    return {"type": "object", "properties": {
        "view": {"type": "string", "enum": ["events", "attempts"],
                 "description": "events (the records, by turn) or attempts (a summary)."},
        "start_turn": {"type": "integer", "minimum": 0, "maximum": 999999,
                       "description": "events: first turn to include."},
        "end_turn": {"type": "integer", "minimum": 0, "maximum": 999999,
                     "description": "events: last turn to include."},
        "limit": {"type": "integer", "minimum": 1, "maximum": 128, "default": 64,
                  "description": "events: at most this many records, the newest."}},
        "required": ["view"], "additionalProperties": False}


# ── act (actions.py) ────────────────────────────────────────────────────────
#: ``ActionSpec.describe()`` of every action, the qPlus-only ones included (MAST does not know
#: the sensor in advance; the server refuses them on a tip that is not a qPlus sensor).
ACTION_LINES = (
    ("scan", ("- `scan` (center: point, size_nm: number [0.5..1500], pixels?: integer "
              "[16..1024], angle_deg?: number [-180..180], line_time_s?: number [0.01..60], "
              "bias_v?: number [-10..10], current_pa?: number [0.1..100000]): acquire and save "
              "one topographic frame (forward and backward, every recorded channel) centred at "
              "`center`; returns the new frame. Unset optional values come from the instrument's "
              "scan policy for that size.")),
    ("set_bias", ("- `set_bias` (bias_v: number [-10..10]): set the sample bias (V). A change that "
                  "crosses zero or is larger than 0.5 V, and any target with |V| < 50 mV, goes through "
                  "MAST's safe path (BiasSettleChange): it ramps through zero without stopping inside "
                  "that band, waits for the feedback to settle, and refuses to end inside the band "
                  "while the feedback is on or unknown; a smaller change of the same sign is set "
                  "directly. `path` in the result says which.")),
    ("set_current", ("- `set_current` (current_pa: number [0.1..100000]): set the feedback "
                     "setpoint current (pA).")),
    ("feedback", ("- `feedback` (on?: boolean, p_gain_pm?: number [0..], i_gain_nm_s?: number "
                  "[0..], time_constant_us?: number [0..]): switch the Z feedback on/off and/or "
                  "set its gains (omitted gains keep their value).")),
    ("move_tip", ("- `move_tip` (to: point): move the tip laterally to a position (feedback "
                  "stays on).")),
    ("pulse", ("- `pulse` (bias_v: number [-10..10], duration_s: number [0.001..1], count?: "
               "integer [1..5], at?: point): apply a bias voltage pulse to the tip (optionally "
               "at a position first). May change the tip, and the surface under it.")),
    ("poke", ("- `poke` (depth_nm: number [0.05..5] (into the surface), bias_v?: number "
              "[-10..10], dwell_s?: number [0.01..5], at?: point): controlled indentation: move "
              "the tip `depth_nm` into the surface and back, feedback restored afterwards "
              "(optionally at a position, at a bias). Changes the tip and leaves a mark.")),
    ("sts", ("- `sts` (start_v: number [-5..5], end_v: number [-5..5], num_points?: integer "
             "[16..4096], z_offset_nm?: number [-1..1] (negative = closer), at?: point): measure "
             "one bias spectrum (I(V), dI/dV where recorded) at a position; returns the spectrum.")),
    ("df_curve", ("- `df_curve` (z_sweep_nm: number [0.05..5], z_offset_nm?: number [-1..5] "
                  "(positive = farther), num_points?: integer [16..4096], at?: point): qPlus "
                  "only: measure one frequency-shift vs distance curve Δf(z) at a position; "
                  "returns the curve.")),
    ("pll", ("- `pll` (on?: boolean, amplitude_pm?: number [1..2000], auto_center?: boolean): "
             "qPlus only: set the oscillation amplitude, switch the PLL/amplitude control on or "
             "off, and/or auto-centre the frequency shift; returns the PLL status.")),
    ("move_atom", ("- `move_atom` (atom: point, target: point, current_pa?: number "
                   "[1000..100000] (default 57000), bias_v?: number [0.001..0.5] (magnitude; "
                   "default 0.01), speed_nm_s?: number [0.01..100] (default 0.5), "
                   "approach_offset_nm?: number [0..1] (start behind the atom; default 0)): "
                   "lateral manipulation: lower the junction resistance behind `atom`, drag the "
                   "tip to `target`, restore the imaging conditions. Does not check where the "
                   "atom went — image it.")),
    ("drift_compensation", ("- `drift_compensation` (enable: boolean, vx_nm_s?: number [-10..10] "
                            "(feature velocity in the scan frame), vy_nm_s?: number [-10..10], "
                            "vz_nm_s?: number [-10..10]): switch the piezo drift compensation "
                            "on/off with velocities in nm/s (the velocity at which features move "
                            "in the scan frame).")),
    ("wait", ("- `wait` (seconds: number [1..3600]): let instrument time pass without acting "
              "(thermal settling, drift measurement).")),
    ("withdraw", ("- `withdraw` (no parameters): withdraw the tip from the surface (feedback "
                  "off, tip retracted).")),
    ("approach", ("- `approach` (no parameters): establish tunnelling the safe way: first close the "
                  "Z feedback without moving the motors; only when the surface is out of the piezo's "
                  "reach, run the coarse current-feedback auto-approach, which stops at the setpoint. "
                  "`method` in the result says which way it went.")),
)
ACTION_NAMES = tuple(name for name, _ in ACTION_LINES)

#: Appended to the image phrase when scan images carry the metric grid (actions.GRID_PHRASE).
GRID_PHRASE = (" (on a scan image, faint amber lines draw a square grid every grid_nm nm, counted from the "
               "frame's top-left corner; grid_nm is in the image's label)")

ACT_WAIT_DEFAULT_S = 45
ACT_WAIT_MAX_S = 50


def act_description(grid: bool = GRID) -> str:
    phrase = "the new image when the action produced one" + (GRID_PHRASE if grid else "")
    lines = [
        ("Execute exactly one instrument action. Each call counts as one action and runs as a MAST job under "
         "the same safety gates as every skill. The result reports what the controller returned, the "
         "instrument state afterwards (with whether the feedback is on and the tip withdrawn; "
         "current_settling: true means a withdrawn tip's current reading had not decayed yet after up "
         "to 3 s of re-reading), the ids of any new frames or spectra, the time it took (elapsed_s), "
         f"{phrase}, and next_token. You observe this result before another action can execute. A scan "
         "that stops before its frame is complete (cancelled, halted) leaves the rows it acquired as a "
         "partial frame (frame id ending .p0, in partial_frame)."),
        ("`after` is the next_token of the latest observation you have looked at (from mast_observe, the "
         "previous act, mast_wait or a refusal). A token works once, and a frame that comes from outside your "
         "acts (operator, user interface, another job) replaces it. Refusals execute nothing: observe_first "
         "(token missing or stale: the reply carries the latest observation and a fresh token; look at it, "
         "then act), act_in_flight (another act is still running: mast_wait for it), same_code_cell (one act "
         "per code cell), checkpoint_requested (save WORKING.md with mast_write_working first), "
         "session_closed (the operator or the runner closed this session: finish with a short "
         "summary and do not act again)."),
        (f"An action still running after wait_s seconds (default {ACT_WAIT_DEFAULT_S}, max "
         f"{ACT_WAIT_MAX_S}) comes back with state running and its job_id: call mast_wait with "
         "conditions [{\"type\": \"job\", \"job_id\": ..}] to get its observation, and do not act "
         "meanwhile (mast_job on that job_id shows elapsed_s, and for a scan estimated_s and eta_s, "
         "while it runs). In Codex Code Mode, call image() on the result and look at it before the next "
         "act. Precise numbers come from mast_read_values and the measure tools, not from reading "
         "pixels."),
        ("Units: nm, V, pA, s, nm/s. A position is {\"x_nm\": .., \"y_nm\": ..} in scan-frame nm, or "
         "{\"frame\": \"t0007.s0\", \"x\": .., \"y\": ..} in display pixels of an archived "
         "frame, converted with that frame's own geometry. Positions in older frames may be stale: the sample "
         "drifts and the piezo creeps."),
        "Actions (`action` name, `params`):",
    ]
    lines += [line for _, line in ACTION_LINES]
    return "\n".join(lines)


def act_schema() -> dict:
    return {"type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(ACTION_NAMES), "description": "which action"},
                "params": {"type": "object", "description": "the action's parameters (see the list above)"},
                "note": {"type": "string", "maxLength": 1000,
                         "description": "optional: what you expect to see (kept in the history)"},
                "after": {"type": "string", "maxLength": 32,
                          "description": "the observation token: next_token of the latest observation you "
                                         "have looked at, e.g. 'K7QM-29XA'"},
                "request_id": {"type": "string", "maxLength": 120,
                               "description": "optional idempotency key: sending the same act again with "
                                              "this id (after a timeout) returns the original act instead of "
                                              "running it twice; generated when omitted"},
                "wait_s": {"type": "number", "minimum": 0, "maximum": ACT_WAIT_MAX_S,
                           "default": ACT_WAIT_DEFAULT_S,
                           "description": "seconds to wait for the action to finish before answering; "
                                          "a longer action keeps running (then mast_wait)"}},
            "required": ["action", "params", "after"], "additionalProperties": False}


# ── observe, wait, detectors, literature (MAST 7.0) ─────────────────────────
OBSERVE_DESCRIPTION = (
    "Look at the instrument without changing it: the latest archived frame as an image, its summary, "
    "the instrument state (bias, setpoint, current, Z, tip position, drift compensation) and a fresh "
    "observation token (next_token) for the next mast_act. Use it at the start of a session and whenever "
    "you no longer hold the latest token; it replaces any earlier token. " + CODEX_IMAGE)

WAIT_DEFAULT_S = 600
WAIT_MAX_S = 840
WAIT_TYPES = ("job", "frame", "alert", "operator_reply", "detector", "timer")
ALERT_LEVELS = ("info", "warning", "critical")

# Two pieces of WAIT_DESCRIPTION that a server whose calls must stay short (MCP over
# HTTP, see tools.Toolbox) replaces; the description itself is unchanged.
WAIT_LIMITS_TEXT = f"(default {WAIT_DEFAULT_S}, max {WAIT_MAX_S})"
WAIT_CLIENT_TIMEOUT_NOTE = ("Set your MCP client's tool timeout to at least 900 s (Codex: "
                            "tool_timeout_sec = 900). ")

WAIT_DESCRIPTION = (
    f"Wait on the MAST server until something happens, at most timeout_s seconds {WAIT_LIMITS_TEXT}"
    ". Conditions: job (a job ends: an act or a plain skill job, by "
    "job_id), frame (a new frame is archived), alert (an anomaly, current-monitor or conduct alert at or "
    "above min_level: info, warning or critical), operator_reply (the operator answers; one request_id or "
    "any), detector (a detector hit or auto-disable; one detector_id or any), timer (seconds pass). "
    "A job that MAST does not know ends the wait at once with an error. The first condition that "
    "fires ends the wait. Each fired entry carries age_s and backlog (true: it happened before this "
    "wait started, so an alert that returns at once may be minutes old); a current-monitor alert "
    "also carries segment_start/segment_end (the ~1 s of current judged), measured (sigma, jump "
    "rate, largest step and mean in pA) and context (scanning or not, seconds since the last "
    "scanning segment, the skill holding the instrument, bias, setpoint). When a finished act or "
    "a new frame fired, the answer carries that "
    "observation with its image and a fresh next_token for mast_act. The answer's cursor marks where "
    "this wait stopped; the next mast_wait continues from it automatically (or pass since), so no event "
    "between two waits is lost. " + WAIT_CLIENT_TIMEOUT_NOTE
    + "Interrupting this call changes nothing on MAST. " + CODEX_IMAGE)


def wait_schema() -> dict:
    condition = {"type": "object", "additionalProperties": False, "required": ["type"],
                 "properties": {
                     "type": {"type": "string", "enum": list(WAIT_TYPES), "description": "What to wait for."},
                     "job_id": {"type": "string", "description": "job: the job (act or plain job) to wait for."},
                     "min_level": {"type": "string", "enum": list(ALERT_LEVELS),
                                   "description": "alert: the lowest level that ends the wait "
                                                  "(default warning)."},
                     "request_id": {"type": "string",
                                    "description": "operator_reply: only this request (default: any)."},
                     "detector_id": {"type": "string",
                                     "description": "detector: only this detector (default: any)."},
                     "seconds": {"type": "number", "minimum": 0, "maximum": WAIT_MAX_S,
                                 "description": "timer: end the wait after this many seconds."}}}
    return {"type": "object", "additionalProperties": False, "required": ["conditions"],
            "properties": {
                "conditions": {"type": "array", "minItems": 1, "maxItems": 16, "items": condition,
                               "description": "Any of these ends the wait, e.g. [{\"type\": \"job\", "
                                              "\"job_id\": \"j_...\"}, {\"type\": \"alert\", "
                                              "\"min_level\": \"warning\"}]."},
                "timeout_s": {"type": "number", "minimum": 1, "maximum": WAIT_MAX_S, "default": WAIT_DEFAULT_S,
                              "description": f"Longest wait in seconds (1-{WAIT_MAX_S})."},
                "since": {"type": "string", "maxLength": 128,
                          "description": "Cursor of an earlier mast_wait answer; default: the last cursor "
                                         "this connection received."}}}


DETECTOR_MAX_RUNTIME_S = 120
DETECTOR_DEFAULT_RUNTIME_S = 20
DETECTOR_NAME_CHARS = 80
DETECTOR_DESCRIPTION_CHARS = 500
DETECTOR_CODE_CHARS = 64 * 1024

DETECTOR_CREATE_DESCRIPTION = (
    "Create an analysis detector for the active MAST experiment: Python code that MAST runs on every newly "
    "archived frame of the kinds in applies_to, in a separate interpreter (no instrument access, no write "
    "access to raw data, killed after max_runtime_s). The code defines def detect(frame): frame.kind "
    "('scan' or 'spectrum'), frame.fid, frame.channels (channel name -> forward 2-D float array, SI units), "
    "frame.backward (the same for the backward pass), frame.meta (geometry, bias, setpoint, units); spectra "
    "also have frame.columns and frame.units. Return a falsy value (no hit), True, or {\"hit\": bool, "
    "\"message\": str, \"value\": ...}. A hit is recorded and ends a mast_wait on {\"type\": \"detector\"}. "
    "Detectors never act; to act on a hit, run an action yourself (mast_act, or a composite skill with "
    "mast_run). Limits: 8 enabled detectors per experiment; max_runtime_s 20 by default, at most 120; "
    "5 errors in a row or more than 30 hits an hour disable a detector, and beyond 60 wakes an hour hits "
    "are recorded without waking anyone. The same name and code sent again return the existing "
    "detector; other code under a taken name is refused (delete the old one first). Test a new "
    "detector on an archived frame with mast_detectors(op=\"test\") first.")


def detector_create_schema() -> dict:
    return {"type": "object", "additionalProperties": False, "required": ["name", "code", "applies_to"],
            "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": DETECTOR_NAME_CHARS,
                         "description": "Short name, unique in this experiment."},
                "code": {"type": "string", "minLength": 1, "maxLength": DETECTOR_CODE_CHARS,
                         "description": "Python source that defines def detect(frame)."},
                "applies_to": {"type": "array", "minItems": 1, "maxItems": 2,
                               "items": {"type": "string", "enum": ["scan", "spectrum"]},
                               "description": "Frame kinds to run on: scan, spectrum or both."},
                "description": {"type": "string", "maxLength": DETECTOR_DESCRIPTION_CHARS,
                                "description": "What it looks for, in a sentence (shown in listings)."},
                "max_runtime_s": {"type": "number", "minimum": 1, "maximum": DETECTOR_MAX_RUNTIME_S,
                                  "default": DETECTOR_DEFAULT_RUNTIME_S,
                                  "description": "Time limit for one frame, in seconds."}}}


DETECTOR_OPS = ("list", "get", "enable", "disable", "delete", "test")

DETECTORS_DESCRIPTION = (
    "Manage the analysis detectors of the active MAST experiment. op=list: every detector with its state "
    "(enabled, or why it was disabled) and its counts; get: one detector with its code and its latest "
    "hits; enable / disable: switch one on or off (enabling clears an automatic disable); delete: remove "
    "one; test: run one once, now, on an archived frame (e.g. 't0007.s0') and return what it returned or "
    "the error it raised, without recording a hit or waking anyone. Create detectors with "
    "mast_detector_create.")


def detectors_schema() -> dict:
    return {"type": "object", "additionalProperties": False, "required": ["op"],
            "properties": {
                "op": {"type": "string", "enum": list(DETECTOR_OPS), "description": "What to do."},
                "detector_id": {"type": "string", "maxLength": 128,
                                "description": "The detector (every op except list)."},
                "frame": {"type": "string", "pattern": FRAME_ID_PATTERN,
                          "description": "test: the archived frame to run it on, e.g. 't0007.s0'."}}}


LITERATURE_K_MAX = 50
LITERATURE_K_DEFAULT = 8

LITERATURE_DESCRIPTION = (
    "Search MAST's literature knowledge base (the operator's library of papers) by meaning, without "
    "touching the instrument. Each hit gives title, year, journal, DOI, a relevance score and an abstract "
    "excerpt. Use it for published values and procedures (lattice constants, adsorption sites, "
    "spectroscopic features, tip preparation) and cite the DOI. material focuses the search on one "
    "material, e.g. 'Cu(111)'. degraded=true means keyword matching only or no library on this machine.")


def literature_schema() -> dict:
    return {"type": "object", "additionalProperties": False, "required": ["query"],
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 500,
                          "description": "What to look for, in words."},
                "k": {"type": "integer", "minimum": 1, "maximum": LITERATURE_K_MAX,
                      "default": LITERATURE_K_DEFAULT, "description": "How many hits (1-50)."},
                "material": {"type": "string", "maxLength": 120,
                             "description": "Optional material to focus on, e.g. 'Cu(111)'."}}}
