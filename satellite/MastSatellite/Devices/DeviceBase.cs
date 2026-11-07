using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Threading;
using MastSatellite.Core;

namespace MastSatellite.Devices
{
    /// <summary>A command failure with one of the contract's error codes (§5.2 result.error.code).</summary>
    public sealed class DeviceError : Exception
    {
        public DeviceError(string code, string message) : base(message) { Code = code; }
        public string Code { get; }
    }

    public static class ErrorCodes
    {
        public const string InvalidArgs = "invalid_args";
        public const string NotSupported = "not_supported";
        public const string Unavailable = "unavailable";
        public const string DeviceBusy = "device_busy";
        public const string OperatorHold = "operator_hold";
        public const string Limit = "limit";
        public const string Timeout = "timeout";
        public const string Cancelled = "cancelled";
        public const string LostOnRestart = "lost_on_restart";
        public const string Internal = "internal";
    }

    /// <summary>Buttons a driver offers in the local status window (e.g. restart LightField).</summary>
    public interface ILocalActions
    {
        IReadOnlyList<string> LocalActions { get; }
        void RunLocalAction(string name);
    }

    /// <summary>What devices need from the satellite core.</summary>
    public interface IDeviceHost
    {
        /// <summary>Append an event to the outgoing buffer (non-blocking).</summary>
        void Emit(string deviceId, string kind, object data);

        /// <summary>Ask for a device_state event now (state, hold or error changed).</summary>
        void StateChanged(DeviceBase device);

        string StateDir { get; }

        /// <summary>Queue a file for upload (spe_watcher, simulated saves). Returns false if no uploader.</summary>
        bool OfferFile(FileUploadRequest request);

        /// <summary>
        /// An acquisition that wrote this file: acq_started_ms / acq_finished_ms (satellite clock),
        /// cmd_id, exposure, frames ... The watcher puts it into file_offer.meta, and the hub uses it
        /// as the acquisition time window ("satellite_meta" source).
        /// </summary>
        void NoteFileMeta(string path, Dictionary<string, object> meta);

        /// <summary>The meta noted for this file; null if none.</summary>
        Dictionary<string, object> GetFileMeta(string path);
    }

    public sealed class FileUploadRequest
    {
        public string FullPath;
        public string RelPath;
        public string Kind;
        public long Size;
        public long MtimeMs;
        public string Sha256;
        public Dictionary<string, object> Meta;
        /// <summary>Called on the uploader thread: (ok, stored path or error).</summary>
        public Action<bool, string> Completed;
    }

    /// <summary>
    /// One hub command while it runs: arguments, cancellation (with the contract code that ends
    /// it), deadline. Device code polls Check()/Wait() so stop, cancel, timeout, deadman and
    /// operator hold all end the job promptly.
    /// </summary>
    public sealed class CommandContext
    {
        private readonly CancellationTokenSource _cts = new CancellationTokenSource();
        private readonly object _gate = new object();

        public CommandContext(string id, string deviceId, string action, Dictionary<string, object> args, double timeoutS, bool fromHub = true)
        {
            Id = id;
            DeviceId = deviceId;
            Action = action;
            Args = args ?? new Dictionary<string, object>(StringComparer.Ordinal);
            TimeoutS = timeoutS;
            FromHub = fromHub;
            StartedMs = Clock.UnixMs();
        }

        public string Id { get; }
        public string DeviceId { get; }
        public string Action { get; }
        public Dictionary<string, object> Args { get; }
        public double TimeoutS { get; }
        public bool FromHub { get; }
        public long StartedMs { get; }
        public CancellationToken Token => _cts.Token;
        public bool IsCancelled => _cts.IsCancellationRequested;
        public string CancelCode { get; private set; }
        public string CancelMessage { get; private set; }

        public void Cancel(string code, string message)
        {
            lock (_gate)
            {
                if (CancelCode == null)
                {
                    CancelCode = code;
                    CancelMessage = message;
                }
            }
            try { _cts.Cancel(); } catch (ObjectDisposedException) { }
        }

        /// <summary>Throws the DeviceError that ends this command if it was cancelled.</summary>
        public void Check()
        {
            if (_cts.IsCancellationRequested) throw new DeviceError(CancelCode ?? ErrorCodes.Cancelled, CancelMessage ?? "cancelled");
        }

        /// <summary>Waits; false if cancelled meanwhile.</summary>
        public bool Wait(double seconds)
        {
            if (seconds <= 0) return !_cts.IsCancellationRequested;
            return !_cts.Token.WaitHandle.WaitOne(TimeSpan.FromSeconds(Math.Min(seconds, 3600)));
        }

        /// <summary>Waits, then throws if cancelled.</summary>
        public void Sleep(double seconds)
        {
            if (!Wait(seconds)) Check();
        }

        public bool Has(string key) { return J.Has(Args, key); }

        public double Num(string key)
        {
            var v = J.NumOpt(Args, key);
            if (v == null || double.IsNaN(v.Value) || double.IsInfinity(v.Value))
                throw new DeviceError(ErrorCodes.InvalidArgs, $"'{key}' (number) is required");
            return v.Value;
        }

        public double? NumOpt(string key)
        {
            if (!J.Has(Args, key)) return null;
            var v = J.NumOpt(Args, key);
            if (v == null || double.IsNaN(v.Value) || double.IsInfinity(v.Value))
                throw new DeviceError(ErrorCodes.InvalidArgs, $"'{key}' must be a finite number");
            return v;
        }

        public int Int(string key)
        {
            var v = Num(key);
            if (Math.Abs(v - Math.Round(v)) > 1e-9 || Math.Abs(v) > int.MaxValue)
                throw new DeviceError(ErrorCodes.InvalidArgs, $"'{key}' must be an integer");
            return (int)Math.Round(v);
        }

        public int? IntOpt(string key)
        {
            return J.Has(Args, key) ? Int(key) : (int?)null;
        }

        public string Str(string key, string def = null) { return J.Str(Args, key, def); }

        public string StrRequired(string key)
        {
            var s = J.Str(Args, key);
            if (string.IsNullOrEmpty(s)) throw new DeviceError(ErrorCodes.InvalidArgs, $"'{key}' is required");
            return s;
        }

        public bool Bool(string key, bool def) { return J.Bool(Args, key, def); }
        public Dictionary<string, object> Dict(string key) { return J.Dict(Args, key); }
    }

    /// <summary>
    /// Common device behaviour (契约 §5.3/§5.4): descriptor, state with operator hold, one
    /// exclusive operation at a time (device_busy otherwise), stop/status/safe always admitted,
    /// tracking of hub-initiated motion/acquisition for the deadman switch.
    /// </summary>
    public abstract class DeviceBase
    {
        public const string StateReady = "ready";
        public const string StateBusy = "busy";
        public const string StateOffline = "offline";
        public const string StateError = "error";
        public const string StateNotStarted = "not_started";
        public const string StateOperatorHold = "operator_hold";

        private readonly object _opGate = new object();
        private CommandContext _activeCtx;
        private string _activeOp;
        private bool _activeIsHubMotion;
        private volatile string _baseState = StateNotStarted;
        private volatile string _reason;
        private volatile bool _operatorHold;

        protected DeviceBase(DeviceConfig cfg, IDeviceHost host, string kind)
        {
            Config = cfg;
            Host = host;
            Id = cfg.Id;
            Kind = kind;
            Driver = cfg.Driver;
            Sim = cfg.Sim;
            Model = cfg.Str("model", "");
            Serial = cfg.Str("serial", "");
            // The operator can start a device already taken over (e.g. the CCD is in manual use all day).
            _operatorHold = cfg.Bool("operator_hold", false);
        }

        public DeviceConfig Config { get; }
        protected IDeviceHost Host { get; }
        public string Id { get; }
        public string Kind { get; }
        public string Driver { get; }
        public bool Sim { get; }
        public string Model { get; protected set; }
        public string Serial { get; protected set; }
        public string LastError { get; protected set; }

        /// <summary>Actions this device accepts; must contain status, stop, safe.</summary>
        public abstract IReadOnlyList<string> Caps { get; }

        /// <summary>Actions that need the device started (everything except these is gated on ready/busy).</summary>
        protected virtual IEnumerable<string> AlwaysAdmitted => new[] { "status", "stop", "safe" };

        public bool OperatorHold => _operatorHold;
        public string Reason => _reason;
        public string ActiveOperation { get { lock (_opGate) return _activeOp; } }

        /// <summary>True while a hub-initiated motion or acquisition runs (the deadman watches this).</summary>
        public bool HubActivityActive { get { lock (_opGate) return _activeCtx != null && _activeIsHubMotion; } }

        public string State
        {
            get
            {
                if (_operatorHold) return StateOperatorHold;
                var b = _baseState;
                if (b == StateReady && (ActiveOperation != null || IsBusyExternally)) return StateBusy;
                return b;
            }
        }

        /// <summary>Hardware busy for reasons outside our commands (e.g. a manual LightField acquisition).</summary>
        protected virtual bool IsBusyExternally => false;

        /// <summary>Something is moving / acquiring right now: a command of ours or the operator's own work.</summary>
        public bool IsActive => ActiveOperation != null || IsBusyExternally;

        protected void SetState(string state, string reason = null)
        {
            bool changed = _baseState != state || _reason != reason;
            _baseState = state;
            _reason = reason;
            if (state == StateError && reason != null) LastError = reason;
            if (changed)
            {
                Log.Info($"[{Id}] state {state}{(reason != null ? " (" + reason + ")" : "")}");
                Host.StateChanged(this);
            }
        }

        public virtual Dictionary<string, object> Info => J.Obj();

        public Dictionary<string, object> Descriptor()
        {
            return J.Obj("id", Id, "kind", Kind, "driver", Driver, "model", Model, "serial", Serial,
                         "sim", Sim, "state", State, "caps", Caps.ToList(), "info", Info);
        }

        /// <summary>Open hardware / start background loops. Failures should set state, not throw.</summary>
        public abstract void Start();

        /// <summary>Release hardware. Called once at satellite shutdown.</summary>
        public virtual void Shutdown() { }

        /// <summary>A command on this device never returned: show it as an error until the operator restarts the driver.</summary>
        public void MarkHung(string why) { SetState(StateError, why); }

        /// <summary>Called after every (re)connect so the hub gets fresh context (e.g. the wavelength axis).</summary>
        public virtual void OnHubConnected() { }

        /// <summary>Hub cancel / command timeout: end that command and stop what it started.</summary>
        public void CancelCommand(CommandContext ctx, string code, string message)
        {
            ctx.Cancel(code, message);
            bool active;
            lock (_opGate) active = ReferenceEquals(_activeCtx, ctx);
            if (active)
            {
                try { StopHardware(emergency: false, hubOnly: true); }
                catch (Exception ex) { Log.Error($"[{Id}] stop after cancel failed", ex); }
            }
        }

        /// <summary>Device-specific status fields (cheap; cached values are fine).</summary>
        protected abstract Dictionary<string, object> BuildStatus(bool fresh);

        /// <summary>Fields that change continuously and must not trigger a device_state on their own.</summary>
        protected virtual IEnumerable<string> VolatileStatusKeys => Enumerable.Empty<string>();

        public Dictionary<string, object> Status(bool fresh)
        {
            Dictionary<string, object> s;
            try { s = BuildStatus(fresh) ?? J.Obj(); }
            catch (Exception ex)
            {
                s = J.Obj();
                s["status_error"] = ex.Message;
            }
            s["state"] = State;
            s["operator_hold"] = _operatorHold;
            if (_reason != null) s["reason"] = _reason;
            if (LastError != null) s["last_error"] = LastError;
            var op = ActiveOperation;
            if (op != null) s["active"] = op;
            return s;
        }

        /// <summary>Key used to decide whether device_state changed (status minus volatile fields).</summary>
        public string StateChangeKey(Dictionary<string, object> status)
        {
            var copy = new SortedDictionary<string, object>(StringComparer.Ordinal);
            var vol = new HashSet<string>(VolatileStatusKeys, StringComparer.Ordinal);
            foreach (var kv in status) if (!vol.Contains(kv.Key)) copy[kv.Key] = kv.Value;
            return Json.Serialize(copy);
        }

        /// <summary>
        /// Admitted under operator hold: stop, safe, abort (both stop the hardware, nothing more) and the
        /// read-only actions - the same set as the hub's Python reference satellite,
        /// mast.satellite.sim.devices.HOLD_ALLOWED.
        /// </summary>
        public static readonly HashSet<string> HoldAllowed = new HashSet<string>(StringComparer.Ordinal)
        {
            "stop", "safe", "abort", "status", "wavelength_axis", "get_settings",
        };

        /// <summary>Admission check before a hub command is accepted; null means admitted.</summary>
        public DeviceError Admit(string action)
        {
            if (!Caps.Contains(action)) return new DeviceError(ErrorCodes.NotSupported, $"device {Id} ({Driver}) does not support '{action}'");
            if (_operatorHold && !HoldAllowed.Contains(action))
                return new DeviceError(ErrorCodes.OperatorHold, $"device {Id} is under operator hold at the satellite (only stop, safe, abort and read-only actions pass)");
            if (!AlwaysAdmitted.Contains(action))
            {
                var b = _baseState;
                if (b != StateReady)
                    return new DeviceError(ErrorCodes.Unavailable, $"device {Id} is {b}{(_reason != null ? ": " + _reason : "")}");
            }
            return null;
        }

        public object Execute(CommandContext ctx)
        {
            switch (ctx.Action)
            {
                case "status":
                    return Status(fresh: true);
                case "stop":
                {
                    DoStop(ctx);
                    var s = Status(fresh: false);
                    s["stopped"] = true;
                    return s;
                }
                case "safe":
                {
                    // The hub's normal shutdown sends safe{reason} to every device (not estop): stop what the
                    // hub started, leave the operator's own work (a manual LightField acquisition) running.
                    bool had = HubActivityActive;
                    var why = ctx.Str("reason");
                    Safe(string.IsNullOrEmpty(why) ? "safe requested by hub" : "safe: " + why);
                    return J.Obj("stopped", had ? new List<object> { Id } : new List<object>());
                }
            }
            return ExecuteAction(ctx);
        }

        protected abstract object ExecuteAction(CommandContext ctx);

        /// <summary>Hub "stop": end the running command and stop the hardware (manual activity included).</summary>
        protected virtual void DoStop(CommandContext ctx)
        {
            CancelActive(ErrorCodes.Cancelled, "stopped by hub 'stop'");
            StopHardware(emergency: false, hubOnly: false);
        }

        /// <summary>Deadman / operator hold: stop only what the hub started.</summary>
        public void Safe(string reason)
        {
            bool hadHub;
            lock (_opGate) hadHub = _activeCtx != null && _activeCtx.FromHub;
            if (hadHub) CancelActive(ErrorCodes.Cancelled, reason);
            try { StopHardware(emergency: false, hubOnly: true); }
            catch (Exception ex) { Log.Error($"[{Id}] safe action failed", ex); throw; }
        }

        public void EmergencyStop(string reason)
        {
            CancelActive(ErrorCodes.Cancelled, "estop: " + reason);
            StopHardware(emergency: true, hubOnly: false);
        }

        /// <summary>
        /// Stop motion/acquisition. hubOnly: leave activity the operator started by hand alone.
        /// Must be fast and must not wait for the exclusive operation lock.
        /// </summary>
        protected abstract void StopHardware(bool emergency, bool hubOnly);

        public void SetOperatorHold(bool on)
        {
            if (_operatorHold == on) return;
            _operatorHold = on;
            Log.Warn($"[{Id}] operator hold {(on ? "ON" : "OFF")}");
            if (on)
            {
                bool hadHub;
                lock (_opGate) hadHub = _activeCtx != null && _activeCtx.FromHub;
                if (hadHub)
                {
                    CancelActive(ErrorCodes.OperatorHold, "operator took over the device at the satellite");
                    try { StopHardware(emergency: false, hubOnly: true); } catch (Exception ex) { Log.Error($"[{Id}] stop on operator hold failed", ex); }
                }
            }
            Host.StateChanged(this);
        }

        protected void CancelActive(string code, string message)
        {
            CommandContext c;
            lock (_opGate) c = _activeCtx;
            c?.Cancel(code, message);
        }

        /// <summary>
        /// Marks an exclusive operation; throws device_busy if one is already running.
        /// hubMotion = motion or acquisition (what the deadman switch must stop).
        /// </summary>
        protected IDisposable BeginExclusive(CommandContext ctx, string op, bool hubMotion)
        {
            lock (_opGate)
            {
                if (_activeOp != null)
                    throw new DeviceError(ErrorCodes.DeviceBusy, $"device {Id} is busy ({_activeOp})");
                if (IsBusyExternally)
                    throw new DeviceError(ErrorCodes.DeviceBusy, $"device {Id} is busy (operator activity)");
                _activeOp = op;
                _activeCtx = ctx;
                _activeIsHubMotion = hubMotion && ctx.FromHub;
            }
            Host.StateChanged(this);
            return new Releaser(this);
        }

        private sealed class Releaser : IDisposable
        {
            private DeviceBase _d;
            public Releaser(DeviceBase d) { _d = d; }
            public void Dispose()
            {
                var d = Interlocked.Exchange(ref _d, null);
                if (d == null) return;
                lock (d._opGate)
                {
                    d._activeOp = null;
                    d._activeCtx = null;
                    d._activeIsHubMotion = false;
                }
                d.Host.StateChanged(d);
            }
        }

        protected void Emit(string kind, object data) { Host.Emit(Id, kind, data); }

        /// <summary>
        /// axis_state (契约 §5.3 item 11): while the device moves for a hub motion command, the data carries that
        /// command's id (cmd_id) and the hub shows the command as running. Idle reports, the moving=false report
        /// sent as soon as a motion ends, and motion nobody here commanded carry none.
        /// </summary>
        protected void EmitAxisState(Dictionary<string, object> data, bool moving)
        {
            if (moving)
            {
                string cmd;
                lock (_opGate) cmd = _activeCtx != null && _activeIsHubMotion ? _activeCtx.Id : null;
                if (cmd != null) data["cmd_id"] = cmd;
            }
            Emit("axis_state", data);
        }

        protected static string F(double v) { return v.ToString("0.###", CultureInfo.InvariantCulture); }
    }
}
