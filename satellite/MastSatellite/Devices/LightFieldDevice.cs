using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Threading;
using MastSatellite.Core;

namespace MastSatellite.Devices
{
    /// <summary>
    /// LightField 6 via the Automation V5 SDK, bound at runtime from %LIGHTFIELD_ROOT%:
    ///   PrincetonInstruments.LightField.AutomationV5.dll       (Automation class)
    ///   AddInViews\PrincetonInstruments.LightFieldViewV5.dll   (ILightFieldApplication, IExperiment, ...)
    ///   PrincetonInstruments.LightFieldAddInSupportServices.dll (setting keys, enums)
    /// Threading follows the SDK samples: the Automation object is created and called on one STA
    /// thread with a message loop (the C# automation sample creates it on its WPF UI thread);
    /// event handlers only copy data and enqueue, and never call Acquire/Stop themselves ("we can
    /// not call acquire in the completed handler", AddIn_AsyncAcquireSample). Automation always
    /// starts a NEW LightField instance (it cannot attach), visible, in the desktop session; the
    /// operator can work in that same window. Refuses to start in session 0.
    /// </summary>
    public sealed class LightFieldDevice : DeviceBase, ILocalActions
    {
        private static readonly string[] CapList =
        {
            "status", "stop", "safe", "wavelength_axis", "configure", "acquire", "set_grating",
            "set_center_wavelength", "set_exit_port", "get_settings", "set_settings",
        };

        // Setting keys (string values of the AddInSupportServices constants, LightField 6.18).
        private const string KExposure = "Camera.ShutterTiming.ExposureTime";
        private const string KShutterMode = "Camera.ShutterTiming.Mode";
        private const string KFrames = "Camera.Experiment.Acquisition.FramesToStore";
        private const string KTempReading = "Camera.Sensor.Temperature.Reading";
        private const string KTempStatus = "Camera.Sensor.Temperature.Status";
        private const string KTempSetPoint = "Camera.Sensor.Temperature.SetPoint";
        private const string KAdcSpeed = "Camera.Adc.Speed";
        private const string KAdcQuality = "Camera.Adc.Quality";
        private const string KTrigResponse = "Camera.HardwareIO.Trigger.Response";
        private const string KTrigDetermination = "Camera.HardwareIO.Trigger.Determination";
        private const string KOutputSignal = "Camera.HardwareIO.OutputSignal";
        private const string KTimeStamps = "Camera.Acquisition.TimeStamping.Stamps";
        private const string KFrameTracking = "Camera.Acquisition.FrameTracking.Enabled";
        private const string KRoiSelection = "Camera.ReadoutControl.RegionsOfInterest.Selection";
        private const string KRoiResult = "Camera.ReadoutControl.RegionsOfInterest.Result";
        private const string KReadoutTime = "Camera.ReadoutControl.Time";
        private const string KDir = "Camera.Experiment.FileNameGeneration.Directory";
        private const string KBaseName = "Camera.Experiment.FileNameGeneration.BaseFileName";
        private const string KAttachDate = "Camera.Experiment.FileNameGeneration.AttachDate";
        private const string KAttachIncrement = "Camera.Experiment.FileNameGeneration.AttachIncrement";
        private const string KOutputFiles = "Camera.Experiment.Acquisition.OutputFiles.Result";
        private const string KGrating = "Spectrometer.Grating.Selected";
        private const string KCenter = "Spectrometer.Grating.CenterWavelength";
        private const string KExit = "Spectrometer.OpticalPort.Exit.Selected";

        private static readonly string[] WatchedSettings =
        {
            KExposure, KShutterMode, KFrames, KAdcSpeed, KAdcQuality, KTrigResponse, KTrigDetermination, KOutputSignal,
            KTimeStamps, KFrameTracking, KRoiSelection, KRoiResult, KGrating, KCenter, KExit, KTempSetPoint, KDir, KBaseName,
        };

        private sealed class Acq
        {
            public string CmdId;
            public bool Manual;
            public int Frames;
            public long StartedMs;
            public int FrameIndex;
            public double Min = double.MaxValue, Max = double.MinValue, Sum;
            public long Count;
            public readonly ManualResetEventSlim Started = new ManualResetEventSlim(false);
            public readonly ManualResetEventSlim Completed = new ManualResetEventSlim(false);
            public readonly ManualResetEventSlim LfCompleted = new ManualResetEventSlim(false); // LightField's ExperimentCompleted seen
            public double LastManualFrameMono;
            public long FirstExposureUs;
            public int EventFrames;            // frames taken from ImageDataSetReceived (under lock(this))
            public bool DatasetDelivered;      // frames taken from Capture's returned set instead (under lock(this))

            public Dictionary<string, object> Summary()
            {
                return J.Obj("min", Count > 0 ? Min : 0, "max", Count > 0 ? Max : 0, "mean", Count > 0 ? Sum / Count : 0);
            }
        }

        private sealed class FrameItem
        {
            public Acq Acq;
            public object Data;      // ushort[] or float[]
            public string Dtype;
            public int N;
            public long? StartedUs, EndedUs, Tracking;
            public long? OriginMs;   // IImageDataSet.TimeStampOrigin; ExposureStarted/Ended are relative to it
            public long ReceivedUs;
            public bool CompletedMarker;
        }

        private readonly object _gate = new object();
        private readonly string _root;
        private readonly bool _visible;
        private readonly bool _suppressDialogs;
        private readonly string _experimentName;
        private readonly double _laserNmCfg;
        private readonly bool _requireLocked;
        private readonly double _manualMaxHz;
        private readonly string _fileDirectoryTemplate;
        private readonly HashSet<string> _settable;
        private readonly BlockingCollection<FrameItem> _frames = new BlockingCollection<FrameItem>(new ConcurrentQueue<FrameItem>(), 4096);
        private readonly Dictionary<string, double> _ownChanges = new Dictionary<string, double>(StringComparer.Ordinal);
        private readonly HashSet<int> _ownPids = new HashSet<int>();
        private StaWorker _sta;
        private Thread _frameThread;
        private Timer _poll;
        private Type _tAutomation, _tApp, _tExperiment, _tFileManager, _tUim, _tDataSet, _tImageData, _tRoi;
        private object _automation, _app, _experiment, _fileManager;
        private Delegate _hStarted, _hCompleted, _hData, _hSetting, _hClosed, _hUpdated;
        private Dictionary<string, object> _status = J.Obj();
        private double[] _calibration;
        private double[] _axis;
        private int _axisVer;
        private string _axisKey;
        private volatile Acq _hubAcq;
        private volatile Acq _lastHubAcq;
        private double _lastHubAcqEndMono;
        private volatile Acq _manualAcq;
        private volatile bool _expectHubStart;
        private volatile bool _running;
        private volatile bool _started;
        private volatile bool _starting;
        private volatile bool _closedByOperator;
        private bool _assembliesLoaded;

        public LightFieldDevice(DeviceConfig cfg, IDeviceHost host) : base(cfg, host, "spectrometer")
        {
            _root = cfg.Path("lightfield_root", null) ?? Environment.GetEnvironmentVariable("LIGHTFIELD_ROOT") ?? @"C:\Program Files\Princeton Instruments\LightField\";
            _visible = cfg.Bool("visible", true);
            _suppressDialogs = cfg.Bool("suppress_dialogs", true);
            _experimentName = cfg.Str("experiment", "");
            _laserNmCfg = cfg.Num("laser_line_nm", double.NaN); // a site value: unset means "unknown" (sent as null)
            _requireLocked = cfg.Bool("require_temperature_locked", true);
            _manualMaxHz = cfg.Num("manual_frame_max_hz", 5);
            // Must stay under the spe_watcher root so hub-driven .spe files are picked up and uploaded.
            _fileDirectoryTemplate = cfg.Str("file_directory", @"D:\Data\{yyyy}\{yyyyMMdd}");
            _settable =new HashSet<string>((cfg.List("settable_keys") ?? new List<object>()).Select(o => Convert.ToString(o, CultureInfo.InvariantCulture)), StringComparer.Ordinal);
            Model = "LightField (Automation V5)";
        }

        public override IReadOnlyList<string> Caps => CapList;
        protected override bool IsBusyExternally => _manualAcq != null;
        protected override IEnumerable<string> VolatileStatusKeys => new[] { "temperature_c", "progress" };
        public IReadOnlyList<string> LocalActions => new[] { "启动/重启 LightField" };

        public override Dictionary<string, object> Info => J.Obj("lightfield_root", _root, "experiment", _experimentName,
            "visible", _visible, "suppress_dialogs", _suppressDialogs, "laser_line_nm", _laserNmCfg, "file_directory", _fileDirectoryTemplate,
            "settable_keys", _settable.ToList());

        // ------------------------------------------------------------------ start / stop

        public override void Start()
        {
            _frameThread = new Thread(FrameLoop) { IsBackground = true, Name = "lightfield-frames" };
            _frameThread.Start();
            _poll = new Timer(_ => Poll(), null, 2000, 2000);
            StartLightField();
        }

        public void RunLocalAction(string name)
        {
            ThreadPool.QueueUserWorkItem(_ =>
            {
                _closedByOperator = false;
                if (_started) StopLightField("restart requested at the satellite");
                StartLightField();
            });
        }

        private static int[] LightFieldPids()
        {
            return Process.GetProcessesByName("PrincetonInstruments.LightField").Select(p => p.Id).ToArray();
        }

        private void StartLightField()
        {
            lock (_gate)
            {
                if (_started || _starting) return;
                _starting = true;
            }
            try
            {
                if (!SessionInfo.HasDesktop)
                {
                    SetState(StateError, "LightField Automation needs the logged-on user's desktop; this process runs in session " + SessionInfo.SessionId + " (start the satellite from the logon task, not over SSH)");
                    return;
                }
                var foreign = LightFieldPids().Where(p => !_ownPids.Contains(p)).ToArray();
                if (foreign.Length > 0)
                {
                    SetState(StateNotStarted, "a LightField window opened by hand is running (pid " + string.Join(",", foreign) + "); close it so the satellite can host LightField");
                    return;
                }
                if (!LoadAssemblies()) return;
                SetState(StateNotStarted, "starting LightField ...");
                if (_sta == null) _sta = new StaWorker("lightfield-sta");
                var before = new HashSet<int>(LightFieldPids());
                _sta.Invoke(() =>
                {
                    _automation = Activator.CreateInstance(_tAutomation, _visible, new List<string>());
                    _app = LateBound.Get(_automation, _tAutomation, "LightFieldApplication");
                    _experiment = LateBound.Get(_app, _tApp, "Experiment");
                    _fileManager = LateBound.Get(_app, _tApp, "FileManager");
                    var uim = LateBound.Get(_app, _tApp, "UserInteractionManager");
                    if (_suppressDialogs) LateBound.Set(uim, _tUim, "SuppressUserInteraction", true);
                }, TimeSpan.FromSeconds(180));
                foreach (var p in LightFieldPids()) if (!before.Contains(p)) _ownPids.Add(p);

                if (!string.IsNullOrEmpty(_experimentName))
                {
                    bool ok = _sta.Invoke(() => (bool)LateBound.Call(_experiment, _tExperiment, "Load", _experimentName), TimeSpan.FromSeconds(120));
                    if (!ok) Log.Warn($"[{Id}] LightField could not load experiment '{_experimentName}'");
                    WaitNotUpdating(TimeSpan.FromSeconds(120));
                }
                _sta.Invoke(() =>
                {
                    _hStarted = LateBound.Subscribe(_experiment, _tExperiment, "ExperimentStarted", this, nameof(OnExperimentStarted));
                    _hCompleted = LateBound.Subscribe(_experiment, _tExperiment, "ExperimentCompleted", this, nameof(OnExperimentCompleted));
                    _hData = LateBound.Subscribe(_experiment, _tExperiment, "ImageDataSetReceived", this, nameof(OnImageDataSetReceived));
                    _hSetting = LateBound.Subscribe(_experiment, _tExperiment, "SettingChanged", this, nameof(OnSettingChanged));
                    _hUpdated = LateBound.Subscribe(_experiment, _tExperiment, "ExperimentUpdated", this, nameof(OnExperimentUpdated));
                    _hClosed = LateBound.Subscribe(_automation, _tAutomation, "LightFieldClosed", this, nameof(OnLightFieldClosed));
                    LateBound.Call(_experiment, _tExperiment, "FilterSettingChanged", (IList<string>)WatchedSettings.ToList());
                }, TimeSpan.FromSeconds(30));
                _started = true;
                RefreshStatus();
                RefreshAxis(force: true);
                if (!HasCamera())
                {
                    SetState(StateError, "the LightField experiment has no camera; load an experiment with the PyLoN (config 'experiment')");
                    return;
                }
                ApplyConfiguredFileDirectory();
                RefreshStatus();
                SetState(StateReady);
                Log.Info($"[{Id}] LightField hosted (pids {string.Join(",", _ownPids)}), experiment '{_status.GetValueOrDefaultStr("experiment_name")}', spe dir '{_status.GetValueOrDefaultStr("file_directory")}'");
            }
            catch (Exception ex)
            {
                Log.Error($"[{Id}] LightField start failed", ex);
                SetState(StateError, "LightField start failed: " + ex.Message);
            }
            finally
            {
                lock (_gate) _starting = false;
            }
        }

        private bool LoadAssemblies()
        {
            if (_assembliesLoaded) return true;
            var root = _root.TrimEnd('\\') + "\\";
            var autoPath = Path.Combine(root, "PrincetonInstruments.LightField.AutomationV5.dll");
            var viewPath = Path.Combine(root, "AddInViews", "PrincetonInstruments.LightFieldViewV5.dll");
            var svcPath = Path.Combine(root, "PrincetonInstruments.LightFieldAddInSupportServices.dll");
            foreach (var p in new[] { autoPath, viewPath, svcPath })
            {
                if (!File.Exists(p))
                {
                    SetState(StateError, "LightField SDK assembly not found: " + p);
                    return false;
                }
            }
            AppDomain.CurrentDomain.AssemblyResolve += (s, e) =>
            {
                var name = new AssemblyName(e.Name).Name;
                foreach (var dir in new[] { root, Path.Combine(root, "AddInViews") })
                {
                    var f = Path.Combine(dir, name + ".dll");
                    if (File.Exists(f)) return Assembly.LoadFrom(f);
                }
                return null;
            };
            var view = Assembly.LoadFrom(viewPath);
            Assembly.LoadFrom(svcPath);
            var auto = Assembly.LoadFrom(autoPath);
            _tAutomation = auto.GetType("PrincetonInstruments.LightField.Automation.Automation", true);
            const string ns = "PrincetonInstruments.LightField.AddIns.";
            _tApp = view.GetType(ns + "ILightFieldApplication", true);
            _tExperiment = view.GetType(ns + "IExperiment", true);
            _tFileManager = view.GetType(ns + "IFileManager", true);
            _tUim = view.GetType(ns + "IUserInteractionManager", true);
            _tDataSet = view.GetType(ns + "IImageDataSet", true);
            _tImageData = view.GetType(ns + "IImageData", true);
            _tRoi = view.GetType(ns + "RegionOfInterest", true);
            _assembliesLoaded = true;
            Log.Info($"[{Id}] LightField SDK loaded from {root} (AutomationV5 {auto.GetName().Version}, ViewV5 {FileVersionInfo.GetVersionInfo(viewPath).FileVersion})");
            return true;
        }

        private void StopLightField(string why)
        {
            _started = false;
            var a = _automation;
            if (a == null || _sta == null) return;
            try
            {
                _sta.Invoke(() =>
                {
                    LateBound.Unsubscribe(_experiment, _tExperiment, "ExperimentStarted", _hStarted);
                    LateBound.Unsubscribe(_experiment, _tExperiment, "ExperimentCompleted", _hCompleted);
                    LateBound.Unsubscribe(_experiment, _tExperiment, "ImageDataSetReceived", _hData);
                    LateBound.Unsubscribe(_experiment, _tExperiment, "SettingChanged", _hSetting);
                    LateBound.Unsubscribe(_experiment, _tExperiment, "ExperimentUpdated", _hUpdated);
                    LateBound.Unsubscribe(_automation, _tAutomation, "LightFieldClosed", _hClosed);
                    ((IDisposable)a).Dispose();
                }, TimeSpan.FromSeconds(60));
            }
            catch (Exception ex) { Log.Error($"[{Id}] LightField dispose failed", ex); }
            _automation = _app = _experiment = _fileManager = null;
            _ownPids.Clear();
            SetState(StateNotStarted, why);
        }

        public override void Shutdown()
        {
            try { _poll?.Dispose(); } catch { }
            StopLightField("satellite shutdown");
            _frames.CompleteAdding();
            _sta?.Dispose();
        }

        // ------------------------------------------------------------------ vendor calls

        private T OnSta<T>(Func<T> f, double timeoutS = 30)
        {
            if (!_started || _sta == null) throw new DeviceError(ErrorCodes.Unavailable, "LightField is not running");
            try { return _sta.Invoke(f, TimeSpan.FromSeconds(timeoutS)); }
            catch (DeviceError) { throw; }
            catch (TimeoutException ex) { throw new DeviceError(ErrorCodes.Timeout, "LightField: " + ex.Message); }
            catch (Exception ex) { throw new DeviceError(ErrorCodes.Internal, "LightField: " + ex.GetType().Name + ": " + ex.Message); }
        }

        private object ExpGet(string p) { return LateBound.Get(_experiment, _tExperiment, p); }
        private object ExpCall(string m, params object[] a) { return LateBound.Call(_experiment, _tExperiment, m, a); }

        private object GetValue(string key)
        {
            if (!(bool)ExpCall("Exists", key)) return null;
            return ExpCall("GetValue", key);
        }

        /// <summary>Converts a JSON value to the CLR type LightField uses for this setting, then validates and sets it.</summary>
        private void SetValueChecked(string key, object jsonValue)
        {
            if (!(bool)ExpCall("Exists", key)) throw new DeviceError(ErrorCodes.NotSupported, $"setting {key} does not exist in this experiment");
            if ((bool)ExpCall("IsReadOnly", key)) throw new DeviceError(ErrorCodes.InvalidArgs, $"setting {key} is read-only");
            var current = ExpCall("GetValue", key);
            var value = ConvertFor(current, jsonValue, key);
            if (!(bool)ExpCall("IsValid", key, value))
                throw new DeviceError(ErrorCodes.InvalidArgs, $"LightField rejects {key} = {Json.Serialize(LateBound.ToJsonValue(value))}{DescribeAllowed(key)}");
            lock (_ownChanges) _ownChanges[key] = Clock.MonoSeconds;
            ExpCall("SetValue", key, value);
        }

        private string DescribeAllowed(string key)
        {
            try
            {
                var caps = ExpCall("GetCurrentCapabilities", key) as System.Collections.IEnumerable;
                if (caps != null)
                {
                    var l = caps.Cast<object>().Select(c => Convert.ToString(c, CultureInfo.InvariantCulture)).ToList();
                    if (l.Count > 0) return " (allowed: " + string.Join(", ", l.Take(20)) + ")";
                }
            }
            catch { }
            try
            {
                var r = ExpCall("GetCurrentRange", key);
                if (r != null)
                {
                    var t = _tExperiment.Assembly.GetType("PrincetonInstruments.LightField.AddIns.ISettingRange");
                    if (t != null)
                        return $" (range {J.ToDouble(LateBound.Get(r, t, "Minimum"), double.NaN)} .. {J.ToDouble(LateBound.Get(r, t, "Maximum"), double.NaN)}, step {J.ToDouble(LateBound.Get(r, t, "Increment"), double.NaN)})";
                }
            }
            catch { }
            return "";
        }

        private static object ConvertFor(object current, object v, string key)
        {
            if (v == null) throw new DeviceError(ErrorCodes.InvalidArgs, "null value for " + key);
            if (current == null) return v is long l ? (object)(int)l : v; // let LightField convert
            var t = current.GetType();
            try
            {
                if (t.IsEnum)
                {
                    // Only LightField's own names (契约 §5.4): Enum.Parse alone would also take "5" or a
                    // misspelt flag list, and the refusal must say which names exist.
                    var names = Enum.GetNames(t).Where(n => n != "UninitializedEnum").ToArray();
                    if (v is string s)
                    {
                        var parts = s.Split(',').Select(p => p.Trim()).ToArray();
                        bool flags = t.IsDefined(typeof(FlagsAttribute), false) || t.Name == "TimeStamps";
                        if ((parts.Length > 1 && !flags) || parts.Any(p => !names.Contains(p, StringComparer.OrdinalIgnoreCase)))
                            throw new DeviceError(ErrorCodes.InvalidArgs, $"{key}: '{s}' is not a {t.Name} name (one of: {string.Join(", ", names)})");
                        return Enum.Parse(t, string.Join(", ", parts), true);
                    }
                    var e = Enum.ToObject(t, Convert.ToInt64(v, CultureInfo.InvariantCulture));
                    if (!Enum.IsDefined(t, e)) throw new DeviceError(ErrorCodes.InvalidArgs, $"{key}: {Json.Serialize(v)} is not a {t.Name} value (one of: {string.Join(", ", names)})");
                    return e;
                }
                if (t == typeof(double)) return Convert.ToDouble(v, CultureInfo.InvariantCulture);
                if (t == typeof(float)) return Convert.ToSingle(v, CultureInfo.InvariantCulture);
                if (t == typeof(int)) return Convert.ToInt32(v, CultureInfo.InvariantCulture);
                if (t == typeof(long)) return Convert.ToInt64(v, CultureInfo.InvariantCulture);
                if (t == typeof(bool)) return v is string bs ? bool.Parse(bs) : Convert.ToBoolean(v, CultureInfo.InvariantCulture);
                if (t == typeof(string)) return Convert.ToString(v, CultureInfo.InvariantCulture);
            }
            catch (Exception ex) when (!(ex is DeviceError))
            {
                throw new DeviceError(ErrorCodes.InvalidArgs, $"cannot convert {Json.Serialize(v)} to {t.Name} for {key}: {ex.Message}");
            }
            throw new DeviceError(ErrorCodes.NotSupported, $"setting {key} has type {t.Name}, which this driver does not set");
        }

        private bool HasCamera()
        {
            try
            {
                var tDevice = _tExperiment.Assembly.GetType("PrincetonInstruments.LightField.AddIns.IDevice", true);
                return OnSta(() =>
                {
                    var devs = ExpGet("ExperimentDevices") as System.Collections.IEnumerable;
                    if (devs == null) return false;
                    foreach (var d in devs)
                    {
                        var type = LateBound.Get(d, tDevice, "Type");
                        if (Convert.ToString(type, CultureInfo.InvariantCulture) == "Camera") return true;
                    }
                    return false;
                });
            }
            catch { return false; }
        }

        private void WaitNotUpdating(TimeSpan max)
        {
            var end = Clock.MonoSeconds + max.TotalSeconds;
            while (Clock.MonoSeconds < end)
            {
                bool upd = _sta.Invoke(() => (bool)ExpGet("IsUpdating"), TimeSpan.FromSeconds(30));
                if (!upd) return;
                Thread.Sleep(200);
            }
            throw new DeviceError(ErrorCodes.Timeout, "LightField stayed in 'updating' for " + max.TotalSeconds + " s");
        }

        // ------------------------------------------------------------------ status / axis

        private int _polling;

        private void Poll()
        {
            // A slow LightField must not stack polls up in the STA queue.
            if (Interlocked.Exchange(ref _polling, 1) == 1) return;
            try { PollOnce(); }
            finally { Interlocked.Exchange(ref _polling, 0); }
        }

        private void PollOnce()
        {
            if (!_started)
            {
                // Retry hosting once a hand-opened LightField has gone away (not after the operator closed ours).
                if (!_starting && !_closedByOperator && State == StateNotStarted && Reason != null && Reason.StartsWith("a LightField window opened by hand", StringComparison.Ordinal))
                    StartLightField();
                return;
            }
            try
            {
                RefreshStatus();
                RefreshAxis(force: false);
            }
            catch (Exception ex)
            {
                Log.Debug($"[{Id}] poll: {ex.Message}");
            }
        }

        private void RefreshStatus()
        {
            var s = OnSta(() =>
            {
                var d = J.Obj();
                d["is_ready"] = ExpGet("IsReadyToRun");
                d["is_running"] = ExpGet("IsRunning");
                d["is_updating"] = ExpGet("IsUpdating");
                d["experiment_status"] = Convert.ToString(ExpGet("Status"), CultureInfo.InvariantCulture);
                d["experiment_name"] = ExpGet("Name");
                d["progress"] = ExpGet("Progress");
                d["temperature_c"] = LateBound.ToJsonValue(GetValue(KTempReading));
                var ts = Convert.ToString(GetValue(KTempStatus), CultureInfo.InvariantCulture);
                d["temperature_status"] = ts;
                d["temperature_locked"] = ts == "Locked";
                d["temperature_setpoint_c"] = LateBound.ToJsonValue(GetValue(KTempSetPoint));
                d["exposure_ms"] = LateBound.ToJsonValue(GetValue(KExposure));
                d["frames"] = LateBound.ToJsonValue(GetValue(KFrames));
                d["grating"] = LateBound.ToJsonValue(GetValue(KGrating));
                d["center_wavelength_nm"] = LateBound.ToJsonValue(GetValue(KCenter));
                var exit = Convert.ToString(GetValue(KExit), CultureInfo.InvariantCulture);
                d["exit_port"] = exit == "FrontExit" ? "front" : exit == "SideExit" ? "side" : exit;
                d["trigger_response"] = LateBound.ToJsonValue(GetValue(KTrigResponse));
                d["trigger_edge"] = LateBound.ToJsonValue(GetValue(KTrigDetermination));
                d["output_signal"] = LateBound.ToJsonValue(GetValue(KOutputSignal));
                d["shutter_mode"] = LateBound.ToJsonValue(GetValue(KShutterMode));
                d["adc_mhz"] = LateBound.ToJsonValue(GetValue(KAdcSpeed));
                d["adc_quality"] = LateBound.ToJsonValue(GetValue(KAdcQuality));
                // TimeStamps flags print as "ExposureStarted, ExposureEnded"; report the configure form (no space).
                d["timestamps"] = Convert.ToString(GetValue(KTimeStamps), CultureInfo.InvariantCulture)?.Replace(" ", "");
                d["frame_tracking"] = LateBound.ToJsonValue(GetValue(KFrameTracking));
                d["readout_ms"] = LateBound.ToJsonValue(GetValue(KReadoutTime));
                d["roi_selection"] = LateBound.ToJsonValue(GetValue(KRoiSelection));
                d["roi"] = RoiStatus(ReadRoi());
                d["file_directory"] = LateBound.ToJsonValue(GetValue(KDir));
                d["base_name"] = LateBound.ToJsonValue(GetValue(KBaseName));
                return d;
            }, 15);
            s["laser_line_nm"] = _laserNmCfg;
            s["axis_ver"] = _axisVer;
            _running = J.Bool(s, "is_running", false);
            lock (_gate) _status = s;
        }

        protected override Dictionary<string, object> BuildStatus(bool fresh)
        {
            if (fresh && _started)
            {
                try { RefreshStatus(); } catch (Exception ex) { Log.Debug($"[{Id}] status refresh: {ex.Message}"); }
            }
            lock (_gate)
            {
                var s = new Dictionary<string, object>(_status, StringComparer.Ordinal);
                s["hosting"] = _started;
                s["manual_acquisition"] = _manualAcq != null;
                return s;
            }
        }

        /// <summary>Reads SystemColumnCalibration; emits wavelength_axis when it (or grating/centre) changed.</summary>
        private void RefreshAxis(bool force)
        {
            var r = OnSta(() =>
            {
                var cal = ExpGet("SystemColumnCalibration") as double[];
                var regions = ExpGet("SelectedRegions") as Array;
                object roi = regions != null && regions.Length > 0 ? regions.GetValue(0) : null;
                return (cal, roi, grating: Convert.ToString(GetValue(KGrating), CultureInfo.InvariantCulture),
                        center: J.ToDouble(GetValue(KCenter), double.NaN));
            }, 15);
            if (r.cal == null || r.cal.Length == 0) return;
            var axis = MapAxis(r.cal, r.roi);
            var key = $"{axis.Length}|{axis[0]:R}|{axis[axis.Length - 1]:R}|{r.grating}|{r.center:R}";
            bool changed;
            lock (_gate)
            {
                changed = force || key != _axisKey;
                if (changed)
                {
                    _calibration = r.cal;
                    _axis = axis;
                    _axisKey = key;
                    _axisVer++;
                }
            }
            if (changed) Emit("wavelength_axis", AxisData(r.grating, r.center));
        }

        /// <summary>Calibration is per sensor column; a region with x offset / binning gets the mean of its columns.</summary>
        private double[] MapAxis(double[] cal, object roi)
        {
            if (roi == null) return cal;
            var d = LateBound.PropsToDict(roi);
            int x = J.Int(d, "X", 0), w = J.Int(d, "Width", cal.Length), xb = Math.Max(1, J.Int(d, "XBinning", 1));
            int n = w / xb;
            if (n == cal.Length || x + n * xb > cal.Length || n <= 0) return cal;
            var a = new double[n];
            for (int i = 0; i < n; i++)
            {
                double s = 0;
                for (int k = 0; k < xb; k++) s += cal[x + i * xb + k];
                a[i] = s / xb;
            }
            return a;
        }

        private Dictionary<string, object> AxisData(string grating = null, double center = double.NaN)
        {
            lock (_gate)
            {
                if (_axis == null) throw new DeviceError(ErrorCodes.Unavailable, "no wavelength calibration yet");
                var bytes = new byte[_axis.Length * 8];
                Buffer.BlockCopy(_axis, 0, bytes, 0, bytes.Length);
                return J.Obj("axis_ver", _axisVer, "n", _axis.Length, "nm_b64", Convert.ToBase64String(bytes), "laser_line_nm", _laserNmCfg,
                             "grating", grating ?? J.Str(_status, "grating"), "center_nm", double.IsNaN(center) ? J.Get(_status, "center_wavelength_nm") : center,
                             "calibration_columns", _calibration?.Length ?? 0);
            }
        }

        public override void OnHubConnected()
        {
            try { if (_axis != null) Emit("wavelength_axis", AxisData()); } catch { }
        }

        // ------------------------------------------------------------------ events (vendor threads: copy + enqueue only)

        private void OnExperimentStarted(object sender, EventArgs e)
        {
            _running = true;
            if (_expectHubStart && _hubAcq != null)
            {
                _expectHubStart = false;
                _hubAcq.Started.Set();
                return;
            }
            var m = new Acq { Manual = true, StartedMs = Clock.UnixMs() };
            _manualAcq = m;
            Emit("manual_activity", J.Obj("what", "acquisition_started", "source", "lightfield_window"));
            Emit("acq_started", J.Obj("cmd_id", null, "manual", true, "started_ms", m.StartedMs));
            Host.StateChanged(this);
        }

        private void OnExperimentCompleted(object sender, EventArgs e)
        {
            _running = false;
            var a = _hubAcq ?? _manualAcq;
            if (a == null) return;
            a.LfCompleted.Set();
            TryAddFrame(new FrameItem { Acq = a, CompletedMarker = true });
        }

        private void OnImageDataSetReceived(object sender, EventArgs e)
        {
            var acq = _hubAcq ?? _manualAcq;
            if (acq == null)
            {
                var last = _lastHubAcq;
                if (!_running && last != null && Clock.MonoSeconds - _lastHubAcqEndMono < 5)
                {
                    // A straggler of the hub acquisition that just ended, not the operator at work.
                    Log.Debug($"[{Id}] ImageDataSetReceived after hub acquisition {last.CmdId} ended; ignored");
                    return;
                }
                // Data without a Started event (e.g. Preview / TakeOneLook from the window).
                acq = new Acq { Manual = true, StartedMs = Clock.UnixMs() };
                _manualAcq = acq;
                Emit("manual_activity", J.Obj("what", "frames_without_acquire", "source", "lightfield_window"));
                Host.StateChanged(this);
            }
            long recvUs = Clock.UnixUs();
            try
            {
                var ds = e.GetType().GetProperty("ImageDataSet").GetValue(e, null);
                long frames = Convert.ToInt64(LateBound.Get(ds, _tDataSet, "Frames"), CultureInfo.InvariantCulture);
                var origin = LateBound.Get(ds, _tDataSet, "TimeStampOrigin") as DateTimeOffset?;
                for (long f = 0; f < frames; f++)
                {
                    if (acq.Manual && _manualMaxHz > 0)
                    {
                        if (Clock.MonoSeconds - acq.LastManualFrameMono < 1.0 / _manualMaxHz) continue;
                        acq.LastManualFrameMono = Clock.MonoSeconds;
                    }
                    var img = LateBound.Call(ds, _tDataSet, "GetFrame", 0, f);
                    int w = Convert.ToInt32(LateBound.Get(img, _tImageData, "Width"), CultureInfo.InvariantCulture);
                    int h = Convert.ToInt32(LateBound.Get(img, _tImageData, "Height"), CultureInfo.InvariantCulture);
                    var arr = LateBound.Call(img, _tImageData, "GetData") as Array;
                    var item = new FrameItem { Acq = acq, ReceivedUs = recvUs, N = w };
                    if (origin.HasValue) item.OriginMs = Clock.UnixMsFromDateTime(origin.Value.UtcDateTime);
                    Bin(arr, w, Math.Max(1, h), item);
                    try
                    {
                        var md = LateBound.Call(ds, _tDataSet, "GetFrameMetaData", f);
                        if (md != null)
                        {
                            var mt = md.GetType();
                            var st = mt.GetProperty("ExposureStarted")?.GetValue(md, null) as TimeSpan?;
                            var en = mt.GetProperty("ExposureEnded")?.GetValue(md, null) as TimeSpan?;
                            var tr = mt.GetProperty("FrameTrackingNumber")?.GetValue(md, null) as long?;
                            long baseUs = origin.HasValue ? Clock.UnixMsFromDateTime(origin.Value.UtcDateTime) * 1000 : 0;
                            if (origin.HasValue && st.HasValue) item.StartedUs = baseUs + st.Value.Ticks / 10;
                            if (origin.HasValue && en.HasValue) item.EndedUs = baseUs + en.Value.Ticks / 10;
                            item.Tracking = tr;
                        }
                    }
                    catch { }
                    lock (acq)
                    {
                        if (acq.DatasetDelivered) return; // Capture's returned set already published these frames
                        acq.EventFrames++;
                    }
                    TryAddFrame(item);
                }
            }
            catch (Exception ex)
            {
                Log.Error($"[{Id}] reading ImageDataSet failed", ex);
            }
        }

        /// <summary>A 1-row region stays u16; a taller region (image) is summed over rows into f32.</summary>
        private static void Bin(Array arr, int w, int h, FrameItem item)
        {
            if (arr is ushort[] u && h == 1) { item.Data = u.Length == w ? u : u.Take(w).ToArray(); item.Dtype = "u16"; return; }
            var f = new float[w];
            for (int i = 0; i < arr.Length; i++)
            {
                double v = Convert.ToDouble(arr.GetValue(i), CultureInfo.InvariantCulture);
                f[i % w] += (float)v;
            }
            item.Data = f;
            item.Dtype = "f32";
        }

        private void TryAddFrame(FrameItem it)
        {
            if (!_frames.TryAdd(it)) Log.Warn($"[{Id}] frame queue full; dropping a frame");
        }

        private void FrameLoop()
        {
            foreach (var it in _frames.GetConsumingEnumerable())
            {
                try
                {
                    var a = it.Acq;
                    if (it.CompletedMarker)
                    {
                        if (a.Manual)
                        {
                            long fin = Clock.UnixMs();
                            // Remember the time window for the file the operator just saved, so its upload carries it.
                            var files = _started ? CollectFiles(a.StartedMs) : new List<object>();
                            foreach (var f in files)
                                Host.NoteFileMeta((string)f, J.Obj("acq_started_ms", a.StartedMs, "acq_finished_ms", fin, "device", Id,
                                                                   "frames", a.FrameIndex, "manual", true));
                            Emit("acq_completed", J.Obj("cmd_id", null, "manual", true, "frames", a.FrameIndex, "files", files,
                                                        "started_ms", a.StartedMs, "finished_ms", fin, "summary", a.Summary()));
                            if (_manualAcq == a) _manualAcq = null;
                            Host.StateChanged(this);
                        }
                        a.Completed.Set();
                        continue;
                    }
                    int idx = a.FrameIndex++;
                    byte[] bytes;
                    if (it.Data is ushort[] u)
                    {
                        bytes = new byte[u.Length * 2];
                        Buffer.BlockCopy(u, 0, bytes, 0, bytes.Length);
                        foreach (var v in u) Acc(a, v);
                    }
                    else
                    {
                        var f = (float[])it.Data;
                        bytes = new byte[f.Length * 4];
                        Buffer.BlockCopy(f, 0, bytes, 0, bytes.Length);
                        foreach (var v in f) Acc(a, v);
                    }
                    var d = J.Obj("cmd_id", a.CmdId, "frame_index", idx, "n", it.N, "dtype", it.Dtype, "data_b64", Convert.ToBase64String(bytes),
                                  "axis_ver", _axisVer, "manual", a.Manual);
                    if (it.StartedUs.HasValue) d["exposure_started_us"] = it.StartedUs.Value;
                    if (it.EndedUs.HasValue) d["exposure_ended_us"] = it.EndedUs.Value;
                    if (it.Tracking.HasValue) d["frame_tracking"] = it.Tracking.Value;
                    if (it.OriginMs.HasValue) d["ts_origin_ms"] = it.OriginMs.Value;
                    if (!it.StartedUs.HasValue) { d["received_us"] = it.ReceivedUs; d["timestamps_source"] = "satellite_receive"; }
                    if (idx == 0) a.FirstExposureUs = it.StartedUs ?? it.ReceivedUs;
                    Emit("spectrum", d);
                    if (!a.Manual) Emit("acq_progress", J.Obj("cmd_id", a.CmdId, "frame", idx + 1, "frame_index", idx, "frames", a.Frames,
                                                              "fraction", a.Frames > 0 ? (idx + 1) / (double)a.Frames : 0));
                }
                catch (Exception ex)
                {
                    Log.Error($"[{Id}] frame publish failed", ex);
                }
            }
        }

        private static void Acc(Acq a, double v)
        {
            if (v < a.Min) a.Min = v;
            if (v > a.Max) a.Max = v;
            a.Sum += v;
            a.Count++;
        }

        private void OnSettingChanged(object sender, EventArgs e)
        {
            string key = null;
            try { key = e.GetType().GetProperty("Setting")?.GetValue(e, null) as string; } catch { }
            if (key == null) return;
            bool own;
            lock (_ownChanges) own = _ownChanges.TryGetValue(key, out var t) && Clock.MonoSeconds - t < 5;
            if (!own) Emit("manual_activity", J.Obj("what", "setting_changed", "setting", key, "source", "lightfield_window"));
            if (key == KGrating || key == KCenter || key == KRoiResult || key == KRoiSelection)
                ThreadPool.QueueUserWorkItem(_ => { try { RefreshAxis(false); } catch { } });
            Host.StateChanged(this);
        }

        private void OnExperimentUpdated(object sender, EventArgs e)
        {
            ThreadPool.QueueUserWorkItem(_ => { try { RefreshStatus(); RefreshAxis(false); } catch { } });
        }

        private void OnLightFieldClosed(object sender, EventArgs e)
        {
            Log.Warn($"[{Id}] the LightField window was closed");
            _started = false;
            _closedByOperator = true;
            _automation = _app = _experiment = _fileManager = null;
            var a = _hubAcq;
            a?.Completed.Set();
            SetState(StateNotStarted, "LightField was closed; use '启动/重启 LightField' in the satellite window");
        }

        // ------------------------------------------------------------------ actions

        protected override object ExecuteAction(CommandContext ctx)
        {
            switch (ctx.Action)
            {
                case "wavelength_axis":
                    RefreshAxis(false);
                    return AxisData();
                case "configure":
                    using (BeginExclusive(ctx, "configure", hubMotion: false)) Configure(ctx);
                    RefreshStatus();
                    return Status(false);
                case "acquire": return Acquire(ctx);
                case "set_grating": return SetGrating(ctx);
                case "set_center_wavelength": return SetCenter(ctx);
                case "set_exit_port": return SetExit(ctx);
                case "get_settings": return GetSettings(ctx);
                case "set_settings": return SetSettings(ctx);
            }
            throw new DeviceError(ErrorCodes.NotSupported, ctx.Action);
        }

        /// <summary>
        /// configure (契约 §5.4): every argument is parsed before anything is applied (SpectrometerArgs), then
        /// LightField checks each value itself (SetValueChecked: name of its enum, IsValid for this camera).
        /// Nothing given is dropped; a refusal names what was already applied.
        /// </summary>
        private void Configure(CommandContext ctx)
        {
            SpectrometerArgs.RejectUnknown(ctx);
            var exp = ctx.NumOpt("exposure_ms");
            var frames = ctx.IntOpt("frames");
            var adc = ctx.NumOpt("adc_mhz");
            string stamps = ctx.Has("timestamps") ? SpectrometerArgs.TimeStamps(ctx.Args["timestamps"]) : null;
            bool? tracking = ctx.Has("frame_tracking") ? SpectrometerArgs.StrictBool("frame_tracking", ctx.Args["frame_tracking"]) : (bool?)null;
            var trig = ctx.Dict("trigger");
            string edge = trig != null && J.Has(trig, "edge") ? SpectrometerArgs.Edge(J.Str(trig, "edge")) : null;
            RoiSpec roi = ctx.Has("roi") ? SpectrometerArgs.ParseRoi(ctx.Dict("roi")) : null;
            var file = ctx.Dict("file");
            string dir = file != null && J.Has(file, "directory") ? SpectrometerArgs.StrictString("file.directory", J.Get(file, "directory")) : null;
            string baseName = file != null && J.Has(file, "base_name") ? SpectrometerArgs.StrictString("file.base_name", J.Get(file, "base_name")) : null;
            bool? attachDate = file != null && J.Has(file, "attach_date") ? SpectrometerArgs.StrictBool("file.attach_date", J.Get(file, "attach_date")) : (bool?)null;
            bool? attachInc = file != null && J.Has(file, "attach_increment") ? SpectrometerArgs.StrictBool("file.attach_increment", J.Get(file, "attach_increment")) : (bool?)null;
            var applied = new List<string>();
            try
            {
                OnSta(() =>
                {
                    void Set(string key, object v) { SetValueChecked(key, v); applied.Add(key); }
                    if (exp != null) Set(KExposure, exp.Value);
                    if (frames != null) Set(KFrames, (long)frames.Value);
                    if (adc != null) Set(KAdcSpeed, adc.Value);
                    if (ctx.Has("adc_quality")) Set(KAdcQuality, ctx.Str("adc_quality"));
                    if (ctx.Has("shutter_mode")) Set(KShutterMode, ctx.Str("shutter_mode"));
                    if (trig != null && J.Has(trig, "response")) Set(KTrigResponse, J.Str(trig, "response"));
                    if (edge != null) Set(KTrigDetermination, edge);
                    if (ctx.Has("output_signal")) Set(KOutputSignal, ctx.Str("output_signal"));
                    if (stamps != null) Set(KTimeStamps, stamps);
                    if (tracking != null) Set(KFrameTracking, tracking.Value);
                    if (dir != null) Set(KDir, dir);
                    if (baseName != null) Set(KBaseName, baseName);
                    if (attachDate != null) Set(KAttachDate, attachDate.Value);
                    if (attachInc != null) Set(KAttachIncrement, attachInc.Value);
                    if (roi != null) { ApplyRoi(roi); applied.Add("roi"); }
                    return true;
                }, 60);
            }
            catch (DeviceError e) when (applied.Count > 0)
            {
                throw new DeviceError(e.Code, e.Message + " (already applied: " + string.Join(", ", applied) + ")");
            }
            WaitNotUpdating(TimeSpan.FromSeconds(60));
            RefreshAxis(false);
        }

        private void ApplyRoi(RoiSpec roi)
        {
            switch (roi.Mode)
            {
                case "full": ExpCall("SetFullSensorRegion"); break;
                case "line": ExpCall("SetLineSensorRegion", roi.Rows); break;
                case "binned": ExpCall("SetBinnedSensorRegion", roi.XBin, roi.YBin); break;
                case "custom":
                {
                    var arr = Array.CreateInstance(_tRoi, roi.Regions.Count);
                    for (int i = 0; i < roi.Regions.Count; i++)
                    {
                        var r = roi.Regions[i];
                        arr.SetValue(Activator.CreateInstance(_tRoi, r[0], r[1], r[2], r[3], r[4], r[5]), i);
                    }
                    ExpCall("SetCustomRegions", arr);
                    break;
                }
            }
            // Read back: the selection must be the requested mode, and explicit custom regions must be
            // exactly what was asked (a region LightField quietly adjusted would be recorded as requested).
            var got = ReadRoi();
            if (got.Mode != roi.Mode)
                throw new DeviceError(ErrorCodes.InvalidArgs, $"LightField did not switch the roi to '{roi.Mode}' (it is '{got.Mode}')");
            if (roi.Mode == "custom" && (got.Regions.Count != roi.Regions.Count || got.Regions.Where((r, i) => !r.SequenceEqual(roi.Regions[i])).Any()))
                throw new DeviceError(ErrorCodes.InvalidArgs, $"LightField set the regions to {Json.Serialize(got.Regions.Select(r => (object)RoiSpec.RegionJson(r)).ToList())} instead of {Json.Serialize(roi.Regions.Select(r => (object)RoiSpec.RegionJson(r)).ToList())}");
        }

        /// <summary>
        /// LightField's current roi in the configure vocabulary. For the line sensor, rows = the region's
        /// height (SetLineSensorRegion(centerRowsBinned) bins that many centre rows; inferred, not verified on hardware).
        /// </summary>
        private RoiSpec ReadRoi()
        {
            var sel = Convert.ToString(GetValue(KRoiSelection), CultureInfo.InvariantCulture);
            var regions = ((ExpGet("SelectedRegions") as Array)?.Cast<object>() ?? Enumerable.Empty<object>()).Select(o =>
            {
                var d = LateBound.PropsToDict(o);
                return new[] { J.Int(d, "X", 0), J.Int(d, "Y", 0), J.Int(d, "Width", 0), J.Int(d, "Height", 0), J.Int(d, "XBinning", 1), J.Int(d, "YBinning", 1) };
            }).ToList();
            var mode = sel == "FullSensor" ? "full" : sel == "LineSensor" ? "line" : sel == "BinnedSensor" ? "binned" : sel == "CustomRegions" ? "custom" : sel;
            var spec = new RoiSpec { Mode = mode, Regions = regions };
            if (regions.Count > 0) { spec.Rows = regions[0][3]; spec.XBin = regions[0][4]; spec.YBin = regions[0][5]; }
            return spec;
        }

        private static Dictionary<string, object> RoiStatus(RoiSpec roi)
        {
            var d = roi.ToJson();
            if (roi.Mode != "custom") d["regions"] = roi.Regions.Select(r => (object)RoiSpec.RegionJson(r)).ToList();
            return d;
        }

        /// <summary>
        /// Points LightField's save directory at the configured template (date placeholders expand in
        /// local time) so files land under the spe_watcher root. A failure is logged, never thrown.
        /// </summary>
        private void ApplyConfiguredFileDirectory()
        {
            if (string.IsNullOrWhiteSpace(_fileDirectoryTemplate)) return;
            try
            {
                var now = DateTime.Now;
                var ci = CultureInfo.InvariantCulture;
                string dir = _fileDirectoryTemplate
                    .Replace("{yyyyMMdd}", now.ToString("yyyyMMdd", ci))
                    .Replace("{yyyy}", now.ToString("yyyy", ci))
                    .Replace("{yy}", now.ToString("yy", ci))
                    .Replace("{MM}", now.ToString("MM", ci))
                    .Replace("{dd}", now.ToString("dd", ci));
                Directory.CreateDirectory(dir);
                OnSta(() => { SetValueChecked(KDir, dir); return true; });
                WaitNotUpdating(TimeSpan.FromSeconds(30));
                Log.Info($"[{Id}] SPE save directory set to '{dir}'");
            }
            catch (Exception ex)
            {
                Log.Warn($"[{Id}] could not set SPE save directory from '{_fileDirectoryTemplate}': {ex.Message}");
            }
        }

        private object Acquire(CommandContext ctx)
        {
            using (BeginExclusive(ctx, "acquire", hubMotion: true))
            {
                RefreshStatus();
                Dictionary<string, object> st;
                lock (_gate) st = _status;
                if (J.Bool(st, "is_running", false)) throw new DeviceError(ErrorCodes.DeviceBusy, "LightField is already acquiring");
                if (!J.Bool(st, "is_ready", false))
                    throw new DeviceError(ErrorCodes.Unavailable, "LightField is not ready to run (status " + J.Str(st, "experiment_status") + ")");
                if (_requireLocked && !J.Bool(st, "temperature_locked", false))
                    throw new DeviceError(ErrorCodes.Unavailable, $"CCD temperature not locked ({J.Str(st, "temperature_c")} C, {J.Str(st, "temperature_status")}); refusing to acquire");
                bool save = ctx.Bool("save", true);
                if (save) ApplyConfiguredFileDirectory();
                OnSta(() =>
                {
                    if (ctx.Has("exposure_ms")) SetValueChecked(KExposure, ctx.Num("exposure_ms"));
                    if (ctx.Has("frames")) SetValueChecked(KFrames, (long)ctx.Int("frames"));
                    if (ctx.Has("base_name")) SetValueChecked(KBaseName, ctx.Str("base_name"));
                    return true;
                });
                WaitNotUpdating(TimeSpan.FromSeconds(30));
                var exposure = J.ToDouble(OnSta(() => GetValue(KExposure)), 0);
                int frames = (int)J.ToDouble(OnSta(() => GetValue(KFrames)), 1);
                var acq = new Acq { CmdId = ctx.Id, Frames = frames, StartedMs = Clock.UnixMs() };
                _hubAcq = acq;
                _expectHubStart = true;
                Emit("acq_started", J.Obj("cmd_id", ctx.Id, "frames", frames, "exposure_ms", exposure, "started_ms", acq.StartedMs, "manual", false,
                                          "tag", ctx.Str("tag"), "save", save, "meta", ctx.Dict("meta"), "axis_ver", _axisVer));
                List<object> files = new List<object>();
                bool aborted = false;
                long finished = 0;
                try
                {
                    if (save)
                    {
                        OnSta(() => { ExpCall("Acquire"); return true; });
                        double budget = ctx.TimeoutS > 0 ? ctx.TimeoutS : frames * (exposure + 200) / 1000.0 * 1.5 + 60;
                        var t0 = Clock.MonoSeconds;
                        var end = t0 + budget;
                        while (!acq.Completed.IsSet)
                        {
                            if (!acq.Started.IsSet && Clock.MonoSeconds - t0 > Config.Num("start_timeout_s", 20) && !_running)
                                throw new DeviceError(ErrorCodes.Internal, "LightField accepted Acquire() but never started the experiment (check the LightField window for an error)");
                            if (!ctx.Wait(0.1))
                            {
                                aborted = true;
                                StopAcquisition();
                                acq.Completed.Wait(TimeSpan.FromSeconds(15));
                                break;
                            }
                            if (Clock.MonoSeconds > end)
                            {
                                aborted = true;
                                StopAcquisition();
                                acq.Completed.Wait(TimeSpan.FromSeconds(15));
                                throw new DeviceError(ErrorCodes.Timeout, $"acquisition did not complete within {budget:0} s");
                            }
                        }
                        if (!_started) throw new DeviceError(ErrorCodes.Unavailable, "LightField closed during the acquisition");
                        files = CollectFiles(acq.StartedMs);
                    }
                    else
                    {
                        // In-memory capture (Capture(n) is synchronous and returns an IImageDataSet to dispose).
                        CaptureToMemory(ctx, acq, frames);
                        _frames.Add(new FrameItem { Acq = acq, CompletedMarker = true });
                        acq.Completed.Wait(TimeSpan.FromSeconds(30));
                    }
                }
                finally
                {
                    _expectHubStart = false;
                    _lastHubAcqEndMono = Clock.MonoSeconds;
                    _lastHubAcq = acq;
                    _hubAcq = null;
                    finished = Clock.UnixMs();
                    foreach (var f in files)
                        Host.NoteFileMeta((string)f, J.Obj("acq_started_ms", acq.StartedMs, "acq_finished_ms", finished, "device", Id, "cmd_id", ctx.Id,
                                                           "exposure_ms", exposure, "frames", acq.FrameIndex, "manual", false, "tag", ctx.Str("tag"),
                                                           "user_meta", ctx.Dict("meta")));
                    Emit("acq_completed", J.Obj("cmd_id", ctx.Id, "manual", false, "frames", acq.FrameIndex, "files", files, "aborted", aborted,
                                                "started_ms", acq.StartedMs, "finished_ms", finished, "summary", acq.Summary()));
                }
                ctx.Check();
                return J.Obj("frames", acq.FrameIndex, "files", files, "exposure_ms", exposure, "started_ms", acq.StartedMs, "finished_ms", finished,
                             "summary", acq.Summary(), "axis_ver", _axisVer, "first_exposure_started_us", acq.FirstExposureUs);
            }
        }

        private void CaptureToMemory(CommandContext ctx, Acq acq, int frames)
        {
            OnSta(() =>
            {
                var ds = ExpCall("Capture", frames);
                try
                {
                    // Capture raises ExperimentStarted like Acquire (manual, table 7-15). Whether it also raises
                    // ImageDataSetReceived is not stated, so wait for its ExperimentCompleted and then take the
                    // frames from exactly one source: the events if any came, otherwise the returned set.
                    if (acq.Started.IsSet) acq.LfCompleted.Wait(TimeSpan.FromSeconds(5));
                    bool fromEvents;
                    lock (acq)
                    {
                        fromEvents = acq.EventFrames > 0;
                        if (!fromEvents) acq.DatasetDelivered = true;
                    }
                    if (fromEvents)
                    {
                        Log.Debug($"[{Id}] Capture frames already delivered by ImageDataSetReceived ({acq.EventFrames})");
                        return true;
                    }
                    long n = Convert.ToInt64(LateBound.Get(ds, _tDataSet, "Frames"), CultureInfo.InvariantCulture);
                    for (long f = 0; f < n; f++)
                    {
                        var img = LateBound.Call(ds, _tDataSet, "GetFrame", 0, f);
                        int w = Convert.ToInt32(LateBound.Get(img, _tImageData, "Width"), CultureInfo.InvariantCulture);
                        int h = Convert.ToInt32(LateBound.Get(img, _tImageData, "Height"), CultureInfo.InvariantCulture);
                        var item = new FrameItem { Acq = acq, ReceivedUs = Clock.UnixUs(), N = w };
                        Bin(LateBound.Call(img, _tImageData, "GetData") as Array, w, Math.Max(1, h), item);
                        _frames.Add(item);
                    }
                }
                finally
                {
                    try { LateBound.Call(ds, _tDataSet, "Dispose"); } catch { }
                }
                return true;
            }, ctx.TimeoutS > 0 ? ctx.TimeoutS : 600);
        }

        /// <summary>
        /// The file this acquisition wrote: the first entry of GetRecentlyAcquiredFileNames (SDK sample:
        /// "always the last file acquired"), accepted only if it was written after the acquisition
        /// started - a stopped acquisition that saved nothing must not be credited with an older file.
        /// </summary>
        private List<object> CollectFiles(long acqStartedMs)
        {
            try
            {
                return OnSta(() =>
                {
                    var r = new List<object>();
                    var recent = LateBound.Call(_fileManager, _tFileManager, "GetRecentlyAcquiredFileNames") as System.Collections.IEnumerable;
                    if (recent != null)
                    {
                        foreach (var o in recent)
                        {
                            var f = Convert.ToString(o, CultureInfo.InvariantCulture);
                            if (File.Exists(f) && Clock.UnixMsFromDateTime(File.GetLastWriteTimeUtc(f)) >= acqStartedMs - 2000) r.Add(f);
                            break;
                        }
                    }
                    return r;
                });
            }
            catch (Exception ex)
            {
                Log.Warn($"[{Id}] cannot read acquired file names: {ex.Message}");
                return new List<object>();
            }
        }

        private void StopAcquisition()
        {
            if (!_started || _experiment == null) return;
            try
            {
                OnSta(() =>
                {
                    if ((bool)ExpGet("IsRunning")) ExpCall("Stop");
                    return true;
                }, 10);
            }
            catch (DeviceError)
            {
                // The STA thread may be blocked in Capture(); the remoting proxy also accepts Stop from here.
                try { ExpCall("Stop"); } catch (Exception ex) { Log.Error($"[{Id}] Stop failed", ex); }
            }
        }

        private object SetGrating(CommandContext ctx)
        {
            var want = ctx.StrRequired("grating");
            using (BeginExclusive(ctx, "set_grating", hubMotion: true))
            {
                OnSta(() =>
                {
                    var choice = want;
                    var caps = (ExpCall("GetCurrentCapabilities", KGrating) as System.Collections.IEnumerable)?.Cast<object>()
                        .Select(c => Convert.ToString(c, CultureInfo.InvariantCulture)).ToList() ?? new List<string>();
                    if (!caps.Contains(want))
                    {
                        // Accept a groove density ("1200") for "[500nm,1200][0][0]".
                        var m = caps.Where(c => c.Contains("," + want + "]")).ToList();
                        if (m.Count != 1) throw new DeviceError(ErrorCodes.InvalidArgs, $"grating '{want}' not found (available: {string.Join(", ", caps)})");
                        choice = m[0];
                    }
                    SetValueChecked(KGrating, choice);
                    return true;
                });
                WaitNotUpdating(TimeSpan.FromSeconds(Config.Num("grating_timeout_s", 90)));
                RefreshStatus();
                RefreshAxis(false);
                return Status(false);
            }
        }

        private object SetCenter(CommandContext ctx)
        {
            var nm = ctx.Num("nm");
            using (BeginExclusive(ctx, "set_center_wavelength", hubMotion: true))
            {
                OnSta(() => { SetValueChecked(KCenter, nm); return true; });
                WaitNotUpdating(TimeSpan.FromSeconds(Config.Num("center_timeout_s", 60)));
                RefreshStatus();
                RefreshAxis(false);
                return Status(false);
            }
        }

        private object SetExit(CommandContext ctx)
        {
            var p = ctx.StrRequired("port").ToLowerInvariant();
            if (p != "front" && p != "side") throw new DeviceError(ErrorCodes.InvalidArgs, "port must be front or side");
            using (BeginExclusive(ctx, "set_exit_port", hubMotion: true))
            {
                OnSta(() => { SetValueChecked(KExit, p == "front" ? "FrontExit" : "SideExit"); return true; });
                WaitNotUpdating(TimeSpan.FromSeconds(30));
                RefreshStatus();
                return Status(false);
            }
        }

        private object GetSettings(CommandContext ctx)
        {
            var keys = (J.List(ctx.Args, "keys") ?? new List<object>()).Select(o => Convert.ToString(o, CultureInfo.InvariantCulture)).Take(400).ToList();
            if (keys.Count == 0) throw new DeviceError(ErrorCodes.InvalidArgs, "'keys' (list of LightField setting keys) is required");
            return OnSta(() =>
            {
                var r = J.Obj();
                foreach (var k in keys) r[k] = LateBound.ToJsonValue(GetValue(k));
                return r;
            });
        }

        private object SetSettings(CommandContext ctx)
        {
            var values = ctx.Dict("values") ?? throw new DeviceError(ErrorCodes.InvalidArgs, "'values' (object) is required");
            var bad = values.Keys.Where(k => !_settable.Contains(k)).ToList();
            if (bad.Count > 0) throw new DeviceError(ErrorCodes.InvalidArgs, "not in settable_keys: " + string.Join(", ", bad));
            using (BeginExclusive(ctx, "set_settings", hubMotion: false))
            {
                OnSta(() => { foreach (var kv in values) SetValueChecked(kv.Key, kv.Value); return true; });
                WaitNotUpdating(TimeSpan.FromSeconds(60));
                RefreshStatus();
                return J.Obj("applied", values.Keys.ToList());
            }
        }

        protected override void StopHardware(bool emergency, bool hubOnly)
        {
            if (hubOnly && _hubAcq == null) return; // an acquisition the operator started stays running
            StopAcquisition();
        }
    }

    internal static class DictExt
    {
        public static string GetValueOrDefaultStr(this Dictionary<string, object> d, string key)
        {
            return d != null && d.TryGetValue(key, out var v) && v != null ? Convert.ToString(v, CultureInfo.InvariantCulture) : "";
        }
    }
}
