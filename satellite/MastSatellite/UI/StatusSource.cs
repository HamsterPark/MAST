using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using MastSatellite.Core;
using MastSatellite.Devices;
using MastSatellite.Protocol;

namespace MastSatellite.UI
{
    /// <summary>Everything the status window reads or asks for. Implementations must not block.</summary>
    public interface IStatusSource
    {
        SatelliteSnapshot Snapshot();
        void SetOperatorHold(string deviceId, bool on);
        IReadOnlyList<string> LocalActions(string deviceId);
        void RunLocalAction(string deviceId, string action);
        IReadOnlyList<string> BusyDeviceIds();
        string LogDirectory { get; }
    }

    internal sealed class CoreStatusSource : IStatusSource
    {
        private readonly SatelliteCore _core;

        public CoreStatusSource(SatelliteCore core) { _core = core; }

        public SatelliteSnapshot Snapshot() => _core.Snapshot();
        public void SetOperatorHold(string deviceId, bool on) => _core.SetOperatorHold(deviceId, on);

        public IReadOnlyList<string> LocalActions(string deviceId)
        {
            var dev = _core.Devices.FirstOrDefault(d => d.Id == deviceId);
            return dev is ILocalActions la ? la.LocalActions : (IReadOnlyList<string>)Array.Empty<string>();
        }

        public void RunLocalAction(string deviceId, string action)
        {
            if (_core.Devices.FirstOrDefault(d => d.Id == deviceId) is ILocalActions la)
            {
                Log.Info($"[{deviceId}] local action: {action}");
                la.RunLocalAction(action);
            }
        }

        public IReadOnlyList<string> BusyDeviceIds() => _core.Devices.Where(d => d.ActiveOperation != null).Select(d => d.Id).ToList();
        public string LogDirectory => Path.Combine(_core.StateDir, "logs");
    }

    /// <summary>
    /// Stand-in data for --ui-demo / --ui-audit: rig1's seven devices with realistic states.
    /// <see cref="Changing"/> false freezes everything except the connected-for clock.
    /// </summary>
    internal sealed class DemoStatusSource : IStatusSource
    {
        private readonly object _gate = new object();
        private readonly List<DeviceRow> _rows;
        private readonly DateTime _startedUtc = DateTime.UtcNow.AddMinutes(-62).AddSeconds(-33);
        private readonly string _scene;
        private DateTime _lastTick = DateTime.MinValue;
        private int _tick;
        private long _uploaded = 12;
        private long _bytes = 3_560_000;

        public bool Changing { get; set; }

        public DemoStatusSource(string scene)
        {
            _scene = string.IsNullOrEmpty(scene) ? "connected" : scene;
            _rows = new List<DeviceRow>
            {
                Row("ccd", "spectrometer", "lightfield", DeviceBase.StateReady, null),
                Row("pol", "rotation_stage", "kinesis_kdc101", DeviceBase.StateReady, null),
                Row("xyz", "xyz_stage", "motion_manipulator", DeviceBase.StateReady, "read-only position from Motion_Manipulator's Coordinates CSV"),
                Row("pico", "openloop_stepper", "picomotor8742", DeviceBase.StateOffline, "Picomotor not reachable via usb (usbdll.dll): newp_usb_open_devices(0x4000) failed (rc=-1, devices=0); is PicomotorApp still open?"),
                Row("aotf", "aotf", "ds1", DeviceBase.StateNotStarted, "DS1 serial protocol not provided by the vendor yet; only status/stop are available"),
                Row("pmt", "pmt", "spectrasense", DeviceBase.StateOffline, "SpectraSense is not running"),
                Row("spe", "file_watcher", "spe_watcher", DeviceBase.StateReady, null),
            };
            if (_scene == "trouble")
            {
                Set("ccd", DeviceBase.StateError, "LightField start failed: the experiment has no camera; load an experiment with the PyLoN");
                _rows.First(r => r.Id == "pol").OperatorHold = true;
                Set("pol", DeviceBase.StateOperatorHold, null);
                _rows.First(r => r.Id == "spe").Active = "upload Raman_ 10.spe";
                Set("spe", DeviceBase.StateBusy, null);
            }
            Log.Info("MastSatellite demo window (scene " + _scene + ")");
            Log.Info("[ccd] LightField hosted (pids 21356), experiment 'Demo', spe dir 'D:\\Data\\2026\\20260101'");
            Log.Info("[spe] watching D:\\Data (backfill 6 h)");
            Log.Warn("[pico] state offline (Picomotor not reachable via usb (usbdll.dll))");
            Log.Info("hub connected: wss://192.0.2.2:7862/ws/satellite/v1");
        }

        private static DeviceRow Row(string id, string kind, string driver, string state, string reason) =>
            new DeviceRow { Id = id, Kind = kind, Driver = driver, State = state, Reason = reason };

        private void Set(string id, string state, string reason)
        {
            var r = _rows.First(x => x.Id == id);
            r.State = state;
            r.Reason = reason;
        }

        public SatelliteSnapshot Snapshot()
        {
            lock (_gate)
            {
                if (Changing && (DateTime.UtcNow - _lastTick).TotalSeconds >= 1)
                {
                    // one device and one log line per second: the first row, so it is on screen
                    _lastTick = DateTime.UtcNow;
                    _tick++;
                    var ccd = _rows.First(r => r.Id == "ccd");
                    bool busy = _tick % 2 == 1;
                    int n = 10 + (_tick + 1) / 2;
                    ccd.State = busy ? DeviceBase.StateBusy : DeviceBase.StateReady;
                    ccd.Active = busy ? "acquire Raman_ " + n + " (1 frame, 1 s)" : null;
                    if (!busy) { _uploaded++; _bytes += 48_000; }
                    Log.Write(_tick % 7 == 0 ? LogLevel.Warn : LogLevel.Info,
                        busy ? $"[ccd] acquiring Raman_ {n}" : $"[spe] uploaded 2026/20260101/Raman_ {n}.spe (48164 B)");
                }
                var s = new SatelliteSnapshot
                {
                    SatelliteId = "rig1",
                    BootId = "4d83d9f362f2496aa9972acbca8113b2",
                    HubUrl = "wss://192.0.2.2:7862/ws/satellite/v1",
                    Link = _scene == "reconnecting" ? LinkState.Backoff : _scene == "rejected" ? LinkState.Rejected : LinkState.Connected,
                    LinkError = _scene == "rejected" ? "401 unauthorized" : _scene == "reconnecting" ? "Unable to connect to the remote server" : null,
                    ConnectedSinceUtc = _scene == "connected" || _scene == "trouble" ? _startedUtc : (DateTime?)null,
                    PingAgeS = _scene == "connected" || _scene == "trouble" ? 0.4 : -1,
                    Reconnects = 2,
                    HubBootId = "bbe1b9732f3e418a885cecc7238fc607",
                    EventsBuffered = 0,
                    EventsLastSeq = 6217,
                    EventsAcked = 6217,
                    UploadQueue = 0,
                    UploadBytes = _bytes,
                    FilesUploaded = _uploaded,
                };
                foreach (var r in _rows)
                    s.Devices.Add(new DeviceRow { Id = r.Id, Kind = r.Kind, Driver = r.Driver, State = r.State, Reason = r.Reason, Active = r.Active, LastError = r.LastError, Sim = r.Sim, OperatorHold = r.OperatorHold });
                return s;
            }
        }

        public void SetOperatorHold(string deviceId, bool on)
        {
            lock (_gate)
            {
                var r = _rows.FirstOrDefault(x => x.Id == deviceId);
                if (r == null) return;
                r.OperatorHold = on;
                if (on) { r.State = DeviceBase.StateOperatorHold; r.Active = null; }
                else if (r.State == DeviceBase.StateOperatorHold) r.State = DeviceBase.StateReady;
                Log.Info($"[{deviceId}] operator hold {(on ? "on" : "off")}");
            }
        }

        public IReadOnlyList<string> LocalActions(string deviceId)
        {
            switch (deviceId)
            {
                case "ccd": return new[] { "启动/重启 LightField" };
                case "pol": return new[] { "重新连接 KDC101" };
                case "pico": return new[] { "重新连接 Picomotor" };
                default: return Array.Empty<string>();
            }
        }

        public void RunLocalAction(string deviceId, string action) => Log.Info($"[{deviceId}] local action: {action}");
        public IReadOnlyList<string> BusyDeviceIds() { lock (_gate) return _rows.Where(r => r.Active != null).Select(r => r.Id).ToList(); }
        public string LogDirectory => null;
    }
}
