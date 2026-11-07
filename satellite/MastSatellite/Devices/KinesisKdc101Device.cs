using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using MastSatellite.Core;

namespace MastSatellite.Devices
{
    /// <summary>
    /// Thorlabs KDC101 + PRM1-Z8 rotation stage through the Kinesis C API
    /// (Thorlabs.MotionControl.KCube.DCServo.dll, CC_* functions; signatures from
    /// Thorlabs.MotionControl.KCube.DCServo.h of Kinesis 1.14.58 and the KDC101 example in the
    /// C API help). The DLL is loaded by path at runtime. Real-world units come from
    /// CC_GetRealValueFromDeviceUnit after CC_LoadSettings (Kinesis maps the cube's serial number
    /// to its stage settings); 1919.64 counts/deg (PRM1-Z8) is the fallback and the cross-check.
    /// Absolute moves need a home after power-up (status bit 0x400).
    /// </summary>
    public sealed class KinesisKdc101Device : DeviceBase, ILocalActions
    {
        private static readonly string[] CapList = { "status", "stop", "safe", "move_abs", "move_rel", "home", "set_velocity", "identify" };
        private const int TypeIdKdc101 = 27;
        private const uint BitCw = 0x10, BitCcw = 0x20, BitJogCw = 0x40, BitJogCcw = 0x80, BitHoming = 0x200, BitHomed = 0x400,
                           BitLimCw = 0x1, BitLimCcw = 0x2, BitEnabled = 0x80000000;

        [UnmanagedFunctionPointer(CallingConvention.Cdecl)] private delegate short DShort();
        [UnmanagedFunctionPointer(CallingConvention.Cdecl, CharSet = CharSet.Ansi)] private delegate short DListByType(StringBuilder buf, uint size, int typeId);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl, CharSet = CharSet.Ansi)] private delegate short DSerShort([MarshalAs(UnmanagedType.LPStr)] string s);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl, CharSet = CharSet.Ansi)] private delegate void DSerVoid([MarshalAs(UnmanagedType.LPStr)] string s);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl, CharSet = CharSet.Ansi)] [return: MarshalAs(UnmanagedType.I1)] private delegate bool DSerBool([MarshalAs(UnmanagedType.LPStr)] string s);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl, CharSet = CharSet.Ansi)] [return: MarshalAs(UnmanagedType.I1)] private delegate bool DSerIntBool([MarshalAs(UnmanagedType.LPStr)] string s, int v);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl, CharSet = CharSet.Ansi)] private delegate int DSerInt([MarshalAs(UnmanagedType.LPStr)] string s);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl, CharSet = CharSet.Ansi)] private delegate uint DSerUInt([MarshalAs(UnmanagedType.LPStr)] string s);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl, CharSet = CharSet.Ansi)] private delegate short DSerIntShort([MarshalAs(UnmanagedType.LPStr)] string s, int v);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl, CharSet = CharSet.Ansi)] [return: MarshalAs(UnmanagedType.I1)] private delegate bool DNextMsg([MarshalAs(UnmanagedType.LPStr)] string s, out ushort type, out ushort id, out uint data);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl, CharSet = CharSet.Ansi)] private delegate short DRealFromDev([MarshalAs(UnmanagedType.LPStr)] string s, int du, out double real, int unitType);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl, CharSet = CharSet.Ansi)] private delegate short DDevFromReal([MarshalAs(UnmanagedType.LPStr)] string s, double real, out int du, int unitType);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl, CharSet = CharSet.Ansi)] private delegate short DGetVel([MarshalAs(UnmanagedType.LPStr)] string s, out int accel, out int maxVel);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl, CharSet = CharSet.Ansi)] private delegate short DSetVel([MarshalAs(UnmanagedType.LPStr)] string s, int accel, int maxVel);

        private DShort _buildList;
        private DListByType _listByType;
        private DSerShort _open, _home, _stopImmediate, _stopProfiled, _enable, _requestStatus, _requestVel;
        private DSerVoid _close, _stopPolling, _clearQueue, _identify;
        private DSetVel _setVel;
        private DSerBool _loadSettings, _checkConnection;
        private double _lastConnCheck;
        private DSerIntBool _startPolling;
        private DSerInt _getPosition, _queueSize;
        private DSerUInt _getStatus;
        private DSerIntShort _moveTo, _moveRel;
        private DNextMsg _nextMsg;
        private DRealFromDev _realFromDev;
        private DDevFromReal _devFromReal;
        private DGetVel _getVel;

        private readonly object _io = new object();
        private readonly string _serial;
        private readonly string _dir;
        private readonly double _countsPerDeg;
        private readonly double _minDeg;
        private readonly double _maxDeg;
        private readonly bool _requireHome;
        private readonly int _pollMs;
        private readonly double _homeTimeoutS;
        private readonly double _retryOpenS;
        private IntPtr _module;
        private volatile bool _open1;
        private bool _useApiUnits;
        private double _speedDegS = 10;          // for timeouts; reported only once read from the cube
        private bool _velKnown;
        private double _accelDegS2 = double.NaN;
        private readonly double _maxVel;
        private readonly double _maxAccel;
        private volatile int _posDu;
        private volatile uint _bits;
        private double _lastAxisEmit;
        private bool _lastEmittedMoving;
        private double _lastManualPos = double.NaN;
        private double _lastManualEmit;
        private volatile bool _waiting;
        private Timer _poll;
        private Timer _retry;

        public KinesisKdc101Device(DeviceConfig cfg, IDeviceHost host) : base(cfg, host, "rotation_stage")
        {
            _serial = cfg.Str("serial", "");
            Serial = _serial;
            Model = "KDC101 + " + cfg.Str("stage", "PRM1-Z8");
            _dir = cfg.Path("kinesis_dir", @"C:\Program Files\Thorlabs\Kinesis");
            _countsPerDeg = cfg.Num("counts_per_deg", 1919.64);
            _minDeg = cfg.Num("min_deg", -360);
            _maxDeg = cfg.Num("max_deg", 720);
            _requireHome = cfg.Bool("require_home_for_abs", true);
            _pollMs = Math.Max(50, cfg.Int("poll_ms", 100));
            _homeTimeoutS = cfg.Num("home_timeout_s", 60);
            _retryOpenS = cfg.Num("retry_open_s", 30);
            _maxVel = cfg.Num("max_velocity_deg_s", 25);
            _maxAccel = cfg.Num("max_accel_deg_s2", 25);
        }

        public override IReadOnlyList<string> Caps => CapList;
        public IReadOnlyList<string> LocalActions => new[] { "重新连接 KDC101" };
        protected override IEnumerable<string> VolatileStatusKeys => new[] { "position_deg", "position_du" };
        public override Dictionary<string, object> Info => J.Obj("kinesis_dir", _dir, "counts_per_deg", _countsPerDeg, "min_deg", _minDeg, "max_deg", _maxDeg,
                                                                "units_source", _useApiUnits ? "kinesis_api" : "config", "require_home_for_abs", _requireHome);

        public void RunLocalAction(string name)
        {
            ThreadPool.QueueUserWorkItem(_ => { Close(); Open(); });
        }

        public override void Start()
        {
            if (string.IsNullOrEmpty(_serial))
            {
                SetState(StateError, "config: 'serial' (the KDC101's serial number, on its label and in Kinesis) is required");
                return;
            }
            try { Bind(); }
            catch (Exception ex)
            {
                SetState(StateError, ex.Message);
                return;
            }
            Open();
            _poll = new Timer(_ => PollOnce(), null, 500, _pollMs);
            _retry = new Timer(_ => { if (!_open1 && State != StateError) Open(); }, null,
                               TimeSpan.FromSeconds(_retryOpenS), TimeSpan.FromSeconds(_retryOpenS));
        }

        private void Bind()
        {
            var dll = Path.Combine(_dir, "Thorlabs.MotionControl.KCube.DCServo.dll");
            if (!File.Exists(dll)) throw new FileNotFoundException("Kinesis KCube.DCServo DLL not found: " + dll);
            Native.SetDllDirectory(_dir);
            _module = Native.LoadVendorDll(dll);
            _buildList = Native.GetFunction<DShort>(_module, "TLI_BuildDeviceList");
            _listByType = Native.GetFunction<DListByType>(_module, "TLI_GetDeviceListByTypeExt");
            _open = Native.GetFunction<DSerShort>(_module, "CC_Open");
            _close = Native.GetFunction<DSerVoid>(_module, "CC_Close");
            _loadSettings = Native.GetFunction<DSerBool>(_module, "CC_LoadSettings");
            _startPolling = Native.GetFunction<DSerIntBool>(_module, "CC_StartPolling");
            _stopPolling = Native.GetFunction<DSerVoid>(_module, "CC_StopPolling");
            _clearQueue = Native.GetFunction<DSerVoid>(_module, "CC_ClearMessageQueue");
            _queueSize = Native.GetFunction<DSerInt>(_module, "CC_MessageQueueSize");
            _nextMsg = Native.GetFunction<DNextMsg>(_module, "CC_GetNextMessage");
            _home = Native.GetFunction<DSerShort>(_module, "CC_Home");
            _moveTo = Native.GetFunction<DSerIntShort>(_module, "CC_MoveToPosition");
            _moveRel = Native.GetFunction<DSerIntShort>(_module, "CC_MoveRelative");
            _stopImmediate = Native.GetFunction<DSerShort>(_module, "CC_StopImmediate");
            _stopProfiled = Native.GetFunction<DSerShort>(_module, "CC_StopProfiled");
            _enable = Native.GetFunction<DSerShort>(_module, "CC_EnableChannel");
            _requestStatus = Native.GetFunction<DSerShort>(_module, "CC_RequestStatusBits");
            _getPosition = Native.GetFunction<DSerInt>(_module, "CC_GetPosition");
            _getStatus = Native.GetFunction<DSerUInt>(_module, "CC_GetStatusBits");
            _realFromDev = Native.TryGetFunction<DRealFromDev>(_module, "CC_GetRealValueFromDeviceUnit");
            _devFromReal = Native.TryGetFunction<DDevFromReal>(_module, "CC_GetDeviceUnitFromRealValue");
            _getVel = Native.TryGetFunction<DGetVel>(_module, "CC_GetVelParams");
            _setVel = Native.TryGetFunction<DSetVel>(_module, "CC_SetVelParams");
            _requestVel = Native.TryGetFunction<DSerShort>(_module, "CC_RequestVelParams");
            _identify = Native.TryGetFunction<DSerVoid>(_module, "CC_Identify");
            _checkConnection = Native.TryGetFunction<DSerBool>(_module, "CC_CheckConnection");
        }

        private static string Err(short code)
        {
            switch (code)
            {
                case 0: return "ok";
                case 2: return "FT_DeviceNotFound";
                case 3: return "FT_DeviceNotOpened";
                case 4: return "FT_IOError";
                case 7: return "FT_DeviceNotPresent (unplugged?)";
                case 32: return "TL_ALREADY_OPEN";
                case 33: return "TL_NO_RESPONSE";
                case 35: return "TL_FAULT_REPORTED";
                case 36: return "TL_INVALID_OPERATION";
                case 37: return "TL_UNHOMED";
                case 38: return "TL_INVALID_POSITION";
                case 41: return "TL_FIRMWARE_BUG";
                case 47: return "TL_CMD_TEMP_UNAVAILABLE";
                default: return "error " + code;
            }
        }

        private void Open()
        {
            lock (_io)
            {
                if (_open1 || _module == IntPtr.Zero) return;
                try
                {
                    short r = _buildList();
                    if (r != 0) { SetState(StateOffline, "TLI_BuildDeviceList: " + Err(r)); return; }
                    var sb = new StringBuilder(1024);
                    _listByType(sb, (uint)sb.Capacity, TypeIdKdc101);
                    var serials = sb.ToString().Split(new[] { ',' }, StringSplitOptions.RemoveEmptyEntries).Select(s => s.Trim()).ToList();
                    if (!serials.Contains(_serial))
                    {
                        // BuildDeviceList does not list devices another process has open (Kinesis GUI).
                        SetState(StateOffline, $"KDC101 {_serial} not listed (unplugged, or held by another program such as the Kinesis GUI; seen: {string.Join(",", serials)})");
                        return;
                    }
                    r = _open(_serial);
                    if (r != 0) { SetState(StateOffline, "CC_Open: " + Err(r)); return; }
                    _open1 = true;
                    if (!_loadSettings(_serial)) Log.Warn($"[{Id}] CC_LoadSettings returned false; real-world units may be wrong");
                    _startPolling(_serial, Math.Max(100, _pollMs));
                }
                catch (Exception ex)
                {
                    SetState(StateError, "Kinesis open failed: " + ex.Message);
                    return;
                }
            }
            // The SDK example waits for the first polls before moving.
            for (int i = 0; i < 30 && ReadBits() == 0; i++) Thread.Sleep(100);
            CheckUnits();
            ReadVelocity(out _, out _);
            SetState(StateReady);
            Log.Info($"[{Id}] KDC101 {_serial} open; position {F(Deg(ReadPos()))} deg, homed={(ReadBits() & BitHomed) != 0}, units {(_useApiUnits ? "Kinesis API" : "config " + _countsPerDeg + " counts/deg")}");
        }

        private void Close()
        {
            lock (_io)
            {
                if (!_open1) return;
                try { _stopPolling(_serial); } catch { }
                try { _close(_serial); } catch { }
                _open1 = false;
            }
            SetState(StateOffline, "closed");
        }

        private void CheckUnits()
        {
            _useApiUnits = false;
            if (_realFromDev == null || _devFromReal == null) return;
            try
            {
                int test = (int)Math.Round(10 * _countsPerDeg);
                short r;
                double real;
                lock (_io) r = _realFromDev(_serial, test, out real, 0);
                if (r == 0 && Math.Abs(real - 10.0) < 0.1) { _useApiUnits = true; return; }
                Log.Warn($"[{Id}] Kinesis unit conversion gives {real} for {test} du (expected ~10 deg); using counts_per_deg={_countsPerDeg}");
            }
            catch (Exception ex) { Log.Warn($"[{Id}] unit check failed: {ex.Message}"); }
        }

        /// <summary>
        /// The move velocity parameters held by the cube (CC_GetVelParams), in deg/s and deg/s2 through the
        /// Kinesis conversion (unitType 1 velocity, 2 acceleration). Only trusted when that conversion passed
        /// the distance check (_useApiUnits); otherwise nothing is reported.
        /// </summary>
        private bool ReadVelocity(out double vel, out double acc)
        {
            vel = acc = double.NaN;
            try
            {
                if (_getVel == null || _realFromDev == null || !_useApiUnits) return false;
                int accDu, velDu;
                lock (_io)
                {
                    if (_getVel(_serial, out accDu, out velDu) != 0) return false;
                    if (_realFromDev(_serial, velDu, out vel, 1) != 0) return false;
                    if (_realFromDev(_serial, accDu, out acc, 2) != 0) acc = double.NaN;
                }
                if (vel > 0.01 && vel < 1000) { _speedDegS = vel; _velKnown = true; }
                _accelDegS2 = acc;
                return true;
            }
            catch { return false; }
        }

        private static bool Near(double got, double want) { return !double.IsNaN(got) && Math.Abs(got - want) <= Math.Max(1e-3, 0.01 * Math.Abs(want)); }

        private double Deg(int du)
        {
            if (_useApiUnits)
            {
                lock (_io)
                    if (_realFromDev(_serial, du, out var real, 0) == 0) return real;
            }
            return du / _countsPerDeg;
        }

        private int Du(double deg)
        {
            if (_useApiUnits)
            {
                lock (_io)
                    if (_devFromReal(_serial, deg, out var du, 0) == 0) return du;
            }
            return (int)Math.Round(deg * _countsPerDeg);
        }

        private int ReadPos() { lock (_io) return _open1 ? _getPosition(_serial) : _posDu; }
        private uint ReadBits() { lock (_io) return _open1 ? _getStatus(_serial) : 0u; }

        private Dictionary<string, object> Snapshot()
        {
            uint b = _bits;
            var s = J.Obj("position_deg", Math.Round(Deg(_posDu), 4), "position_du", _posDu,
                          "moving", (b & (BitCw | BitCcw | BitJogCw | BitJogCcw | BitHoming)) != 0,
                          "homing", (b & BitHoming) != 0, "homed", (b & BitHomed) != 0, "enabled", (b & BitEnabled) != 0,
                          "limit_cw", (b & BitLimCw) != 0, "limit_ccw", (b & BitLimCcw) != 0, "status_bits", "0x" + b.ToString("X8"));
            if (_velKnown) s["velocity_deg_s"] = Math.Round(_speedDegS, 4);
            if (!double.IsNaN(_accelDegS2)) s["accel_deg_s2"] = Math.Round(_accelDegS2, 4);
            return s;
        }

        private void PollOnce()
        {
            if (!_open1) return;
            try
            {
                // CC_CheckConnection: "true if the USB is listed by the ftdi controller" - catches an unplugged cube.
                if (_checkConnection != null && ActiveOperation == null && Clock.MonoSeconds - _lastConnCheck > 5)
                {
                    _lastConnCheck = Clock.MonoSeconds;
                    bool ok;
                    lock (_io) ok = _checkConnection(_serial);
                    if (!ok)
                    {
                        Log.Warn($"[{Id}] KDC101 {_serial} no longer on USB");
                        Close();
                        SetState(StateOffline, "KDC101 disconnected (USB); reconnects automatically when it is back");
                        return;
                    }
                }
                _posDu = ReadPos();
                _bits = ReadBits();
                bool moving = IsMoving(_bits);
                double now = Clock.MonoSeconds;
                // At once when motion starts or ends (契约 §5.3 item 11), otherwise 20 Hz moving / 1 Hz idle.
                if (moving != _lastEmittedMoving || now - _lastAxisEmit >= (moving ? 0.05 : 1.0))
                    EmitAxisNow(moving);
                // Movement nobody here commanded = someone used the front panel.
                double deg = Deg(_posDu);
                if (ActiveOperation == null && !_waiting)
                {
                    if (!double.IsNaN(_lastManualPos) && Math.Abs(deg - _lastManualPos) > 0.05 && now - _lastManualEmit > 2)
                    {
                        _lastManualEmit = now;
                        Emit("manual_activity", J.Obj("what", "moved_without_hub_command", "position_deg", Math.Round(deg, 3)));
                    }
                }
                _lastManualPos = deg;
                if (!_waiting) lock (_io) if (_queueSize(_serial) > 50) _clearQueue(_serial);
            }
            catch (Exception ex)
            {
                Log.Debug($"[{Id}] poll: {ex.Message}");
            }
        }

        protected override Dictionary<string, object> BuildStatus(bool fresh)
        {
            if (fresh && _open1) { _posDu = ReadPos(); _bits = ReadBits(); }
            var s = Snapshot();
            s["open"] = _open1;
            return s;
        }

        protected override object ExecuteAction(CommandContext ctx)
        {
            if (!_open1) throw new DeviceError(ErrorCodes.Unavailable, "KDC101 not open");
            switch (ctx.Action)
            {
                case "move_abs":
                {
                    var target = ctx.Num("deg");
                    CheckLimits(target);
                    if (_requireHome && (ReadBits() & BitHomed) == 0) throw new DeviceError(ErrorCodes.Unavailable, "stage not homed since power-up: run 'home' first");
                    using (BeginExclusive(ctx, "move_abs", hubMotion: true))
                    {
                        EnsureEnabled();
                        double from = Deg(ReadPos());
                        RunMove(ctx, () => _moveTo(_serial, Du(target)), 1, Math.Abs(target - from) / _speedDegS * 2 + 10);
                    }
                    return Snapshot();
                }
                case "move_rel":
                {
                    var d = ctx.Num("deg");
                    double from = Deg(ReadPos());
                    CheckLimits(from + d);
                    using (BeginExclusive(ctx, "move_rel", hubMotion: true))
                    {
                        EnsureEnabled();
                        int du = _useApiUnits ? Du(from + d) - Du(from) : (int)Math.Round(d * _countsPerDeg);
                        RunMove(ctx, () => _moveRel(_serial, du), 1, Math.Abs(d) / _speedDegS * 2 + 10);
                    }
                    return Snapshot();
                }
                case "home":
                    using (BeginExclusive(ctx, "home", hubMotion: true))
                    {
                        EnsureEnabled();
                        RunMove(ctx, () => _home(_serial), 0, _homeTimeoutS);
                    }
                    return Snapshot();
                case "set_velocity":
                {
                    var v = ctx.Num("deg_s");
                    var acc = ctx.NumOpt("accel_deg_s2");
                    SimRotationStage.CheckVelocity(v, acc, _maxVel, _maxAccel);
                    if (_setVel == null || _getVel == null || _devFromReal == null || !_useApiUnits)
                        throw new DeviceError(ErrorCodes.Unavailable, "set_velocity needs CC_SetVelParams / CC_GetVelParams and the Kinesis unit conversion (units_source is "
                                                                      + (_useApiUnits ? "kinesis_api" : "config") + ")");
                    using (BeginExclusive(ctx, "set_velocity", hubMotion: false))
                    {
                        short r;
                        lock (_io)
                        {
                            r = _getVel(_serial, out int curAcc, out _);
                            if (r != 0) throw new DeviceError(ErrorCodes.Unavailable, "CC_GetVelParams: " + Err(r));
                            if (_devFromReal(_serial, v, out int velDu, 1) != 0) throw new DeviceError(ErrorCodes.Internal, "Kinesis velocity conversion failed");
                            int accDu = curAcc;
                            if (acc != null && _devFromReal(_serial, acc.Value, out accDu, 2) != 0) throw new DeviceError(ErrorCodes.Internal, "Kinesis acceleration conversion failed");
                            r = _setVel(_serial, accDu, velDu);
                        }
                        if (r != 0) throw new DeviceError(ErrorCodes.Internal, "CC_SetVelParams: " + Err(r));
                        // Read back what the cube holds now (CC_RequestVelParams refreshes the DLL's copy).
                        double gotV, gotA;
                        var end = Clock.MonoSeconds + 2.0;
                        do
                        {
                            if (_requestVel != null) lock (_io) _requestVel(_serial);
                            Thread.Sleep(100);
                            ReadVelocity(out gotV, out gotA);
                        } while (Clock.MonoSeconds < end && !(Near(gotV, v) && (acc == null || Near(gotA, acc.Value))));
                        if (!Near(gotV, v) || (acc != null && !Near(gotA, acc.Value)))
                            throw new DeviceError(ErrorCodes.Internal, $"KDC101 reads back {F(gotV)} deg/s, {F(gotA)} deg/s2 after setting {F(v)} deg/s"
                                                                      + (acc != null ? $", {F(acc.Value)} deg/s2" : ""));
                    }
                    return Snapshot();
                }
                case "identify":
                    // CC_Identify: "Sends a command to the device to make it identify itself" (the front-panel LED flashes).
                    if (_identify == null) throw new DeviceError(ErrorCodes.NotSupported, "CC_Identify is not in this Kinesis DLL");
                    lock (_io) _identify(_serial);
                    return Snapshot();
            }
            throw new DeviceError(ErrorCodes.NotSupported, ctx.Action);
        }

        private void CheckLimits(double target)
        {
            if (target < _minDeg || target > _maxDeg)
                throw new DeviceError(ErrorCodes.Limit, $"target {F(target)} deg outside soft limits [{F(_minDeg)}, {F(_maxDeg)}]");
        }

        private void EnsureEnabled()
        {
            if ((ReadBits() & BitEnabled) != 0) return;
            short r;
            lock (_io) r = _enable(_serial);
            if (r != 0) throw new DeviceError(ErrorCodes.Unavailable, "CC_EnableChannel: " + Err(r));
            Thread.Sleep(300);
        }

        /// <summary>
        /// Start a move and wait for the matching GenericMotor message (type 2: 0 homed, 1 moved,
        /// 2 stopped), with the status bits as a fallback. Cancel -> CC_StopProfiled.
        /// </summary>
        private void RunMove(CommandContext ctx, Func<short> start, ushort doneId, double timeoutS)
        {
            short r;
            _waiting = true;
            try
            {
                lock (_io)
                {
                    _clearQueue(_serial);
                    r = start();
                }
                if (r != 0) throw new DeviceError(r == 37 ? ErrorCodes.Unavailable : r == 38 ? ErrorCodes.Limit : ErrorCodes.Internal, "Kinesis: " + Err(r));
                var t0 = Clock.MonoSeconds;
                bool sawMotion = false;
                while (true)
                {
                    if (!ctx.Wait(0.05))
                    {
                        lock (_io) _stopProfiled(_serial);
                        WaitStill(3);
                        ctx.Check();
                    }
                    while (true)
                    {
                        bool got = false;
                        ushort type = 0, id = 0;
                        uint data;
                        lock (_io) if (_queueSize(_serial) > 0) got = _nextMsg(_serial, out type, out id, out data);
                        if (!got) break;
                        if (type == 2 && id == doneId) return;
                        if (type == 2 && id == 2) throw new DeviceError(ErrorCodes.Cancelled, "motion stopped (limit switch, front panel or stop)");
                    }
                    uint b = ReadBits();
                    bool moving = (b & (BitCw | BitCcw | BitJogCw | BitJogCcw | BitHoming)) != 0;
                    if (moving) sawMotion = true;
                    if (doneId == 0 && (b & BitHomed) != 0 && !moving && sawMotion) return;
                    if (doneId == 1 && sawMotion && !moving && Clock.MonoSeconds - t0 > 0.3) return;
                    if (doneId == 1 && !sawMotion && Clock.MonoSeconds - t0 > 2) return; // zero-length move
                    if (Clock.MonoSeconds - t0 > timeoutS)
                    {
                        lock (_io) _stopProfiled(_serial);
                        throw new DeviceError(ErrorCodes.Timeout, $"motion did not finish within {timeoutS:0} s");
                    }
                }
            }
            finally
            {
                _waiting = false;
                _posDu = ReadPos();
                _bits = ReadBits();
                EmitAxisNow(IsMoving(_bits)); // normally moving=false, without cmd_id; if still decelerating, the poll reports the stop at once
            }
        }

        private static bool IsMoving(uint bits) { return (bits & (BitCw | BitCcw | BitJogCw | BitJogCcw | BitHoming)) != 0; }

        private void EmitAxisNow(bool moving)
        {
            _lastAxisEmit = Clock.MonoSeconds;
            _lastEmittedMoving = moving;
            EmitAxisState(Snapshot(), moving);
        }

        private void WaitStill(double maxS)
        {
            var end = Clock.MonoSeconds + maxS;
            while (Clock.MonoSeconds < end)
            {
                if ((ReadBits() & (BitCw | BitCcw | BitJogCw | BitJogCcw | BitHoming)) == 0) return;
                Thread.Sleep(50);
            }
        }

        protected override void StopHardware(bool emergency, bool hubOnly)
        {
            if (!_open1) return;
            if (hubOnly && ActiveOperation == null) return;
            lock (_io)
            {
                if (emergency) _stopImmediate(_serial);
                else _stopProfiled(_serial);
            }
        }

        public override void Shutdown()
        {
            try { _poll?.Dispose(); } catch { }
            try { _retry?.Dispose(); } catch { }
            Close();
        }
    }
}
