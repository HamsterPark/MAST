using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using MastSatellite.Core;

namespace MastSatellite.Devices
{
    /// <summary>
    /// file_watcher / spe_watcher (契约 §11.1): watches the LightField data folder, waits until a
    /// file is stable (size and mtime unchanged for stable_s), hashes it, parses the SPE 3.0
    /// footer and hands it to the uploader. A local manifest keeps uploads from repeating across
    /// restarts; only files newer than backfill_hours are picked up at start. Read-only on the
    /// data folder: files are opened with FileShare.ReadWrite and never modified.
    /// </summary>
    public sealed class SpeWatcherDevice : DeviceBase
    {
        private static readonly string[] CapList = { "status", "stop", "safe", "rescan", "pause", "resume" };

        private sealed class Pending
        {
            public long Size;
            public long Mtime;
            public double SinceMono;
            public int Failures;
            public double RetryAfterMono;
        }

        private sealed class ManifestEntry
        {
            public long Size;
            public long Mtime;
            public string Sha;
            public string State;
            public string Stored;
        }

        private readonly object _gate = new object();
        private readonly string _root;
        private readonly double _backfillHours;
        private readonly double _stableS;
        private readonly double _scanIntervalS;
        private readonly long _maxBytes;
        private readonly string[] _extensions;
        private readonly string _manifestPath;
        private readonly Dictionary<string, Pending> _pending = new Dictionary<string, Pending>(StringComparer.OrdinalIgnoreCase);
        private readonly HashSet<string> _inFlight = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        private readonly Dictionary<string, ManifestEntry> _manifest = new Dictionary<string, ManifestEntry>(StringComparer.OrdinalIgnoreCase);
        private readonly Dictionary<string, int> _rejections = new Dictionary<string, int>(StringComparer.OrdinalIgnoreCase);
        private FileSystemWatcher _fsw;
        private Timer _timer;
        private volatile bool _paused;
        private double _lastScanMono = -1e9;
        private volatile bool _scanRequested = true;
        private long _seen;
        private long _uploaded;
        private long _bytesUploaded;
        private long _failed;
        private string _lastFile;
        private int _ticking;

        public SpeWatcherDevice(DeviceConfig cfg, IDeviceHost host) : base(cfg, host, "file_watcher")
        {
            Model = "spe_watcher";
            _root = cfg.Path("root", null);
            _backfillHours = cfg.Num("backfill_hours", 24);
            _stableS = cfg.Num("stable_s", 2.0);
            _scanIntervalS = cfg.Num("scan_interval_s", 300);
            _maxBytes = (long)(cfg.Num("max_file_mb", 2048) * 1024 * 1024);
            var ext = cfg.List("extensions");
            _extensions = ext != null
                ? ext.Select(o => Convert.ToString(o, CultureInfo.InvariantCulture).TrimStart('*').ToLowerInvariant()).ToArray()
                : new[] { ".spe", ".csv" };
            _manifestPath = Path.Combine(host.StateDir, "uploads-" + cfg.Id + ".jsonl");
        }

        public override IReadOnlyList<string> Caps => CapList;
        protected override IEnumerable<string> AlwaysAdmitted => CapList;
        public override Dictionary<string, object> Info => J.Obj("root", _root, "backfill_hours", _backfillHours, "stable_s", _stableS, "extensions", _extensions);
        protected override IEnumerable<string> VolatileStatusKeys => new[] { "seen", "queued", "uploaded", "bytes_uploaded", "last_file", "pending", "failed" };

        public override void Start()
        {
            if (string.IsNullOrEmpty(_root))
            {
                SetState(StateError, "config: 'root' (the folder the spectra are saved under) is required");
                return;
            }
            LoadManifest();
            _timer = new Timer(_ => Tick(), null, 500, 1000);
            TryStartWatcher();
        }

        private void TryStartWatcher()
        {
            if (_fsw != null) return;
            if (!Directory.Exists(_root))
            {
                SetState(StateOffline, "watch root not found: " + _root);
                return;
            }
            try
            {
                var w = new FileSystemWatcher(_root)
                {
                    IncludeSubdirectories = true,
                    NotifyFilter = NotifyFilters.FileName | NotifyFilters.LastWrite | NotifyFilters.Size,
                    InternalBufferSize = 64 * 1024,
                };
                w.Created += (s, e) => Touch(e.FullPath);
                w.Changed += (s, e) => Touch(e.FullPath);
                w.Renamed += (s, e) => Touch(e.FullPath);
                w.Error += (s, e) =>
                {
                    Log.Warn($"[{Id}] watcher error ({e.GetException()?.Message}); rescanning");
                    _scanRequested = true;
                };
                w.EnableRaisingEvents = true;
                _fsw = w;
                SetState(StateReady);
            }
            catch (Exception ex)
            {
                SetState(StateError, "cannot watch " + _root + ": " + ex.Message);
            }
        }

        private bool Matches(string path)
        {
            var ext = Path.GetExtension(path).ToLowerInvariant();
            return _extensions.Contains(ext);
        }

        private void Touch(string path)
        {
            if (!Matches(path)) return;
            lock (_gate)
            {
                _rejections.Remove(path); // the file changed: worth another try
                if (!_pending.ContainsKey(path)) _pending[path] = new Pending { Size = -1, Mtime = -1, SinceMono = Clock.MonoSeconds };
            }
        }

        public string RelPath(string full)
        {
            var root = Path.GetFullPath(_root).TrimEnd('\\', '/') + Path.DirectorySeparatorChar;
            var f = Path.GetFullPath(full);
            var rel = f.StartsWith(root, StringComparison.OrdinalIgnoreCase) ? f.Substring(root.Length) : Path.GetFileName(f);
            return rel.Replace('\\', '/');
        }

        private void Tick()
        {
            if (Interlocked.Exchange(ref _ticking, 1) == 1) return;
            try
            {
                if (_fsw == null) TryStartWatcher();
                if (_fsw == null) return;
                if (_scanRequested || Clock.MonoSeconds - _lastScanMono > _scanIntervalS)
                {
                    _scanRequested = false;
                    _lastScanMono = Clock.MonoSeconds;
                    Scan();
                }
                CheckPending();
            }
            catch (Exception ex)
            {
                Log.Error($"[{Id}] tick failed", ex);
            }
            finally
            {
                Interlocked.Exchange(ref _ticking, 0);
            }
        }

        private void Scan()
        {
            var cutoff = DateTime.UtcNow.AddHours(-_backfillHours);
            int added = 0;
            foreach (var f in Walk(_root))
            {
                if (!Matches(f)) continue;
                DateTime mt;
                try { mt = File.GetLastWriteTimeUtc(f); } catch { continue; }
                if (mt < cutoff) continue;
                var rel = RelPath(f);
                lock (_gate)
                {
                    if (_inFlight.Contains(f) || _pending.ContainsKey(f)) continue;
                    if (_rejections.TryGetValue(f, out var nrej) && nrej >= 6) continue;
                    if (_manifest.TryGetValue(rel, out var me) && me.State == "uploaded" && me.Mtime == Clock.UnixMsFromDateTime(mt))
                        continue;
                    _pending[f] = new Pending { Size = -1, Mtime = -1, SinceMono = Clock.MonoSeconds };
                    added++;
                }
            }
            if (added > 0) Log.Info($"[{Id}] scan found {added} file(s) to check under {_root}");
        }

        /// <summary>Depth-first walk that skips folders we may not read instead of aborting.</summary>
        private static IEnumerable<string> Walk(string root)
        {
            var stack = new Stack<string>();
            stack.Push(root);
            while (stack.Count > 0)
            {
                var d = stack.Pop();
                string[] files = null, dirs = null;
                try { files = Directory.GetFiles(d); dirs = Directory.GetDirectories(d); }
                catch { }
                if (files != null) foreach (var f in files) yield return f;
                if (dirs != null) foreach (var s in dirs) stack.Push(s);
            }
        }

        private void CheckPending()
        {
            List<KeyValuePair<string, Pending>> items;
            lock (_gate) items = _pending.ToList();
            var now = Clock.MonoSeconds;
            foreach (var kv in items)
            {
                var path = kv.Key;
                var p = kv.Value;
                if (now < p.RetryAfterMono) continue;
                FileInfo fi;
                try { fi = new FileInfo(path); }
                catch { lock (_gate) _pending.Remove(path); continue; }
                if (!fi.Exists) { lock (_gate) _pending.Remove(path); continue; }
                long size = fi.Length, mt = Clock.UnixMsFromDateTime(fi.LastWriteTimeUtc);
                if (size != p.Size || mt != p.Mtime)
                {
                    p.Size = size;
                    p.Mtime = mt;
                    p.SinceMono = now;
                    continue;
                }
                if (now - p.SinceMono < _stableS) continue;
                if (_paused) continue;
                if (size > _maxBytes)
                {
                    Log.Warn($"[{Id}] skipping {path}: {size} bytes exceeds max_file_mb");
                    lock (_gate) _pending.Remove(path);
                    continue;
                }
                if (size == 0) continue; // LightField creates the file first, fills it later
                var rel = RelPath(path);
                lock (_gate)
                {
                    if (_manifest.TryGetValue(rel, out var me) && me.State == "uploaded" && me.Size == size && me.Mtime == mt)
                    {
                        _pending.Remove(path);
                        continue;
                    }
                }
                if (!TryOffer(path, rel, size, mt))
                {
                    p.Failures++;
                    p.RetryAfterMono = now + Math.Min(300, 5 * Math.Pow(2, p.Failures));
                    continue;
                }
                lock (_gate) _pending.Remove(path);
            }
        }

        private bool TryOffer(string path, string rel, long size, long mt)
        {
            string sha;
            try { sha = Sha256File(path); }
            catch (IOException ex)
            {
                Log.Info($"[{Id}] {rel} not readable yet ({ex.Message}); will retry");
                return false;
            }
            catch (UnauthorizedAccessException ex)
            {
                Log.Warn($"[{Id}] {rel}: {ex.Message}");
                return false;
            }
            // The file must not have changed while it was being hashed.
            var fi = new FileInfo(path);
            if (fi.Length != size || Clock.UnixMsFromDateTime(fi.LastWriteTimeUtc) != mt) return false;
            lock (_gate)
            {
                if (_manifest.TryGetValue(rel, out var me) && me.State == "uploaded" && me.Sha == sha)
                {
                    // Same content re-saved (only the timestamp moved): remember it, do not upload again.
                    me.Mtime = mt;
                    me.Size = size;
                    AppendManifest(rel, me);
                    return true;
                }
            }
            var kind = KindOf(path);
            var meta = J.Obj("satellite_mtime_ms", mt);
            try { meta["satellite_ctime_ms"] = Clock.UnixMsFromDateTime(File.GetCreationTimeUtc(path)); } catch { }
            // An acquisition this satellite ran (or saw in the LightField window) knows its time window.
            var acq = Host.GetFileMeta(path);
            if (acq != null) foreach (var kv in acq) meta[kv.Key] = kv.Value;
            if (kind == "spe")
            {
                try
                {
                    var info = SpeFile.Read(path);
                    foreach (var kv in info.Meta) meta[kv.Key] = kv.Value;
                }
                catch (Exception ex)
                {
                    meta["spe_parse_error"] = ex.Message;
                }
            }
            Interlocked.Increment(ref _seen);
            _lastFile = rel;
            Emit("file_seen", J.Obj("rel_path", rel, "size", size, "sha256", sha, "mtime_ms", mt, "kind", kind));
            lock (_gate) _inFlight.Add(path);
            var req = new FileUploadRequest
            {
                FullPath = path,
                RelPath = rel,
                Kind = kind,
                Size = size,
                MtimeMs = mt,
                Sha256 = sha,
                Meta = meta,
                Completed = (ok, info2) => OnUploaded(path, rel, size, mt, sha, ok, info2),
            };
            if (!Host.OfferFile(req))
            {
                lock (_gate) _inFlight.Remove(path);
                return false;
            }
            return true;
        }

        private void OnUploaded(string path, string rel, long size, long mt, string sha, bool ok, string info)
        {
            lock (_gate)
            {
                _inFlight.Remove(path);
                var me = new ManifestEntry { Size = size, Mtime = mt, Sha = sha, State = ok ? "uploaded" : "failed", Stored = ok ? info : null };
                if (ok)
                {
                    _manifest[rel] = me;
                    _uploaded++;
                    _bytesUploaded += size;
                }
                else
                {
                    _failed++;
                    // Retry with growing pauses (1 min .. 1 h); after 6 refusals wait for a rescan or a change of the file.
                    _rejections.TryGetValue(path, out var n);
                    _rejections[path] = ++n;
                    if (n < 6)
                        _pending[path] = new Pending { Size = size, Mtime = mt, SinceMono = Clock.MonoSeconds, Failures = n,
                                                       RetryAfterMono = Clock.MonoSeconds + Math.Min(3600, 60 * Math.Pow(2, n - 1)) };
                    else
                        Log.Warn($"[{Id}] giving up on {rel} after {n} refusals ({info}); 'rescan' retries it");
                }
                AppendManifest(rel, me, ok ? null : info);
            }
            Host.StateChanged(this);
        }

        public static string KindOf(string path)
        {
            switch (Path.GetExtension(path).ToLowerInvariant())
            {
                case ".spe": return "spe";
                case ".csv": return "csv";
                case ".lfe": return "lfe";
                default: return "other";
            }
        }

        public static string Sha256File(string path)
        {
            using (var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete, 1 << 16))
            using (var sha = SHA256.Create())
                return Spki.Hex(sha.ComputeHash(fs));
        }

        private void LoadManifest()
        {
            try
            {
                if (!File.Exists(_manifestPath)) return;
                foreach (var line in File.ReadAllLines(_manifestPath, Encoding.UTF8))
                {
                    if (string.IsNullOrWhiteSpace(line)) continue;
                    Dictionary<string, object> d;
                    try { d = Json.ParseObject(line); } catch { continue; }
                    var rel = J.Str(d, "rel_path");
                    if (rel == null) continue;
                    var e = new ManifestEntry
                    {
                        Size = J.Long(d, "size", -1),
                        Mtime = J.Long(d, "mtime_ms", -1),
                        Sha = J.Str(d, "sha256"),
                        State = J.Str(d, "state"),
                        Stored = J.Str(d, "stored"),
                    };
                    if (e.State == "uploaded") _manifest[rel] = e;
                }
                _uploaded = _manifest.Count;
                Log.Info($"[{Id}] manifest: {_manifest.Count} file(s) already uploaded");
            }
            catch (Exception ex)
            {
                Log.Error($"[{Id}] cannot read manifest {_manifestPath}", ex);
            }
        }

        private void AppendManifest(string rel, ManifestEntry e, string error = null)
        {
            try
            {
                var rec = J.Obj("rel_path", rel, "size", e.Size, "mtime_ms", e.Mtime, "sha256", e.Sha, "state", e.State,
                                "stored", e.Stored, "ts_ms", Clock.UnixMs());
                if (error != null) rec["error"] = error;
                File.AppendAllText(_manifestPath, Json.Serialize(rec) + "\n", new UTF8Encoding(false));
            }
            catch (Exception ex)
            {
                Log.Error($"[{Id}] manifest append failed", ex);
            }
        }

        protected override Dictionary<string, object> BuildStatus(bool fresh)
        {
            lock (_gate)
            {
                return J.Obj("root", _root, "seen", _seen, "queued", _inFlight.Count, "pending", _pending.Count,
                             "uploaded", _uploaded, "bytes_uploaded", _bytesUploaded, "failed", _failed,
                             "last_file", _lastFile, "paused", _paused);
            }
        }

        protected override object ExecuteAction(CommandContext ctx)
        {
            switch (ctx.Action)
            {
                case "rescan":
                    lock (_gate) _rejections.Clear();
                    _scanRequested = true;
                    ThreadPool.QueueUserWorkItem(_ => Tick());
                    return J.Obj("rescan", "requested");
                case "pause":
                    _paused = true;
                    Host.StateChanged(this);
                    return Status(false);
                case "resume":
                    _paused = false;
                    Host.StateChanged(this);
                    return Status(false);
            }
            throw new DeviceError(ErrorCodes.NotSupported, ctx.Action);
        }

        protected override void StopHardware(bool emergency, bool hubOnly)
        {
            // Nothing moves. An upload in progress is not an experiment activity; leave it.
        }

        public override void Shutdown()
        {
            try { _timer?.Dispose(); } catch { }
            try { if (_fsw != null) { _fsw.EnableRaisingEvents = false; _fsw.Dispose(); } } catch { }
        }
    }
}
