using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;

namespace MastSatellite.Core
{
    public sealed class HubConfig
    {
        public string Url;
        public string SpkiSha256;
        public string TokenFile;
        public double ConnectTimeoutS = 10;
        public double BackoffMaxS = 30;
        /// <summary>Wait after HTTP 401/403 or close 4401/4403 (wrong token, satellite disabled).</summary>
        public double AuthRetryS = 60;
        /// <summary>Wait after close 4406 (protocol version mismatch).</summary>
        public double ProtoRetryS = 300;
        /// <summary>Defaults until the hub's welcome overrides them.</summary>
        public double HeartbeatS = 1.0;
        public double DeadmanS = 5.0;
    }

    public sealed class UploadConfig
    {
        public long MaxBytesPerS = 1048576;
        public int ChunkBytes = 262144;
    }

    /// <summary>One entry of "devices": common keys plus the raw object for driver-specific keys.</summary>
    public sealed class DeviceConfig
    {
        public string Id;
        public string Driver;
        public bool Enabled;
        public bool Sim;
        public Dictionary<string, object> Raw = new Dictionary<string, object>(StringComparer.Ordinal);

        public string Str(string key, string def = null) { return J.Str(Raw, key, def); }
        public string Path(string key, string def = null)
        {
            var s = J.Str(Raw, key, def);
            return string.IsNullOrEmpty(s) ? s : Environment.ExpandEnvironmentVariables(s);
        }
        public double Num(string key, double def) { return J.Num(Raw, key, def); }
        public int Int(string key, int def) { return J.Int(Raw, key, def); }
        public bool Bool(string key, bool def) { return J.Bool(Raw, key, def); }
        public Dictionary<string, object> Dict(string key) { return J.Dict(Raw, key); }
        public List<object> List(string key) { return J.List(Raw, key); }
        public bool Has(string key) { return J.Has(Raw, key); }
    }

    public sealed class SatelliteConfig
    {
        public string SatelliteId;
        public HubConfig Hub = new HubConfig();
        public string StateDir;
        public UploadConfig Upload = new UploadConfig();
        public List<DeviceConfig> Devices = new List<DeviceConfig>();
        public int EventBufferMaxEvents = 4000;
        public long EventBufferMaxBytes = 32L * 1024 * 1024;
        public int JournalMaxEntries = 10000;
        public string LogLevelName = "info";
        public int LogKeepDays = 14;
        public bool StartMinimized;
        public string SourcePath;
        public Dictionary<string, object> Raw;

        public static SatelliteConfig Load(string path)
        {
            var text = File.ReadAllText(path, Encoding.UTF8);
            var cfg = FromJson(Json.ParseObject(text));
            cfg.SourcePath = System.IO.Path.GetFullPath(path);
            return cfg;
        }

        public static SatelliteConfig FromJson(Dictionary<string, object> d)
        {
            var c = new SatelliteConfig { Raw = d };
            c.SatelliteId = J.Str(d, "satellite_id");
            var hub = J.Dict(d, "hub");
            if (hub != null)
            {
                c.Hub.Url = J.Str(hub, "url");
                c.Hub.SpkiSha256 = J.Str(hub, "spki_sha256");
                c.Hub.TokenFile = Expand(J.Str(hub, "token_file"));
                c.Hub.ConnectTimeoutS = J.Num(hub, "connect_timeout_s", c.Hub.ConnectTimeoutS);
                c.Hub.BackoffMaxS = J.Num(hub, "backoff_max_s", c.Hub.BackoffMaxS);
                c.Hub.AuthRetryS = J.Num(hub, "auth_retry_s", c.Hub.AuthRetryS);
                c.Hub.ProtoRetryS = J.Num(hub, "proto_retry_s", c.Hub.ProtoRetryS);
                c.Hub.HeartbeatS = J.Num(hub, "heartbeat_s", c.Hub.HeartbeatS);
                c.Hub.DeadmanS = J.Num(hub, "deadman_s", c.Hub.DeadmanS);
            }
            c.StateDir = Expand(J.Str(d, "state_dir", "%ProgramData%\\MAST-Satellite"));
            var up = J.Dict(d, "upload");
            if (up != null)
            {
                c.Upload.MaxBytesPerS = J.Long(up, "max_bytes_per_s", c.Upload.MaxBytesPerS);
                c.Upload.ChunkBytes = J.Int(up, "chunk_bytes", c.Upload.ChunkBytes);
            }
            var eb = J.Dict(d, "event_buffer");
            if (eb != null)
            {
                c.EventBufferMaxEvents = J.Int(eb, "max_events", c.EventBufferMaxEvents);
                c.EventBufferMaxBytes = J.Long(eb, "max_bytes", c.EventBufferMaxBytes);
            }
            var jn = J.Dict(d, "journal");
            if (jn != null) c.JournalMaxEntries = J.Int(jn, "max_entries", c.JournalMaxEntries);
            var lg = J.Dict(d, "log");
            if (lg != null)
            {
                c.LogLevelName = J.Str(lg, "level", c.LogLevelName);
                c.LogKeepDays = J.Int(lg, "keep_days", c.LogKeepDays);
            }
            var ui = J.Dict(d, "ui");
            if (ui != null) c.StartMinimized = J.Bool(ui, "start_minimized", false);

            var devs = J.List(d, "devices");
            if (devs != null)
            {
                foreach (var o in devs)
                {
                    if (!(o is Dictionary<string, object> dd)) continue;
                    c.Devices.Add(new DeviceConfig
                    {
                        Id = J.Str(dd, "id"),
                        Driver = J.Str(dd, "driver"),
                        Enabled = J.Bool(dd, "enabled", true),
                        Sim = J.Bool(dd, "sim", false),
                        Raw = dd,
                    });
                }
            }
            return c;
        }

        public static string Expand(string s)
        {
            return string.IsNullOrEmpty(s) ? s : Environment.ExpandEnvironmentVariables(s);
        }

        /// <summary>Returns human-readable problems; empty means the config is usable.</summary>
        public List<string> Validate(bool allowPlainLoopback)
        {
            var errs = new List<string>();
            if (string.IsNullOrWhiteSpace(SatelliteId)) errs.Add("satellite_id is required");
            if (string.IsNullOrWhiteSpace(Hub.Url)) errs.Add("hub.url is required");
            else
            {
                if (!Uri.TryCreate(Hub.Url, UriKind.Absolute, out var u)) errs.Add("hub.url is not a valid URI");
                else if (u.Scheme == "wss")
                {
                    if (!IsHex64(Hub.SpkiSha256)) errs.Add("hub.spki_sha256 must be 64 hex characters (SHA-256 of the hub certificate's SubjectPublicKeyInfo); without a pin the satellite refuses to connect");
                }
                else if (u.Scheme == "ws")
                {
                    if (!allowPlainLoopback) errs.Add("plain ws:// is only allowed with --allow-plain-loopback");
                    else if (!IsLoopback(u)) errs.Add("plain ws:// is only allowed to 127.0.0.1 / ::1");
                }
                else errs.Add("hub.url must start with wss:// (or ws:// for loopback tests)");
            }
            if (string.IsNullOrWhiteSpace(StateDir)) errs.Add("state_dir is required");
            if (Upload.ChunkBytes < 1024 || Upload.ChunkBytes > 512 * 1024) errs.Add("upload.chunk_bytes must be between 1 KiB and 512 KiB (one frame is at most 1 MiB)");
            if (Upload.MaxBytesPerS < 1024) errs.Add("upload.max_bytes_per_s must be at least 1024");
            var seen = new HashSet<string>(StringComparer.Ordinal);
            foreach (var dv in Devices)
            {
                if (string.IsNullOrWhiteSpace(dv.Id)) errs.Add("every device needs an id");
                else if (!seen.Add(dv.Id)) errs.Add("duplicate device id: " + dv.Id);
                if (string.IsNullOrWhiteSpace(dv.Driver)) errs.Add("device " + dv.Id + ": driver is required");
            }
            return errs;
        }

        public static bool IsHex64(string s)
        {
            return s != null && s.Length == 64 && s.All(Uri.IsHexDigit);
        }

        public static bool IsLoopback(Uri u)
        {
            return u.IsLoopback && (u.Host == "127.0.0.1" || u.Host == "[::1]" || u.Host == "::1");
        }

        public LogLevel ParsedLogLevel()
        {
            switch ((LogLevelName ?? "").ToLowerInvariant())
            {
                case "debug": return LogLevel.Debug;
                case "warn": case "warning": return LogLevel.Warn;
                case "error": return LogLevel.Error;
                default: return LogLevel.Info;
            }
        }
    }
}
