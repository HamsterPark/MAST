using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Threading;
using MastSatellite.Core;
using MastSatellite.Devices;

namespace MastSatellite.Protocol
{
    /// <summary>
    /// File transfer to the hub (契约 §5.2): file_offer -> file_want{offset} -> file_chunk... ->
    /// file_end -> file_done. One file at a time, rate limited, resumable: after a reconnect the
    /// same file is offered again and the hub answers with the offset it already holds. Follows
    /// the hub's receiver (mast.satellite.files): an offer may be answered by file_done alone
    /// (too large, disk low), a known file by want{offset=size} plus done, and a misaligned chunk
    /// by a fresh file_want that the sender re-seeks to.
    /// </summary>
    public sealed class FileUploader : IDisposable
    {
        private readonly HubLink _link;
        private readonly TokenBucket _bucket;
        private readonly int _chunkBytes;
        private readonly long _localMaxRate;
        private readonly object _gate = new object();
        private readonly LinkedList<FileUploadRequest> _queue = new LinkedList<FileUploadRequest>();
        private readonly AutoResetEvent _signal = new AutoResetEvent(false);
        private readonly CancellationTokenSource _stop = new CancellationTokenSource();
        private Thread _thread;
        private string _currentId;
        private BlockingCollection<Dictionary<string, object>> _replies;
        private CancellationTokenSource _connCts = new CancellationTokenSource();

        public FileUploader(HubLink link, long maxBytesPerS, int chunkBytes)
        {
            _link = link;
            _localMaxRate = Math.Max(1024, maxBytesPerS);
            _chunkBytes = Math.Max(1024, chunkBytes);
            _bucket = new TokenBucket(_localMaxRate, _chunkBytes);
        }

        public long BytesSent { get; private set; }
        public long FilesUploaded { get; private set; }
        public long FilesFailed { get; private set; }
        public string CurrentFile { get; private set; }
        public long CurrentOffset { get; private set; }
        public long CurrentSize { get; private set; }
        public double RateBytesPerS => _bucket.Rate;

        public int QueueLength { get { lock (_gate) return _queue.Count; } }

        public void Start()
        {
            _thread = new Thread(Loop) { IsBackground = true, Name = "file-uploader" };
            _thread.Start();
        }

        /// <summary>The hub's welcome may lower the rate; never raise it above our own configuration.</summary>
        public void ApplyHubRate(long hubMax)
        {
            long r = hubMax > 0 ? Math.Min(hubMax, _localMaxRate) : _localMaxRate;
            _bucket.SetRate(r, _chunkBytes);
        }

        public bool Enqueue(FileUploadRequest r)
        {
            lock (_gate)
            {
                for (var n = _queue.First; n != null; n = n.Next)
                {
                    if (string.Equals(n.Value.FullPath, r.FullPath, StringComparison.OrdinalIgnoreCase))
                    {
                        n.Value = r;
                        return true;
                    }
                }
                _queue.AddLast(r);
            }
            _signal.Set();
            return true;
        }

        public void OnHubMessage(Dictionary<string, object> m)
        {
            var id = J.Str(m, "file_id");
            lock (_gate)
            {
                if (id == null || id != _currentId) return; // e.g. the file_done that follows want{offset=size}
                _replies?.TryAdd(m);
            }
        }

        public void OnConnected()
        {
            lock (_gate) _connCts = new CancellationTokenSource();
            _signal.Set();
        }

        public void OnDisconnected()
        {
            lock (_gate) _connCts.Cancel();
        }

        private void Loop()
        {
            while (!_stop.IsCancellationRequested)
            {
                FileUploadRequest r = null;
                lock (_gate)
                {
                    if (_queue.Count > 0 && _link.IsConnected)
                    {
                        r = _queue.First.Value;
                        _queue.RemoveFirst();
                    }
                }
                if (r == null)
                {
                    _signal.WaitOne(500);
                    continue;
                }
                string outcome;
                bool ok = false, retry = false;
                string stored = null;
                try
                {
                    ok = UploadOne(r, out stored, out outcome, out retry);
                }
                catch (Exception ex)
                {
                    outcome = ex.GetType().Name + ": " + ex.Message;
                    retry = !(ex is FileNotFoundException || ex is DirectoryNotFoundException);
                }
                finally
                {
                    lock (_gate)
                    {
                        _currentId = null;
                        _replies = null;
                    }
                    CurrentFile = null;
                }
                if (ok)
                {
                    FilesUploaded++;
                    Log.Info($"uploaded {r.RelPath} ({r.Size} B) -> {stored ?? "hub"}");
                    SafeComplete(r, true, stored);
                }
                else if (retry && !_stop.IsCancellationRequested)
                {
                    Log.Info($"upload of {r.RelPath} interrupted ({outcome}); will resume");
                    lock (_gate) _queue.AddFirst(r);
                    _signal.WaitOne(1000);
                }
                else
                {
                    FilesFailed++;
                    Log.Warn($"upload of {r.RelPath} failed: {outcome}");
                    SafeComplete(r, false, outcome);
                }
            }
        }

        private static void SafeComplete(FileUploadRequest r, bool ok, string info)
        {
            try { r.Completed?.Invoke(ok, info); }
            catch (Exception ex) { Log.Error("upload completion callback failed", ex); }
        }

        private Dictionary<string, object> Next(BlockingCollection<Dictionary<string, object>> q, TimeSpan timeout, CancellationToken conn)
        {
            try { return q.TryTake(out var m, (int)timeout.TotalMilliseconds, conn) ? m : null; }
            catch (OperationCanceledException) { return null; }
        }

        private static string ErrorOf(Dictionary<string, object> done)
        {
            return J.Str(done, "error") ?? "rejected";
        }

        private bool UploadOne(FileUploadRequest r, out string stored, out string outcome, out bool retry)
        {
            stored = null;
            retry = false;
            var fi = new FileInfo(r.FullPath);
            if (!fi.Exists) { outcome = "file vanished"; return false; }
            if (fi.Length != r.Size || Clock.UnixMsFromDateTime(fi.LastWriteTimeUtc) != r.MtimeMs)
            {
                outcome = "file changed after hashing (will be re-detected)";
                return false;
            }
            CancellationToken conn;
            var replies = new BlockingCollection<Dictionary<string, object>>();
            lock (_gate)
            {
                _currentId = r.Sha256;
                _replies = replies;
                conn = _connCts.Token;
            }
            CurrentFile = r.RelPath;
            CurrentSize = r.Size;
            CurrentOffset = 0;
            var offer = J.Obj("t", "file_offer", "file_id", r.Sha256, "name", Path.GetFileName(r.FullPath), "rel_path", r.RelPath,
                              "size", r.Size, "sha256", r.Sha256, "mtime_ms", r.MtimeMs, "kind", r.Kind, "meta", r.Meta ?? J.Obj());
            if (!_link.SendBulk(Json.Serialize(offer), conn, TimeSpan.FromSeconds(30))) { outcome = "link down"; retry = true; return false; }

            bool ended = false;
            while (true)
            {
                var m = Next(replies, TimeSpan.FromSeconds(ended ? 120 : 30), conn);
                if (m == null) { outcome = ended ? "no file_done from hub" : "no file_want from hub"; retry = true; return false; }
                if (J.Str(m, "t") == "file_done")
                {
                    if (J.Bool(m, "ok", false)) { stored = J.Str(m, "stored"); outcome = "ok"; return true; }
                    outcome = "hub rejected: " + ErrorOf(m);
                    return false;
                }
                // file_want: (re)start at the offset the hub holds.
                long offset = J.Long(m, "offset", 0);
                if (offset < 0 || offset > r.Size) { outcome = $"hub asked for offset {offset} of a {r.Size}-byte file"; return false; }
                if (offset == r.Size)
                {
                    // Already complete on the hub; its file_done follows with the stored path.
                    var d = Next(replies, TimeSpan.FromSeconds(30), conn);
                    if (d != null && J.Str(d, "t") == "file_done")
                    {
                        if (!J.Bool(d, "ok", false)) { outcome = "hub rejected: " + ErrorOf(d); return false; }
                        stored = J.Str(d, "stored");
                    }
                    outcome = "already on hub";
                    return true;
                }
                var result = Stream(r, offset, replies, conn, out outcome, out retry);
                if (result == StreamResult.Failed) return false;
                if (result == StreamResult.Interrupted) continue; // a reply arrived mid-stream: handle it
                if (!_link.SendBulk(Json.Serialize(J.Obj("t", "file_end", "file_id", r.Sha256)), conn, TimeSpan.FromSeconds(30)))
                {
                    outcome = "link down";
                    retry = true;
                    return false;
                }
                ended = true;
            }
        }

        private enum StreamResult { Sent, Interrupted, Failed }

        private StreamResult Stream(FileUploadRequest r, long offset, BlockingCollection<Dictionary<string, object>> replies,
                                    CancellationToken conn, out string outcome, out bool retry)
        {
            outcome = null;
            retry = false;
            var buf = new byte[_chunkBytes];
            using (var fs = new FileStream(r.FullPath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
            {
                fs.Position = offset;
                while (offset < r.Size)
                {
                    if (replies.Count > 0) return StreamResult.Interrupted; // re-align or early file_done
                    int want = (int)Math.Min(_chunkBytes, r.Size - offset);
                    int got = 0;
                    while (got < want)
                    {
                        int n = fs.Read(buf, got, want - got);
                        if (n <= 0) break;
                        got += n;
                    }
                    if (got <= 0) { outcome = "file shrank while uploading"; return StreamResult.Failed; }
                    if (!_bucket.Take(got, conn)) { outcome = "link down"; retry = true; return StreamResult.Failed; }
                    var chunk = J.Obj("t", "file_chunk", "file_id", r.Sha256, "offset", offset, "data_b64", Convert.ToBase64String(buf, 0, got));
                    if (!_link.SendBulk(Json.Serialize(chunk), conn, TimeSpan.FromSeconds(60))) { outcome = "link down"; retry = true; return StreamResult.Failed; }
                    offset += got;
                    BytesSent += got;
                    CurrentOffset = offset;
                }
            }
            return StreamResult.Sent;
        }

        public void Stop()
        {
            _stop.Cancel();
            OnDisconnected();
            _signal.Set();
            _thread?.Join(3000);
        }

        public void Dispose() { Stop(); }
    }
}
