using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using MastSatellite.Core;
using MastSatellite.Devices;

namespace MastSatellite.Protocol
{
    /// <summary>Read-only view of the satellite for the status window.</summary>
    public sealed class SatelliteSnapshot
    {
        public string SatelliteId;
        public string BootId;
        public string HubUrl;
        public LinkState Link;
        public string LinkError;
        public DateTime? ConnectedSinceUtc;
        public double PingAgeS;
        public int Reconnects;
        public string HubBootId;
        public long EventsBuffered;
        public long EventsLastSeq;
        public long EventsAcked;
        public long EventsDropped;
        public int UploadQueue;
        public string UploadCurrent;
        public long UploadBytes;
        public long FilesUploaded;
        public List<DeviceRow> Devices = new List<DeviceRow>();
    }

    public sealed class DeviceRow
    {
        public string Id, Kind, Driver, State, Reason, Active, LastError;
        public bool Sim, OperatorHold;
    }

    /// <summary>
    /// The satellite: devices, command routing with the journal (idempotent request_id),
    /// cancel / cmd_status / estop, the deadman switch and periodic device_state (契约 §5.3).
    /// </summary>
    public sealed class SatelliteCore : IDeviceHost, IDisposable
    {
        public const string SoftwareVersion = "1.0.0";
        private const double StatePeriodS = 5.0;

        private readonly ConcurrentDictionary<string, (DeviceBase dev, CommandContext ctx)> _running =
            new ConcurrentDictionary<string, (DeviceBase, CommandContext)>(StringComparer.Ordinal);
        private readonly ConcurrentDictionary<string, bool> _dirty = new ConcurrentDictionary<string, bool>(StringComparer.Ordinal);
        private readonly Dictionary<string, string> _lastStateKey = new Dictionary<string, string>(StringComparer.Ordinal);
        private readonly Dictionary<string, double> _lastStateMono = new Dictionary<string, double>(StringComparer.Ordinal);
        private readonly AutoResetEvent _monitorWake = new AutoResetEvent(false);
        private readonly TokenBucket _logBudget = new TokenBucket(0.5, 5);
        private readonly string _token;
        private readonly Func<DeviceConfig, IDeviceHost, DeviceBase> _factory;
        private Thread _monitor;
        private volatile bool _stopping;
        private double _deadmanS;

        public SatelliteCore(SatelliteConfig cfg, string token, Func<DeviceConfig, IDeviceHost, DeviceBase> factory = null)
        {
            Config = cfg;
            _token = token;
            _factory = factory ?? DeviceFactory.Create;
            _deadmanS = cfg.Hub.DeadmanS;
            BootId = Guid.NewGuid().ToString("N");
            Events = new EventBuffer(cfg.EventBufferMaxEvents, cfg.EventBufferMaxBytes);
            Journal = new Journal(Path.Combine(cfg.StateDir, "journal.jsonl"), BootId, cfg.JournalMaxEntries);
        }

        public SatelliteConfig Config { get; }
        public string BootId { get; }
        public EventBuffer Events { get; }
        public Journal Journal { get; }
        public HubLink Link { get; private set; }
        public FileUploader Uploader { get; private set; }
        public List<DeviceBase> Devices { get; } = new List<DeviceBase>();
        public string StateDir => Config.StateDir;

        public void Start()
        {
            Directory.CreateDirectory(StateDir);
            int lost = Journal.Open();
            Log.Info($"satellite {Config.SatelliteId} boot {BootId} ({SessionInfo.Describe()}); journal {Journal.Count} entries, {lost} marked lost_on_restart");

            foreach (var dc in Config.Devices)
            {
                try { Devices.Add(dc.Enabled ? _factory(dc, this) : new DisabledDevice(dc, this)); }
                catch (Exception ex)
                {
                    Log.Error($"device {dc.Id} ({dc.Driver}) cannot be created", ex);
                    Devices.Add(new DisabledDevice(dc, this, "driver error: " + ex.Message));
                }
            }

            var uri = new Uri(Config.Hub.Url);
            Link = new HubLink(uri, Config.SatelliteId, _token, Config.Hub, Events, BuildHello);
            Link.Message += OnMessage;
            Link.Connected += OnConnected;
            Link.Disconnected += OnDisconnected;
            Uploader = new FileUploader(Link, Config.Upload.MaxBytesPerS, Config.Upload.ChunkBytes);
            Uploader.Start();
            Log.Line += ForwardLog;

            // Devices start in the background: LightField alone can take tens of seconds, and the
            // hub should see the satellite (with not_started devices) meanwhile.
            foreach (var d in Devices)
            {
                var dev = d;
                Task.Run(() =>
                {
                    try { dev.Start(); }
                    catch (Exception ex)
                    {
                        Log.Error($"[{dev.Id}] start failed", ex);
                    }
                    StateChanged(dev);
                });
            }
            Link.Start();
            _monitor = new Thread(MonitorLoop) { IsBackground = true, Name = "satellite-monitor" };
            _monitor.Start();
        }

        private Dictionary<string, object> BuildHello()
        {
            return J.Obj("t", "hello", "proto", 1, "satellite_id", Config.SatelliteId, "boot_id", BootId,
                         "sw", "MastSatellite/" + SoftwareVersion, "host", Environment.MachineName, "time_ms", Clock.UnixMs(),
                         "devices", Devices.Select(d => (object)d.Descriptor()).ToList(), "evt_seq", Events.LastSeq);
        }

        private void OnConnected(WelcomeInfo w)
        {
            if (w.DeadmanS > 0) _deadmanS = w.DeadmanS;
            Uploader.ApplyHubRate(w.MaxUploadBytesPerS);
            Uploader.OnConnected();
            foreach (var d in Devices)
            {
                try { d.OnHubConnected(); } catch (Exception ex) { Log.Error($"[{d.Id}] OnHubConnected", ex); }
                _dirty[d.Id] = true;
                lock (_lastStateKey) _lastStateKey.Remove(d.Id); // force a fresh device_state
            }
            _monitorWake.Set();
        }

        private void OnDisconnected(string reason)
        {
            Uploader.OnDisconnected();
        }

        private void OnMessage(Dictionary<string, object> m, long t1)
        {
            switch (J.Str(m, "t"))
            {
                case "cmd": OnCmd(m); break;
                case "cancel": OnCancel(m); break;
                case "evt_ack": Events.Ack(J.Long(m, "upto", 0)); break;
                case "file_want":
                case "file_done": Uploader.OnHubMessage(m); break;
                case "cmd_status": OnCmdStatus(m); break;
                case "estop": OnEstop(m); break;
                case "welcome": Log.Warn("unexpected second welcome; ignored"); break;
                default: Log.Debug("ignoring hub message t=" + J.Str(m, "t")); break;
            }
        }

        // ---------------------------------------------------------------- commands

        private DeviceBase Find(string id) { return Devices.FirstOrDefault(d => d.Id == id); }

        private static Dictionary<string, object> ResultMsg(string id, bool ok, object value, string code, string message, long started, long finished)
        {
            var r = J.Obj("t", "result", "id", id, "ok", ok, "started_ms", started, "finished_ms", finished);
            if (ok) r["value"] = value;
            else r["error"] = J.Obj("code", code, "message", message);
            return r;
        }

        private void OnCmd(Dictionary<string, object> m)
        {
            var id = J.Str(m, "id");
            var devId = J.Str(m, "device");
            var action = J.Str(m, "action");
            var args = J.Dict(m, "args") ?? new Dictionary<string, object>(StringComparer.Ordinal);
            var timeout = J.Num(m, "timeout_s", 0);
            if (string.IsNullOrEmpty(id))
            {
                Log.Warn("cmd without id ignored");
                return;
            }
            var existing = Journal.Get(id);
            if (existing != null) { RespondDuplicate(existing); return; }

            var dev = devId == null ? null : Find(devId);
            DeviceError refusal = null;
            if (_stopping) refusal = new DeviceError(ErrorCodes.Unavailable, "satellite is shutting down");
            else if (string.IsNullOrEmpty(action)) refusal = new DeviceError(ErrorCodes.InvalidArgs, "cmd without action");
            else if (dev == null) refusal = new DeviceError(ErrorCodes.InvalidArgs, $"unknown device '{devId}'");
            else refusal = dev.Admit(action);

            if (!Journal.TryBegin(id, devId, action, out existing)) { RespondDuplicate(existing); return; }
            if (refusal != null)
            {
                long now = Clock.UnixMs();
                var res = ResultMsg(id, false, null, refusal.Code, refusal.Message, now, now);
                Journal.Complete(id, res);
                // reason is the bare error code: the hub ends the command on a rejected ack and
                // takes the code from it; the message travels in the result that follows.
                Link.SendControl(J.Obj("t", "ack", "id", id, "state", "rejected", "reason", refusal.Code));
                Link.SendControl(res);
                Log.Info($"cmd {Short(id)} {devId}.{action} rejected: {refusal.Code}: {refusal.Message}");
                return;
            }
            Link.SendControl(J.Obj("t", "ack", "id", id, "state", "accepted"));
            var ctx = new CommandContext(id, devId, action, args, timeout);
            _running[id] = (dev, ctx);
            Log.Info($"cmd {Short(id)} {devId}.{action} {Json.Serialize(args)}");
            var t = new Thread(() => RunCommand(dev, ctx)) { IsBackground = true, Name = "cmd-" + devId + "-" + action };
            t.Start();
        }

        /// <summary>How long after its timeout a command may keep its thread before it is declared hung (self-test shortens it).</summary>
        internal static double HungGraceS = 30;

        private void RunCommand(DeviceBase dev, CommandContext ctx)
        {
            int finalized = 0;
            Timer timer = null, watchdog = null;
            if (ctx.TimeoutS > 0)
            {
                timer = new Timer(_ => dev.CancelCommand(ctx, ErrorCodes.Timeout, $"timed out after {ctx.TimeoutS:0.###} s"),
                                  null, TimeSpan.FromSeconds(ctx.TimeoutS), Timeout.InfiniteTimeSpan);
                // A vendor call that ignores cancellation must not leave the hub waiting forever.
                watchdog = new Timer(state =>
                {
                    if (Interlocked.CompareExchange(ref finalized, 1, 0) != 0) return;
                    var why = $"{dev.Id}.{ctx.Action} did not return {HungGraceS:0} s after its timeout; the vendor call looks hung - restart the device driver or the satellite";
                    Log.Error(why);
                    dev.MarkHung(why);
                    _running.TryRemove(ctx.Id, out _);
                    var hung = ResultMsg(ctx.Id, false, null, ErrorCodes.Timeout, why, ctx.StartedMs, Clock.UnixMs());
                    Journal.Complete(ctx.Id, hung);
                    Link.SendControl(hung);
                }, null, TimeSpan.FromSeconds(ctx.TimeoutS + HungGraceS), Timeout.InfiniteTimeSpan);
            }
            object value = null;
            string code = null, msg = null;
            try
            {
                value = dev.Execute(ctx);
            }
            catch (DeviceError e) { code = e.Code; msg = e.Message; }
            catch (OperationCanceledException) { code = ctx.CancelCode ?? ErrorCodes.Cancelled; msg = ctx.CancelMessage ?? "cancelled"; }
            catch (Exception e)
            {
                code = ErrorCodes.Internal;
                msg = e.GetType().Name + ": " + e.Message;
                Log.Error($"[{dev.Id}] {ctx.Action} crashed", e);
            }
            finally
            {
                timer?.Dispose();
                watchdog?.Dispose();
            }
            if (Interlocked.CompareExchange(ref finalized, 1, 0) != 0)
            {
                Log.Warn($"cmd {Short(ctx.Id)} {ctx.DeviceId}.{ctx.Action} returned after it was declared hung; late outcome ignored ({code ?? "ok"})");
                return;
            }
            _running.TryRemove(ctx.Id, out _);
            var res = ResultMsg(ctx.Id, code == null, value, code, msg, ctx.StartedMs, Clock.UnixMs());
            Journal.Complete(ctx.Id, res);
            Link.SendControl(res);
            Log.Info($"cmd {Short(ctx.Id)} {ctx.DeviceId}.{ctx.Action} -> {(code == null ? "ok" : code + ": " + msg)}");
        }

        private void RespondDuplicate(Journal.Entry e)
        {
            if (e.State == Journal.Running)
            {
                Link.SendControl(J.Obj("t", "ack", "id", e.Id, "state", "duplicate_running"));
                return;
            }
            Link.SendControl(J.Obj("t", "ack", "id", e.Id, "state", "duplicate_done"));
            if (e.Result != null) Link.SendControl(e.Result);
        }

        private void OnCancel(Dictionary<string, object> m)
        {
            var id = J.Str(m, "id");
            if (id != null && _running.TryGetValue(id, out var r))
            {
                Log.Info($"cancel {Short(id)} ({r.ctx.DeviceId}.{r.ctx.Action})");
                r.dev.CancelCommand(r.ctx, ErrorCodes.Cancelled, "cancelled by hub");
            }
        }

        private void OnCmdStatus(Dictionary<string, object> m)
        {
            var items = new List<object>();
            foreach (var o in J.List(m, "ids") ?? new List<object>())
            {
                var id = Convert.ToString(o, System.Globalization.CultureInfo.InvariantCulture);
                var e = Journal.Get(id);
                if (e == null) items.Add(J.Obj("id", id, "state", "unknown"));
                else if (e.State == Journal.Running) items.Add(J.Obj("id", id, "state", _running.ContainsKey(id) ? "running" : "lost_on_restart"));
                else if (e.LostOnRestart) items.Add(J.Obj("id", id, "state", "lost_on_restart", "result", e.Result));
                else items.Add(J.Obj("id", id, "state", "done", "result", e.Result));
            }
            Link.SendControl(J.Obj("t", "cmd_status_reply", "items", items));
        }

        private void OnEstop(Dictionary<string, object> m)
        {
            var reason = J.Str(m, "reason", "hub E-STOP");
            Log.Warn("E-STOP from hub: " + reason);
            var r = EmergencyStopAll(reason);
            Link.SendControl(J.Obj("t", "estop_ack", "stopped", r.stopped, "errors", r.errors, "active", r.active));
        }

        /// <summary>
        /// Stops every device in parallel (bounded wait) and ends every running command. stopped = every
        /// device whose stop completed, idle ones included; errors = [{device, message}] for the rest;
        /// active = the devices that were actually moving or acquiring (information only: the hub also
        /// sends each device a stop, and which of the two ends a job first is a race).
        /// </summary>
        public (List<object> stopped, List<object> errors, List<object> active) EmergencyStopAll(string reason)
        {
            var active = Devices.Where(d => d.IsActive).Select(d => (object)d.Id).ToList();
            foreach (var r in _running.Values) r.ctx.Cancel(ErrorCodes.Cancelled, "estop: " + reason);
            var stopped = new List<object>();
            var errors = new List<object>();
            var tasks = Devices.Select(d => (d, Task.Run(() => d.EmergencyStop(reason)))).ToList();
            foreach (var (d, t) in tasks)
            {
                try
                {
                    if (t.Wait(TimeSpan.FromSeconds(3))) stopped.Add(d.Id);
                    else errors.Add(J.Obj("device", d.Id, "message", "stop did not return within 3 s"));
                }
                catch (AggregateException ex)
                {
                    errors.Add(J.Obj("device", d.Id, "message", ex.GetBaseException().Message));
                }
            }
            Events.Append("", "error", J.Obj("what", "estop", "reason", reason, "stopped", stopped, "errors", errors, "active", active));
            return (stopped, errors, active);
        }

        private static string Short(string id) { return id.Length > 8 ? id.Substring(0, 8) : id; }

        // ---------------------------------------------------------------- monitor: deadman + device_state

        private void MonitorLoop()
        {
            double lastStates = 0;
            while (!_stopping)
            {
                _monitorWake.WaitOne(200);
                try
                {
                    CheckDeadman();
                    double now = Clock.MonoSeconds;
                    bool periodic = now - lastStates >= 1.0;
                    if (periodic) lastStates = now;
                    foreach (var d in Devices)
                    {
                        bool dirty = _dirty.TryRemove(d.Id, out _);
                        if (dirty || periodic) EmitStateIfNeeded(d, dirty);
                    }
                }
                catch (Exception ex)
                {
                    Log.Error("monitor loop", ex);
                }
            }
        }

        private void EmitStateIfNeeded(DeviceBase d, bool dirty)
        {
            var st = d.Status(fresh: false);
            var key = d.StateChangeKey(st);
            double now = Clock.MonoSeconds;
            bool emit;
            lock (_lastStateKey)
            {
                _lastStateKey.TryGetValue(d.Id, out var lastKey);
                _lastStateMono.TryGetValue(d.Id, out var lastT);
                emit = key != lastKey || now - lastT >= StatePeriodS;
                if (emit)
                {
                    _lastStateKey[d.Id] = key;
                    _lastStateMono[d.Id] = now;
                }
            }
            if (emit) Events.Append(d.Id, "device_state", st);
        }

        private void CheckDeadman()
        {
            var link = Link;
            if (link == null || link.LastPingMono < 0) return; // never connected in this boot
            double silence = Clock.MonoSeconds - link.LastPingMono;
            if (silence < _deadmanS) return;
            var active = Devices.Where(d => d.HubActivityActive).ToList();
            if (active.Count == 0) return;
            var stopped = new List<object>();
            var errors = new List<object>();
            foreach (var d in active)
            {
                try
                {
                    d.Safe($"deadman: no ping from the hub for {silence:0.0} s");
                    stopped.Add(d.Id);
                }
                catch (Exception ex)
                {
                    errors.Add(J.Obj("device", d.Id, "message", ex.Message));
                }
            }
            Log.Warn($"DEADMAN: no ping for {silence:0.0} s; safe action on {string.Join(", ", stopped)}");
            Events.Append("", "deadman", J.Obj("reason", $"no ping from the hub for {silence:0.0} s", "silent_s", Math.Round(silence, 2),
                                              "deadman_s", _deadmanS, "stopped", stopped, "errors", errors, "link", link.State.ToString()));
        }

        // ---------------------------------------------------------------- IDeviceHost

        public void Emit(string deviceId, string kind, object data) { Events.Append(deviceId, kind, data); }

        public void StateChanged(DeviceBase device)
        {
            _dirty[device.Id] = true;
            _monitorWake.Set();
        }

        public bool OfferFile(FileUploadRequest request)
        {
            var u = Uploader;
            return u != null && u.Enqueue(request);
        }

        private readonly Dictionary<string, (Dictionary<string, object> meta, double mono)> _fileMeta =
            new Dictionary<string, (Dictionary<string, object>, double)>(StringComparer.OrdinalIgnoreCase);

        public void NoteFileMeta(string path, Dictionary<string, object> meta)
        {
            if (string.IsNullOrEmpty(path) || meta == null) return;
            lock (_fileMeta)
            {
                _fileMeta[Path.GetFullPath(path)] = (meta, Clock.MonoSeconds);
                // Bounded: entries no watcher picked up within a day are forgotten.
                foreach (var k in _fileMeta.Where(kv => Clock.MonoSeconds - kv.Value.mono > 86400).Select(kv => kv.Key).ToList()) _fileMeta.Remove(k);
            }
        }

        public Dictionary<string, object> GetFileMeta(string path)
        {
            lock (_fileMeta)
            {
                // Kept (not removed) so a retried upload still carries it; expires after a day.
                return _fileMeta.TryGetValue(Path.GetFullPath(path), out var v) ? v.meta : null;
            }
        }

        private void ForwardLog(LogLevel level, string line)
        {
            if (level < LogLevel.Warn || Link == null || !Link.IsConnected) return;
            if (!_logBudget.TryTake(1)) return;
            Link.SendControl(J.Obj("t", "log", "level", level == LogLevel.Error ? "error" : "warning",
                                   "msg", line.Length > 500 ? line.Substring(0, 500) : line, "ts_ms", Clock.UnixMs()));
        }

        // ---------------------------------------------------------------- UI / shutdown

        public void SetOperatorHold(string deviceId, bool on) { Find(deviceId)?.SetOperatorHold(on); }

        public SatelliteSnapshot Snapshot()
        {
            var s = new SatelliteSnapshot
            {
                SatelliteId = Config.SatelliteId,
                BootId = BootId,
                HubUrl = Config.Hub.Url,
                EventsBuffered = Events.Count,
                EventsLastSeq = Events.LastSeq,
                EventsAcked = Events.Acked,
                EventsDropped = Events.Dropped,
            };
            var l = Link;
            if (l != null)
            {
                s.Link = l.State;
                s.LinkError = l.LastError;
                s.ConnectedSinceUtc = l.ConnectedSinceUtc;
                s.PingAgeS = l.LastPingMono < 0 ? -1 : Clock.MonoSeconds - l.LastPingMono;
                s.Reconnects = l.Reconnects;
                s.HubBootId = l.Welcome?.HubBootId;
            }
            var u = Uploader;
            if (u != null)
            {
                s.UploadQueue = u.QueueLength;
                s.UploadCurrent = u.CurrentFile;
                s.UploadBytes = u.BytesSent;
                s.FilesUploaded = u.FilesUploaded;
            }
            foreach (var d in Devices)
            {
                s.Devices.Add(new DeviceRow
                {
                    Id = d.Id, Kind = d.Kind, Driver = d.Driver, State = d.State, Reason = d.Reason,
                    Active = d.ActiveOperation, LastError = d.LastError, Sim = d.Sim, OperatorHold = d.OperatorHold,
                });
            }
            return s;
        }

        public void Stop()
        {
            if (_stopping) return;
            _stopping = true;
            Log.Info("satellite stopping");
            foreach (var r in _running.Values) r.dev.CancelCommand(r.ctx, ErrorCodes.Cancelled, "satellite shutting down");
            var deadline = Clock.MonoSeconds + 5;
            while (!_running.IsEmpty && Clock.MonoSeconds < deadline) Thread.Sleep(50);
            Thread.Sleep(300); // let the last results reach the socket
            try { Uploader?.Stop(); } catch { }
            try { Link?.Stop(TimeSpan.FromSeconds(2)); } catch { }
            var shut = Devices.Select(d => Task.Run(() =>
            {
                try { d.Shutdown(); } catch (Exception ex) { Log.Error($"[{d.Id}] shutdown", ex); }
            })).ToArray();
            Task.WaitAll(shut, TimeSpan.FromSeconds(15));
            Log.Line -= ForwardLog;
            Journal.Dispose();
            _monitorWake.Set();
            Log.Info("satellite stopped");
        }

        public void Dispose() { Stop(); }
    }
}
