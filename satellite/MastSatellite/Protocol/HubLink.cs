using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.WebSockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using MastSatellite.Core;

namespace MastSatellite.Protocol
{
    public sealed class WelcomeInfo
    {
        public long Proto;
        public string HubBootId;
        public string Session;
        public long TimeMs;
        public long EvtAck;
        public double HeartbeatS = 1;
        public double DeadmanS = 5;
        public long MaxUploadBytesPerS;
        public Dictionary<string, object> Raw;

        public static WelcomeInfo From(Dictionary<string, object> m)
        {
            var cfg = J.Dict(m, "config");
            return new WelcomeInfo
            {
                Proto = J.Long(m, "proto", 0),
                HubBootId = J.Str(m, "hub_boot_id"),
                Session = J.Str(m, "session"),
                TimeMs = J.Long(m, "time_ms", 0),
                EvtAck = J.Long(m, "evt_ack", 0),
                HeartbeatS = J.Num(cfg, "heartbeat_s", 1.0),
                DeadmanS = J.Num(cfg, "deadman_s", 5.0),
                MaxUploadBytesPerS = J.Long(cfg, "max_upload_bytes_per_s", 0),
                Raw = m,
            };
        }
    }

    public enum LinkState { Idle, Connecting, Connected, Backoff, Rejected, Stopped }

    internal sealed class LinkClosedException : Exception
    {
        public LinkClosedException(string message, double backoffS) : base(message) { BackoffS = backoffS; }
        public double BackoffS { get; }
    }

    /// <summary>
    /// The satellite's outbound WebSocket to the hub (契约 §5.1): connect with bearer token,
    /// hello -> welcome, then one receive loop and one send loop. Control messages (pong, ack,
    /// result, ...) go first, then events from the EventBuffer, then file chunks. Reconnects with
    /// exponential backoff; 401/403 and close 4406 back off much longer.
    /// </summary>
    public sealed class HubLink : IDisposable
    {
        public const int MaxFrameBytes = 1024 * 1024;

        private sealed class Outgoing
        {
            public Func<string> Build;
            public TaskCompletionSource<bool> Done;
            public int Gen;
        }

        private readonly Uri _uri;
        private readonly string _satelliteId;
        private readonly string _token;
        private readonly HubConfig _hubCfg;
        private readonly EventBuffer _events;
        private readonly Func<Dictionary<string, object>> _hello;
        private readonly ConcurrentQueue<Outgoing> _control = new ConcurrentQueue<Outgoing>();
        private readonly ConcurrentQueue<Outgoing> _bulk = new ConcurrentQueue<Outgoing>();
        private readonly SemaphoreSlim _wake = new SemaphoreSlim(0, int.MaxValue);
        private readonly CancellationTokenSource _stop = new CancellationTokenSource();
        private readonly Random _rng = new Random();
        private Task _runner;
        private ClientWebSocket _ws;
        private int _gen;
        private volatile bool _connected;

        public HubLink(Uri uri, string satelliteId, string token, HubConfig hubCfg, EventBuffer events,
                       Func<Dictionary<string, object>> helloFactory)
        {
            _uri = uri;
            _satelliteId = satelliteId;
            _token = token;
            _hubCfg = hubCfg;
            _events = events;
            _hello = helloFactory;
            _events.Appended += Wake;
        }

        /// <summary>Every message from the hub except ping (answered here). Args: message, receive time (unix ms).</summary>
        public event Action<Dictionary<string, object>, long> Message;
        public event Action<WelcomeInfo> Connected;
        public event Action<string> Disconnected;

        public LinkState State { get; private set; } = LinkState.Idle;
        public bool IsConnected => _connected;
        public string LastError { get; private set; }
        public DateTime? ConnectedSinceUtc { get; private set; }
        public WelcomeInfo Welcome { get; private set; }
        public double LastPingMono { get; private set; } = -1;
        public long PingCount { get; private set; }
        public long FramesSent { get; private set; }
        public long FramesReceived { get; private set; }
        public int Reconnects { get; private set; }
        public double NextAttemptInS { get; private set; }

        public void Start()
        {
            if (_uri.Scheme == "wss") TlsPinning.Configure(_uri, _hubCfg.SpkiSha256);
            _runner = Task.Run(RunAsync);
        }

        public void Wake()
        {
            try { _wake.Release(); } catch (SemaphoreFullException) { }
        }

        /// <summary>Queue a control message. Dropped if the link is down (results are in the journal).</summary>
        public void SendControl(object msg)
        {
            SendControl(() => Json.Serialize(msg));
        }

        public void SendControl(Func<string> build)
        {
            if (!_connected) return;
            _control.Enqueue(new Outgoing { Build = build, Gen = Volatile.Read(ref _gen) });
            Wake();
        }

        /// <summary>Queue a bulk message (file chunk) and wait until it is on the wire. False if the link dropped.</summary>
        public bool SendBulk(string text, CancellationToken ct, TimeSpan timeout)
        {
            if (!_connected) return false;
            var o = new Outgoing { Build = () => text, Done = new TaskCompletionSource<bool>(), Gen = Volatile.Read(ref _gen) };
            _bulk.Enqueue(o);
            Wake();
            try
            {
                if (!o.Done.Task.Wait((int)timeout.TotalMilliseconds, ct)) return false;
                return o.Done.Task.Result;
            }
            catch (OperationCanceledException) { return false; }
            catch (AggregateException) { return false; }
        }

        private async Task RunAsync()
        {
            int attempt = 0;
            while (!_stop.IsCancellationRequested)
            {
                double backoff = 0;
                var connectedAt = -1.0;
                string reason = null;
                var ws = new ClientWebSocket();
                try
                {
                    State = LinkState.Connecting;
                    ws.Options.SetRequestHeader("Authorization", "Bearer " + _token);
                    ws.Options.SetRequestHeader("X-MAST-Satellite-Id", _satelliteId);
                    ws.Options.SetRequestHeader("X-MAST-Satellite-Proto", "1");
                    ws.Options.KeepAliveInterval = TimeSpan.FromSeconds(15);
                    ws.Options.Proxy = null;
                    ws.Options.SetBuffer(64 * 1024, 64 * 1024);
                    using (var cts = CancellationTokenSource.CreateLinkedTokenSource(_stop.Token))
                    {
                        cts.CancelAfter(TimeSpan.FromSeconds(Math.Max(2, _hubCfg.ConnectTimeoutS)));
                        await ws.ConnectAsync(_uri, cts.Token).ConfigureAwait(false);
                    }
                    var hello = _hello();
                    await SendTextAsync(ws, Json.Serialize(hello), _stop.Token).ConfigureAwait(false);
                    var first = await ReceiveMessageAsync(ws, TimeSpan.FromSeconds(10), _stop.Token).ConfigureAwait(false);
                    if (first == null)
                    {
                        string why = "hub closed the connection before welcome" + CloseInfo(ws);
                        double wait = ClassifyClose(ws, ref why);
                        throw new LinkClosedException(why, wait);
                    }
                    var m = Json.ParseObject(first);
                    if (J.Str(m, "t") != "welcome") throw new IOException("expected welcome, got " + J.Str(m, "t"));
                    var w = WelcomeInfo.From(m);
                    if (w.Proto != 1) throw new IOException("hub speaks protocol " + w.Proto);

                    Interlocked.Increment(ref _gen);
                    Welcome = w;
                    _ws = ws;
                    _events.Rewind(w.EvtAck);
                    LastPingMono = Clock.MonoSeconds; // the deadman counts from the welcome
                    _connected = true;
                    ConnectedSinceUtc = DateTime.UtcNow;
                    connectedAt = Clock.MonoSeconds;
                    State = LinkState.Connected;
                    LastError = null;
                    Log.Info($"hub connected: {_uri} hub_boot_id={w.HubBootId} session={w.Session} evt_ack={w.EvtAck}");
                    try { Connected?.Invoke(w); } catch (Exception ex) { Log.Error("Connected handler", ex); }

                    using (var conn = CancellationTokenSource.CreateLinkedTokenSource(_stop.Token))
                    {
                        var recv = ReceiveLoopAsync(ws, conn.Token);
                        var send = SendLoopAsync(ws, conn.Token);
                        var done = await Task.WhenAny(recv, send).ConfigureAwait(false);
                        conn.Cancel();
                        try { await Task.WhenAll(recv, send).ConfigureAwait(false); } catch { }
                        reason = done.IsFaulted ? done.Exception?.GetBaseException().Message : "closed" + CloseInfo(ws);
                    }
                    backoff = ClassifyClose(ws, ref reason);
                }
                catch (OperationCanceledException) when (_stop.IsCancellationRequested)
                {
                    reason = "stopping";
                }
                catch (OperationCanceledException)
                {
                    reason = "timeout while connecting or waiting for welcome";
                }
                catch (LinkClosedException lc)
                {
                    reason = lc.Message;
                    backoff = lc.BackoffS;
                }
                catch (Exception ex)
                {
                    int? http = HttpStatus(ex);
                    reason = Describe(ex, http);
                    if (http == 401 || http == 403)
                    {
                        State = LinkState.Rejected;
                        backoff = _hubCfg.AuthRetryS;
                    }
                }
                finally
                {
                    bool was = _connected;
                    _connected = false;
                    _ws = null;
                    ConnectedSinceUtc = null;
                    FailPending();
                    try { ws.Abort(); } catch { }
                    try { ws.Dispose(); } catch { }
                    if (was)
                    {
                        Reconnects++;
                        Log.Warn("hub link lost: " + reason);
                        try { Disconnected?.Invoke(reason); } catch (Exception ex) { Log.Error("Disconnected handler", ex); }
                    }
                }
                if (_stop.IsCancellationRequested) break;
                LastError = reason;
                if (connectedAt >= 0 && Clock.MonoSeconds - connectedAt > 10) attempt = 0;
                if (backoff <= 0)
                {
                    backoff = Math.Min(Math.Max(1, _hubCfg.BackoffMaxS), 0.5 * Math.Pow(2, Math.Min(attempt, 10)));
                    backoff *= 0.75 + 0.5 * _rng.NextDouble();
                }
                // Also after a close code with its own wait (4400, 4409): the window must not keep saying "connected".
                if (State != LinkState.Rejected) State = LinkState.Backoff;
                attempt++;
                if (!SuppressRepeat(reason)) Log.Info($"hub connect failed ({reason}); retry in {backoff:0.0} s");
                NextAttemptInS = backoff;
                try { await Task.Delay(TimeSpan.FromSeconds(backoff), _stop.Token).ConfigureAwait(false); }
                catch (OperationCanceledException) { break; }
            }
            State = LinkState.Stopped;
        }

        private string _lastLoggedReason;
        private int _sameReasonCount;

        // Keeps a hub that is down for hours from filling the log with identical lines.
        private bool SuppressRepeat(string reason)
        {
            if (reason == _lastLoggedReason)
            {
                _sameReasonCount++;
                return _sameReasonCount % 20 != 0;
            }
            _lastLoggedReason = reason;
            _sameReasonCount = 0;
            return false;
        }

        private double ClassifyClose(ClientWebSocket ws, ref string reason)
        {
            var st = ws.CloseStatus;
            if (st == null) return 0;
            int code = (int)st.Value;
            reason = $"closed by hub: {code} {ws.CloseStatusDescription}";
            if (code == 4406)
            {
                State = LinkState.Rejected;
                Log.Error($"hub rejected the protocol version (4406); retrying in {_hubCfg.ProtoRetryS:0} s");
                return _hubCfg.ProtoRetryS;
            }
            if (code == 4401 || code == 4403 || code == 1008)
            {
                State = LinkState.Rejected;
                return _hubCfg.AuthRetryS;
            }
            if (code == 4400)
            {
                // The hub found a message it could not parse: a bug on one side, not a network problem.
                Log.Error("hub closed with 4400 (protocol error): " + ws.CloseStatusDescription);
                return 30;
            }
            if (code == 4409)
            {
                // A newer connection presented this satellite id: a second copy of the satellite (or of its
                // config) elsewhere. Coming straight back would only knock that one off in turn.
                Log.Error($"hub handed satellite id '{_satelliteId}' to a newer connection (4409); is another satellite running with this id? retrying in {_hubCfg.AuthRetryS:0} s");
                return _hubCfg.AuthRetryS;
            }
            return 0;
        }

        private static string CloseInfo(ClientWebSocket ws)
        {
            try { return ws.CloseStatus != null ? $" ({(int)ws.CloseStatus} {ws.CloseStatusDescription})" : ""; }
            catch { return ""; }
        }

        private void FailPending()
        {
            // Control messages of a dead connection are dropped: acks are meaningless now and
            // results are in the journal (the hub asks with cmd_status).
            while (_control.TryDequeue(out var c)) c.Done?.TrySetResult(false);
            while (_bulk.TryDequeue(out var b)) b.Done?.TrySetResult(false);
        }

        private async Task ReceiveLoopAsync(ClientWebSocket ws, CancellationToken ct)
        {
            var buf = new byte[64 * 1024];
            var acc = new MemoryStream();
            while (!ct.IsCancellationRequested)
            {
                var r = await ws.ReceiveAsync(new ArraySegment<byte>(buf), ct).ConfigureAwait(false);
                if (r.MessageType == WebSocketMessageType.Close)
                {
                    try
                    {
                        using (var cts = new CancellationTokenSource(2000))
                            await ws.CloseOutputAsync(WebSocketCloseStatus.NormalClosure, "bye", cts.Token).ConfigureAwait(false);
                    }
                    catch { }
                    return;
                }
                acc.Write(buf, 0, r.Count);
                if (acc.Length > MaxFrameBytes + 4096)
                    throw new IOException("hub frame exceeds 1 MiB");
                if (!r.EndOfMessage) continue;
                long t1 = Clock.UnixMs();
                var text = Encoding.UTF8.GetString(acc.GetBuffer(), 0, (int)acc.Length);
                acc.SetLength(0);
                FramesReceived++;
                Dispatch(text, t1);
            }
        }

        private void Dispatch(string text, long t1)
        {
            Dictionary<string, object> m;
            try { m = Json.ParseObject(text); }
            catch (Exception ex)
            {
                Log.Warn("unparseable hub frame: " + ex.Message);
                return;
            }
            var t = J.Str(m, "t");
            if (t == "ping")
            {
                LastPingMono = Clock.MonoSeconds;
                PingCount++;
                var n = J.Get(m, "n");
                var t0 = J.Get(m, "t0");
                // t2 is taken when the pong actually goes out, so queueing time is not counted as transit.
                SendControl(() => Json.Serialize(J.Obj("t", "pong", "n", n, "t0", t0, "t1", t1, "t2", Clock.UnixMs())));
                return;
            }
            try { Message?.Invoke(m, t1); }
            catch (Exception ex) { Log.Error("handling hub message '" + t + "' failed", ex); }
        }

        private async Task SendLoopAsync(ClientWebSocket ws, CancellationToken ct)
        {
            int gen = Volatile.Read(ref _gen);
            while (!ct.IsCancellationRequested)
            {
                if (_control.TryDequeue(out var c))
                {
                    if (c.Gen != gen) { c.Done?.TrySetResult(false); continue; }
                    await SendOneAsync(ws, c, ct).ConfigureAwait(false);
                    continue;
                }
                if (_events.TryTakeNext(out var e))
                {
                    await SendTextAsync(ws, e.Text, ct).ConfigureAwait(false);
                    continue;
                }
                if (_bulk.TryDequeue(out var b))
                {
                    if (b.Gen != gen) { b.Done?.TrySetResult(false); continue; }
                    await SendOneAsync(ws, b, ct).ConfigureAwait(false);
                    continue;
                }
                await _wake.WaitAsync(250, ct).ConfigureAwait(false);
            }
        }

        private async Task SendOneAsync(ClientWebSocket ws, Outgoing o, CancellationToken ct)
        {
            string text;
            try { text = o.Build(); }
            catch (Exception ex)
            {
                Log.Error("building outgoing message failed", ex);
                o.Done?.TrySetResult(false);
                return;
            }
            try
            {
                await SendTextAsync(ws, text, ct).ConfigureAwait(false);
                o.Done?.TrySetResult(true);
            }
            catch
            {
                o.Done?.TrySetResult(false);
                throw;
            }
        }

        private async Task SendTextAsync(ClientWebSocket ws, string text, CancellationToken ct)
        {
            var bytes = Encoding.UTF8.GetBytes(text);
            if (bytes.Length > MaxFrameBytes)
            {
                Log.Error($"dropping outgoing frame of {bytes.Length} bytes (limit 1 MiB): {text.Substring(0, Math.Min(80, text.Length))}");
                return;
            }
            await ws.SendAsync(new ArraySegment<byte>(bytes), WebSocketMessageType.Text, true, ct).ConfigureAwait(false);
            FramesSent++;
        }

        private static async Task<string> ReceiveMessageAsync(ClientWebSocket ws, TimeSpan timeout, CancellationToken stop)
        {
            using (var cts = CancellationTokenSource.CreateLinkedTokenSource(stop))
            {
                cts.CancelAfter(timeout);
                var buf = new byte[64 * 1024];
                var acc = new MemoryStream();
                while (true)
                {
                    var r = await ws.ReceiveAsync(new ArraySegment<byte>(buf), cts.Token).ConfigureAwait(false);
                    if (r.MessageType == WebSocketMessageType.Close) return null;
                    acc.Write(buf, 0, r.Count);
                    if (acc.Length > MaxFrameBytes + 4096) throw new IOException("hub frame exceeds 1 MiB");
                    if (r.EndOfMessage) return Encoding.UTF8.GetString(acc.GetBuffer(), 0, (int)acc.Length);
                }
            }
        }

        private static int? HttpStatus(Exception ex)
        {
            for (var e = ex; e != null; e = e.InnerException)
            {
                if (e is WebException we && we.Response is HttpWebResponse r) return (int)r.StatusCode;
            }
            return null;
        }

        private static string Describe(Exception ex, int? http)
        {
            var parts = new List<string>();
            if (http != null) parts.Add("HTTP " + http.Value);
            for (var e = ex; e != null; e = e.InnerException)
            {
                var s = e.GetType().Name + ": " + e.Message;
                if (e is WebException we && we.Status == WebExceptionStatus.TrustFailure)
                    s += " [TLS pin check failed; hub presented spki_sha256=" + (TlsPinning.LastSeenPin ?? "?") + "]";
                parts.Add(s);
            }
            return string.Join(" <- ", parts);
        }

        /// <summary>Close the socket politely (1000) and stop reconnecting.</summary>
        public void Stop(TimeSpan grace)
        {
            var ws = _ws;
            if (ws != null && _connected)
            {
                try
                {
                    using (var cts = new CancellationTokenSource(grace))
                        ws.CloseOutputAsync(WebSocketCloseStatus.NormalClosure, "satellite shutdown", cts.Token).Wait(grace);
                }
                catch { }
            }
            _stop.Cancel();
            try { _runner?.Wait(grace); } catch { }
        }

        public void Dispose()
        {
            _events.Appended -= Wake;
            _stop.Cancel();
        }
    }
}
