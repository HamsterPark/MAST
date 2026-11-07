using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.IO.Ports;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using MastSatellite.Core;

namespace MastSatellite.Devices
{
    /// <summary>
    /// UIRobot XYZ stage driven by Fermion Instruments' Motion_Manipulator v1.8 (LabVIEW 2016).
    /// What its manual documents: the WebSocket listener on 6341 exists
    /// but its message format, units, replies and errors are not written down; the documented
    /// remote interface is EPICS Channel Access (per-axis CurPosM/TgtPosC/MoveC/StopC/MovingM...),
    /// which needs the NI DSC runtime, the EPICS server switched on and the GUI put into EPICS
    /// control; the VI Server port exposes undocumented internal VIs. None of these is usable as
    /// documented today, so this driver sends nothing to the stage: no move_* in caps.
    ///
    /// Read-only position (optional, "coordinates_dir"): Motion_Manipulator writes
    /// Data\Coordinates_yyyyMMddHHmmss.csv, one row at BeginMotion / EndMotion / Configured (format
    /// checked on sample files: ASCII, CRLF, the 17-field header below, time of day
    /// only, display and encoder columns in mm). The newest file is followed; status reports the last
    /// row - the end point of the latest motion, not a live position - and moving is inferred from
    /// "last row is BeginMotion". A Configured row redefines coordinates without motion. A header that
    /// differs from the known one is reported, not guessed at.
    ///
    /// The satellite cannot stop this stage: stop / E-STOP answer with an error unless the CSV shows it
    /// idle, so nobody reads "stopped" for a stage that may still move.
    /// </summary>
    public sealed class MotionManipulatorDevice : DeviceBase
    {
        private static readonly string[] CapList = { "status", "stop", "safe" };
        internal const string CoordHeader = "Time,State,X (display),Y (display),Z (display),R1 (display),R2 (display),dummy (display),Transformed?,theta0,X (encoder),Y (encoder),Z (encoder),R1 (encoder),R2 (encoder),dummy (encoder),";
        private const long MaxCoordFileBytes = 32L * 1024 * 1024;
        private const double BeginMotionStaleS = 120; // longer than any move on this stage (<= 1 mm/s over ~15 mm)
        private readonly object _gate = new object();
        private readonly double[] _min = { double.NaN, double.NaN, double.NaN };
        private readonly double[] _max = { double.NaN, double.NaN, double.NaN };
        private readonly double _maxStep;
        private readonly string _coordDir;
        private readonly double _coordPollS;
        private Timer _coordTimer;
        private int _polling;
        private string _coordFile;
        private long _coordOffset;
        private DateTime _coordPrevLocal;
        private bool _coordHeaderSeen, _coordHeaderOk, _coordInitialLoad = true;
        private string _coordProblem;
        private long _rows, _badRows;
        private double? _x, _y, _z;
        private string _lastEvent;
        private long _lastEventMs;
        private double _lastManualEmit = -1e9;

        public MotionManipulatorDevice(DeviceConfig cfg, IDeviceHost host) : base(cfg, host, "xyz_stage")
        {
            Model = "UIRobot XYZ via Motion_Manipulator v1.8";
            _maxStep = cfg.Num("max_step_mm", 0.5);
            var lim = cfg.Dict("limits_mm");
            var names = new[] { "x", "y", "z" };
            for (int i = 0; i < 3; i++)
            {
                var l = lim != null ? J.List(lim, names[i]) : null;
                if (l != null && l.Count == 2) { _min[i] = J.ToDouble(l[0], _min[i]); _max[i] = J.ToDouble(l[1], _max[i]); }
            }
            _coordDir = cfg.Path("coordinates_dir", null);
            _coordPollS = Math.Max(0.05, cfg.Num("coordinates_poll_s", 1.0));
        }

        public override IReadOnlyList<string> Caps => CapList;
        public override Dictionary<string, object> Info => J.Obj(
            "motion", "not supported: Motion_Manipulator has no documented remote protocol (ask Fermion)",
            "position_source", _coordDir != null ? "coordinates_csv" : "none", "coordinates_dir", _coordDir, "max_step_mm", _maxStep,
            "limits_mm", J.Obj("x", new[] { _min[0], _max[0] }, "y", new[] { _min[1], _max[1] }, "z", new[] { _min[2], _max[2] }));
        protected override IEnumerable<string> VolatileStatusKeys => new[] { "position_age_s" };

        public override void Start()
        {
            if (_coordDir == null)
            {
                SetState(StateOffline, "Motion_Manipulator: no documented remote protocol (WebSocket format undocumented; EPICS CA needs NI DSC and EPICS control mode); set coordinates_dir for a read-only position");
                return;
            }
            PollCoordinates();
            _coordTimer = new Timer(_ => PollCoordinates(), null, TimeSpan.FromSeconds(_coordPollS), TimeSpan.FromSeconds(_coordPollS));
        }

        public override void Shutdown()
        {
            try { _coordTimer?.Dispose(); } catch { }
        }

        /// <summary>true / false from the CSV; null when unknown (no file, or a BeginMotion without EndMotion for too long).</summary>
        private bool? MovingNow()
        {
            lock (_gate)
            {
                if (_lastEvent == null) return null;
                if (_lastEvent == "BeginMotion") return Clock.UnixMs() - _lastEventMs < BeginMotionStaleS * 1000 ? true : (bool?)null;
                if (_lastEvent == "EndMotion" || _lastEvent == "Configured") return false;
                return null;
            }
        }

        protected override Dictionary<string, object> BuildStatus(bool fresh)
        {
            var moving = MovingNow();
            lock (_gate)
            {
                var s = J.Obj("x_mm", _x, "y_mm", _y, "z_mm", _z, "moving", moving, "protocol_known", false,
                              "position_source", _coordDir != null ? "coordinates_csv" : "none");
                if (_coordDir != null)
                {
                    s["position_kind"] = _lastEvent == null ? null : _lastEvent == "BeginMotion" ? "start_of_current_motion"
                                       : _lastEvent == "Configured" ? "redefined_coordinates" : "end_of_last_motion";
                    s["position_event"] = _lastEvent;
                    s["position_at_ms"] = _lastEvent != null ? (object)_lastEventMs : null;
                    s["position_age_s"] = _lastEvent != null ? (object)Math.Round((Clock.UnixMs() - _lastEventMs) / 1000.0, 1) : null;
                    s["moving_source"] = "inferred_from_coordinates_csv";
                    s["coordinates_file"] = _coordFile != null ? Path.GetFileName(_coordFile) : null;
                    s["rows"] = _rows;
                    s["bad_rows"] = _badRows;
                    if (_coordProblem != null) s["coordinates_problem"] = _coordProblem;
                }
                return s;
            }
        }

        protected override object ExecuteAction(CommandContext ctx)
        {
            throw new DeviceError(ErrorCodes.NotSupported, "Motion_Manipulator has no documented remote protocol; the satellite does not move this stage");
        }

        private const string CannotStop = "the satellite cannot stop the XYZ stage (Motion_Manipulator has no documented remote interface) and it may be moving; use STOP in Motion_Manipulator";

        protected override void DoStop(CommandContext ctx)
        {
            if (MovingNow() != false) throw new DeviceError(ErrorCodes.NotSupported, CannotStop);
        }

        protected override void StopHardware(bool emergency, bool hubOnly)
        {
            // Nothing the satellite started can be moving; an E-STOP must not count this stage as stopped
            // unless the CSV shows it idle.
            if (emergency && MovingNow() != false) throw new InvalidOperationException(CannotStop);
        }

        // ------------------------------------------------------------------ Coordinates_*.csv (read only)

        private void PollCoordinates()
        {
            if (Interlocked.Exchange(ref _polling, 1) == 1) return;
            try { PollCoordinatesOnce(); }
            catch (Exception ex)
            {
                Log.Debug($"[{Id}] coordinates: {ex.Message}");
                SetProblem("cannot read the coordinates file: " + ex.Message);
            }
            finally
            {
                _coordInitialLoad = false; // whatever exists at the first poll is history; later rows are news
                Interlocked.Exchange(ref _polling, 0);
            }
        }

        private void SetProblem(string p)
        {
            bool changed;
            lock (_gate) { changed = _coordProblem != p; _coordProblem = p; }
            if (changed && p != null) Log.Warn($"[{Id}] {p}");
        }

        /// <summary>The newest Coordinates_yyyyMMddHHmmss.csv by the time in its name (the session's first row).</summary>
        internal static (string path, DateTime start)? NewestCoordinatesFile(string dir)
        {
            (string, DateTime)? best = null;
            foreach (var f in Directory.EnumerateFiles(dir, "Coordinates_*.csv"))
            {
                var stem = Path.GetFileNameWithoutExtension(f).Substring("Coordinates_".Length);
                if (!DateTime.TryParseExact(stem, "yyyyMMddHHmmss", CultureInfo.InvariantCulture, DateTimeStyles.AssumeLocal, out var t)) continue;
                if (best == null || t > best.Value.Item2) best = (f, t);
            }
            return best;
        }

        private void PollCoordinatesOnce()
        {
            if (!Directory.Exists(_coordDir))
            {
                SetState(StateOffline, "coordinates_dir not found: " + _coordDir);
                return;
            }
            var newest = NewestCoordinatesFile(_coordDir);
            if (newest == null)
            {
                SetState(StateReady, "read-only: no Coordinates_*.csv yet (Motion_Manipulator writes one per session at its first motion); motion not supported");
                return;
            }
            if (!string.Equals(newest.Value.path, _coordFile, StringComparison.OrdinalIgnoreCase))
            {
                bool switching = _coordFile != null;
                lock (_gate)
                {
                    _coordFile = newest.Value.path;
                    _coordOffset = 0;
                    _coordPrevLocal = newest.Value.start;
                    _coordHeaderSeen = _coordHeaderOk = false;
                    _rows = _badRows = 0;
                }
                SetProblem(null);
                if (switching) _coordInitialLoad = false; // a new session's rows all happened after we started watching
                Log.Info($"[{Id}] following {Path.GetFileName(_coordFile)}");
            }
            long len = new FileInfo(_coordFile).Length;
            if (len > MaxCoordFileBytes) { SetProblem($"coordinates file larger than {MaxCoordFileBytes >> 20} MB; not read"); return; }
            if (len < _coordOffset) { _coordFile = null; return; } // replaced or truncated: start over next poll
            if (len > _coordOffset)
            {
                byte[] chunk;
                using (var fs = new FileStream(_coordFile, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
                {
                    fs.Seek(_coordOffset, SeekOrigin.Begin);
                    chunk = new byte[len - _coordOffset];
                    int got = 0;
                    while (got < chunk.Length)
                    {
                        int n = fs.Read(chunk, got, chunk.Length - got);
                        if (n <= 0) break;
                        got += n;
                    }
                    if (got < chunk.Length) Array.Resize(ref chunk, got);
                }
                int lastNl = Array.LastIndexOf(chunk, (byte)'\n');
                if (lastNl >= 0) // only complete lines; a half-written one waits for the next poll
                {
                    _coordOffset += lastNl + 1;
                    foreach (var raw in Encoding.ASCII.GetString(chunk, 0, lastNl + 1).Split('\n'))
                    {
                        var line = raw.TrimEnd('\r');
                        if (line.Length == 0) continue;
                        if (!_coordHeaderSeen)
                        {
                            _coordHeaderSeen = true;
                            _coordHeaderOk = line.TrimEnd() == CoordHeader;
                            if (!_coordHeaderOk) SetProblem("unrecognized Coordinates CSV header (format changed?): " + (line.Length > 120 ? line.Substring(0, 120) + "..." : line));
                            continue;
                        }
                        if (_coordHeaderOk) ParseRow(line, emit: !_coordInitialLoad);
                    }
                }
            }
            if (_coordHeaderOk || !_coordHeaderSeen)
                SetState(StateReady, "read-only: position from Motion_Manipulator's Coordinates CSV (end point of the latest motion); motion not supported");
            else
                SetState(StateError, "read-only position unavailable: " + _coordProblem);
            Host.StateChanged(this);
        }

        private static bool TryMm(string s, out double v)
        {
            if (!double.TryParse(s, NumberStyles.Float, CultureInfo.InvariantCulture, out v) || double.IsNaN(v) || double.IsInfinity(v)) return false;
            if (v == 0) v = 0; // "-0.00" -> 0
            return true;
        }

        private void ParseRow(string line, bool emit)
        {
            var f = line.Split(',');
            if (f.Length < 16 || !TimeSpan.TryParseExact(f[0], @"hh\:mm\:ss", CultureInfo.InvariantCulture, out var tod)
                || !TryMm(f[2], out var x) || !TryMm(f[3], out var y) || !TryMm(f[4], out var z))
            {
                lock (_gate) _badRows++;
                return;
            }
            var ev = f[1];
            var t = _coordPrevLocal.Date + tod;
            while (t < _coordPrevLocal - TimeSpan.FromMinutes(1)) t = t.AddDays(1); // the time column has no date: midnight
            _coordPrevLocal = t;
            long ms = Clock.UnixMsFromDateTime(DateTime.SpecifyKind(t, DateTimeKind.Local));
            lock (_gate)
            {
                _x = x; _y = y; _z = z;
                _lastEvent = ev;
                _lastEventMs = ms;
                _rows++;
            }
            if (!emit || Clock.UnixMs() - ms > 600000) return; // old rows (e.g. a folder that appeared late) are not live activity
            Emit("axis_state", J.Obj("x_mm", x, "y_mm", y, "z_mm", z, "moving", MovingNow(), "event", ev, "position_at_ms", ms, "source", "coordinates_csv"));
            if (ev == "Configured")
                Emit("manual_activity", J.Obj("what", "xyz_coordinates_redefined", "source", "motion_manipulator", "x_mm", x, "y_mm", y, "z_mm", z));
            else if (ev == "BeginMotion" && Clock.MonoSeconds - _lastManualEmit >= 2)
            {
                _lastManualEmit = Clock.MonoSeconds;
                Emit("manual_activity", J.Obj("what", "xyz_motion_started", "source", "motion_manipulator", "x_mm", x, "y_mm", y, "z_mm", z));
            }
        }
    }

    /// <summary>
    /// Brimrose DS1 AOTF driver on a serial port (a USB CDC virtual COM port). The serial protocol is not
    /// published (DS1CtrlSw's manual has none), so by default only status/stop answer and
    /// everything else is not_supported; the port is not even opened. Once Brimrose provides the
    /// protocol it can be configured as command templates (no code change):
    ///   "commands": {"set": "...{channel}...{frequency_mhz}...{amplitude}...", "rf_off": "..."},
    ///   "terminator": "\r", "reply_terminator": "\r", "expect_ok": "regex", "baud": 128000
    /// </summary>
    public sealed class Ds1Device : DeviceBase
    {
        private static readonly string[] CapList = { "status", "stop", "safe", "set", "rf_off" };
        private readonly object _io = new object();
        private readonly string _port;
        private readonly int _baud;
        private readonly Dictionary<string, object> _commands;
        private readonly string _term;
        private readonly string _replyTerm;
        private readonly Regex _expect;
        private SerialPort _sp;

        public Ds1Device(DeviceConfig cfg, IDeviceHost host) : base(cfg, host, "aotf")
        {
            Model = "Brimrose DS1";
            _port = cfg.Str("port", "");
            _baud = cfg.Int("baud", 128000);
            _commands = cfg.Dict("commands");
            _term = Unescape(cfg.Str("terminator", "\\r"));
            _replyTerm = Unescape(cfg.Str("reply_terminator", "\\r"));
            var ex = cfg.Str("expect_ok");
            _expect = string.IsNullOrEmpty(ex) ? null : new Regex(ex);
        }

        private bool ProtocolKnown => _commands != null && _commands.Count > 0;

        public override IReadOnlyList<string> Caps => CapList;
        public override Dictionary<string, object> Info => J.Obj("port", _port, "baud", _baud, "protocol_known", ProtocolKnown);

        private static string Unescape(string s) { return (s ?? "").Replace("\\r", "\r").Replace("\\n", "\n"); }

        public override void Start()
        {
            if (!ProtocolKnown)
            {
                SetState(StateReady, "DS1 serial protocol not provided by the vendor yet; only status/stop are available");
                return;
            }
            if (string.IsNullOrEmpty(_port))
            {
                SetState(StateError, "config: 'port' (the DS1's COM port) is required once 'commands' are configured");
                return;
            }
            SetState(StateReady);
        }

        protected override Dictionary<string, object> BuildStatus(bool fresh)
        {
            bool present = false;
            try { present = !string.IsNullOrEmpty(_port) && SerialPort.GetPortNames().Contains(_port, StringComparer.OrdinalIgnoreCase); } catch { }
            return J.Obj("port", _port, "port_present", present, "protocol_known", ProtocolKnown, "port_open", _sp?.IsOpen ?? false);
        }

        protected override object ExecuteAction(CommandContext ctx)
        {
            if (!ProtocolKnown) throw new DeviceError(ErrorCodes.NotSupported, "DS1 serial protocol is not available yet (waiting for Brimrose)");
            var template = J.Str(_commands, ctx.Action);
            if (template == null) throw new DeviceError(ErrorCodes.NotSupported, $"no command template for '{ctx.Action}'");
            var vars = new Dictionary<string, string>(StringComparer.Ordinal);
            foreach (var kv in ctx.Args)
                vars[kv.Key] = Convert.ToString(kv.Value, CultureInfo.InvariantCulture);
            if (ctx.Has("amplitude")) vars["amplitude_pct"] = (ctx.Num("amplitude") * 100).ToString("0.##", CultureInfo.InvariantCulture);
            if (!vars.ContainsKey("channel")) vars["channel"] = "0";
            var cmd = Regex.Replace(template, @"\{(\w+)\}", m =>
            {
                if (!vars.TryGetValue(m.Groups[1].Value, out var v)) throw new DeviceError(ErrorCodes.InvalidArgs, $"template needs '{m.Groups[1].Value}'");
                return v;
            });
            using (BeginExclusive(ctx, ctx.Action, hubMotion: false))
            {
                var reply = Transact(cmd);
                if (_expect != null && !_expect.IsMatch(reply)) throw new DeviceError(ErrorCodes.Internal, $"DS1 replied '{reply}'");
                return J.Obj("sent", cmd, "reply", reply);
            }
        }

        private string Transact(string cmd)
        {
            lock (_io)
            {
                if (_sp == null || !_sp.IsOpen)
                {
                    _sp = new SerialPort(_port, _baud) { ReadTimeout = 2000, WriteTimeout = 2000, NewLine = _replyTerm, Encoding = Encoding.ASCII };
                    _sp.Open();
                }
                _sp.DiscardInBuffer();
                _sp.Write(cmd + _term);
                try { return _sp.ReadLine().Trim(); }
                catch (TimeoutException) { return ""; }
            }
        }

        protected override void StopHardware(bool emergency, bool hubOnly)
        {
            if (emergency && ProtocolKnown && J.Has(_commands, "rf_off"))
            {
                try { Transact(J.Str(_commands, "rf_off").Replace("{channel}", "0")); } catch (Exception ex) { Log.Error($"[{Id}] rf_off on estop failed", ex); }
            }
        }

        public override void Shutdown()
        {
            lock (_io) { try { _sp?.Close(); } catch { } }
        }
    }

    /// <summary>
    /// SpectraSense 5.2 + SpectraHub PMT through SpectraSense's out-of-process COM automation
    /// (ProgID SpectraSense.SpectraSense_Interface, methods from its type library:
    /// NCL_Present, NCL_ChPresent, getNCLITime/setNCLITime, NCLChRead(ch, ref double),
    /// getNCLChHVon/setNCLChHVon, getNCLChHV/setNCLChHV, IsSpectraSenseUp). Experimental and off by
    /// default: channel numbers start at 1 and 0 means failure (inferred), and creating the object
    /// starts SpectraSense.exe, which claims the spectrometer's serial port as well - never together
    /// with LightField in CCD mode. Needs the desktop session.
    /// </summary>
    public sealed class SpectraSenseDevice : DeviceBase
    {
        private static readonly string[] CapList = { "status", "stop", "safe", "read", "hv_off", "set_hv" };
        private const string ProgId = "SpectraSense.SpectraSense_Interface";
        private static readonly Guid Clsid = new Guid("3DC4AEF8-331E-11D4-9DBF-00A0C98BBD0A");
        private readonly int _channel;
        private readonly double _maxHv;
        private readonly bool _safeHvOff;
        private StaWorker _sta;
        private object _ss;
        private bool _hvByHub;
        private Dictionary<string, object> _cache = J.Obj();

        [DllImport("ole32.dll")]
        private static extern int CoCreateInstance([In] ref Guid clsid, IntPtr outer, uint ctx, [In] ref Guid iid, [MarshalAs(UnmanagedType.IUnknown)] out object obj);

        public SpectraSenseDevice(DeviceConfig cfg, IDeviceHost host) : base(cfg, host, "pmt")
        {
            Model = "SpectraSense / SpectraHub";
            _channel = cfg.Int("channel", 1);
            _maxHv = cfg.Num("max_hv", 1000);
            _safeHvOff = cfg.Bool("safe_hv_off", true);
        }

        public override IReadOnlyList<string> Caps => CapList;
        public override Dictionary<string, object> Info => J.Obj("prog_id", ProgId, "channel", _channel, "max_hv", _maxHv, "experimental", true);

        public override void Start()
        {
            if (!SessionInfo.HasDesktop)
            {
                SetState(StateError, "SpectraSense COM needs the desktop session (session " + SessionInfo.SessionId + ")");
                return;
            }
            try
            {
                _sta = new StaWorker("spectrasense-sta");
                _sta.Invoke(() =>
                {
                    var t = Type.GetTypeFromProgID(ProgId, false);
                    if (t != null)
                    {
                        try { _ss = Activator.CreateInstance(t); }
                        catch (COMException) { _ss = null; }
                    }
                    if (_ss == null)
                    {
                        // SpectraSense is a 32-bit server; its LocalServer32 key can be only in the 32-bit registry view.
                        var clsid = Clsid;
                        var iid = new Guid("00020400-0000-0000-C000-000000000046"); // IDispatch
                        int hr = CoCreateInstance(ref clsid, IntPtr.Zero, 0x4 | 0x40000, ref iid, out _ss); // LOCAL_SERVER | ACTIVATE_32_BIT_SERVER
                        if (hr != 0) throw new COMException("CoCreateInstance SpectraSense failed", hr);
                    }
                }, TimeSpan.FromSeconds(120));
                RefreshCache();
                SetState(StateReady);
            }
            catch (Exception ex)
            {
                SetState(StateError, "SpectraSense COM not available: " + ex.Message);
            }
        }

        private T Com<T>(Func<dynamic, T> f, double timeoutS = 30)
        {
            if (_ss == null) throw new DeviceError(ErrorCodes.Unavailable, "SpectraSense not connected");
            try { return _sta.Invoke(() => f((dynamic)_ss), TimeSpan.FromSeconds(timeoutS)); }
            catch (DeviceError) { throw; }
            catch (TimeoutException ex) { throw new DeviceError(ErrorCodes.Timeout, "SpectraSense: " + ex.Message); }
            catch (Exception ex) { throw new DeviceError(ErrorCodes.Internal, "SpectraSense: " + ex.Message); }
        }

        private void RefreshCache()
        {
            var d = Com(ss =>
            {
                var r = J.Obj();
                r["up"] = (int)ss.IsSpectraSenseUp() != 0;
                r["ncl_present"] = (int)ss.NCL_Present() != 0;
                r["channel_present"] = (int)ss.NCL_ChPresent(_channel) != 0;
                r["integration_ms"] = (int)ss.getNCLITime();
                r["hv_on"] = (int)ss.getNCLChHVon(_channel) != 0;
                r["hv_volts"] = (int)ss.getNCLChHV(_channel);
                return r;
            });
            _cache = d;
        }

        protected override Dictionary<string, object> BuildStatus(bool fresh)
        {
            if (fresh && _ss != null) { try { RefreshCache(); } catch (Exception ex) { Log.Debug($"[{Id}] {ex.Message}"); } }
            return new Dictionary<string, object>(_cache, StringComparer.Ordinal);
        }

        protected override object ExecuteAction(CommandContext ctx)
        {
            switch (ctx.Action)
            {
                case "read":
                {
                    int ms = (int)Math.Round(ctx.NumOpt("integration_ms") ?? 100);
                    if (ms < 5 || ms > 64000) throw new DeviceError(ErrorCodes.InvalidArgs, "integration_ms must be 5..64000");
                    using (BeginExclusive(ctx, "read", hubMotion: true))
                    {
                        var v = Com(ss =>
                        {
                            if ((int)ss.setNCLITime(ms) == 0) throw new DeviceError(ErrorCodes.Internal, "setNCLITime failed");
                            double val = 0;
                            if ((int)ss.NCLChRead(_channel, ref val) == 0) throw new DeviceError(ErrorCodes.Internal, "NCLChRead failed");
                            return val;
                        }, ms / 1000.0 + 30);
                        return J.Obj("value", v, "integration_ms", ms, "channel", _channel);
                    }
                }
                case "set_hv":
                {
                    var volts = ctx.Num("volts");
                    if (volts < 0 || volts > _maxHv) throw new DeviceError(ErrorCodes.Limit, $"volts must be 0..{F(_maxHv)}");
                    Com(ss =>
                    {
                        if ((int)ss.setNCLChHV(_channel, (int)Math.Round(volts)) == 0) throw new DeviceError(ErrorCodes.Internal, "setNCLChHV failed");
                        if ((int)ss.setNCLChHVon(_channel, volts > 0 ? 1 : 0) == 0) throw new DeviceError(ErrorCodes.Internal, "setNCLChHVon failed");
                        return true;
                    });
                    _hvByHub = ctx.FromHub && volts > 0;
                    RefreshCache();
                    return BuildStatus(false);
                }
                case "hv_off":
                    HvOff();
                    RefreshCache();
                    return BuildStatus(false);
            }
            throw new DeviceError(ErrorCodes.NotSupported, ctx.Action);
        }

        private void HvOff()
        {
            Com(ss => { ss.setNCLChHVon(_channel, 0); return true; });
            _hvByHub = false;
        }

        protected override void StopHardware(bool emergency, bool hubOnly)
        {
            if (_ss == null) return;
            if (emergency || (_hvByHub && _safeHvOff))
            {
                try { HvOff(); } catch (Exception ex) { Log.Error($"[{Id}] HV off failed", ex); }
            }
        }

        public override void Shutdown()
        {
            if (_ss != null && _sta != null)
            {
                try { _sta.Invoke(() => Marshal.ReleaseComObject(_ss), TimeSpan.FromSeconds(10)); } catch { }
            }
            _ss = null;
            _sta?.Dispose();
        }
    }
}
