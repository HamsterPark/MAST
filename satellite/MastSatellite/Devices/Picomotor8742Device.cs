using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Net.Sockets;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using MastSatellite.Core;

namespace MastSatellite.Devices
{
    /// <summary>ASCII line transport to a Picomotor 8742 (one command, optional one-line reply).</summary>
    public interface IPicoTransport : IDisposable
    {
        string Name { get; }
        void Open();
        void Write(string cmd);
        string Query(string cmd);
    }

    /// <summary>
    /// 8742 operations a transport documents as typed methods (CmdLib8742's public API, "Picomotor
    /// Samples.pdf" section 5). set_velocity / set_accel / abort use only these: their ASCII forms
    /// (VA, AC, AB) are not yet checked against the 8742 user's manual. address 0 = the controller itself.
    /// </summary>
    public interface IPicoTypedCommands
    {
        int GetVelocity(int address, int motor);
        void SetVelocity(int address, int motor, int stepsPerSec);
        int GetAcceleration(int address, int motor);
        void SetAcceleration(int address, int motor, int stepsPerSec2);
        void AbortMotion(int address);
    }

    /// <summary>
    /// Newport USB Driver native API (usbdll.dll, "Newport USB Driver API User's Manual", installed
    /// in C:\Program Files\Newport\Newport USB Driver\Docs): newp_usb_open_devices(productId,
    /// useUsbAddress=false, &amp;n), newp_usb_get_device_info, newp_usb_send_ascii (appends CR),
    /// newp_usb_get_ascii_by_DeviceID (strips the CR terminator), newp_usb_uninit_system.
    /// </summary>
    public sealed class NewportUsbTransport : IPicoTransport
    {
        [UnmanagedFunctionPointer(CallingConvention.Cdecl, CharSet = CharSet.Ansi)] private delegate int DOpen(int productId, [MarshalAs(UnmanagedType.I1)] bool useUsbAddress, out int n);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl, CharSet = CharSet.Ansi)] private delegate int DInfo(StringBuilder buf);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl, CharSet = CharSet.Ansi)] private delegate int DSend(int devId, [MarshalAs(UnmanagedType.LPStr)] string cmd, uint len);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)] private delegate int DGet(int devId, [Out] byte[] buf, uint len, out uint read);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)] private delegate void DUninit();

        private readonly string _dll;
        private readonly int _productId;
        private readonly string _serialFilter;
        private IntPtr _module;
        private DOpen _openDevices;
        private DInfo _info;
        private DSend _send;
        private DGet _get;
        private DUninit _uninit;
        private int _devId = -1;

        public NewportUsbTransport(string dll, int productId, string serialFilter)
        {
            _dll = dll;
            _productId = productId;
            _serialFilter = serialFilter;
        }

        public string Name => "usb (usbdll.dll)";
        public string DeviceDescription { get; private set; }

        public void Open()
        {
            if (_module == IntPtr.Zero)
            {
                if (!File.Exists(_dll)) throw new FileNotFoundException("Newport usbdll.dll not found: " + _dll);
                Native.SetDllDirectory(Path.GetDirectoryName(_dll));
                _module = Native.LoadVendorDll(_dll);
                _openDevices = Native.GetFunction<DOpen>(_module, "newp_usb_open_devices");
                _info = Native.GetFunction<DInfo>(_module, "newp_usb_get_device_info");
                _send = Native.GetFunction<DSend>(_module, "newp_usb_send_ascii");
                _get = Native.TryGetFunction<DGet>(_module, "newp_usb_get_ascii_by_DeviceID") ?? Native.GetFunction<DGet>(_module, "newp_usb_get_ascii");
                _uninit = Native.GetFunction<DUninit>(_module, "newp_usb_uninit_system");
            }
            int rc = _openDevices(_productId, false, out var n);
            if (rc != 0 || n <= 0) throw new IOException($"newp_usb_open_devices(0x{_productId:X4}) failed (rc={rc}, devices={n}); is PicomotorApp still open?");
            var sb = new StringBuilder(4096);
            rc = _info(sb);
            if (rc != 0) throw new IOException("newp_usb_get_device_info failed: " + rc);
            // "<DevID1>,<Description1>;<DevID2>,<Description2>;"
            foreach (var entry in sb.ToString().Split(new[] { ';' }, StringSplitOptions.RemoveEmptyEntries))
            {
                var comma = entry.IndexOf(',');
                if (comma < 0) continue;
                if (!int.TryParse(entry.Substring(0, comma).Trim(), NumberStyles.Integer, CultureInfo.InvariantCulture, out var id)) continue;
                var desc = entry.Substring(comma + 1).Trim();
                if (desc.IndexOf("8742", StringComparison.Ordinal) < 0) continue;
                if (!string.IsNullOrEmpty(_serialFilter) && desc.IndexOf(_serialFilter, StringComparison.Ordinal) < 0) continue;
                _devId = id;
                DeviceDescription = desc;
                return;
            }
            throw new IOException("no 8742 in the Newport USB device list: " + sb);
        }

        public void Write(string cmd)
        {
            int rc = _send(_devId, cmd, (uint)cmd.Length);
            if (rc != 0) throw new IOException($"newp_usb_send_ascii('{cmd}') failed: {rc}");
        }

        public string Query(string cmd)
        {
            Write(cmd);
            var buf = new byte[256];
            for (int attempt = 0; ; attempt++)
            {
                int rc = _get(_devId, buf, (uint)buf.Length, out var read);
                if (rc != 0) throw new IOException($"newp_usb_get_ascii('{cmd}') failed: {rc}");
                var reply = DecodeReply(buf, read);
                if (reply.Length > 0 || attempt >= 2) return reply;
                Thread.Sleep(50); // the reply was not there yet
            }
        }

        /// <summary>
        /// The driver writes <paramref name="read"/> bytes and does not terminate the buffer, so
        /// anything past them is a previous reply or garbage (e.g. "0\r\nNO ERROR DETECTED…"
        /// for a TP? that answered "0"). Decodes only those bytes and returns the first line.
        /// </summary>
        public static string DecodeReply(byte[] buf, uint read)
        {
            int n = (int)Math.Min(read, (uint)buf.Length);
            foreach (var line in Encoding.ASCII.GetString(buf, 0, n).Split(new[] { '\r', '\n' }, StringSplitOptions.RemoveEmptyEntries))
            {
                var t = line.Trim(' ', '\0');
                if (t.Length > 0) return t;
            }
            return "";
        }

        public void Dispose()
        {
            try { if (_uninit != null) _uninit(); } catch { }
        }
    }

    /// <summary>
    /// New Focus CmdLib8742.dll (.NET, public API listed in "Picomotor Samples.pdf" section 5):
    /// CmdLib8742(bool logging, int msecDelayForDiscovery, ref string deviceKey), Open(key),
    /// Query(key, cmd, ref string), Write(key, cmd), Close(key), Shutdown(), and the typed
    /// Get/SetVelocity(key[, address], motor, [ref] stepsPerSec), Get/SetAcceleration(...), AbortMotion(key[, address]).
    /// </summary>
    public sealed class CmdLibTransport : IPicoTransport, IPicoTypedCommands
    {
        private readonly string _path;
        private readonly string _wantedKey;
        private object _lib;
        private Type _t;
        private string _key;

        public CmdLibTransport(string path, string deviceKey)
        {
            _path = path;
            _wantedKey = deviceKey;
        }

        public string Name => "cmdlib (CmdLib8742.dll)";

        public void Open()
        {
            if (!File.Exists(_path)) throw new FileNotFoundException("CmdLib8742.dll not found: " + _path);
            var dir = Path.GetDirectoryName(_path);
            Native.SetDllDirectory(@"C:\Program Files\Newport\Newport USB Driver\Bin");
            AppDomain.CurrentDomain.AssemblyResolve += (s, e) =>
            {
                var f = Path.Combine(dir, new AssemblyName(e.Name).Name + ".dll");
                return File.Exists(f) ? Assembly.LoadFrom(f) : null;
            };
            var asm = Assembly.LoadFrom(_path);
            _t = asm.GetType("NewFocus.Picomotor.CmdLib8742", true);
            var ctor = _t.GetConstructor(new[] { typeof(bool), typeof(int), typeof(string).MakeByRefType() });
            var args = new object[] { false, 5000, null };
            _lib = ctor.Invoke(args);
            _key = _wantedKey ?? args[2] as string;
            if (string.IsNullOrEmpty(_key)) throw new IOException("CmdLib8742 discovered no Picomotor controller");
            if (!(bool)LateBound.Call(_lib, _t, "Open", _key)) throw new IOException("CmdLib8742.Open(" + _key + ") failed");
        }

        public void Write(string cmd)
        {
            if (!(bool)LateBound.Call(_lib, _t, "Write", _key, cmd)) throw new IOException($"CmdLib8742.Write('{cmd}') failed");
        }

        public string Query(string cmd)
        {
            var m = _t.GetMethod("Query", new[] { typeof(string), typeof(string), typeof(string).MakeByRefType() });
            var args = new object[] { _key, cmd, "" };
            bool ok;
            try { ok = (bool)m.Invoke(_lib, args); }
            catch (TargetInvocationException e) when (e.InnerException != null) { throw e.InnerException; }
            if (!ok) throw new IOException($"CmdLib8742.Query('{cmd}') failed");
            return (args[2] as string ?? "").Trim();
        }

        private void Invoke(string name, Type[] sig, object[] args)
        {
            var m = _t.GetMethod(name, sig) ?? throw new MissingMethodException("CmdLib8742", name);
            bool ok;
            try { ok = (bool)m.Invoke(_lib, args); }
            catch (TargetInvocationException e) when (e.InnerException != null) { throw e.InnerException; }
            if (!ok) throw new IOException($"CmdLib8742.{name} failed");
        }

        private int GetInt(string name, int address, int motor)
        {
            var r = typeof(int).MakeByRefType();
            var args = address > 0 ? new object[] { _key, address, motor, 0 } : new object[] { _key, motor, 0 };
            Invoke(name, address > 0 ? new[] { typeof(string), typeof(int), typeof(int), r } : new[] { typeof(string), typeof(int), r }, args);
            return (int)args[args.Length - 1];
        }

        private void SetInt(string name, int address, int motor, int value)
        {
            var args = address > 0 ? new object[] { _key, address, motor, value } : new object[] { _key, motor, value };
            Invoke(name, address > 0 ? new[] { typeof(string), typeof(int), typeof(int), typeof(int) } : new[] { typeof(string), typeof(int), typeof(int) }, args);
        }

        public int GetVelocity(int address, int motor) { return GetInt("GetVelocity", address, motor); }
        public void SetVelocity(int address, int motor, int stepsPerSec) { SetInt("SetVelocity", address, motor, stepsPerSec); }
        public int GetAcceleration(int address, int motor) { return GetInt("GetAcceleration", address, motor); }
        public void SetAcceleration(int address, int motor, int stepsPerSec2) { SetInt("SetAcceleration", address, motor, stepsPerSec2); }

        public void AbortMotion(int address)
        {
            if (address > 0) Invoke("AbortMotion", new[] { typeof(string), typeof(int) }, new object[] { _key, address });
            else Invoke("AbortMotion", new[] { typeof(string) }, new object[] { _key });
        }

        public void Dispose()
        {
            try { if (_lib != null && _key != null) LateBound.Call(_lib, _t, "Close", _key); } catch { }
            try { if (_lib != null) LateBound.Call(_lib, _t, "Shutdown"); } catch { }
        }
    }

    /// <summary>8742 Ethernet: telnet server on TCP 23, commands terminated by CR, replies end with CR LF.</summary>
    public sealed class TcpPicoTransport : IPicoTransport
    {
        private readonly string _host;
        private readonly int _port;
        private readonly int _timeoutMs;
        private TcpClient _tcp;
        private NetworkStream _s;
        private readonly StringBuilder _rx = new StringBuilder();

        public TcpPicoTransport(string host, int port, int timeoutMs)
        {
            _host = host;
            _port = port;
            _timeoutMs = timeoutMs;
        }

        public string Name => $"tcp {_host}:{_port}";

        public void Open()
        {
            Dispose();
            _tcp = new TcpClient { NoDelay = true, ReceiveTimeout = _timeoutMs, SendTimeout = _timeoutMs };
            var ar = _tcp.BeginConnect(_host, _port, null, null);
            if (!ar.AsyncWaitHandle.WaitOne(_timeoutMs)) throw new IOException($"connect to {_host}:{_port} timed out");
            _tcp.EndConnect(ar);
            _s = _tcp.GetStream();
            Thread.Sleep(200);
            Drain();
        }

        private void Drain()
        {
            while (_tcp.Available > 0)
            {
                var b = new byte[_tcp.Available];
                _s.Read(b, 0, b.Length);
            }
            _rx.Clear();
        }

        public void Write(string cmd)
        {
            var b = Encoding.ASCII.GetBytes(cmd + "\r");
            _s.Write(b, 0, b.Length);
        }

        public string Query(string cmd)
        {
            Drain();
            Write(cmd);
            var end = Environment.TickCount + _timeoutMs;
            var buf = new byte[256];
            while (Environment.TickCount - end < 0)
            {
                int n = _s.Read(buf, 0, buf.Length);
                if (n <= 0) throw new IOException("connection closed");
                for (int i = 0; i < n; i++)
                {
                    byte c = buf[i];
                    if (c == 0xFF) { i += 2; continue; } // telnet IAC negotiation: skip command + option
                    if (c == '\n')
                    {
                        var line = _rx.ToString().Trim('\r', ' ');
                        _rx.Clear();
                        return line;
                    }
                    _rx.Append((char)c);
                }
            }
            throw new IOException($"no reply to '{cmd}' within {_timeoutMs} ms");
        }

        public void Dispose()
        {
            try { _s?.Dispose(); } catch { }
            try { _tcp?.Close(); } catch { }
            _s = null;
            _tcp = null;
        }
    }

    /// <summary>
    /// New Focus 8742 open-loop Picomotor controller. Uses the NMCL ASCII commands of the 8742
    /// user's manual ({axis}PR{n} relative move, {axis}MD? motion done, {axis}TP? step counter,
    /// {axis}ST stop, AB abort, {axis}DH define home, *IDN?, TB? error) over a swappable
    /// transport (USB native API, CmdLib8742 .NET, TCP 23). Open loop: "steps" is the satellite's
    /// running count of executed steps (from the controller counter before/after each jog),
    /// persisted in the state folder - not a position measurement. move_abs goes to a count by the
    /// same relative move; set_velocity / set_accel / abort exist only with the CmdLib8742 transport
    /// (its documented typed methods) until the ASCII VA / AC / AB are checked against the manual.
    /// </summary>
    public sealed class Picomotor8742Device : DeviceBase, ILocalActions
    {
        private static readonly string[] BaseCaps = { "status", "stop", "safe", "jog", "set_zero", "move_abs" };
        private static readonly string[] TypedCaps = { "set_velocity", "set_accel", "abort" };
        private readonly string[] _caps;
        private readonly object _io = new object();
        private readonly object _gate = new object();
        private readonly int _axes;
        private readonly int _maxSteps;
        private readonly int _maxVel;
        private readonly int _maxAcc;
        private readonly double _rate;
        private readonly int _address;
        private readonly string _prefix;
        private readonly string _countFile;
        private readonly long[] _steps;
        private readonly bool[] _moving;
        private readonly int[] _controller;
        private readonly int?[] _vel;
        private readonly int?[] _acc;
        private IPicoTransport _t;
        private volatile bool _open;
        private string _identity;
        private long _countSinceMs;
        private Timer _retry;
        private Timer _idle;

        public Picomotor8742Device(DeviceConfig cfg, IDeviceHost host) : base(cfg, host, "openloop_stepper")
        {
            Model = "New Focus 8742";
            _axes = Math.Max(1, Math.Min(4, cfg.Int("axes", 4)));
            _maxSteps = cfg.Int("max_steps_per_cmd", 2000);
            _maxVel = cfg.Int("max_velocity_steps_s", 2000);
            _maxAcc = cfg.Int("max_accel_steps_s2", 200000);
            _rate = cfg.Num("steps_per_s", 2000);
            _address = cfg.Int("address", 0);
            _prefix = _address > 0 ? _address.ToString(CultureInfo.InvariantCulture) + ">" : "";
            _countFile = Path.Combine(host.StateDir, "pico-" + cfg.Id + "-steps.json");
            _steps = new long[_axes + 1];
            _moving = new bool[_axes + 1];
            _controller = new int[_axes + 1];
            _vel = new int?[_axes + 1];
            _acc = new int?[_axes + 1];
            // caps say what this transport can do from documentation (the hub builds panel buttons from them)
            _caps = cfg.Str("transport", "usb") == "cmdlib" ? BaseCaps.Concat(TypedCaps).ToArray() : BaseCaps;
        }

        public override IReadOnlyList<string> Caps => _caps;
        protected override IEnumerable<string> AlwaysAdmitted => new[] { "status", "stop", "safe", "abort" };
        public IReadOnlyList<string> LocalActions => new[] { "重新连接 Picomotor" };
        protected override IEnumerable<string> VolatileStatusKeys => new[] { "axes" };
        public override Dictionary<string, object> Info => J.Obj("transport", _t?.Name ?? Config.Str("transport", "usb"), "identity", _identity,
                                                                "axes", _axes, "max_steps_per_cmd", _maxSteps, "open_loop", true);

        private IPicoTransport MakeTransport()
        {
            switch (Config.Str("transport", "usb"))
            {
                case "tcp":
                    return new TcpPicoTransport(Config.Str("host") ?? throw new ArgumentException("transport tcp needs 'host'"), Config.Int("port", 23), Config.Int("timeout_ms", 2000));
                case "cmdlib":
                    return new CmdLibTransport(Config.Path("cmdlib_path", @"C:\Program Files\New Focus\New Focus Picomotor Application\Bin\CmdLib8742.dll"), Config.Str("device_key"));
                case "usb":
                    return new NewportUsbTransport(Config.Path("usb_dll", @"C:\Program Files\Newport\Newport USB Driver\Bin\usbdll.dll"),
                                                   Config.Int("usb_product_id", 0x4000), Config.Str("serial"));
                default:
                    throw new ArgumentException("transport must be usb, cmdlib or tcp");
            }
        }

        public override void Start()
        {
            LoadCounts();
            Open();
            _retry = new Timer(_ => { if (!_open) Open(); }, null, TimeSpan.FromSeconds(30), TimeSpan.FromSeconds(30));
            _idle = new Timer(_ => { if (_open && !_moving.Any(m => m) && ActiveOperation == null) PollIdle(); }, null, 1000, 1000);
        }

        public void RunLocalAction(string name)
        {
            ThreadPool.QueueUserWorkItem(_ => { CloseTransport("reconnect requested"); Open(); });
        }

        private void Open()
        {
            lock (_io)
            {
                if (_open) return;
                try
                {
                    _t?.Dispose();
                    _t = MakeTransport();
                    _t.Open();
                    _identity = _t.Query("*IDN?");
                    Query("TB?"); // reading the error buffer clears a stale error
                    for (int a = 1; a <= _axes; a++) _controller[a] = ParseInt(Query(a + "TP?"));
                    if (_t is IPicoTypedCommands typed)
                    {
                        for (int a = 1; a <= _axes; a++)
                        {
                            try { _vel[a] = typed.GetVelocity(_address, a); _acc[a] = typed.GetAcceleration(_address, a); }
                            catch (Exception ex) { Log.Debug($"[{Id}] axis {a} velocity/acceleration not readable: {ex.Message}"); }
                        }
                    }
                    _open = true;
                }
                catch (Exception ex)
                {
                    SetState(StateOffline, "Picomotor not reachable via " + (_t?.Name ?? Config.Str("transport", "usb")) + ": " + ex.Message);
                    try { _t?.Dispose(); } catch { }
                    _t = null;
                    return;
                }
            }
            SetState(StateReady);
            Log.Info($"[{Id}] Picomotor open: {_identity} via {_t.Name}");
        }

        private void CloseTransport(string why)
        {
            lock (_io)
            {
                _open = false;
                try { _t?.Dispose(); } catch { }
                _t = null;
            }
            SetState(StateOffline, why);
        }

        private string Query(string cmd) { lock (_io) return _t.Query(_prefix + cmd); }
        private void Write(string cmd) { lock (_io) _t.Write(_prefix + cmd); }

        private static int ParseInt(string s)
        {
            if (int.TryParse((s ?? "").Trim(), NumberStyles.Integer, CultureInfo.InvariantCulture, out var v)) return v;
            throw new IOException("unexpected reply: '" + Printable(s) + "'");
        }

        /// <summary>Reply text safe for a state reason and the log: control bytes escaped, length capped.</summary>
        public static string Printable(string s)
        {
            if (string.IsNullOrEmpty(s)) return "";
            var sb = new StringBuilder();
            foreach (char c in s)
            {
                if (sb.Length >= 60) { sb.Append('…'); break; }
                if (c >= 0x20 && c < 0x7F) sb.Append(c);
                else sb.Append("\\x").Append(((int)c & 0xFF).ToString("X2", CultureInfo.InvariantCulture));
            }
            return sb.ToString();
        }

        private void LoadCounts()
        {
            try
            {
                if (!File.Exists(_countFile)) { _countSinceMs = Clock.UnixMs(); return; }
                var d = Json.ParseObject(File.ReadAllText(_countFile));
                var axes = J.Dict(d, "axes");
                for (int a = 1; a <= _axes; a++) _steps[a] = J.Long(axes, a.ToString(CultureInfo.InvariantCulture), 0);
                _countSinceMs = J.Long(d, "since_ms", Clock.UnixMs());
            }
            catch (Exception ex) { Log.Warn($"[{Id}] step counter file unreadable: {ex.Message}"); }
        }

        private void SaveCounts()
        {
            try
            {
                var axes = J.Obj();
                lock (_gate) for (int a = 1; a <= _axes; a++) axes[a.ToString(CultureInfo.InvariantCulture)] = _steps[a];
                File.WriteAllText(_countFile, Json.Serialize(J.Obj("axes", axes, "since_ms", _countSinceMs, "saved_ms", Clock.UnixMs())));
            }
            catch (Exception ex) { Log.Warn($"[{Id}] cannot save step counter: {ex.Message}"); }
        }

        private Dictionary<string, object> Snapshot()
        {
            var axes = J.Obj();
            lock (_gate)
                for (int a = 1; a <= _axes; a++)
                {
                    var ax = J.Obj("steps", _steps[a], "moving", _moving[a], "controller_steps", _controller[a]);
                    if (_vel[a] != null) ax["velocity_steps_s"] = _vel[a].Value;
                    if (_acc[a] != null) ax["accel_steps_s2"] = _acc[a].Value;
                    axes[a.ToString(CultureInfo.InvariantCulture)] = ax;
                }
            return J.Obj("axes", axes, "counting_since_ms", _countSinceMs, "open", _open);
        }

        private void PollIdle()
        {
            try
            {
                for (int a = 1; a <= _axes; a++)
                {
                    int tp = ParseInt(Query(a + "TP?"));
                    lock (_gate)
                    {
                        if (tp != _controller[a])
                        {
                            // The counter moved while we did nothing: someone used the hand pad / PicomotorApp.
                            _steps[a] += tp - _controller[a];
                            _controller[a] = tp;
                            Emit("manual_activity", J.Obj("what", "moved_without_hub_command", "axis", a, "controller_steps", tp));
                        }
                    }
                }
                EmitAxisState(Snapshot(), moving: false);
            }
            catch (Exception ex)
            {
                CloseTransport("lost contact: " + ex.Message);
            }
        }

        protected override Dictionary<string, object> BuildStatus(bool fresh) { return Snapshot(); }

        private int Axis(CommandContext ctx)
        {
            int a = ctx.Int("axis");
            if (a < 1 || a > _axes) throw new DeviceError(ErrorCodes.InvalidArgs, $"axis must be 1..{_axes}");
            return a;
        }

        protected override object ExecuteAction(CommandContext ctx)
        {
            if (!_open) throw new DeviceError(ErrorCodes.Unavailable, "Picomotor not connected");
            switch (ctx.Action)
            {
                case "jog":
                {
                    int a = Axis(ctx);
                    int steps = ctx.Int("steps");
                    if (Math.Abs(steps) > _maxSteps) throw new DeviceError(ErrorCodes.Limit, $"|steps| {Math.Abs(steps)} exceeds max_steps_per_cmd {_maxSteps}");
                    if (steps == 0) return Snapshot();
                    using (BeginExclusive(ctx, "jog", hubMotion: true)) Jog(ctx, a, steps);
                    return Snapshot();
                }
                case "set_zero":
                {
                    int a = Axis(ctx);
                    using (BeginExclusive(ctx, "set_zero", hubMotion: false))
                    {
                        Write(a + "DH");
                        int tp = ParseInt(Query(a + "TP?"));
                        lock (_gate) { _controller[a] = tp; _steps[a] = 0; }
                        SaveCounts();
                    }
                    EmitAxisState(Snapshot(), moving: false);
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
                    var typed = Typed();
                    using (BeginExclusive(ctx, ctx.Action, hubMotion: false))
                    {
                        int back;
                        try
                        {
                            lock (_io)
                            {
                                if (vel) { typed.SetVelocity(_address, a, v); back = typed.GetVelocity(_address, a); }
                                else { typed.SetAcceleration(_address, a, v); back = typed.GetAcceleration(_address, a); }
                            }
                        }
                        catch (IOException ex) { throw new DeviceError(ErrorCodes.Unavailable, "Picomotor: " + ex.Message); }
                        lock (_gate) { if (vel) _vel[a] = back; else _acc[a] = back; }
                        if (back != v) throw new DeviceError(ErrorCodes.Internal, $"8742 reports {back} on axis {a} after setting {v} ({ctx.Action})");
                    }
                    EmitAxisState(Snapshot(), moving: false);
                    return Snapshot();
                }
                case "abort":
                {
                    // All axes at once, no deceleration; the running jog / move_abs ends as cancelled.
                    var typed = Typed();
                    CancelActive(ErrorCodes.Cancelled, "aborted by hub (all axes, no deceleration)");
                    try { lock (_io) typed.AbortMotion(_address); }
                    catch (IOException ex) { throw new DeviceError(ErrorCodes.Unavailable, "Picomotor: " + ex.Message); }
                    var end = Clock.MonoSeconds + 2.0;
                    while (Clock.MonoSeconds < end && _moving.Any(m => m)) Thread.Sleep(20);
                    return Snapshot();
                }
            }
            throw new DeviceError(ErrorCodes.NotSupported, ctx.Action);
        }

        private IPicoTypedCommands Typed()
        {
            if (!_open) throw new DeviceError(ErrorCodes.Unavailable, "Picomotor not connected");
            return _t as IPicoTypedCommands
                ?? throw new DeviceError(ErrorCodes.NotSupported, "only the CmdLib8742 transport has documented velocity / acceleration / abort calls (set transport \"cmdlib\")");
        }

        private void Jog(CommandContext ctx, int a, int steps)
        {
            int tp0;
            try { tp0 = ParseInt(Query(a + "TP?")); }
            catch (Exception ex) { CloseTransport(ex.Message); throw new DeviceError(ErrorCodes.Unavailable, "Picomotor: " + ex.Message); }
            lock (_gate) { _controller[a] = tp0; _moving[a] = true; }
            Host.StateChanged(this);
            double timeout = Math.Abs(steps) / Math.Max(1, _rate) * 3 + 5;
            var t0 = Clock.MonoSeconds;
            bool stopped = false;
            try
            {
                Write(a + "PR" + steps.ToString(CultureInfo.InvariantCulture));
                double lastEmit = 0;
                while (true)
                {
                    if (!ctx.Wait(0.05))
                    {
                        Write(a + "ST");
                        stopped = true;
                        WaitDone(a, 2);
                        break;
                    }
                    if (Query(a + "MD?").Trim() == "1") break;
                    if (Clock.MonoSeconds - lastEmit > 0.05)
                    {
                        lastEmit = Clock.MonoSeconds;
                        int tp = ParseInt(Query(a + "TP?"));
                        lock (_gate) { _steps[a] += tp - _controller[a]; _controller[a] = tp; }
                        EmitAxisState(Snapshot(), moving: true);
                    }
                    if (Clock.MonoSeconds - t0 > timeout)
                    {
                        Write(a + "ST");
                        throw new DeviceError(ErrorCodes.Timeout, $"jog of {steps} steps not done after {timeout:0} s");
                    }
                }
            }
            catch (IOException ex)
            {
                CloseTransport("lost contact: " + ex.Message);
                throw new DeviceError(ErrorCodes.Unavailable, "Picomotor: " + ex.Message);
            }
            finally
            {
                try
                {
                    if (_open)
                    {
                        int tp1 = ParseInt(Query(a + "TP?"));
                        lock (_gate) { _steps[a] += tp1 - _controller[a]; _controller[a] = tp1; }
                    }
                }
                catch { }
                lock (_gate) _moving[a] = false;
                SaveCounts();
                EmitAxisState(Snapshot(), moving: false); // the motion is over: at once, without cmd_id
                Host.StateChanged(this);
            }
            var err = SafeError();
            if (err != null && !err.StartsWith("0", StringComparison.Ordinal)) throw new DeviceError(ErrorCodes.Internal, "8742 reports: " + err);
            if (stopped) ctx.Check();
        }

        private string SafeError()
        {
            try { return Query("TB?"); } catch { return null; }
        }

        private void WaitDone(int a, double maxS)
        {
            var end = Clock.MonoSeconds + maxS;
            while (Clock.MonoSeconds < end)
            {
                try { if (Query(a + "MD?").Trim() == "1") return; } catch { return; }
                Thread.Sleep(50);
            }
        }

        protected override void DoStop(CommandContext ctx)
        {
            if (ctx.Has("axis") && _open)
            {
                int a = Axis(ctx);
                try { Write(a + "ST"); } catch (Exception ex) { throw new DeviceError(ErrorCodes.Unavailable, ex.Message); }
                return;
            }
            base.DoStop(ctx);
        }

        protected override void StopHardware(bool emergency, bool hubOnly)
        {
            if (!_open) return;
            if (hubOnly && ActiveOperation == null) return;
            try
            {
                if (emergency && _t is IPicoTypedCommands typed)
                {
                    lock (_io) typed.AbortMotion(_address); // documented CmdLib8742.AbortMotion
                }
                else if (emergency)
                {
                    // AB (abort) is not yet checked against the 8742 manual: send it, and stop each axis as well.
                    try { Write("AB"); } catch (Exception ex) { Log.Error($"[{Id}] AB failed", ex); }
                    for (int a = 1; a <= _axes; a++) Write(a + "ST");
                }
                else for (int a = 1; a <= _axes; a++) Write(a + "ST");
            }
            catch (Exception ex) { Log.Error($"[{Id}] stop failed", ex); }
        }

        public override void Shutdown()
        {
            try { _retry?.Dispose(); } catch { }
            try { _idle?.Dispose(); } catch { }
            SaveCounts();
            CloseTransport("satellite shutdown");
        }
    }
}
