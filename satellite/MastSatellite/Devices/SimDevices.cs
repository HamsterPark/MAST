using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Threading;
using MastSatellite.Core;

namespace MastSatellite.Devices
{
    /// <summary>
    /// Simulated devices ("sim": true). Same actions, arguments, events and error codes as the
    /// real drivers, with plausible timing (exposure plus
    /// ~22 ms readout, PRM1-Z8 10 deg/s, Picomotor ~2000 steps/s, UIRobot XYZ ~0.75 mm/s).
    /// </summary>
    internal static class SimUtil
    {
        public static string NowStamp() { return DateTime.Now.ToString("yyyyMMdd", CultureInfo.InvariantCulture); }
    }

    /// <summary>
    /// configure arguments of 契约 §5.4, shared by the real LightField driver and the sim. Nothing the
    /// caller asked for may be dropped silently (MAST records "requested" as "set"): unknown keys,
    /// unknown option names and values of the wrong type are refused with invalid_args.
    /// </summary>
    public static class SpectrometerArgs
    {
        /// <summary>configure keys of 契约 §5.4; anything else is refused (catches typos before they become silent no-ops).</summary>
        public static readonly HashSet<string> ConfigureKeys = new HashSet<string>(StringComparer.Ordinal)
        {
            "exposure_ms", "frames", "roi", "adc_mhz", "adc_quality", "shutter_mode", "trigger", "output_signal",
            "timestamps", "frame_tracking", "file",
        };

        private static readonly string[] TriggerKeys = { "response", "edge" };
        private static readonly string[] FileKeys = { "directory", "base_name", "attach_date", "attach_increment" };
        private static readonly string[] RoiKeys = { "mode", "rows", "x", "y", "width", "height", "x_bin", "y_bin", "regions" };

        // LightField's own enum names (PrincetonInstruments.LightField.AddIns in LightField 6.18's
        // AddInSupportServices metadata, without UninitializedEnum). Whether a given camera supports a
        // value is LightField's IsValid's business; these lists only catch names that do not exist.
        public static readonly string[] TriggerResponses = { "NoResponse", "ReadoutPerTrigger", "ShiftPerTrigger", "ExposeDuringTriggerPulse", "StartOnSingleTrigger", "GatePerTrigger" };
        public static readonly string[] TriggerEdges = { "RisingEdge", "FallingEdge", "PositivePolarity", "NegativePolarity", "AlternatingEdgeRising", "AlternatingEdgeFalling" };
        public static readonly string[] ShutterModes = { "Normal", "AlwaysOpen", "AlwaysClosed", "OpenBeforeTrigger" };
        public static readonly string[] AdcQualities = { "LowNoise", "HighCapacity", "HighSpeed", "ElectronMultiplied", "HighDynamicRange", "LongExposure" };
        public static readonly string[] OutputSignals =
        {
            "NotReadingOut", "ShutterOpen", "Busy", "AlwaysLow", "AlwaysHigh", "Acquiring", "ShiftingUnderMask", "Exposing",
            "EffectivelyExposing", "EffectivelyExposingAlternation", "ReadingOut", "WaitingForTrigger", "InternalTriggerT0", "Gate", "AuxOutput",
        };
        private static readonly string[] StampNames = { "None", "ExposureStarted", "ExposureEnded" };

        public static void RejectUnknown(CommandContext ctx)
        {
            var bad = ctx.Args.Keys.Where(k => !ConfigureKeys.Contains(k)).ToList();
            if (bad.Count > 0)
                throw new DeviceError(ErrorCodes.InvalidArgs, "unknown configure key(s): " + string.Join(", ", bad) + " (known: " + string.Join(", ", ConfigureKeys) + ")");
            CheckObject(ctx, "trigger", TriggerKeys);
            CheckObject(ctx, "file", FileKeys);
            CheckObject(ctx, "roi", RoiKeys);
        }

        private static void CheckObject(CommandContext ctx, string key, string[] known)
        {
            if (!ctx.Has(key)) return;
            var d = ctx.Dict(key) ?? throw new DeviceError(ErrorCodes.InvalidArgs, $"'{key}' must be an object");
            var bad = d.Keys.Where(k => !known.Contains(k)).ToList();
            if (bad.Count > 0)
                throw new DeviceError(ErrorCodes.InvalidArgs, $"unknown {key} key(s): {string.Join(", ", bad)} (known: {string.Join(", ", known)})");
        }

        /// <summary>The LightField name (input matched case-insensitively, canonical spelling returned) or invalid_args listing the names.</summary>
        public static string OneOf(string key, string value, IEnumerable<string> names)
        {
            var hit = value == null ? null : names.FirstOrDefault(n => string.Equals(n, value, StringComparison.OrdinalIgnoreCase));
            if (hit == null) throw new DeviceError(ErrorCodes.InvalidArgs, $"{key} '{value}' is not a LightField name (one of: {string.Join(", ", names)})");
            return hit;
        }

        /// <summary>trigger.edge: a TriggerDetermination name; "rising" / "falling" are accepted as shorthands.</summary>
        public static string Edge(string value)
        {
            if (string.Equals(value, "rising", StringComparison.OrdinalIgnoreCase)) return "RisingEdge";
            if (string.Equals(value, "falling", StringComparison.OrdinalIgnoreCase)) return "FallingEdge";
            return OneOf("trigger.edge", value, TriggerEdges);
        }

        /// <summary>
        /// timestamps: LightField's TimeStamps flags as a string - "None", "ExposureStarted", "ExposureEnded" or
        /// "ExposureStarted,ExposureEnded" - or a bool (true = both). Returns that canonical comma form.
        /// </summary>
        public static string TimeStamps(object value)
        {
            if (value is bool b) return b ? "ExposureStarted,ExposureEnded" : "None";
            if (!(value is string s) || s.Trim().Length == 0)
                throw new DeviceError(ErrorCodes.InvalidArgs, "timestamps must be \"None\", \"ExposureStarted\", \"ExposureEnded\" or \"ExposureStarted,ExposureEnded\"");
            var parts = s.Split(',').Select(p => OneOf("timestamps", p.Trim(), StampNames)).ToList();
            if (parts.Contains("None") && parts.Count > 1) throw new DeviceError(ErrorCodes.InvalidArgs, "timestamps: None cannot be combined with other stamps");
            bool started = parts.Contains("ExposureStarted"), ended = parts.Contains("ExposureEnded");
            return started && ended ? "ExposureStarted,ExposureEnded" : started ? "ExposureStarted" : ended ? "ExposureEnded" : "None";
        }

        /// <summary>A JSON bool (or "true"/"false"); anything else is invalid_args rather than a silent false.</summary>
        public static bool StrictBool(string key, object value)
        {
            if (value is bool b) return b;
            if (value is string s && bool.TryParse(s, out var p)) return p;
            throw new DeviceError(ErrorCodes.InvalidArgs, $"'{key}' must be true or false");
        }

        public static string StrictString(string key, object value)
        {
            if (value is string s && s.Length > 0) return s;
            throw new DeviceError(ErrorCodes.InvalidArgs, $"'{key}' must be a non-empty string");
        }

        /// <summary>
        /// roi: {mode:"full"}, {mode:"line", rows}, {mode:"binned", x_bin, y_bin}, {mode:"custom", x, y, width,
        /// height, x_bin?, y_bin?} or {mode:"custom", regions:[{x, y, width, height, x_bin?, y_bin?}]}. A custom
        /// region without y_bin is binned to one row in hardware (y_bin = height), as the line mode does;
        /// y_bin:1 keeps the rows (the satellite then sums them into an f32 spectrum).
        /// </summary>
        public static RoiSpec ParseRoi(Dictionary<string, object> roi)
        {
            var mode = J.Str(roi, "mode");
            var spec = new RoiSpec { Mode = mode };
            switch (mode)
            {
                case "full":
                    break;
                case "line":
                    spec.Rows = PositiveInt(roi, "rows", "roi.rows");
                    break;
                case "binned":
                    spec.XBin = PositiveInt(roi, "x_bin", "roi.x_bin");
                    spec.YBin = PositiveInt(roi, "y_bin", "roi.y_bin");
                    break;
                case "custom":
                {
                    var regs = J.List(roi, "regions");
                    bool flat = J.Has(roi, "x") || J.Has(roi, "width") || J.Has(roi, "y") || J.Has(roi, "height");
                    if (regs != null && flat) throw new DeviceError(ErrorCodes.InvalidArgs, "roi: give either x/y/width/height or regions, not both");
                    if (regs == null) regs = new List<object> { roi };
                    if (regs.Count == 0) throw new DeviceError(ErrorCodes.InvalidArgs, "roi.regions is empty");
                    spec.Regions = new List<int[]>();
                    foreach (var o in regs)
                    {
                        var r = o as Dictionary<string, object> ?? throw new DeviceError(ErrorCodes.InvalidArgs, "roi.regions entries must be objects");
                        int x = NonNegativeInt(r, "x"), y = NonNegativeInt(r, "y");
                        int w = PositiveInt(r, "width", "roi.width"), h = PositiveInt(r, "height", "roi.height");
                        int xb = J.Has(r, "x_bin") ? PositiveInt(r, "x_bin", "roi.x_bin") : 1;
                        int yb = J.Has(r, "y_bin") ? PositiveInt(r, "y_bin", "roi.y_bin") : h;
                        spec.Regions.Add(new[] { x, y, w, h, xb, yb });
                    }
                    break;
                }
                default:
                    throw new DeviceError(ErrorCodes.InvalidArgs, "roi.mode must be full, line, binned or custom");
            }
            return spec;
        }

        private static int PositiveInt(Dictionary<string, object> d, string key, string name)
        {
            var v = J.Get(d, key);
            double x = v == null || v is string || v is bool ? double.NaN : J.ToDouble(v, double.NaN);
            if (double.IsNaN(x) || x < 1 || x > int.MaxValue || Math.Abs(x - Math.Round(x)) > 1e-9)
                throw new DeviceError(ErrorCodes.InvalidArgs, $"{name} must be a positive integer");
            return (int)Math.Round(x);
        }

        private static int NonNegativeInt(Dictionary<string, object> d, string key)
        {
            var v = J.Get(d, key);
            double x = v == null || v is string || v is bool ? double.NaN : J.ToDouble(v, double.NaN);
            if (double.IsNaN(x) || x < 0 || x > int.MaxValue || Math.Abs(x - Math.Round(x)) > 1e-9)
                throw new DeviceError(ErrorCodes.InvalidArgs, $"roi.{key} must be an integer >= 0");
            return (int)Math.Round(x);
        }
    }

    /// <summary>A parsed roi (see SpectrometerArgs.ParseRoi); ToJson is the status form.</summary>
    public sealed class RoiSpec
    {
        public string Mode;
        public int Rows;
        public int XBin = 1, YBin = 1;
        public List<int[]> Regions; // x, y, width, height, x_bin, y_bin

        public static Dictionary<string, object> RegionJson(int[] r)
        {
            return J.Obj("x", r[0], "y", r[1], "width", r[2], "height", r[3], "x_bin", r[4], "y_bin", r[5]);
        }

        public Dictionary<string, object> ToJson()
        {
            var d = J.Obj("mode", Mode);
            if (Mode == "line") d["rows"] = Rows;
            if (Mode == "binned") { d["x_bin"] = XBin; d["y_bin"] = YBin; }
            if (Mode == "custom" && Regions != null)
            {
                if (Regions.Count == 1) foreach (var kv in RegionJson(Regions[0])) d[kv.Key] = kv.Value;
                d["regions"] = Regions.Select(r => (object)RegionJson(r)).ToList();
            }
            return d;
        }
    }

    // ------------------------------------------------------------------------------------
    // Spectrometer (stands in for LightField: PyLoN 100B + HRS-300)
    // ------------------------------------------------------------------------------------
    public sealed class SimSpectrometer : DeviceBase
    {
        private static readonly string[] CapList =
        {
            "status", "stop", "safe", "wavelength_axis", "configure", "acquire", "set_grating",
            "set_center_wavelength", "set_exit_port", "get_settings", "set_settings",
        };

        // nm per pixel at the detector centre, per groove density (HRS-300 + PyLoN 100B: 1200 g/mm ~0.0427 nm/px).
        private static readonly Dictionary<int, double> Dispersion = new Dictionary<int, double> { { 1200, 0.04262 }, { 600, 0.0866 }, { 150, 0.42 } };

        private readonly object _gate = new object();
        private readonly Random _rng = new Random(1234);
        private readonly int _pixels;
        private readonly double _readoutMs;
        private readonly double _gratingMoveS;
        private readonly double _centerMoveS;
        private readonly double _triggerHz;
        private readonly HashSet<string> _settable;
        private readonly Dictionary<string, object> _settings = new Dictionary<string, object>(StringComparer.Ordinal);
        private double _exposureMs = 1000;
        private int _frames = 1;
        private int _grating = 1200;
        private double _centerNm = 664.5495;
        private string _exitPort = "front";
        private string _shutterMode = "Normal";
        private string _triggerResponse = "NoResponse";
        private string _triggerEdge = "RisingEdge";
        private string _outputSignal = "ShutterOpen";
        private string _timestamps = "None";
        private bool _frameTracking;
        private double _adcMhz = 1;
        private string _adcQuality = "LowNoise";
        private const int SensorRows = 100; // PyLoN 100B: 1340 x 100
        private RoiSpec _roi = new RoiSpec { Mode = "line", Rows = 50 };
        private string _fileDir;
        private string _baseName = "Raman_";
        private bool _attachDate;
        private bool _attachIncrement = true;
        private int _increment = 1;
        private double _laserNm;
        private int _axisVer;
        private double[] _axis;
        private volatile bool _running;
        private volatile bool _manualRunning;
        private volatile bool _abort;
        private volatile bool _shutdown;
        private long _frameCounter;
        // Like the reference sim's manual_acq_every_s: now and then someone clicks Acquire in the LightField window.
        private readonly double _manualEveryS;
        private readonly int _manualFrames;
        private readonly double _manualExposureMs;
        private Thread _manualThread;

        public SimSpectrometer(DeviceConfig cfg, IDeviceHost host) : base(cfg, host, "spectrometer")
        {
            Model = "SIM PyLoN 100B + HRS-300";
            Serial = "SIM-CCD";
            _pixels = cfg.Int("pixels", 1340);
            _readoutMs = cfg.Num("readout_ms", 22.4);
            _gratingMoveS = cfg.Num("grating_move_s", 2.0);
            _centerMoveS = cfg.Num("center_move_s", 0.5);
            _triggerHz = cfg.Num("sim_trigger_hz", 5.0);
            _manualEveryS = cfg.Num("sim_manual_every_s", 0);
            _manualFrames = Math.Max(1, cfg.Int("sim_manual_frames", 1));
            _manualExposureMs = cfg.Num("sim_manual_exposure_ms", 100);
            _laserNm = cfg.Num("laser_line_nm", 633.0);
            _fileDir = cfg.Path("data_dir", System.IO.Path.Combine(host.StateDir, "sim_data"));
            _settable = new HashSet<string>((cfg.List("settable_keys") ?? new List<object>()).Select(o => Convert.ToString(o, CultureInfo.InvariantCulture)), StringComparer.Ordinal);
            _settings["Camera.Sensor.Temperature.SetPoint"] = -120.0;
            _settings["Camera.Adc.AnalogGain"] = "High";
            _settings["Camera.Experiment.OnlineCorrections.CosmicRayCorrection.Enabled"] = false;
        }

        public override IReadOnlyList<string> Caps => CapList;
        protected override bool IsBusyExternally => _manualRunning;
        protected override IEnumerable<string> VolatileStatusKeys => new[] { "temperature_c" };
        public override Dictionary<string, object> Info => J.Obj("pixels", _pixels, "simulated", true, "laser_line_nm", _laserNm);

        public override void Start()
        {
            RebuildAxis(emit: true);
            SetState(StateReady);
            if (_manualEveryS > 0)
            {
                _manualThread = new Thread(ManualLoop) { IsBackground = true, Name = Id + "-manual" };
                _manualThread.Start();
            }
        }

        public override void Shutdown()
        {
            _shutdown = true;
            _abort = true;
        }

        private void ManualLoop()
        {
            while (!_shutdown)
            {
                Thread.Sleep(TimeSpan.FromSeconds(_manualEveryS));
                if (_shutdown || _running || _manualRunning || ActiveOperation != null) continue;
                try { SimulateManualAcquisition(_manualFrames, _manualExposureMs); }
                catch (Exception ex) { Log.Debug($"[{Id}] simulated manual acquisition: {ex.Message}"); }
            }
        }

        public override void OnHubConnected()
        {
            EmitAxis();
        }

        protected override Dictionary<string, object> BuildStatus(bool fresh)
        {
            lock (_gate)
            {
                return J.Obj(
                    "is_running", _running || _manualRunning,
                    "manual_acquisition", _manualRunning,
                    "temperature_c", -120.0 + (_rng.NextDouble() - 0.5) * 0.02,
                    "temperature_locked", true,
                    "exposure_ms", _exposureMs,
                    "frames", _frames,
                    "grating", GratingString(_grating),
                    "center_wavelength_nm", _centerNm,
                    "exit_port", _exitPort,
                    "trigger_response", _triggerResponse,
                    "trigger_edge", _triggerEdge,
                    "output_signal", _outputSignal,
                    "shutter_mode", _shutterMode,
                    "roi", _roi.ToJson(),
                    "adc_mhz", _adcMhz,
                    "adc_quality", _adcQuality,
                    "timestamps", _timestamps,
                    "frame_tracking", _frameTracking,
                    "laser_line_nm", _laserNm,
                    "axis_ver", _axisVer,
                    "file_directory", _fileDir,
                    "base_name", _baseName,
                    "attach_date", _attachDate,
                    "attach_increment", _attachIncrement);
            }
        }

        private static string GratingString(int g)
        {
            // LightField's form for a turret position: "[500nm,1200][0][0]".
            switch (g)
            {
                case 1200: return "[500nm,1200][0][0]";
                case 600: return "[500nm,600][1][0]";
                default: return "[800nm,150][2][0]";
            }
        }

        private void RebuildAxis(bool emit)
        {
            lock (_gate)
            {
                double d = Dispersion[_grating];
                double q = -1.277e-6 * d / 0.04262;
                double mid = (_pixels - 1) / 2.0;
                _axis = new double[_pixels];
                for (int i = 0; i < _pixels; i++)
                {
                    double u = i - mid;
                    _axis[i] = _centerNm + d * u + q * u * u;
                }
                _axisVer++;
            }
            if (emit) EmitAxis();
        }

        private Dictionary<string, object> AxisData()
        {
            lock (_gate)
            {
                var bytes = new byte[_axis.Length * 8];
                Buffer.BlockCopy(_axis, 0, bytes, 0, bytes.Length);
                return J.Obj("axis_ver", _axisVer, "n", _axis.Length, "nm_b64", Convert.ToBase64String(bytes),
                             "laser_line_nm", _laserNm, "grating", GratingString(_grating), "center_nm", _centerNm);
            }
        }

        private void EmitAxis() { Emit("wavelength_axis", AxisData()); }

        protected override object ExecuteAction(CommandContext ctx)
        {
            switch (ctx.Action)
            {
                case "wavelength_axis": return AxisData();
                case "configure":
                    using (BeginExclusive(ctx, "configure", hubMotion: false)) { Configure(ctx); }
                    return Status(false);
                case "acquire": return Acquire(ctx);
                case "set_grating": return SetGrating(ctx);
                case "set_center_wavelength": return SetCenter(ctx);
                case "set_exit_port": return SetExitPort(ctx);
                case "get_settings": return GetSettings(ctx);
                case "set_settings": return SetSettings(ctx);
            }
            throw new DeviceError(ErrorCodes.NotSupported, ctx.Action);
        }

        /// <summary>
        /// Same vocabulary and refusals as the real driver (SpectrometerArgs); everything is checked before
        /// anything is applied, and nothing the caller gave is dropped.
        /// </summary>
        private void Configure(CommandContext ctx)
        {
            SpectrometerArgs.RejectUnknown(ctx);
            var exp = ctx.NumOpt("exposure_ms");
            var frames = ctx.IntOpt("frames");
            if (exp != null && (exp < 0.001 || exp > 3600000)) throw new DeviceError(ErrorCodes.InvalidArgs, "exposure_ms out of range (0.001 .. 3.6e6)");
            if (frames != null && (frames < 1 || frames > 1000000)) throw new DeviceError(ErrorCodes.InvalidArgs, "frames out of range (1 .. 1e6)");
            var adc = ctx.NumOpt("adc_mhz");
            if (adc != null && !new[] { 0.1, 1.0, 4.0 }.Any(a => Math.Abs(a - adc.Value) < 1e-9))
                throw new DeviceError(ErrorCodes.InvalidArgs, "adc_mhz must be one of 0.1, 1, 4 (sim)");
            string shutter = ctx.Has("shutter_mode") ? SpectrometerArgs.OneOf("shutter_mode", ctx.Str("shutter_mode"), SpectrometerArgs.ShutterModes) : null;
            string quality = ctx.Has("adc_quality") ? SpectrometerArgs.OneOf("adc_quality", ctx.Str("adc_quality"), SpectrometerArgs.AdcQualities) : null;
            string signal = ctx.Has("output_signal") ? SpectrometerArgs.OneOf("output_signal", ctx.Str("output_signal"), SpectrometerArgs.OutputSignals) : null;
            string stamps = ctx.Has("timestamps") ? SpectrometerArgs.TimeStamps(ctx.Args["timestamps"]) : null;
            bool? tracking = ctx.Has("frame_tracking") ? SpectrometerArgs.StrictBool("frame_tracking", ctx.Args["frame_tracking"]) : (bool?)null;
            var trig = ctx.Dict("trigger");
            string resp = trig != null && J.Has(trig, "response") ? SpectrometerArgs.OneOf("trigger.response", J.Str(trig, "response"), SpectrometerArgs.TriggerResponses) : null;
            string edge = trig != null && J.Has(trig, "edge") ? SpectrometerArgs.Edge(J.Str(trig, "edge")) : null;
            RoiSpec roi = null;
            if (ctx.Has("roi"))
            {
                roi = SpectrometerArgs.ParseRoi(ctx.Dict("roi"));
                CheckRoiFits(roi);
            }
            var file = ctx.Dict("file");
            string dir = file != null && J.Has(file, "directory") ? SpectrometerArgs.StrictString("file.directory", J.Get(file, "directory")) : null;
            string baseName = file != null && J.Has(file, "base_name") ? SpectrometerArgs.StrictString("file.base_name", J.Get(file, "base_name")) : null;
            bool? attachDate = file != null && J.Has(file, "attach_date") ? SpectrometerArgs.StrictBool("file.attach_date", J.Get(file, "attach_date")) : (bool?)null;
            bool? attachInc = file != null && J.Has(file, "attach_increment") ? SpectrometerArgs.StrictBool("file.attach_increment", J.Get(file, "attach_increment")) : (bool?)null;
            lock (_gate)
            {
                if (exp != null) _exposureMs = exp.Value;
                if (frames != null) _frames = frames.Value;
                if (adc != null) _adcMhz = adc.Value;
                if (shutter != null) _shutterMode = shutter;
                if (quality != null) _adcQuality = quality;
                if (signal != null) _outputSignal = signal;
                if (stamps != null) _timestamps = stamps;
                if (tracking != null) _frameTracking = tracking.Value;
                if (resp != null) _triggerResponse = resp;
                if (edge != null) _triggerEdge = edge;
                if (roi != null) _roi = roi;
                if (dir != null) _fileDir = dir;
                if (baseName != null) _baseName = baseName;
                if (attachDate != null) _attachDate = attachDate.Value;
                if (attachInc != null) _attachIncrement = attachInc.Value;
            }
        }

        private void CheckRoiFits(RoiSpec roi)
        {
            switch (roi.Mode)
            {
                case "line":
                    if (roi.Rows > SensorRows) throw new DeviceError(ErrorCodes.InvalidArgs, $"roi.rows must be 1..{SensorRows}");
                    break;
                case "binned":
                    if (roi.XBin > _pixels || roi.YBin > SensorRows) throw new DeviceError(ErrorCodes.InvalidArgs, $"roi binning larger than the sensor ({_pixels} x {SensorRows})");
                    break;
                case "custom":
                    foreach (var r in roi.Regions)
                    {
                        if (r[0] + r[2] > _pixels || r[1] + r[3] > SensorRows)
                            throw new DeviceError(ErrorCodes.InvalidArgs, $"roi region {Json.Serialize(RoiSpec.RegionJson(r))} is outside the {_pixels} x {SensorRows} sensor");
                        if (r[4] > r[2] || r[5] > r[3])
                            throw new DeviceError(ErrorCodes.InvalidArgs, $"roi binning larger than the region {Json.Serialize(RoiSpec.RegionJson(r))}");
                    }
                    break;
            }
        }

        private object Acquire(CommandContext ctx)
        {
            using (BeginExclusive(ctx, "acquire", hubMotion: true))
            {
                var exp = ctx.NumOpt("exposure_ms");
                var frames = ctx.IntOpt("frames");
                if (exp != null && (exp < 0.001 || exp > 3600000)) throw new DeviceError(ErrorCodes.InvalidArgs, "exposure_ms out of range");
                if (frames != null && (frames < 1 || frames > 1000000)) throw new DeviceError(ErrorCodes.InvalidArgs, "frames out of range");
                lock (_gate)
                {
                    if (exp != null) _exposureMs = exp.Value;
                    if (frames != null) _frames = frames.Value;
                }
                bool save = ctx.Bool("save", true);
                var baseName = ctx.Str("base_name");
                if (baseName != null) lock (_gate) _baseName = baseName;
                return RunAcquisition(ctx, manual: false, save: save, tag: ctx.Str("tag"));
            }
        }

        /// <summary>An acquisition someone started by hand in the (simulated) LightField window; hub settings stay as they are.</summary>
        public void SimulateManualAcquisition(int frames, double exposureMs)
        {
            _manualRunning = true;
            Host.StateChanged(this);
            Emit("manual_activity", J.Obj("what", "acquisition_started", "source", "lightfield_window"));
            try { RunAcquisition(null, manual: true, save: false, tag: null, framesOverride: frames, exposureOverride: exposureMs); }
            finally
            {
                _manualRunning = false;
                Host.StateChanged(this);
            }
        }

        private Dictionary<string, object> RunAcquisition(CommandContext ctx, bool manual, bool save, string tag,
                                                          int? framesOverride = null, double? exposureOverride = null)
        {
            double exposure;
            int frames;
            string resp;
            lock (_gate) { exposure = exposureOverride ?? _exposureMs; frames = framesOverride ?? _frames; resp = _triggerResponse; }
            _abort = false;
            _running = !manual;
            long started = Clock.UnixMs();
            long firstExposureUs = 0;
            string cmdId = ctx?.Id;
            Emit("acq_started", J.Obj("cmd_id", cmdId, "frames", frames, "exposure_ms", exposure, "started_ms", started,
                                      "manual", manual, "tag", tag, "trigger_response", resp, "axis_ver", _axisVer));
            var collected = new List<ushort[]>();
            double min = double.MaxValue, max = double.MinValue, sum = 0;
            long count = 0;
            bool aborted = false;
            string abortCode = null;
            try
            {
                double period = (exposure + _readoutMs) / 1000.0;
                if (resp == "ReadoutPerTrigger") period = Math.Max(period, 1.0 / Math.Max(0.1, _triggerHz));
                for (int f = 0; f < frames; f++)
                {
                    long t0 = Clock.UnixUs();
                    if (f == 0) firstExposureUs = t0;
                    bool ok = ctx != null ? ctx.Wait(period) : !WaitAbort(period);
                    if (!ok || _abort)
                    {
                        aborted = true;
                        abortCode = ctx?.CancelCode;
                        break;
                    }
                    var spec = MakeSpectrum(exposure);
                    collected.Add(spec);
                    foreach (var v in spec)
                    {
                        if (v < min) min = v;
                        if (v > max) max = v;
                        sum += v;
                    }
                    count += spec.Length;
                    long frameNo = Interlocked.Increment(ref _frameCounter);
                    var bytes = new byte[spec.Length * 2];
                    Buffer.BlockCopy(spec, 0, bytes, 0, bytes.Length);
                    int axisVer;
                    bool ts, ft;
                    lock (_gate) { axisVer = _axisVer; ts = _timestamps != "None"; ft = _frameTracking; }
                    var data = J.Obj("cmd_id", cmdId, "frame_index", f, "n", spec.Length, "dtype", "u16",
                                     "data_b64", Convert.ToBase64String(bytes), "axis_ver", axisVer, "manual", manual,
                                     "exposure_started_us", t0, "exposure_ended_us", t0 + (long)(exposure * 1000));
                    if (ft) data["frame_tracking"] = frameNo;
                    if (!ts) data["timestamps_source"] = "satellite_clock";
                    Emit("spectrum", data);
                    Emit("acq_progress", J.Obj("cmd_id", cmdId, "frame", f + 1, "frame_index", f, "frames", frames,
                                               "fraction", (f + 1) / (double)frames, "manual", manual));
                }
            }
            finally
            {
                _running = false;
            }
            long finished = Clock.UnixMs();
            var summary = J.Obj("min", count > 0 ? min : 0, "max", count > 0 ? max : 0, "mean", count > 0 ? sum / count : 0);
            var files = new List<object>();
            if (save && collected.Count > 0)
            {
                var path = SaveSpe(collected, exposure);
                files.Add(path);
                Host.NoteFileMeta(path, J.Obj("acq_started_ms", started, "acq_finished_ms", finished, "device", Id, "cmd_id", cmdId,
                                              "exposure_ms", exposure, "frames", collected.Count, "manual", manual, "tag", tag,
                                              "user_meta", ctx?.Dict("meta")));
            }
            Emit("acq_completed", J.Obj("cmd_id", cmdId, "frames", collected.Count, "files", files, "aborted", aborted,
                                        "started_ms", started, "finished_ms", finished, "manual", manual, "summary", summary));
            if (aborted && ctx != null)
                throw new DeviceError(abortCode ?? ErrorCodes.Cancelled, (ctx.CancelMessage ?? "acquisition stopped") + $" after {collected.Count}/{frames} frames");
            return J.Obj("frames", collected.Count, "files", files, "exposure_ms", exposure, "started_ms", started, "finished_ms", finished,
                         "summary", summary, "axis_ver", _axisVer, "first_exposure_started_us", firstExposureUs);
        }

        private bool WaitAbort(double seconds)
        {
            var end = Clock.MonoSeconds + seconds;
            while (Clock.MonoSeconds < end)
            {
                if (_abort) return true;
                Thread.Sleep(10);
            }
            return _abort;
        }

        private ushort[] MakeSpectrum(double exposureMs)
        {
            double[] axis;
            string exit, shutter;
            lock (_gate) { axis = (double[])_axis.Clone(); exit = _exitPort; shutter = _shutterMode; }
            bool light = exit == "front" && shutter != "AlwaysClosed";
            double s = exposureMs / 1000.0;
            var r = new ushort[axis.Length];
            for (int i = 0; i < axis.Length; i++)
            {
                double shift = 1e7 / _laserNm - 1e7 / axis[i]; // Raman shift, cm^-1
                double sig = 600 + 0.02 * i;
                if (light)
                {
                    sig += s * (300 + 40 * Math.Exp(-shift / 900.0));
                    sig += s * 2500 * Lorentz(shift, 520.7, 4.5);
                    sig += s * 160 * Lorentz(shift, 302, 25) + s * 220 * Lorentz(shift, 960, 30);
                }
                double noise = Math.Sqrt(Math.Max(1, sig - 600)) * Gauss() + 4 * Gauss();
                r[i] = (ushort)Math.Max(0, Math.Min(65535, Math.Round(sig + noise)));
            }
            lock (_gate)
            {
                if (_rng.NextDouble() < 0.02) r[_rng.Next(r.Length)] = 60000; // a cosmic ray now and then
            }
            return r;
        }

        private static double Lorentz(double x, double x0, double fwhm)
        {
            double g = fwhm / 2;
            return g * g / ((x - x0) * (x - x0) + g * g);
        }

        private double Gauss()
        {
            lock (_gate)
            {
                double u1 = 1.0 - _rng.NextDouble(), u2 = _rng.NextDouble();
                return Math.Sqrt(-2.0 * Math.Log(u1)) * Math.Cos(2 * Math.PI * u2);
            }
        }

        private string SaveSpe(List<ushort[]> frames, double exposure)
        {
            string dir, name;
            double[] axis;
            lock (_gate)
            {
                dir = Path.Combine(_fileDir, DateTime.Now.ToString("yyyy", CultureInfo.InvariantCulture), SimUtil.NowStamp());
                name = _baseName + (_attachIncrement ? " " + _increment++ : "");
                axis = (double[])_axis.Clone();
            }
            Directory.CreateDirectory(dir);
            var path = Path.Combine(dir, name + ".spe");
            while (File.Exists(path))
            {
                lock (_gate) name = _baseName + " " + _increment++;
                path = Path.Combine(dir, name + ".spe");
            }
            var settings = new Dictionary<string, string>(StringComparer.Ordinal)
            {
                ["Camera.ShutterTiming.ExposureTime"] = exposure.ToString("R", CultureInfo.InvariantCulture) + "|Double",
                ["Camera.ShutterTiming.Mode"] = _shutterMode + "|ShutterTimingMode",
                ["Camera.Sensor.Temperature.SetPoint"] = "-120|Double",
                ["Camera.Sensor.Temperature.Reading"] = "-120|Double",
                ["Camera.Sensor.Temperature.Status"] = "Locked|SensorTemperatureStatus",
                ["Camera.Adc.Speed"] = _adcMhz.ToString("R", CultureInfo.InvariantCulture) + "|Double",
                ["Camera.Adc.Quality"] = _adcQuality + "|AdcQuality",
                ["Camera.HardwareIO.Trigger.Response"] = _triggerResponse + "|TriggerResponse",
                ["Camera.HardwareIO.Trigger.Determination"] = _triggerEdge + "|TriggerDetermination",
                ["Camera.HardwareIO.OutputSignal"] = _outputSignal + "|OutputSignal",
                ["Camera.Experiment.Acquisition.FramesToStore"] = frames.Count.ToString(CultureInfo.InvariantCulture) + "|Int64",
                ["Camera.Experiment.FileNameGeneration.BaseFileName"] = _baseName + "|String",
                ["Camera.Experiment.FileNameGeneration.Directory"] = dir + "|String",
                ["Spectrometer.Grating.Selected"] = GratingString(_grating) + "|String",
                ["Spectrometer.Grating.CenterWavelength"] = _centerNm.ToString("R", CultureInfo.InvariantCulture) + "|Double",
                ["Spectrometer.OpticalPort.Exit.Selected"] = (_exitPort == "front" ? "FrontExit" : "SideExit") + "|OpticalPortLocation",
            };
            SpeFile.WriteU16(path, frames, axis, _laserNm, settings, DateTimeOffset.Now);
            return path;
        }

        private object SetGrating(CommandContext ctx)
        {
            using (BeginExclusive(ctx, "set_grating", hubMotion: true))
            {
                var g = ParseGrating(ctx.StrRequired("grating"));
                if (g != _grating)
                {
                    ctx.Sleep(_gratingMoveS);
                    lock (_gate) _grating = g;
                    RebuildAxis(emit: true);
                }
                return Status(false);
            }
        }

        private static int ParseGrating(string s)
        {
            foreach (var g in new[] { 1200, 600, 150 })
            {
                if (s == g.ToString(CultureInfo.InvariantCulture) || s.Contains("," + g + "]") || s.EndsWith("," + g, StringComparison.Ordinal)) return g;
            }
            throw new DeviceError(ErrorCodes.InvalidArgs, "unknown grating '" + s + "' (sim turret: 1200, 600, 150 g/mm)");
        }

        private object SetCenter(CommandContext ctx)
        {
            using (BeginExclusive(ctx, "set_center_wavelength", hubMotion: true))
            {
                var nm = ctx.Num("nm");
                if (nm < 200 || nm > 1100) throw new DeviceError(ErrorCodes.Limit, "center wavelength out of range (200 .. 1100 nm)");
                ctx.Sleep(_centerMoveS);
                lock (_gate) _centerNm = nm;
                RebuildAxis(emit: true);
                return Status(false);
            }
        }

        private object SetExitPort(CommandContext ctx)
        {
            using (BeginExclusive(ctx, "set_exit_port", hubMotion: true))
            {
                var p = ctx.StrRequired("port").ToLowerInvariant();
                if (p != "front" && p != "side") throw new DeviceError(ErrorCodes.InvalidArgs, "port must be front or side");
                ctx.Sleep(0.3);
                lock (_gate) _exitPort = p;
                return Status(false);
            }
        }

        private object GetSettings(CommandContext ctx)
        {
            var keys = (ctx.Args.TryGetValue("keys", out var k) ? k as List<object> : null) ?? new List<object>();
            var r = J.Obj();
            lock (_gate)
            {
                if (keys.Count == 0) keys = _settings.Keys.Cast<object>().ToList(); // like the reference sim: no keys = all
                foreach (var o in keys)
                {
                    var key = Convert.ToString(o, CultureInfo.InvariantCulture);
                    r[key] = _settings.TryGetValue(key, out var v) ? v : null;
                }
            }
            return r;
        }

        private object SetSettings(CommandContext ctx)
        {
            var values = ctx.Dict("values") ?? throw new DeviceError(ErrorCodes.InvalidArgs, "'values' (object) is required");
            foreach (var key in values.Keys)
                if (!_settable.Contains(key))
                    throw new DeviceError(ErrorCodes.InvalidArgs, "setting not in settable_keys: " + key);
            using (BeginExclusive(ctx, "set_settings", hubMotion: false))
            {
                lock (_gate) foreach (var kv in values) _settings[kv.Key] = kv.Value;
            }
            return J.Obj("applied", values.Keys.ToList());
        }

        protected override void StopHardware(bool emergency, bool hubOnly)
        {
            if (hubOnly && _manualRunning && !_running) return; // the operator's own acquisition keeps going
            _abort = true;
        }
    }

    // ------------------------------------------------------------------------------------
    // Rotation stage (stands in for KDC101 + PRM1-Z8)
    // ------------------------------------------------------------------------------------
    public sealed class SimRotationStage : DeviceBase
    {
        private static readonly string[] CapList = { "status", "stop", "safe", "move_abs", "move_rel", "home", "set_velocity", "identify" };
        private readonly object _gate = new object();
        private double _speed;
        private double _accel;
        private readonly double _maxVel;
        private readonly double _maxAccel;
        private readonly double _homeS;
        private readonly double _minDeg;
        private readonly double _maxDeg;
        private readonly bool _requireHome;
        private readonly double _axisHz;
        private double _pos;
        private bool _homed;
        private volatile bool _moving;
        private volatile bool _stop;
        private Timer _idle;

        public SimRotationStage(DeviceConfig cfg, IDeviceHost host) : base(cfg, host, "rotation_stage")
        {
            Model = "SIM KDC101 + PRM1-Z8";
            Serial = cfg.Str("serial", "SIM-POL");
            _speed = cfg.Num("speed_deg_s", 10);
            _accel = cfg.Num("accel_deg_s2", 10);
            _maxVel = cfg.Num("max_velocity_deg_s", 25);
            _maxAccel = cfg.Num("max_accel_deg_s2", 25);
            _homeS = cfg.Num("home_time_s", 3);
            _minDeg = cfg.Num("min_deg", -360);
            _maxDeg = cfg.Num("max_deg", 720);
            _requireHome = cfg.Bool("require_home_for_abs", true);
            _axisHz = Math.Min(20, cfg.Num("axis_state_hz", 20));
            _homed = cfg.Bool("sim_start_homed", false);
            _pos = cfg.Num("sim_start_deg", 0);
        }

        public override IReadOnlyList<string> Caps => CapList;
        protected override IEnumerable<string> VolatileStatusKeys => new[] { "position_deg" };
        public override Dictionary<string, object> Info => J.Obj("min_deg", _minDeg, "max_deg", _maxDeg, "counts_per_deg", 1919.64, "simulated", true);

        public override void Start()
        {
            SetState(StateReady);
            _idle = new Timer(_ => { if (!_moving) EmitAxis(); }, null, 1000, 1000);
        }

        public override void Shutdown() { _idle?.Dispose(); }

        private Dictionary<string, object> Snapshot()
        {
            lock (_gate) return J.Obj("position_deg", Math.Round(_pos, 4), "moving", _moving, "homed", _homed, "enabled", true,
                                      "velocity_deg_s", _speed, "accel_deg_s2", _accel);
        }

        private void EmitAxis()
        {
            var s = Snapshot();
            EmitAxisState(s, J.Bool(s, "moving", false));
        }

        protected override Dictionary<string, object> BuildStatus(bool fresh) { return Snapshot(); }

        protected override object ExecuteAction(CommandContext ctx)
        {
            switch (ctx.Action)
            {
                case "move_abs":
                {
                    var target = ctx.Num("deg");
                    CheckLimits(target);
                    if (_requireHome && !_homed) throw new DeviceError(ErrorCodes.Unavailable, "stage not homed since power-up: run 'home' first");
                    using (BeginExclusive(ctx, "move_abs", hubMotion: true)) MoveTo(ctx, target, _speed);
                    return Snapshot();
                }
                case "move_rel":
                {
                    var d = ctx.Num("deg");
                    double target;
                    lock (_gate) target = _pos + d;
                    CheckLimits(target);
                    using (BeginExclusive(ctx, "move_rel", hubMotion: true)) MoveTo(ctx, target, _speed);
                    return Snapshot();
                }
                case "home":
                {
                    using (BeginExclusive(ctx, "home", hubMotion: true))
                    {
                        double from;
                        lock (_gate) { from = _pos; _homed = false; }
                        MoveTo(ctx, 0, Math.Max(Math.Abs(from) / Math.Max(0.1, _homeS), 1));
                        lock (_gate) _homed = true;
                    }
                    return Snapshot();
                }
                case "set_velocity":
                {
                    var v = ctx.Num("deg_s");
                    var a = ctx.NumOpt("accel_deg_s2");
                    CheckVelocity(v, a, _maxVel, _maxAccel);
                    using (BeginExclusive(ctx, "set_velocity", hubMotion: false))
                        lock (_gate) { _speed = v; if (a != null) _accel = a.Value; }
                    return Snapshot();
                }
                case "identify":
                    Log.Info($"[{Id}] identify (simulated: the controller LED would flash)");
                    return Snapshot();
            }
            throw new DeviceError(ErrorCodes.NotSupported, ctx.Action);
        }

        /// <summary>set_velocity ranges shared with the real KDC101 driver (max_velocity_deg_s / max_accel_deg_s2); out of range = invalid_args, as in the reference satellite.</summary>
        internal static void CheckVelocity(double v, double? a, double maxVel, double maxAccel)
        {
            if (!(v > 0) || v > maxVel) throw new DeviceError(ErrorCodes.InvalidArgs, $"deg_s {F(v)} outside (0, {F(maxVel)}] (max_velocity_deg_s)");
            if (a != null && (!(a.Value > 0) || a.Value > maxAccel))
                throw new DeviceError(ErrorCodes.InvalidArgs, $"accel_deg_s2 {F(a.Value)} outside (0, {F(maxAccel)}] (max_accel_deg_s2)");
        }

        private void CheckLimits(double target)
        {
            if (target < _minDeg || target > _maxDeg)
                throw new DeviceError(ErrorCodes.Limit, $"target {F(target)} deg outside soft limits [{F(_minDeg)}, {F(_maxDeg)}]");
        }

        private void MoveTo(CommandContext ctx, double target, double speed)
        {
            _stop = false;
            _moving = true;
            Host.StateChanged(this);
            double dt = 1.0 / _axisHz;
            try
            {
                while (true)
                {
                    double pos;
                    lock (_gate) pos = _pos;
                    double remaining = target - pos;
                    if (Math.Abs(remaining) < 1e-6) break;
                    if (!ctx.Wait(dt) || _stop) break;
                    double step = Math.Sign(remaining) * Math.Min(Math.Abs(remaining), speed * dt);
                    lock (_gate) _pos += step;
                    EmitAxis();
                }
            }
            finally
            {
                _moving = false;
                EmitAxis();
                Host.StateChanged(this);
            }
            ctx.Check();
            if (_stop) throw new DeviceError(ErrorCodes.Cancelled, "motion stopped");
        }

        protected override void StopHardware(bool emergency, bool hubOnly) { _stop = true; }
    }

    // ------------------------------------------------------------------------------------
    // Open-loop stepper (stands in for the Picomotor 8742)
    // ------------------------------------------------------------------------------------
    public sealed class SimOpenLoopStepper : DeviceBase
    {
        private static readonly string[] CapList = { "status", "stop", "safe", "jog", "set_zero", "move_abs", "set_velocity", "set_accel", "abort" };
        private readonly object _gate = new object();
        private readonly int _axes;
        private readonly int _maxSteps;
        private readonly int _maxVel;
        private readonly int _maxAcc;
        private readonly int[] _vel;
        private readonly int[] _acc;
        private readonly long[] _steps;
        private readonly bool[] _moving;
        private volatile int _stopMask;
        private Timer _idle;

        public SimOpenLoopStepper(DeviceConfig cfg, IDeviceHost host) : base(cfg, host, "openloop_stepper")
        {
            Model = "SIM Picomotor 8742";
            Serial = "SIM-PICO";
            _axes = Math.Max(1, Math.Min(4, cfg.Int("axes", 4)));
            _maxSteps = cfg.Int("max_steps_per_cmd", 2000);
            _maxVel = cfg.Int("max_velocity_steps_s", 2000);
            _maxAcc = cfg.Int("max_accel_steps_s2", 200000);
            _steps = new long[_axes + 1];
            _moving = new bool[_axes + 1];
            _vel = new int[_axes + 1];
            _acc = new int[_axes + 1];
            for (int a = 1; a <= _axes; a++)
            {
                _vel[a] = (int)Math.Round(cfg.Num("steps_per_s", 2000));
                _acc[a] = cfg.Int("accel_steps_s2", 100000);
            }
        }

        public override IReadOnlyList<string> Caps => CapList;
        protected override IEnumerable<string> AlwaysAdmitted => new[] { "status", "stop", "safe", "abort" };
        protected override IEnumerable<string> VolatileStatusKeys => new[] { "axes" };
        public override Dictionary<string, object> Info => J.Obj("axes", _axes, "max_steps_per_cmd", _maxSteps, "simulated", true, "open_loop", true);

        public override void Start()
        {
            SetState(StateReady);
            _idle = new Timer(_ => { if (!AnyMoving()) EmitAxis(); }, null, 1000, 1000);
        }

        public override void Shutdown() { _idle?.Dispose(); }

        private bool AnyMoving() { lock (_gate) return _moving.Any(m => m); }

        private Dictionary<string, object> Snapshot()
        {
            var axes = J.Obj();
            lock (_gate)
                for (int a = 1; a <= _axes; a++)
                    axes[a.ToString(CultureInfo.InvariantCulture)] = J.Obj("steps", _steps[a], "moving", _moving[a],
                                                                            "velocity_steps_s", _vel[a], "accel_steps_s2", _acc[a]);
            return J.Obj("axes", axes);
        }

        private void EmitAxis() { EmitAxisState(Snapshot(), AnyMoving()); }

        protected override Dictionary<string, object> BuildStatus(bool fresh) { return Snapshot(); }

        private int Axis(CommandContext ctx)
        {
            int a = ctx.Int("axis");
            if (a < 1 || a > _axes) throw new DeviceError(ErrorCodes.InvalidArgs, $"axis must be 1..{_axes}");
            return a;
        }

        protected override object ExecuteAction(CommandContext ctx)
        {
            switch (ctx.Action)
            {
                case "jog":
                {
                    int a = Axis(ctx);
                    int steps = ctx.Int("steps");
                    if (steps == 0) return Snapshot();
                    if (Math.Abs(steps) > _maxSteps) throw new DeviceError(ErrorCodes.Limit, $"|steps| {Math.Abs(steps)} exceeds max_steps_per_cmd {_maxSteps}");
                    using (BeginExclusive(ctx, "jog", hubMotion: true)) Jog(ctx, a, steps);
                    return Snapshot();
                }
                case "set_zero":
                {
                    int a = Axis(ctx);
                    using (BeginExclusive(ctx, "set_zero", hubMotion: false)) lock (_gate) _steps[a] = 0;
                    EmitAxis();
                    return Snapshot();
                }
                case "move_abs":
                {
                    // Target = the satellite's step count since set_zero; executed as the relative move to it.
                    int a = Axis(ctx);
                    long target = ctx.Int("steps");
                    long delta;
                    lock (_gate) delta = target - _steps[a];
                    if (delta == 0) return Snapshot();
                    if (Math.Abs(delta) > _maxSteps)
                        throw new DeviceError(ErrorCodes.Limit, $"move_abs needs {delta} steps on axis {a}, more than max_steps_per_cmd {_maxSteps}");
                    using (BeginExclusive(ctx, "move_abs", hubMotion: true)) Jog(ctx, a, (int)delta);
                    return Snapshot();
                }
                case "set_velocity":
                case "set_accel":
                {
                    int a = Axis(ctx);
                    bool vel = ctx.Action == "set_velocity";
                    int v = ctx.Int(vel ? "steps_s" : "steps_s2");
                    int max = vel ? _maxVel : _maxAcc;
                    if (v < 1 || v > max)
                        throw new DeviceError(ErrorCodes.InvalidArgs, $"{(vel ? "steps_s" : "steps_s2")} {v} outside 1..{max} ({(vel ? "max_velocity_steps_s" : "max_accel_steps_s2")})");
                    using (BeginExclusive(ctx, ctx.Action, hubMotion: false))
                        lock (_gate) { if (vel) _vel[a] = v; else _acc[a] = v; }
                    EmitAxis();
                    return Snapshot();
                }
                case "abort":
                {
                    // All axes at once, no deceleration; the running jog / move_abs ends as cancelled.
                    CancelActive(ErrorCodes.Cancelled, "aborted by hub (all axes, no deceleration)");
                    _stopMask = ~0;
                    var end = Clock.MonoSeconds + 1.0;
                    while (AnyMoving() && Clock.MonoSeconds < end) Thread.Sleep(10);
                    return Snapshot();
                }
            }
            throw new DeviceError(ErrorCodes.NotSupported, ctx.Action);
        }

        private void Jog(CommandContext ctx, int a, int steps)
        {
            _stopMask = 0;
            lock (_gate) _moving[a] = true;
            Host.StateChanged(this);
            int done = 0;
            int dir = Math.Sign(steps);
            try
            {
                while (done < Math.Abs(steps))
                {
                    if (!ctx.Wait(0.05) || (_stopMask & (1 << a)) != 0) break;
                    int rate;
                    lock (_gate) rate = _vel[a];
                    int chunk = (int)Math.Min(Math.Abs(steps) - done, Math.Max(1, rate * 0.05));
                    done += chunk;
                    lock (_gate) _steps[a] += dir * chunk;
                    EmitAxis();
                }
            }
            finally
            {
                lock (_gate) _moving[a] = false;
                EmitAxis();
                Host.StateChanged(this);
            }
            ctx.Check();
            if (done < Math.Abs(steps)) throw new DeviceError(ErrorCodes.Cancelled, $"stopped after {done} of {Math.Abs(steps)} steps");
        }

        protected override void DoStop(CommandContext ctx)
        {
            if (ctx.Has("axis"))
            {
                int a = Axis(ctx);
                _stopMask |= 1 << a;
                return;
            }
            base.DoStop(ctx);
        }

        protected override void StopHardware(bool emergency, bool hubOnly) { _stopMask = ~0; }
    }

    // ------------------------------------------------------------------------------------
    // XYZ stage (stands in for UIRobot via Motion_Manipulator)
    // ------------------------------------------------------------------------------------
    public sealed class SimXyzStage : DeviceBase
    {
        private static readonly string[] CapList = { "status", "stop", "safe", "move_abs", "move_rel" };
        private static readonly string[] AxisNames = { "x", "y", "z" };
        private readonly object _gate = new object();
        private readonly double[] _pos = new double[3];
        private readonly double[] _min = { -2, 0, 0 };
        private readonly double[] _max = { 13.3, 5, 9.6 };
        private readonly double _maxStep;
        private readonly double _speed;
        private volatile bool _moving;
        private volatile bool _stop;
        private Timer _idle;

        public SimXyzStage(DeviceConfig cfg, IDeviceHost host) : base(cfg, host, "xyz_stage")
        {
            Model = "SIM UIRobot XYZ";
            Serial = "SIM-XYZ";
            var lim = cfg.Dict("limits_mm");
            for (int i = 0; i < 3; i++)
            {
                var l = lim != null ? J.List(lim, AxisNames[i]) : null;
                if (l != null && l.Count == 2)
                {
                    _min[i] = J.ToDouble(l[0], _min[i]);
                    _max[i] = J.ToDouble(l[1], _max[i]);
                }
                _pos[i] = Math.Max(_min[i], Math.Min(_max[i], cfg.Num("sim_start_" + AxisNames[i], (_min[i] + _max[i]) / 2)));
            }
            _maxStep = cfg.Num("max_step_mm", 0.5);
            _speed = cfg.Num("speed_mm_s", 0.75);
        }

        public override IReadOnlyList<string> Caps => CapList;
        protected override IEnumerable<string> VolatileStatusKeys => new[] { "x_mm", "y_mm", "z_mm" };
        public override Dictionary<string, object> Info => J.Obj("limits_mm", J.Obj("x", new[] { _min[0], _max[0] }, "y", new[] { _min[1], _max[1] }, "z", new[] { _min[2], _max[2] }),
                                                                "max_step_mm", _maxStep, "simulated", true, "open_loop", true);

        public override void Start()
        {
            SetState(StateReady);
            _idle = new Timer(_ => { if (!_moving) EmitAxis(); }, null, 1000, 1000);
        }

        public override void Shutdown() { _idle?.Dispose(); }

        private Dictionary<string, object> Snapshot()
        {
            lock (_gate) return J.Obj("x_mm", Math.Round(_pos[0], 5), "y_mm", Math.Round(_pos[1], 5), "z_mm", Math.Round(_pos[2], 5), "moving", _moving);
        }

        private void EmitAxis()
        {
            var s = Snapshot();
            EmitAxisState(s, J.Bool(s, "moving", false));
        }

        protected override Dictionary<string, object> BuildStatus(bool fresh) { return Snapshot(); }

        protected override object ExecuteAction(CommandContext ctx)
        {
            if (ctx.Action != "move_abs" && ctx.Action != "move_rel") throw new DeviceError(ErrorCodes.NotSupported, ctx.Action);
            bool rel = ctx.Action == "move_rel";
            var target = new double[3];
            bool any = false;
            lock (_gate) Array.Copy(_pos, target, 3);
            for (int i = 0; i < 3; i++)
            {
                var key = rel ? "d" + AxisNames[i] : AxisNames[i];
                var v = ctx.NumOpt(key);
                if (v == null) continue;
                any = true;
                target[i] = rel ? target[i] + v.Value : v.Value;
                if (target[i] < _min[i] || target[i] > _max[i])
                    throw new DeviceError(ErrorCodes.Limit, $"{AxisNames[i]} target {F(target[i])} mm outside soft limits [{F(_min[i])}, {F(_max[i])}]");
                double cur;
                lock (_gate) cur = _pos[i];
                if (Math.Abs(target[i] - cur) > _maxStep + 1e-12)
                    throw new DeviceError(ErrorCodes.Limit, $"{AxisNames[i]} step {F(Math.Abs(target[i] - cur))} mm exceeds max_step_mm {F(_maxStep)}");
            }
            if (!any) throw new DeviceError(ErrorCodes.InvalidArgs, rel ? "give at least one of dx, dy, dz" : "give at least one of x, y, z");
            using (BeginExclusive(ctx, ctx.Action, hubMotion: true))
            {
                _stop = false;
                _moving = true;
                Host.StateChanged(this);
                try
                {
                    while (true)
                    {
                        double maxRem = 0;
                        lock (_gate) for (int i = 0; i < 3; i++) maxRem = Math.Max(maxRem, Math.Abs(target[i] - _pos[i]));
                        if (maxRem < 1e-9) break;
                        if (!ctx.Wait(0.05) || _stop) break;
                        lock (_gate)
                            for (int i = 0; i < 3; i++)
                            {
                                double r = target[i] - _pos[i];
                                _pos[i] += Math.Sign(r) * Math.Min(Math.Abs(r), _speed * 0.05);
                            }
                        EmitAxis();
                    }
                }
                finally
                {
                    _moving = false;
                    EmitAxis();
                    Host.StateChanged(this);
                }
                ctx.Check();
                if (_stop) throw new DeviceError(ErrorCodes.Cancelled, "motion stopped");
            }
            return Snapshot();
        }

        protected override void StopHardware(bool emergency, bool hubOnly) { _stop = true; }
    }

    // ------------------------------------------------------------------------------------
    // AOTF (stands in for the Brimrose DS1)
    // ------------------------------------------------------------------------------------
    public sealed class SimAotf : DeviceBase
    {
        private static readonly string[] CapList = { "status", "stop", "safe", "set", "rf_off" };
        private readonly object _gate = new object();
        private readonly Dictionary<int, Dictionary<string, object>> _ch = new Dictionary<int, Dictionary<string, object>>();

        public SimAotf(DeviceConfig cfg, IDeviceHost host) : base(cfg, host, "aotf")
        {
            Model = "SIM DS1";
            Serial = "SIM-AOTF";
        }

        public override IReadOnlyList<string> Caps => CapList;
        public override Dictionary<string, object> Info => J.Obj("range_nm", new[] { 550, 1000 }, "range_mhz", new[] { 85, 170 }, "simulated", true);

        public override void Start() { SetState(StateReady); }

        // Tuning roughly linear in 1/lambda between the DS1 end points (550 nm ~ 170 MHz, 1000 nm ~ 85 MHz).
        public static double MhzFromNm(double nm) { return 85 + 85 * (1 / nm - 1 / 1000.0) / (1 / 550.0 - 1 / 1000.0); }
        public static double NmFromMhz(double mhz) { return 1 / ((mhz - 85) / 85 * (1 / 550.0 - 1 / 1000.0) + 1 / 1000.0); }

        protected override Dictionary<string, object> BuildStatus(bool fresh)
        {
            var c = J.Obj();
            lock (_gate) foreach (var kv in _ch) c[kv.Key.ToString(CultureInfo.InvariantCulture)] = new Dictionary<string, object>(kv.Value);
            return J.Obj("channels", c, "protocol_known", true);
        }

        protected override object ExecuteAction(CommandContext ctx)
        {
            switch (ctx.Action)
            {
                case "set":
                {
                    int ch = ctx.IntOpt("channel") ?? 0;
                    if (ch < 0 || ch > 7) throw new DeviceError(ErrorCodes.InvalidArgs, "channel must be 0..7");
                    var nm = ctx.NumOpt("wavelength_nm");
                    var mhz = ctx.NumOpt("frequency_mhz");
                    if ((nm == null) == (mhz == null)) throw new DeviceError(ErrorCodes.InvalidArgs, "give exactly one of wavelength_nm or frequency_mhz");
                    if (nm != null && (nm < 550 || nm > 1000)) throw new DeviceError(ErrorCodes.Limit, "wavelength_nm outside 550..1000");
                    if (mhz != null && (mhz < 85 || mhz > 170)) throw new DeviceError(ErrorCodes.Limit, "frequency_mhz outside 85..170");
                    double amp = ctx.NumOpt("amplitude") ?? 1.0;
                    if (amp < 0 || amp > 1) throw new DeviceError(ErrorCodes.Limit, "amplitude must be 0..1");
                    double f = mhz ?? MhzFromNm(nm.Value);
                    lock (_gate) _ch[ch] = J.Obj("frequency_mhz", Math.Round(f, 4), "wavelength_nm", Math.Round(nm ?? NmFromMhz(f), 3), "amplitude", amp, "rf_on", true);
                    return BuildStatus(false);
                }
                case "rf_off":
                    RfOff();
                    return BuildStatus(false);
            }
            throw new DeviceError(ErrorCodes.NotSupported, ctx.Action);
        }

        private void RfOff()
        {
            lock (_gate) foreach (var c in _ch.Values) { c["rf_on"] = false; c["amplitude"] = 0.0; }
        }

        protected override void StopHardware(bool emergency, bool hubOnly)
        {
            if (emergency) RfOff();
        }
    }

    // ------------------------------------------------------------------------------------
    // PMT (stands in for SpectraSense + SpectraHub)
    // ------------------------------------------------------------------------------------
    public sealed class SimPmt : DeviceBase
    {
        private static readonly string[] CapList = { "status", "stop", "safe", "read", "hv_off", "set_hv" };
        private readonly object _gate = new object();
        private readonly double _maxHv;
        private readonly Random _rng = new Random(7);
        private bool _hvOn;
        private double _hv;
        private double _last;
        private bool _hvByHub;

        public SimPmt(DeviceConfig cfg, IDeviceHost host) : base(cfg, host, "pmt")
        {
            Model = "SIM SpectraHub PMT";
            Serial = "SIM-PMT";
            _maxHv = cfg.Num("max_hv", 1000);
        }

        public override IReadOnlyList<string> Caps => CapList;
        public override Dictionary<string, object> Info => J.Obj("max_hv", _maxHv, "simulated", true);

        public override void Start() { SetState(StateReady); }

        protected override Dictionary<string, object> BuildStatus(bool fresh)
        {
            lock (_gate) return J.Obj("hv_on", _hvOn, "hv_volts", _hv, "last_value", _last);
        }

        protected override object ExecuteAction(CommandContext ctx)
        {
            switch (ctx.Action)
            {
                case "read":
                {
                    double ms = ctx.NumOpt("integration_ms") ?? 100;
                    if (ms < 5 || ms > 64000) throw new DeviceError(ErrorCodes.InvalidArgs, "integration_ms must be 5..64000");
                    using (BeginExclusive(ctx, "read", hubMotion: true))
                    {
                        ctx.Sleep(ms / 1000.0);
                        double v;
                        lock (_gate)
                        {
                            v = _hvOn ? 1000 * ms / 1000.0 * (1 + 0.05 * (_rng.NextDouble() - 0.5)) * Math.Pow(_hv / 500.0, 6) : _rng.NextDouble() * 2;
                            _last = v;
                        }
                        return J.Obj("value", v, "unit", "counts", "integration_ms", ms);
                    }
                }
                case "set_hv":
                {
                    var v = ctx.Num("volts");
                    if (v < 0 || v > _maxHv) throw new DeviceError(ErrorCodes.Limit, $"volts must be 0..{F(_maxHv)}");
                    lock (_gate) { _hv = v; _hvOn = v > 0; _hvByHub = ctx.FromHub; }
                    return BuildStatus(false);
                }
                case "hv_off":
                    lock (_gate) { _hvOn = false; _hv = 0; }
                    return BuildStatus(false);
            }
            throw new DeviceError(ErrorCodes.NotSupported, ctx.Action);
        }

        protected override void StopHardware(bool emergency, bool hubOnly)
        {
            lock (_gate)
            {
                if (emergency || (_hvByHub && Config.Bool("safe_hv_off", true))) { _hvOn = false; _hv = 0; }
            }
        }
    }
}
