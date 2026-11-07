using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;

namespace MastSatellite.Core
{
    /// <summary>
    /// Command journal (JSONL, one record per line). request_id -> state/result, so a hub that
    /// re-sends a cmd gets the same job back, and a cmd left running by a previous boot is
    /// reported as lost_on_restart instead of being replayed. Keeps the newest N ids.
    /// Every append is flushed to disk before the caller acks (the host PC can blue-screen).
    /// </summary>
    public sealed class Journal : IDisposable
    {
        public const string Running = "running";
        public const string Done = "done";
        private const int MaxResultChars = 256 * 1024;

        public sealed class Entry
        {
            public string Id;
            public string Device;
            public string Action;
            public string BootId;
            public string State;
            public long AcceptedMs;
            public long FinishedMs;
            public Dictionary<string, object> Result;

            public bool LostOnRestart =>
                State == Done && Result != null && J.Str(J.Dict(Result, "error"), "code") == "lost_on_restart";
        }

        private readonly object _gate = new object();
        private readonly string _path;
        private readonly string _bootId;
        private readonly int _max;
        private readonly Dictionary<string, Entry> _map = new Dictionary<string, Entry>(StringComparer.Ordinal);
        private readonly LinkedList<string> _order = new LinkedList<string>();
        private readonly Dictionary<string, LinkedListNode<string>> _nodes = new Dictionary<string, LinkedListNode<string>>(StringComparer.Ordinal);
        private FileStream _fs;
        private int _lines;

        public Journal(string path, string bootId, int maxEntries)
        {
            _path = path;
            _bootId = bootId;
            _max = Math.Max(100, maxEntries);
        }

        public int Count { get { lock (_gate) return _map.Count; } }

        /// <summary>Reads the file, marks unfinished entries of earlier boots lost_on_restart, compacts.</summary>
        public int Open()
        {
            int lost = 0;
            lock (_gate)
            {
                Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(_path)));
                if (File.Exists(_path))
                {
                    foreach (var line in ReadLinesShared(_path))
                    {
                        if (string.IsNullOrWhiteSpace(line)) continue;
                        Dictionary<string, object> rec;
                        try { rec = Json.ParseObject(line); }
                        catch { continue; } // a torn last line after a crash
                        Apply(rec);
                    }
                }
                foreach (var e in _map.Values.Where(x => x.State == Running && x.BootId != _bootId).ToList())
                {
                    e.State = Done;
                    e.FinishedMs = Clock.UnixMs();
                    e.Result = J.Obj("t", "result", "id", e.Id, "ok", false,
                        "error", J.Obj("code", "lost_on_restart", "message", "satellite restarted while this command was running (previous boot " + e.BootId + ")"),
                        "started_ms", e.AcceptedMs, "finished_ms", e.FinishedMs);
                    lost++;
                }
                Rewrite();
            }
            return lost;
        }

        private static IEnumerable<string> ReadLinesShared(string path)
        {
            using (var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
            using (var sr = new StreamReader(fs, Encoding.UTF8))
            {
                string l;
                while ((l = sr.ReadLine()) != null) yield return l;
            }
        }

        private void Apply(Dictionary<string, object> rec)
        {
            var id = J.Str(rec, "id");
            if (string.IsNullOrEmpty(id)) return;
            if (!_map.TryGetValue(id, out var e))
            {
                e = new Entry { Id = id };
                _map[id] = e;
                _nodes[id] = _order.AddLast(id);
            }
            e.Device = J.Str(rec, "device", e.Device);
            e.Action = J.Str(rec, "action", e.Action);
            e.BootId = J.Str(rec, "boot", e.BootId);
            e.State = J.Str(rec, "state", e.State);
            e.AcceptedMs = J.Long(rec, "accepted_ms", e.AcceptedMs);
            e.FinishedMs = J.Long(rec, "finished_ms", e.FinishedMs);
            if (J.Dict(rec, "result") is Dictionary<string, object> r) e.Result = r;
        }

        public Entry Get(string id)
        {
            lock (_gate) return _map.TryGetValue(id, out var e) ? Clone(e) : null;
        }

        /// <summary>Atomically records a new running entry; false (with the existing entry) if the id is known.</summary>
        public bool TryBegin(string id, string device, string action, out Entry existing)
        {
            lock (_gate)
            {
                if (_map.TryGetValue(id, out var e)) { existing = Clone(e); return false; }
                e = new Entry { Id = id, Device = device, Action = action, BootId = _bootId, State = Running, AcceptedMs = Clock.UnixMs() };
                _map[id] = e;
                _nodes[id] = _order.AddLast(id);
                Append(Record(e));
                Trim();
                existing = null;
                return true;
            }
        }

        public void Complete(string id, Dictionary<string, object> result)
        {
            lock (_gate)
            {
                if (!_map.TryGetValue(id, out var e)) return;
                e.State = Done;
                e.FinishedMs = Clock.UnixMs();
                e.Result = Bounded(result);
                Append(Record(e));
                if (_lines > 2 * _max + 100) Rewrite();
            }
        }

        public List<Entry> RunningEntries()
        {
            lock (_gate) return _map.Values.Where(e => e.State == Running).Select(Clone).ToList();
        }

        private static Dictionary<string, object> Bounded(Dictionary<string, object> result)
        {
            if (result == null) return null;
            var s = Json.Serialize(result);
            if (s.Length <= MaxResultChars) return result;
            var r = new Dictionary<string, object>(result, StringComparer.Ordinal);
            r.Remove("value");
            r["value_truncated"] = true;
            return r;
        }

        private Dictionary<string, object> Record(Entry e)
        {
            var d = J.Obj("id", e.Id, "device", e.Device, "action", e.Action, "boot", e.BootId,
                          "state", e.State, "accepted_ms", e.AcceptedMs);
            if (e.State == Done)
            {
                d["finished_ms"] = e.FinishedMs;
                d["result"] = e.Result;
            }
            return d;
        }

        private void Append(Dictionary<string, object> rec)
        {
            try
            {
                if (_fs == null)
                    _fs = new FileStream(_path, FileMode.Append, FileAccess.Write, FileShare.Read | FileShare.Delete, 4096, FileOptions.WriteThrough);
                var bytes = Encoding.UTF8.GetBytes(Json.Serialize(rec) + "\n");
                _fs.Write(bytes, 0, bytes.Length);
                _fs.Flush(true);
                _lines++;
            }
            catch (Exception ex)
            {
                Log.Error("journal append failed", ex);
            }
        }

        private void Trim()
        {
            while (_order.Count > _max)
            {
                var oldest = _order.First.Value;
                if (_map.TryGetValue(oldest, out var e) && e.State == Running) break; // never forget a live job
                _order.RemoveFirst();
                _nodes.Remove(oldest);
                _map.Remove(oldest);
            }
        }

        private void Rewrite()
        {
            Trim();
            try
            {
                _fs?.Dispose();
                _fs = null;
                var tmp = _path + ".tmp";
                using (var w = new StreamWriter(new FileStream(tmp, FileMode.Create, FileAccess.Write, FileShare.None), new UTF8Encoding(false)))
                {
                    foreach (var id in _order) w.Write(Json.Serialize(Record(_map[id])) + "\n");
                    w.Flush();
                    ((FileStream)w.BaseStream).Flush(true);
                }
                if (File.Exists(_path)) File.Replace(tmp, _path, null, true);
                else File.Move(tmp, _path);
                _lines = _order.Count;
            }
            catch (Exception ex)
            {
                Log.Error("journal compaction failed", ex);
            }
        }

        private static Entry Clone(Entry e)
        {
            return new Entry
            {
                Id = e.Id, Device = e.Device, Action = e.Action, BootId = e.BootId, State = e.State,
                AcceptedMs = e.AcceptedMs, FinishedMs = e.FinishedMs, Result = e.Result,
            };
        }

        public void Dispose()
        {
            lock (_gate)
            {
                try { _fs?.Dispose(); } catch { }
                _fs = null;
            }
        }
    }
}
