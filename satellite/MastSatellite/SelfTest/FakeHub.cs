using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using MastSatellite.Core;

namespace MastSatellite.SelfTest
{
    /// <summary>
    /// Minimal hub for the self-test: a plain-text WebSocket server (RFC 6455) on 127.0.0.1 that
    /// speaks the hub side of 契约 §5 just enough to drive the satellite: welcome, ping, evt_ack,
    /// file_want/file_done with a resumable store. Not a reference implementation of the hub.
    /// </summary>
    internal sealed class FakeHub : IDisposable
    {
        private readonly TcpListener _listener;
        private readonly Thread _acceptThread;
        private readonly object _sendGate = new object();
        private readonly List<Dictionary<string, object>> _received = new List<Dictionary<string, object>>();
        private readonly object _recvGate = new object();
        private volatile bool _stop;
        private TcpClient _client;
        private Stream _stream;
        private Timer _pinger;
        private long _pingN;
        private long _lastEvtSeq;

        public FakeHub()
        {
            _listener = new TcpListener(IPAddress.Loopback, 0);
            _listener.Start();
            Port = ((IPEndPoint)_listener.LocalEndpoint).Port;
            _acceptThread = new Thread(AcceptLoop) { IsBackground = true, Name = "fakehub-accept" };
            _acceptThread.Start();
        }

        public int Port { get; }
        public string Url => $"ws://127.0.0.1:{Port}/ws/satellite/v1";
        public string ExpectedToken = "selftest-token-0123456789abcdef";
        public int RejectWithHttp;              // e.g. 401 to refuse the handshake
        public int CloseAfterHelloWith;         // e.g. 4406
        public long WelcomeEvtAck = -1;         // -1: ack what we have seen in this hub's memory
        public double DeadmanS = 2.0;
        public long MaxUploadBytesPerS = 200000;
        public bool AutoAckEvents = true;
        public int DropAfterChunks = -1;        // drop the TCP connection after this many file_chunk frames
        public int Connections;
        public int HandshakeAttempts;
        public Dictionary<string, string> LastRequestHeaders = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        public readonly Dictionary<string, MemoryStream> Files = new Dictionary<string, MemoryStream>();
        public readonly List<long> WantOffsets = new List<long>();
        public readonly List<long> ChunkOffsets = new List<long>();
        /// <summary>(connection number, chunk offset) for every file_chunk received.</summary>
        public readonly List<(int conn, long offset)> ChunkLog = new List<(int, long)>();
        private int _chunksThisConnection;

        public bool IsConnected => _stream != null;
        public long LastEvtSeq => Interlocked.Read(ref _lastEvtSeq);

        private void AcceptLoop()
        {
            while (!_stop)
            {
                TcpClient c;
                try { c = _listener.AcceptTcpClient(); }
                catch { return; }
                try { Serve(c); }
                catch (Exception ex) { if (!_stop) Log.Debug("fakehub: " + ex.Message); }
                finally
                {
                    _stream = null;
                    try { c.Close(); } catch { }
                }
            }
        }

        private void Serve(TcpClient c)
        {
            c.NoDelay = true;
            var s = c.GetStream();
            Interlocked.Increment(ref HandshakeAttempts);
            var headers = ReadHttpHeaders(s);
            LastRequestHeaders = headers;
            string auth = headers.TryGetValue("Authorization", out var a) ? a : "";
            if (RejectWithHttp > 0 || auth != "Bearer " + ExpectedToken)
            {
                int code = RejectWithHttp > 0 ? RejectWithHttp : 401;
                var phrase = code == 401 ? "Unauthorized" : code == 403 ? "Forbidden" : code == 503 ? "Service Unavailable" : "Error";
                var resp = Encoding.ASCII.GetBytes($"HTTP/1.1 {code} {phrase}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n");
                s.Write(resp, 0, resp.Length);
                return;
            }
            var key = headers["Sec-WebSocket-Key"];
            string accept;
            using (var sha = SHA1.Create())
                accept = Convert.ToBase64String(sha.ComputeHash(Encoding.ASCII.GetBytes(key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11")));
            var ok = Encoding.ASCII.GetBytes("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " + accept + "\r\n\r\n");
            s.Write(ok, 0, ok.Length);
            _client = c;
            _stream = s;
            _chunksThisConnection = 0;
            Interlocked.Increment(ref Connections);
            var acc = new MemoryStream();
            while (!_stop)
            {
                int b0 = s.ReadByte();
                int b1 = s.ReadByte();
                if (b0 < 0 || b1 < 0) return;
                int opcode = b0 & 0x0F;
                bool fin = (b0 & 0x80) != 0;
                long len = b1 & 0x7F;
                if (len == 126) len = (ReadByteStrict(s) << 8) | ReadByteStrict(s);
                else if (len == 127) { len = 0; for (int i = 0; i < 8; i++) len = (len << 8) | (uint)ReadByteStrict(s); }
                var mask = new byte[4];
                bool masked = (b1 & 0x80) != 0;
                if (masked) ReadExact(s, mask, 4);
                var payload = new byte[len];
                ReadExact(s, payload, (int)len);
                if (masked) for (int i = 0; i < payload.Length; i++) payload[i] ^= mask[i % 4];
                if (opcode == 8) { SendFrame(8, payload); return; }
                if (opcode == 9) { SendFrame(10, payload); continue; }
                if (opcode == 10) continue;
                acc.Write(payload, 0, payload.Length);
                if (!fin) continue;
                var text = Encoding.UTF8.GetString(acc.ToArray());
                acc.SetLength(0);
                OnMessage(Json.ParseObject(text));
                if (_stream == null) return; // dropped inside the handler
            }
        }

        private static int ReadByteStrict(Stream s)
        {
            int b = s.ReadByte();
            if (b < 0) throw new EndOfStreamException();
            return b;
        }

        private static void ReadExact(Stream s, byte[] buf, int n)
        {
            int got = 0;
            while (got < n)
            {
                int r = s.Read(buf, got, n - got);
                if (r <= 0) throw new EndOfStreamException();
                got += r;
            }
        }

        private static Dictionary<string, string> ReadHttpHeaders(Stream s)
        {
            var sb = new StringBuilder();
            while (!sb.ToString().EndsWith("\r\n\r\n", StringComparison.Ordinal))
            {
                int b = s.ReadByte();
                if (b < 0) throw new EndOfStreamException();
                sb.Append((char)b);
                if (sb.Length > 16384) throw new IOException("header too long");
            }
            var h = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            foreach (var line in sb.ToString().Split(new[] { "\r\n" }, StringSplitOptions.RemoveEmptyEntries).Skip(1))
            {
                var i = line.IndexOf(':');
                if (i > 0) h[line.Substring(0, i).Trim()] = line.Substring(i + 1).Trim();
            }
            return h;
        }

        private void Record(Dictionary<string, object> m)
        {
            lock (_recvGate)
            {
                _received.Add(m);
                Monitor.PulseAll(_recvGate);
            }
        }

        private void OnMessage(Dictionary<string, object> m)
        {
            // hello is recorded only after the welcome is on the wire: a test that waits for hello and
            // then sends a cmd must not overtake the welcome (the satellite would rightly drop the link).
            bool isHello = J.Str(m, "t") == "hello";
            if (!isHello) Record(m);
            try { Handle(m); }
            finally { if (isHello) Record(m); }
        }

        private void Handle(Dictionary<string, object> m)
        {
            switch (J.Str(m, "t"))
            {
                case "hello":
                    if (CloseAfterHelloWith > 0)
                    {
                        var p = new byte[2 + 5];
                        p[0] = (byte)(CloseAfterHelloWith >> 8);
                        p[1] = (byte)CloseAfterHelloWith;
                        Encoding.ASCII.GetBytes("proto").CopyTo(p, 2);
                        SendFrame(8, p);
                        return;
                    }
                    Send(J.Obj("t", "welcome", "proto", 1, "hub_boot_id", "fakehub", "session", Guid.NewGuid().ToString("N"), "time_ms", Clock.UnixMs(),
                               "evt_ack", WelcomeEvtAck >= 0 ? WelcomeEvtAck : LastEvtSeq,
                               "config", J.Obj("heartbeat_s", 0.5, "deadman_s", DeadmanS, "max_upload_bytes_per_s", MaxUploadBytesPerS)));
                    break;
                case "evt":
                {
                    long seq = J.Long(m, "seq", 0);
                    long prev = LastEvtSeq;
                    if (seq == prev + 1) Interlocked.Exchange(ref _lastEvtSeq, seq);
                    if (AutoAckEvents) Send(J.Obj("t", "evt_ack", "upto", LastEvtSeq));
                    break;
                }
                case "file_offer":
                {
                    // Like mast.satellite.files.FileReceiver.offer: a complete file gets want{size} + done.
                    var id = J.Str(m, "file_id");
                    long have;
                    lock (Files) have = Files.TryGetValue(id, out var ms) ? ms.Length : 0;
                    lock (WantOffsets) WantOffsets.Add(have);
                    Send(J.Obj("t", "file_want", "file_id", id, "offset", have));
                    if (have == J.Long(m, "size", -1))
                        Send(J.Obj("t", "file_done", "file_id", id, "ok", true, "stored", "fakehub/" + id.Substring(0, 8), "error", null));
                    break;
                }
                case "file_chunk":
                {
                    var id = J.Str(m, "file_id");
                    long off = J.Long(m, "offset", 0);
                    var data = Convert.FromBase64String(J.Str(m, "data_b64"));
                    lock (ChunkOffsets)
                    {
                        ChunkOffsets.Add(off);
                        ChunkLog.Add((Connections, off));
                    }
                    lock (Files)
                    {
                        if (!Files.TryGetValue(id, out var ms)) Files[id] = ms = new MemoryStream();
                        if (off == ms.Length) ms.Write(data, 0, data.Length);
                    }
                    _chunksThisConnection++;
                    if (DropAfterChunks >= 0 && _chunksThisConnection >= DropAfterChunks)
                    {
                        DropAfterChunks = -1;
                        Drop();
                    }
                    break;
                }
                case "file_end":
                {
                    var id = J.Str(m, "file_id");
                    byte[] content;
                    lock (Files) content = Files.TryGetValue(id, out var ms) ? ms.ToArray() : new byte[0];
                    string sha;
                    using (var h = SHA256.Create()) sha = Spki.Hex(h.ComputeHash(content));
                    bool good = sha == id;
                    Send(J.Obj("t", "file_done", "file_id", id, "ok", good, "stored", good ? "fakehub/" + id.Substring(0, 8) : null,
                               "error", good ? null : "sha256 mismatch"));
                    break;
                }
            }
        }

        public void StartPinging(int periodMs)
        {
            _pinger?.Dispose();
            _pinger = new Timer(_ =>
            {
                if (_stream == null) return;
                try { Send(J.Obj("t", "ping", "n", Interlocked.Increment(ref _pingN), "t0", Clock.UnixMs())); } catch { }
            }, null, 0, periodMs);
        }

        public void StopPinging() { _pinger?.Dispose(); _pinger = null; }

        public void Send(object msg)
        {
            SendFrame(1, Encoding.UTF8.GetBytes(Json.Serialize(msg)));
        }

        private void SendFrame(int opcode, byte[] payload)
        {
            lock (_sendGate)
            {
                var s = _stream;
                if (s == null) return;
                var hdr = new List<byte> { (byte)(0x80 | opcode) };
                if (payload.Length < 126) hdr.Add((byte)payload.Length);
                else if (payload.Length < 65536) { hdr.Add(126); hdr.Add((byte)(payload.Length >> 8)); hdr.Add((byte)payload.Length); }
                else { hdr.Add(127); for (int i = 7; i >= 0; i--) hdr.Add((byte)((long)payload.Length >> (8 * i))); }
                try
                {
                    s.Write(hdr.ToArray(), 0, hdr.Count);
                    s.Write(payload, 0, payload.Length);
                }
                catch (IOException) { }
                catch (ObjectDisposedException) { }
            }
        }

        /// <summary>Close politely with a WebSocket close code.</summary>
        public void CloseWith(int code)
        {
            var p = new byte[] { (byte)(code >> 8), (byte)code };
            SendFrame(8, p);
            Thread.Sleep(100);
            Drop();
        }

        /// <summary>Kill the TCP connection without a close frame (cable pulled).</summary>
        public void Drop()
        {
            var c = _client;
            _stream = null;
            try { c?.Client.Close(0); } catch { }
            try { c?.Close(); } catch { }
        }

        public List<Dictionary<string, object>> Received()
        {
            lock (_recvGate) return _received.ToList();
        }

        public int Mark() { lock (_recvGate) return _received.Count; }

        /// <summary>Waits for a message (received after index `from`) matching pred.</summary>
        public Dictionary<string, object> WaitFor(Func<Dictionary<string, object>, bool> pred, double timeoutS, int from = 0)
        {
            var end = DateTime.UtcNow.AddSeconds(timeoutS);
            lock (_recvGate)
            {
                while (true)
                {
                    for (int i = from; i < _received.Count; i++)
                        if (pred(_received[i])) return _received[i];
                    var left = end - DateTime.UtcNow;
                    if (left <= TimeSpan.Zero) return null;
                    Monitor.Wait(_recvGate, left);
                }
            }
        }

        public void Dispose()
        {
            _stop = true;
            StopPinging();
            Drop();
            try { _listener.Stop(); } catch { }
        }
    }
}
