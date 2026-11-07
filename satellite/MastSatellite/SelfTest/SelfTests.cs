using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using System.Text;
using System.Threading;
using MastSatellite.Core;
using MastSatellite.Devices;
using MastSatellite.Protocol;

namespace MastSatellite.SelfTest
{
    /// <summary>
    /// --selftest: offline checks of the pieces that do not need vendor hardware. Prints one line
    /// per test and returns 0 only if everything passed. Uses a temp folder and an in-process
    /// fake hub on 127.0.0.1; touches nothing else.
    /// </summary>
    public static class SelfTests
    {
        private sealed class Failure : Exception { public Failure(string m) : base(m) { } }

        private static int _pass, _fail, _skip;
        private static string _tmp;

        private static void Check(bool cond, string what)
        {
            if (!cond) throw new Failure(what);
        }

        private static void Eq(object want, object got, string what)
        {
            if (!Equals(Norm(want), Norm(got))) throw new Failure($"{what}: want {Json.Serialize(want)}, got {Json.Serialize(got)}");
        }

        private static object Norm(object o) { return o is int i ? (long)i : o; }

        private static void T(string name, Action body)
        {
            var sw = Stopwatch.StartNew();
            try
            {
                body();
                _pass++;
                Console.WriteLine($"PASS  {name}  ({sw.ElapsedMilliseconds} ms)");
            }
            catch (SkipException s)
            {
                _skip++;
                Console.WriteLine($"SKIP  {name}: {s.Message}");
            }
            catch (Exception ex)
            {
                _fail++;
                Console.WriteLine($"FAIL  {name}: {(ex is Failure ? ex.Message : ex.ToString())}");
            }
        }

        private sealed class SkipException : Exception { public SkipException(string m) : base(m) { } }

        public static int Run(string speSamples, bool verbose)
        {
            _tmp = Path.Combine(Path.GetTempPath(), "mastsat-selftest-" + Guid.NewGuid().ToString("N").Substring(0, 8));
            Directory.CreateDirectory(_tmp);
            Log.Init(Path.Combine(_tmp, "logs"), 1);
            Log.MinLevel = verbose ? LogLevel.Debug : LogLevel.Info;
            Log.EchoToConsole = verbose;
            Console.WriteLine("MastSatellite " + SatelliteCore.SoftwareVersion + " self-test, temp " + _tmp);

            T("json: parse/serialize round trip", TestJson);
            T("spki: DER cut-out equals re-encoded SubjectPublicKeyInfo (RSA, ECDSA)", TestSpki);
            T("dpapi: token protect/unprotect round trip", TestDpapi);
            T("rate limiter: long-run average equals the configured rate", TestRateLimiter);
            T("journal: idempotent ids, lost_on_restart after reboot, compaction", TestJournal);
            T("event buffer: ack, rewind on reconnect, bounded overflow", TestEventBuffer);
            T("spe: write + read synthetic SPE 3.0 (header, data, footer)", TestSpeRoundTrip);
            T("spe: parse real LightField footer samples", () => TestSpeSamples(speSamples));
            T("sim devices: spectrometer acquire, axes, limits, busy, hold", TestSimDevices);
            T("spectrometer configure: the contract's LightField names pass, anything else is refused, nothing half-applied", TestSpectrometerConfigure);
            T("safe (MAST shutdown): stops the hub's acquisition with the reason, spares the operator's, admitted under hold", TestSafeSparesManual);
            T("axis_state: names the hub command while it moves, then moving=false without cmd_id at once", TestAxisStateCmdId);
            T("manual panel actions: set_velocity/identify (rotation), move_abs/set_velocity/set_accel/abort (stepper), honest caps", TestManualActions);
            T("xyz: read-only position from Motion_Manipulator's Coordinates CSV; no false 'stopped'", TestXyzCoordinates);
            T("picomotor usb: only the bytes the driver wrote are the reply; reasons stay printable", TestPicoUsbDecode);
            T("protocol: hello/welcome/ping/cmd/duplicates/cancel/cmd_status/hold", TestProtocol);
            T("protocol: reconnect resends unacked events from evt_ack", TestReconnectResend);
            T("protocol: deadman stops hub motion when pings stop", TestDeadman);
            T("protocol: estop stops everything and acks", TestEstop);
            T("protocol: file upload with resume after a dropped link", TestUploadResume);
            T("protocol: 401, close 4406 and close 4409 back off instead of spinning; 503 retries normally", TestRejections);
            T("protocol: a vendor call that ignores cancel is declared hung, result sent once", TestHungCommand);

            Console.WriteLine($"{_pass} passed, {_fail} failed, {_skip} skipped");
            Log.Close();
            try { if (_fail == 0) Directory.Delete(_tmp, true); } catch { }
            return _fail == 0 ? 0 : 1;
        }

        // ------------------------------------------------------------------ unit tests

        private static void TestPicoUsbDecode()
        {
            // Observed on hardware: the buffer still held an earlier reply after the 3 new bytes "0\r\n".
            var buf = new byte[256];
            var stale = Encoding.ASCII.GetBytes("New_Focus 8742 v3.04 09/09/16 103961\r\n");
            Array.Copy(stale, buf, stale.Length);
            for (int i = stale.Length; i < buf.Length; i++) buf[i] = (byte)(0x80 + i % 100);
            var fresh = Encoding.ASCII.GetBytes("0\r\n");
            Array.Copy(fresh, buf, fresh.Length);
            Eq("0", NewportUsbTransport.DecodeReply(buf, (uint)fresh.Length), "decode only the bytes written");
            Eq("New_Focus 8742 v3.04 09/09/16 103961", NewportUsbTransport.DecodeReply(stale, (uint)stale.Length), "identity line");
            Eq("", NewportUsbTransport.DecodeReply(buf, 0), "nothing read");
            var lead = Encoding.ASCII.GetBytes("\r\n0, NO ERROR DETECTED\r\n");
            Eq("0, NO ERROR DETECTED", NewportUsbTransport.DecodeReply(lead, (uint)lead.Length), "skips an empty first line");
            Eq("0", NewportUsbTransport.DecodeReply(fresh, 4096), "a count past the buffer is clamped");
            Eq("0\\x0D\\x0A\\xFF", Picomotor8742Device.Printable("0\r\nÿ"), "control bytes escaped");
            var p = Picomotor8742Device.Printable(new string('x', 200));
            Check(p.Length == 61 && p.EndsWith("…", StringComparison.Ordinal), "length capped");
        }

        private static void TestJson()
        {
            var s = "{\"a\":1,\"b\":2.5,\"c\":[true,null,\"x\\u00e9\\n\"],\"d\":{\"e\":-3}}";
            var o = Json.ParseObject(s);
            Eq(1L, o["a"], "int");
            Eq(2.5, o["b"], "double");
            var c = (List<object>)o["c"];
            Eq(true, c[0], "bool");
            Check(c[1] == null, "null");
            Eq("xé\n", c[2], "string");
            Eq(-3L, ((Dictionary<string, object>)o["d"])["e"], "nested");
            var back = Json.ParseObject(Json.Serialize(o));
            Eq(Json.Serialize(o), Json.Serialize(back), "round trip");
            Eq("{\"x\":null,\"y\":0.1}", Json.Serialize(J.Obj("x", double.NaN, "y", 0.1)), "NaN -> null, 0.1 exact");
            Eq("\"a\\u0001b\"", Json.Serialize("a\u0001b"), "control char escape");
        }

        private static void TestSpki()
        {
            using (var rsa = RSA.Create(2048))
            {
                var req = new CertificateRequest("CN=selftest-rsa", rsa, HashAlgorithmName.SHA256, RSASignaturePadding.Pkcs1);
                using (var cert = req.CreateSelfSigned(DateTimeOffset.UtcNow.AddDays(-1), DateTimeOffset.UtcNow.AddDays(1)))
                {
                    var cut = Spki.ExtractSpki(cert.RawData);
                    var enc = Spki.EncodeSpki(cert.PublicKey);
                    Check(cut.SequenceEqual(enc), "RSA SPKI cut-out differs from the re-encoded one");
                    Eq(64, Spki.Sha256Hex(cert).Length, "hex length");
                }
            }
            using (var ec = ECDsa.Create(ECCurve.NamedCurves.nistP256))
            {
                var req = new CertificateRequest("CN=selftest-ec", ec, HashAlgorithmName.SHA256);
                using (var cert = req.CreateSelfSigned(DateTimeOffset.UtcNow.AddDays(-1), DateTimeOffset.UtcNow.AddDays(1)))
                {
                    var cut = Spki.ExtractSpki(cert.RawData);
                    var enc = Spki.EncodeSpki(cert.PublicKey);
                    Check(cut.SequenceEqual(enc), "EC SPKI cut-out differs from the re-encoded one");
                }
            }
            Check(TlsPinning.FixedTimeEquals("ab", "ab") && !TlsPinning.FixedTimeEquals("ab", "ac"), "fixed-time compare");
        }

        private static void TestDpapi()
        {
            var p = Path.Combine(_tmp, "token.dpapi");
            TokenStore.Save(p, "  secret-token-value-1234  ", DataProtectionScope.CurrentUser);
            Check(!File.ReadAllText(p, Encoding.UTF8).Contains("secret-token"), "token stored in clear");
            Eq("secret-token-value-1234", TokenStore.Load(p), "round trip");
        }

        private static void TestRateLimiter()
        {
            double now = 0, slept = 0;
            var tb = new TokenBucket(1000, 1000, () => now, (s, ct) => { now += s; slept += s; return true; });
            for (int i = 0; i < 10; i++) tb.Take(1000, CancellationToken.None);
            // first chunk is covered by the initial burst, the other 9 cost 1 s each
            Check(Math.Abs(now - 9.0) < 1e-6, $"10 x 1000 B at 1000 B/s should take 9 s, took {now}");
            now += 100; // idle: refill is capped at the burst
            slept = 0;
            tb.Take(1000, CancellationToken.None);
            tb.Take(1000, CancellationToken.None);
            Check(Math.Abs(slept - 1.0) < 1e-6, $"after idle, two chunks should cost 1 s, cost {slept}");
            Check(!tb.TryTake(1), "TryTake must refuse when the bucket is in debt");
        }

        private static void TestJournal()
        {
            var p = Path.Combine(_tmp, "journal-test.jsonl");
            using (var j = new Journal(p, "boot1", 100))
            {
                j.Open();
                Check(j.TryBegin("a", "ccd", "acquire", out _), "first begin");
                Check(!j.TryBegin("a", "ccd", "acquire", out var ex1) && ex1.State == Journal.Running, "duplicate while running");
                j.Complete("a", J.Obj("t", "result", "id", "a", "ok", true, "value", 5));
                Check(!j.TryBegin("a", "ccd", "acquire", out var ex2) && ex2.State == Journal.Done && J.Long(ex2.Result, "value") == 5, "duplicate after done keeps the result");
                Check(j.TryBegin("b", "pol", "move_abs", out _), "second id");
            }
            File.AppendAllText(p, "{\"id\":\"torn\",\"sta"); // a crash mid-write
            using (var j = new Journal(p, "boot2", 100))
            {
                int lost = j.Open();
                Eq(1, lost, "unfinished entries of the previous boot");
                var b = j.Get("b");
                Check(b.LostOnRestart, "b must be lost_on_restart");
                Check(j.Get("a").State == Journal.Done, "a stays done");
                for (int i = 0; i < 400; i++)
                {
                    j.TryBegin("x" + i, "d", "status", out _);
                    j.Complete("x" + i, J.Obj("ok", true));
                }
                Check(j.Count <= 100, "trimmed to max entries, have " + j.Count);
            }
            Check(File.ReadAllLines(p).Length <= 2 * 100 + 102, "file compacted");
        }

        private static void TestEventBuffer()
        {
            var eb = new EventBuffer(10, 1 << 20);
            for (int i = 0; i < 5; i++) eb.Append("d", "k", J.Obj("i", i));
            Check(eb.TryTakeNext(out var a) && a.Seq == 1, "first seq is 1");
            Check(eb.TryTakeNext(out var b) && b.Seq == 2, "then 2");
            eb.Ack(2);
            Eq(3, eb.Count, "acked removed");
            eb.Rewind(1); // a hub that only had 1 (cannot go below the ack)
            Check(eb.TryTakeNext(out var c) && c.Seq == 3, "after rewind the next unacked is 3");
            eb.Rewind(3);
            Check(eb.TryTakeNext(out var d) && d.Seq == 4, "rewind(3) resumes at 4");
            for (int i = 0; i < 20; i++) eb.Append("d", "k", J.Obj("i", i));
            Check(eb.Count <= 10, "bounded");
            Check(eb.Dropped > 0, "drops counted");
            Eq(25L, eb.LastSeq, "seq keeps counting");
        }

        private static void TestSpeRoundTrip()
        {
            var p = Path.Combine(_tmp, "rt.spe");
            var frames = new List<ushort[]>();
            for (int f = 0; f < 3; f++) frames.Add(Enumerable.Range(0, 1340).Select(i => (ushort)(i + f * 7)).ToArray());
            var wl = Enumerable.Range(0, 1340).Select(i => 635.0 + i * 0.0427).ToArray();
            SpeFile.WriteU16(p, frames, wl, 633, new Dictionary<string, string>
            {
                ["Camera.ShutterTiming.ExposureTime"] = "250|Double",
                ["Camera.ShutterTiming.Mode"] = "Normal|ShutterTimingMode",
                ["Spectrometer.Grating.Selected"] = "[500nm,1200][0][0]|String",
            }, DateTimeOffset.Now);
            var info = SpeFile.Read(p, keepWavelength: true);
            Eq(3, info.Frames, "frames");
            Eq(1340, info.Width, "width");
            Eq(1, info.Height, "height");
            Eq("MonochromeUnsigned16", info.PixelFormat, "pixel format");
            Eq(633.0, info.LaserLineNm, "laser line");
            Eq(1340, info.Wavelength.Length, "wavelength count");
            Check(Math.Abs(info.Wavelength[1339] - wl[1339]) < 1e-9, "wavelength value");
            Eq(250.0, info.Meta["exposure_ms"], "exposure from footer");
            Eq("Normal", info.Meta["shutter_mode"], "shutter mode (second setting in the same branch)");
            Eq("[500nm,1200][0][0]", info.Meta["grating"], "grating");
            var f2 = SpeFile.ReadFrameU16(p, info, 2);
            Check(f2[0] == 14 && f2[1339] == 1339 + 14, "frame 2 data");
        }

        private static string FindSamples(string given)
        {
            if (!string.IsNullOrEmpty(given)) return Directory.Exists(given) ? given : null;
            return FindUp(Path.Combine("local-samples", "team", "lightfield", "raw", "spe_footers"));
        }

        /// <summary>A repository folder found by walking up from the current directory or the exe.</summary>
        private static string FindUp(string rel)
        {
            foreach (var start in new[] { Environment.CurrentDirectory, AppDomain.CurrentDomain.BaseDirectory })
            {
                var d = new DirectoryInfo(start);
                for (int i = 0; i < 8 && d != null; i++, d = d.Parent)
                {
                    var c = Path.Combine(d.FullName, rel);
                    if (Directory.Exists(c)) return c;
                }
            }
            return null;
        }

        private static void TestSpeSamples(string given)
        {
            var dir = FindSamples(given);
            if (dir == null) throw new SkipException("no footer samples (pass --spe-samples <dir> with *.spe.footer.xml from a folder of LightField footer samples)");
            var files = Directory.GetFiles(dir, "*.footer.xml");
            if (files.Length == 0) throw new SkipException("no *.footer.xml in " + dir);
            foreach (var f in files)
            {
                var xml = File.ReadAllBytes(f);
                // Rebuild a full SPE around the real footer: header + zero frames + footer.
                var text = Encoding.UTF8.GetString(xml);
                var m = System.Text.RegularExpressions.Regex.Match(text, "<DataBlock type=\"Frame\" count=\"(\\d+)\"[^>]*stride=\"(\\d+)\"");
                Check(m.Success, "frame DataBlock in " + Path.GetFileName(f));
                long frames = long.Parse(m.Groups[1].Value, CultureInfo.InvariantCulture);
                long stride = long.Parse(m.Groups[2].Value, CultureInfo.InvariantCulture);
                var p = Path.Combine(_tmp, Path.GetFileName(f) + ".spe");
                using (var fs = new FileStream(p, FileMode.Create))
                {
                    var hdr = new byte[SpeFile.HeaderSize];
                    long footer = SpeFile.HeaderSize + frames * stride;
                    Buffer.BlockCopy(BitConverter.GetBytes((ulong)footer), 0, hdr, 678, 8);
                    Buffer.BlockCopy(BitConverter.GetBytes(3.0f), 0, hdr, 1992, 4);
                    fs.Write(hdr, 0, hdr.Length);
                    fs.SetLength(footer);
                    fs.Position = footer;
                    fs.Write(xml, 0, xml.Length);
                }
                var info = SpeFile.Read(p, keepWavelength: true);
                var name = Path.GetFileName(f);
                Eq(frames, (long)info.Frames, name + " frames");
                Eq(1340, info.Width, name + " width");
                // The Raman_ experiments carry laserLine="633"; the pump-probe PP_ files have no laser line at all.
                if (text.Contains("laserLine=\"633\"")) Eq(633.0, info.LaserLineNm, name + " laser line");
                else Check(info.LaserLineNm == null && !info.Meta.ContainsKey("laser_line_nm"), name + " has no laser line, parser must not invent one");
                Eq(1340, info.Wavelength?.Length ?? 0, name + " wavelength points");
                Check(info.Meta.ContainsKey("exposure_ms") && info.Meta.ContainsKey("grating") && info.Meta.ContainsKey("center_wavelength_nm"), name + " experiment fields");
                Eq("PyLoN: 100B eXcelon", info.Meta["camera_model"], name + " camera");
                if (name.StartsWith("Raman__13", StringComparison.Ordinal))
                {
                    Eq(1000.0, info.Meta["exposure_ms"], "Raman_ 13 exposure");
                    Eq("[500nm,1200][0][0]", info.Meta["grating"], "Raman_ 13 grating");
                    Eq("Locked", info.Meta["temperature_status"], "Raman_ 13 temperature status");
                    Check(Math.Abs(J.ToDouble(info.Meta["center_wavelength_nm"], 0) - 664.5495) < 1e-3, "Raman_ 13 centre wavelength");
                    Check(Math.Abs(info.Wavelength[0] - 635.42716) < 1e-4, "Raman_ 13 first wavelength");
                }
            }
        }

        // ------------------------------------------------------------------ sim devices without a hub

        private sealed class TestHost : IDeviceHost
        {
            public readonly List<(string dev, string kind, object data)> Events = new List<(string, string, object)>();
            public readonly List<FileUploadRequest> Files = new List<FileUploadRequest>();
            public string StateDir { get; set; }
            public void Emit(string deviceId, string kind, object data) { lock (Events) Events.Add((deviceId, kind, data)); }
            public void StateChanged(DeviceBase device) { }
            public bool OfferFile(FileUploadRequest r) { lock (Files) Files.Add(r); return true; }
            public readonly Dictionary<string, Dictionary<string, object>> FileMeta = new Dictionary<string, Dictionary<string, object>>(StringComparer.OrdinalIgnoreCase);
            public void NoteFileMeta(string path, Dictionary<string, object> meta) { lock (FileMeta) FileMeta[path] = meta; }
            public Dictionary<string, object> GetFileMeta(string path) { lock (FileMeta) return FileMeta.TryGetValue(path, out var m) ? m : null; }
            public int Count(string kind) { lock (Events) return Events.Count(e => e.kind == kind); }
        }

        private static DeviceConfig Dc(string id, string driver, params object[] kv)
        {
            var raw = J.Obj("id", id, "driver", driver, "sim", true);
            for (int i = 0; i + 1 < kv.Length; i += 2) raw[(string)kv[i]] = kv[i + 1];
            return SatelliteConfig.FromJson(J.Obj("devices", new List<object> { raw })).Devices[0];
        }

        private static object Exec(DeviceBase d, string action, Dictionary<string, object> args = null, double timeout = 0)
        {
            var refusal = d.Admit(action);
            if (refusal != null) throw refusal;
            return d.Execute(new CommandContext(Guid.NewGuid().ToString("N"), d.Id, action, args, timeout));
        }

        private static string ExpectError(Action a)
        {
            try { a(); }
            catch (DeviceError e) { return e.Code; }
            throw new Failure("expected a DeviceError");
        }

        private static void TestSimDevices()
        {
            var host = new TestHost { StateDir = Path.Combine(_tmp, "simdev") };
            Directory.CreateDirectory(host.StateDir);

            var ccd = new SimSpectrometer(Dc("ccd", "lightfield", "readout_ms", 5.0), host);
            ccd.Start();
            var r = (Dictionary<string, object>)Exec(ccd, "acquire", J.Obj("exposure_ms", 20.0, "frames", 3, "save", true, "base_name", "Selftest_"));
            Eq(3L, J.Long(r, "frames"), "frames in result");
            Eq(3, host.Count("spectrum"), "spectrum events");
            Eq(1, host.Count("acq_completed"), "acq_completed");
            var file = (string)((List<object>)r["files"])[0];
            Check(File.Exists(file) && SpeFile.Read(file).Frames == 3, "saved SPE readable");
            var fm = host.GetFileMeta(file);
            Check(fm != null && J.Long(fm, "acq_started_ms") > 0 && J.Long(fm, "acq_finished_ms") >= J.Long(fm, "acq_started_ms"), "acquisition time window noted for the saved file");
            var lastProgress = (Dictionary<string, object>)host.Events.Last(e => e.kind == "acq_progress").data;
            Eq(3L, J.Long(lastProgress, "frame"), "acq_progress.frame is 1-based");
            Check(J.Has((Dictionary<string, object>)host.Events.Last(e => e.kind == "acq_completed").data, "summary"), "acq_completed carries summary");
            var spec = host.Events.First(e => e.kind == "spectrum").data as Dictionary<string, object>;
            Eq(1340, Convert.FromBase64String((string)spec["data_b64"]).Length / 2, "1340 u16 pixels");
            Check(J.Has(spec, "exposure_started_us") && J.Long(spec, "axis_ver") >= 1, "frame metadata");
            Eq("limit", ExpectError(() => Exec(ccd, "set_center_wavelength", J.Obj("nm", 50.0))), "centre out of range");
            Eq("invalid_args", ExpectError(() => Exec(ccd, "set_settings", J.Obj("values", J.Obj("Camera.Adc.Speed", 4.0)))), "set_settings outside the whitelist");
            int axisBefore = host.Count("wavelength_axis");
            Exec(ccd, "set_grating", J.Obj("grating", "600"));
            Check(host.Count("wavelength_axis") == axisBefore + 1, "grating change emits wavelength_axis");

            // busy: a second acquire while one runs
            var t = new Thread(() => { try { Exec(ccd, "acquire", J.Obj("exposure_ms", 300.0, "frames", 2, "save", false)); } catch { } });
            t.Start();
            Thread.Sleep(100);
            Eq("device_busy", ExpectError(() => Exec(ccd, "acquire", J.Obj("frames", 1, "save", false))), "concurrent acquire");
            t.Join();

            // operator hold
            ccd.SetOperatorHold(true);
            Eq("operator_hold", ExpectError(() => Exec(ccd, "acquire", J.Obj("frames", 1))), "hold rejects acquire");
            Check(Exec(ccd, "stop") != null && Exec(ccd, "status") != null && Exec(ccd, "safe") != null, "hold admits stop, safe and status");
            ccd.SetOperatorHold(false);

            var pol = new SimRotationStage(Dc("pol", "kinesis_kdc101", "speed_deg_s", 200.0, "home_time_s", 0.3), host);
            pol.Start();
            Eq("unavailable", ExpectError(() => Exec(pol, "move_abs", J.Obj("deg", 10.0))), "not homed");
            Exec(pol, "home");
            var st = (Dictionary<string, object>)Exec(pol, "move_abs", J.Obj("deg", 45.0));
            Check(Math.Abs(J.Num(st, "position_deg") - 45) < 1e-6 && J.Bool(st, "homed"), "moved to 45");
            Eq("limit", ExpectError(() => Exec(pol, "move_abs", J.Obj("deg", 9999.0))), "soft limit");
            Eq("invalid_args", ExpectError(() => Exec(pol, "move_abs", J.Obj())), "missing deg");
            Check(host.Count("axis_state") > 0, "axis_state while moving");
            pol.Shutdown();

            var pico = new SimOpenLoopStepper(Dc("pico", "picomotor8742", "max_steps_per_cmd", 500, "steps_per_s", 20000.0), host);
            pico.Start();
            Exec(pico, "jog", J.Obj("axis", 2, "steps", 300));
            Exec(pico, "jog", J.Obj("axis", 2, "steps", -100));
            var ps = (Dictionary<string, object>)Exec(pico, "status");
            Eq(200L, J.Long(J.Dict(J.Dict(ps, "axes"), "2"), "steps"), "accumulated steps");
            Eq("limit", ExpectError(() => Exec(pico, "jog", J.Obj("axis", 1, "steps", 501))), "step cap");
            Eq("invalid_args", ExpectError(() => Exec(pico, "jog", J.Obj("axis", 9, "steps", 1))), "bad axis");
            pico.Shutdown();

            var xyz = new SimXyzStage(Dc("xyz", "motion_manipulator", "max_step_mm", 0.5, "speed_mm_s", 10.0), host);
            xyz.Start();
            Exec(xyz, "move_rel", J.Obj("dz", 0.2));
            Eq("limit", ExpectError(() => Exec(xyz, "move_rel", J.Obj("dx", 0.6))), "per-command step cap");
            Eq("limit", ExpectError(() => Exec(xyz, "move_abs", J.Obj("y", 7.0))), "xyz soft limit");
            xyz.Shutdown();

            var aotf = new SimAotf(Dc("aotf", "ds1"), host);
            aotf.Start();
            Exec(aotf, "set", J.Obj("channel", 0, "wavelength_nm", 700.0, "amplitude", 0.5));
            Eq("limit", ExpectError(() => Exec(aotf, "set", J.Obj("wavelength_nm", 400.0))), "aotf range");

            var pmt = new SimPmt(Dc("pmt", "spectrasense", "max_hv", 900.0), host);
            pmt.Start();
            Eq("limit", ExpectError(() => Exec(pmt, "set_hv", J.Obj("volts", 950.0))), "hv cap");
            Exec(pmt, "set_hv", J.Obj("volts", 500.0));
            Check(J.Num((Dictionary<string, object>)Exec(pmt, "read", J.Obj("integration_ms", 10.0)), "value") > 0, "pmt reads");

            // a disabled real driver never touches hardware
            var ds1 = new Ds1Device(SatelliteConfig.FromJson(J.Obj("devices", new List<object> { J.Obj("id", "aotf2", "driver", "ds1", "port", "COM99") })).Devices[0], host);
            ds1.Start();
            Eq("not_supported", ExpectError(() => Exec(ds1, "set", J.Obj("wavelength_nm", 700.0))), "ds1 without vendor protocol");
            Check(Exec(ds1, "status") != null, "ds1 status works");
        }

        private static void TestSpectrometerConfigure()
        {
            var host = new TestHost { StateDir = Path.Combine(_tmp, "simcfg") };
            Directory.CreateDirectory(host.StateDir);
            var ccd = new SimSpectrometer(Dc("ccd", "lightfield", "readout_ms", 5.0), host);
            ccd.Start();
            // What MAST's ConfigureSpectrometer sends (契约 §5.4, devices 10-20), every option at once.
            var st = (Dictionary<string, object>)Exec(ccd, "configure", J.Obj(
                "exposure_ms", 250.0, "frames", 3, "adc_mhz", 4.0, "adc_quality", "HighCapacity", "shutter_mode", "OpenBeforeTrigger",
                "output_signal", "EffectivelyExposing", "timestamps", "ExposureStarted,ExposureEnded", "frame_tracking", true,
                "trigger", J.Obj("response", "ShiftPerTrigger", "edge", "FallingEdge"),
                "roi", J.Obj("mode", "custom", "x", 0, "width", 1340, "y", 40, "height", 20),
                "file", J.Obj("base_name", "Cfg_", "attach_increment", false)));
            Eq("ExposureStarted,ExposureEnded", J.Str(st, "timestamps"), "timestamps string kept (not read as a bool)");
            Eq("FallingEdge", J.Str(st, "trigger_edge"), "trigger.edge FallingEdge");
            Eq("ShiftPerTrigger", J.Str(st, "trigger_response"), "trigger.response");
            Eq("EffectivelyExposing", J.Str(st, "output_signal"), "output_signal");
            Eq("OpenBeforeTrigger", J.Str(st, "shutter_mode"), "shutter_mode");
            Eq("HighCapacity", J.Str(st, "adc_quality"), "adc_quality");
            Eq(4.0, J.Num(st, "adc_mhz"), "adc_mhz");
            var roi = J.Dict(st, "roi");
            Check(J.Str(roi, "mode") == "custom" && J.Long(roi, "y") == 40 && J.Long(roi, "height") == 20 && J.Long(roi, "x_bin") == 1 && J.Long(roi, "y_bin") == 20,
                  "custom roi from flat x/width/y/height, binned to one row by default: " + Json.Serialize(roi));
            Check(!J.Bool(st, "attach_increment", true) && J.Str(st, "base_name") == "Cfg_", "file merged");
            st = (Dictionary<string, object>)Exec(ccd, "configure", J.Obj("trigger", J.Obj("edge", "RisingEdge")));
            Check(J.Str(st, "trigger_response") == "ShiftPerTrigger" && J.Str(st, "trigger_edge") == "RisingEdge", "trigger merges with the current value");
            st = (Dictionary<string, object>)Exec(ccd, "configure", J.Obj("roi", J.Obj("mode", "line", "rows", 30), "timestamps", "None"));
            var line = J.Dict(st, "roi");
            Check(J.Str(line, "mode") == "line" && J.Long(line, "rows") == 30, "line roi reads back as {mode:line, rows}: " + Json.Serialize(line));
            Eq("None", J.Str(st, "timestamps"), "timestamps None");
            Eq(250.0, J.Num(st, "exposure_ms"), "exposure kept");

            // Refused, and nothing applied: a valid exposure rides along each time and must not stick.
            var bad = new (string what, Dictionary<string, object> args)[]
            {
                ("output_signal LightField does not have", J.Obj("output_signal", "EffectiveExposure")),
                ("misspelt timestamps", J.Obj("timestamps", "ExposureStart")),
                ("timestamps None combined", J.Obj("timestamps", "None,ExposureEnded")),
                ("unknown trigger edge", J.Obj("trigger", J.Obj("edge", "Sideways"))),
                ("unknown trigger key", J.Obj("trigger", J.Obj("respons", "NoResponse"))),
                ("custom roi without y/height", J.Obj("roi", J.Obj("mode", "custom", "x", 0, "width", 1340))),
                ("roi without mode", J.Obj("roi", J.Obj("rows", 10))),
                ("roi outside the sensor", J.Obj("roi", J.Obj("mode", "custom", "x", 1000, "width", 400, "y", 0, "height", 10))),
                ("unknown file key", J.Obj("file", J.Obj("base_name", "x", "dir", "D:\\"))),
                ("frame_tracking not a bool", J.Obj("frame_tracking", "yes")),
                ("trigger not an object", J.Obj("trigger", "ReadoutPerTrigger")),
                ("unknown top-level key", J.Obj("exposure", 10.0)),
            };
            foreach (var (what, args) in bad)
            {
                args["exposure_ms"] = 999.0;
                Eq("invalid_args", ExpectError(() => Exec(ccd, "configure", args)), what);
                Eq(250.0, J.Num((Dictionary<string, object>)Exec(ccd, "status"), "exposure_ms"), "nothing applied after: " + what);
            }
        }

        private static void TestSafeSparesManual()
        {
            var host = new TestHost { StateDir = Path.Combine(_tmp, "safe") };
            Directory.CreateDirectory(host.StateDir);
            var ccd = new SimSpectrometer(Dc("ccd", "lightfield", "readout_ms", 5.0), host);
            ccd.Start();
            // The operator's acquisition in the LightField window: safe (MAST shutting down) leaves it alone.
            var manual = new Thread(() => ccd.SimulateManualAcquisition(20, 40.0)) { IsBackground = true };
            manual.Start();
            WaitUntil(() => J.Bool(ccd.Status(false), "manual_acquisition", false), 3, "manual acquisition running");
            var r = (Dictionary<string, object>)Exec(ccd, "safe", J.Obj("reason", "MAST 关停"));
            Eq(0, J.List(r, "stopped").Count, "safe stops nothing the hub did not start");
            Check(manual.Join(TimeSpan.FromSeconds(10)), "manual acquisition ended");
            Dictionary<string, object> done;
            lock (host.Events) done = (Dictionary<string, object>)host.Events.Last(e => e.kind == "acq_completed").data;
            Check(J.Bool(done, "manual", false) && !J.Bool(done, "aborted", true) && J.Long(done, "frames") == 20,
                  "the manual acquisition ran to the end: " + Json.Serialize(done));
            // A hub acquisition is stopped by safe, and its result says why.
            string code = null, msg = null;
            var hubAcq = new Thread(() =>
            {
                try { Exec(ccd, "acquire", J.Obj("exposure_ms", 50.0, "frames", 100, "save", false)); }
                catch (DeviceError e) { code = e.Code; msg = e.Message; }
            }) { IsBackground = true };
            hubAcq.Start();
            WaitUntil(() => ccd.HubActivityActive, 3, "hub acquisition running");
            r = (Dictionary<string, object>)Exec(ccd, "safe", J.Obj("reason", "MAST 关停"));
            Eq("ccd", J.List(r, "stopped").FirstOrDefault(), "safe stopped the hub acquisition");
            Check(hubAcq.Join(TimeSpan.FromSeconds(10)), "hub acquisition ended");
            Check(code == "cancelled" && (msg ?? "").Contains("MAST 关停"), $"hub acquisition cancelled with the reason: {code} {msg}");
            // Under operator hold safe is admitted (it is a subset of stop).
            ccd.SetOperatorHold(true);
            Eq(0, J.List((Dictionary<string, object>)Exec(ccd, "safe", J.Obj("reason", "t")), "stopped").Count, "safe admitted under hold");
            ccd.SetOperatorHold(false);
            ccd.Shutdown();
        }

        private static void TestAxisStateCmdId()
        {
            var host = new TestHost { StateDir = Path.Combine(_tmp, "axis") };
            Directory.CreateDirectory(host.StateDir);
            List<Dictionary<string, object>> Axis(int from)
            {
                lock (host.Events) return host.Events.Skip(from).Where(e => e.kind == "axis_state").Select(e => (Dictionary<string, object>)e.data).ToList();
            }
            int Mark() { lock (host.Events) return host.Events.Count; }
            object Run(DeviceBase d, CommandContext ctx)
            {
                var refusal = d.Admit(ctx.Action);
                if (refusal != null) throw refusal;
                return d.Execute(ctx);
            }

            var pol = new SimRotationStage(Dc("pol", "kinesis_kdc101", "speed_deg_s", 100.0, "sim_start_homed", true), host);
            pol.Start();
            int m = Mark();
            var id = Guid.NewGuid().ToString("N");
            Run(pol, new CommandContext(id, "pol", "move_rel", J.Obj("deg", 20.0), 10));
            var a = Axis(m);
            var moving = a.Where(d => J.Bool(d, "moving", false)).ToList();
            Check(moving.Count > 0 && moving.All(d => J.Str(d, "cmd_id") == id), "pol: axis_state names the hub command while it moves");
            Check(!J.Bool(a.Last(), "moving", true) && !J.Has(a.Last(), "cmd_id"), "pol: right after the motion, moving=false without cmd_id: " + Json.Serialize(a.Last()));
            // A motion that did not come from the hub (e.g. the satellite's own window) names no command.
            m = Mark();
            Run(pol, new CommandContext(Guid.NewGuid().ToString("N"), "pol", "move_rel", J.Obj("deg", -20.0), 10, fromHub: false));
            Check(Axis(m).Count > 0 && Axis(m).All(d => !J.Has(d, "cmd_id")), "pol: no cmd_id for a motion the hub did not send");
            pol.Shutdown();

            var pico = new SimOpenLoopStepper(Dc("pico", "picomotor8742", "steps_per_s", 2000.0), host);
            pico.Start();
            m = Mark();
            var jid = Guid.NewGuid().ToString("N");
            Run(pico, new CommandContext(jid, "pico", "jog", J.Obj("axis", 1, "steps", 400), 10));
            var p = Axis(m);
            bool PicoMoving(Dictionary<string, object> d) => J.Bool(J.Dict(J.Dict(d, "axes"), "1"), "moving", false);
            var pm = p.Where(PicoMoving).ToList();
            Check(pm.Count > 0 && pm.All(d => J.Str(d, "cmd_id") == jid), "pico: axis_state names the jog while it runs");
            Check(!PicoMoving(p.Last()) && !J.Has(p.Last(), "cmd_id"), "pico: the report after the jog: not moving, no cmd_id");
            pico.Shutdown();
        }

        private static void TestManualActions()
        {
            var host = new TestHost { StateDir = Path.Combine(_tmp, "manual") };
            Directory.CreateDirectory(host.StateDir);
            Dictionary<string, object> Ax(Dictionary<string, object> s, int a) => J.Dict(J.Dict(s, "axes"), a.ToString(CultureInfo.InvariantCulture));

            // rotation stage: set_velocity (read back into the status), identify, limits, busy, hold
            var pol = new SimRotationStage(Dc("pol", "kinesis_kdc101", "speed_deg_s", 10.0, "sim_start_homed", true), host);
            pol.Start();
            Check(new[] { "set_velocity", "identify" }.All(pol.Caps.Contains), "rotation caps list set_velocity and identify");
            var s = (Dictionary<string, object>)Exec(pol, "set_velocity", J.Obj("deg_s", 20.0, "accel_deg_s2", 15.0));
            Check(J.Num(s, "velocity_deg_s") == 20 && J.Num(s, "accel_deg_s2") == 15, "set_velocity returns the new values: " + Json.Serialize(s));
            s = (Dictionary<string, object>)Exec(pol, "set_velocity", J.Obj("deg_s", 12.0));
            Check(J.Num(s, "velocity_deg_s") == 12 && J.Num(s, "accel_deg_s2") == 15, "accel kept when not given");
            Eq("invalid_args", ExpectError(() => Exec(pol, "set_velocity", J.Obj("deg_s", 30.0))), "velocity above max_velocity_deg_s");
            Eq("invalid_args", ExpectError(() => Exec(pol, "set_velocity", J.Obj("deg_s", 5.0, "accel_deg_s2", 0.0))), "acceleration 0");
            Eq("invalid_args", ExpectError(() => Exec(pol, "set_velocity", J.Obj())), "deg_s missing");
            Check(J.Has((Dictionary<string, object>)Exec(pol, "identify"), "position_deg"), "identify returns the status");
            var mover = new Thread(() => { try { Exec(pol, "move_rel", J.Obj("deg", 6.0)); } catch { } }) { IsBackground = true };
            mover.Start();
            WaitUntil(() => pol.ActiveOperation != null, 3, "pol moving");
            Eq("device_busy", ExpectError(() => Exec(pol, "set_velocity", J.Obj("deg_s", 5.0))), "no velocity change during a move");
            mover.Join(TimeSpan.FromSeconds(10));
            pol.SetOperatorHold(true);
            Eq("operator_hold", ExpectError(() => Exec(pol, "set_velocity", J.Obj("deg_s", 5.0))), "hold refuses set_velocity");
            Eq("operator_hold", ExpectError(() => Exec(pol, "identify")), "hold refuses identify");
            pol.SetOperatorHold(false);
            pol.Shutdown();

            // open-loop stepper: move_abs to a count since set_zero, velocity / acceleration, abort without deceleration
            var pico = new SimOpenLoopStepper(Dc("pico", "picomotor8742", "steps_per_s", 2000.0, "max_steps_per_cmd", 500), host);
            pico.Start();
            Check(new[] { "move_abs", "set_velocity", "set_accel", "abort" }.All(pico.Caps.Contains), "stepper caps list the new actions");
            Exec(pico, "set_zero", J.Obj("axis", 1));
            Exec(pico, "jog", J.Obj("axis", 1, "steps", 100));
            s = (Dictionary<string, object>)Exec(pico, "move_abs", J.Obj("axis", 1, "steps", 40));
            Eq(40L, J.Long(Ax(s, 1), "steps"), "move_abs goes to the count since set_zero");
            s = (Dictionary<string, object>)Exec(pico, "move_abs", J.Obj("axis", 1, "steps", 40));
            Eq(40L, J.Long(Ax(s, 1), "steps"), "move_abs to where it is: nothing to do");
            Eq("limit", ExpectError(() => Exec(pico, "move_abs", J.Obj("axis", 1, "steps", 600))), "move_abs farther than max_steps_per_cmd");
            s = (Dictionary<string, object>)Exec(pico, "set_velocity", J.Obj("axis", 2, "steps_s", 800));
            Eq(800L, J.Long(Ax(s, 2), "velocity_steps_s"), "set_velocity read back");
            s = (Dictionary<string, object>)Exec(pico, "set_accel", J.Obj("axis", 2, "steps_s2", 5000));
            Eq(5000L, J.Long(Ax(s, 2), "accel_steps_s2"), "set_accel read back");
            Eq("invalid_args", ExpectError(() => Exec(pico, "set_velocity", J.Obj("axis", 2, "steps_s", 2001))), "velocity above max_velocity_steps_s");
            Eq("invalid_args", ExpectError(() => Exec(pico, "set_accel", J.Obj("axis", 2, "steps_s2", 0))), "acceleration 0");
            Eq("invalid_args", ExpectError(() => Exec(pico, "set_velocity", J.Obj("axis", 2, "steps_s", 10.5))), "velocity must be an integer");
            // abort: admitted while a jog runs, stops at once, the jog ends as cancelled
            Exec(pico, "set_velocity", J.Obj("axis", 3, "steps_s", 100));
            string code = null;
            var jog = new Thread(() => { try { Exec(pico, "jog", J.Obj("axis", 3, "steps", 500)); } catch (DeviceError e) { code = e.Code; } }) { IsBackground = true };
            jog.Start();   // 5 s at 100 steps/s
            WaitUntil(() => J.Bool(Ax(pico.Status(false), 3), "moving", false), 3, "axis 3 moving");
            Thread.Sleep(500);
            var midway = J.Long(Ax(pico.Status(false), 3), "steps");
            Check(midway >= 20 && midway <= 100, $"the jog runs at the set velocity: {midway} steps after 0.5 s at 100 steps/s");
            var sw = Stopwatch.StartNew();
            s = (Dictionary<string, object>)Exec(pico, "abort");
            Check(jog.Join(TimeSpan.FromSeconds(3)) && sw.ElapsedMilliseconds < 1500, "abort stops the jog at once (" + sw.ElapsedMilliseconds + " ms)");
            Eq("cancelled", code, "the aborted jog ends as cancelled");
            Check(!J.Bool(Ax(s, 3), "moving", true), "abort returns the stopped state");
            pico.SetOperatorHold(true);
            Eq("operator_hold", ExpectError(() => Exec(pico, "set_velocity", J.Obj("axis", 1, "steps_s", 100))), "hold refuses set_velocity");
            Eq("operator_hold", ExpectError(() => Exec(pico, "move_abs", J.Obj("axis", 1, "steps", 0))), "hold refuses move_abs");
            pico.SetOperatorHold(false);
            // Under operator hold abort is admitted (it only stops, like stop) and ends a running jog as
            // cancelled - here a jog the hub did not send (fromHub false), which the hold itself leaves running.
            Exec(pico, "set_velocity", J.Obj("axis", 4, "steps_s", 100));
            pico.SetOperatorHold(true);
            string holdJogCode = null;
            var localJog = new Thread(() =>
            {
                try { pico.Execute(new CommandContext(Guid.NewGuid().ToString("N"), "pico", "jog", J.Obj("axis", 4, "steps", 500), 30, fromHub: false)); }
                catch (DeviceError e) { holdJogCode = e.Code; }
            }) { IsBackground = true };
            localJog.Start();   // 5 s at 100 steps/s
            WaitUntil(() => J.Bool(Ax(pico.Status(false), 4), "moving", false), 3, "jog on axis 4 moving under hold");
            var held = (Dictionary<string, object>)Exec(pico, "abort");
            Check(localJog.Join(TimeSpan.FromSeconds(3)), "abort under hold stops the jog at once");
            Eq("cancelled", holdJogCode, "abort under operator_hold ends the jog as cancelled");
            Check(!J.Bool(Ax(held, 4), "moving", true) && pico.OperatorHold, "abort under hold returns the stopped state, hold stays on");
            pico.SetOperatorHold(false);
            pico.Shutdown();

            // Real drivers list only what they can do from documentation (no hardware touched: Start is not called).
            var usbPico = new Picomotor8742Device(SatelliteConfig.FromJson(J.Obj("devices", new List<object> { J.Obj("id", "p1", "driver", "picomotor8742", "transport", "usb") })).Devices[0], host);
            Check(usbPico.Caps.Contains("move_abs") && !usbPico.Caps.Contains("set_velocity") && !usbPico.Caps.Contains("set_accel") && !usbPico.Caps.Contains("abort"),
                  "8742 over USB: no set_velocity/set_accel/abort until VA/AC/AB are checked: " + string.Join(",", usbPico.Caps));
            var cmdlibPico = new Picomotor8742Device(SatelliteConfig.FromJson(J.Obj("devices", new List<object> { J.Obj("id", "p2", "driver", "picomotor8742", "transport", "cmdlib") })).Devices[0], host);
            Check(new[] { "move_abs", "set_velocity", "set_accel", "abort" }.All(cmdlibPico.Caps.Contains), "8742 over CmdLib8742: the documented typed calls are listed");
            var kdc = new KinesisKdc101Device(SatelliteConfig.FromJson(J.Obj("devices", new List<object> { J.Obj("id", "k1", "driver", "kinesis_kdc101", "serial", "123") })).Devices[0], host);
            Check(kdc.Caps.Contains("set_velocity") && kdc.Caps.Contains("identify"), "KDC101 lists set_velocity and identify (CC_SetVelParams, CC_Identify)");
        }

        private static void WaitUntil(Func<bool> cond, double timeoutS, string what)
        {
            var end = DateTime.UtcNow.AddSeconds(timeoutS);
            while (DateTime.UtcNow < end)
            {
                if (cond()) return;
                Thread.Sleep(20);
            }
            Check(cond(), "timed out: " + what);
        }

        private static void TestXyzCoordinates()
        {
            var host = new TestHost { StateDir = Path.Combine(_tmp, "xyz") };
            Directory.CreateDirectory(host.StateDir);
            string header = MotionManipulatorDevice.CoordHeader + "\r\n";
            string Row(DateTime t, string ev, double x, double y, double z) => string.Format(CultureInfo.InvariantCulture,
                "{0:HH:mm:ss},{1},{2:0.00},{3:0.00},{4:0.00},0.00,0.00,0,N,NaN,{2:0.00},{3:0.00},{4:0.00},0.00,0.00,0,\r\n", t, ev, x, y, z);
            string Name(DateTime t) => "Coordinates_" + t.ToString("yyyyMMddHHmmss", CultureInfo.InvariantCulture) + ".csv";
            DeviceConfig Cfg(string dir) => SatelliteConfig.FromJson(J.Obj("devices", new List<object>
                { J.Obj("id", "xyz", "driver", "motion_manipulator", "coordinates_dir", dir, "coordinates_poll_s", 0.05) })).Devices[0];
            int Count(string kind) { lock (host.Events) return host.Events.Count(e => e.kind == kind); }

            // 1. The time column has no date: a row after midnight belongs to the next day. Backlog emits nothing.
            var d1 = Path.Combine(_tmp, "coords-midnight");
            Directory.CreateDirectory(d1);
            var t1 = new DateTime(2026, 1, 1, 23, 59, 50, DateTimeKind.Local);
            File.WriteAllText(Path.Combine(d1, Name(t1)), header + Row(t1, "BeginMotion", 1, 2, 3) + Row(t1.AddSeconds(15), "EndMotion", 1.5, 2, 3), Encoding.ASCII);
            var a = new MotionManipulatorDevice(Cfg(d1), host);
            a.Start();
            var s = (Dictionary<string, object>)Exec(a, "status");
            Eq(1.5, J.Num(s, "x_mm"), "x from the last row");
            Eq(Clock.UnixMsFromDateTime(new DateTime(2026, 1, 2, 0, 0, 5, DateTimeKind.Local)), J.Long(s, "position_at_ms"), "row after midnight dated the next day");
            Eq(false, J.Get(s, "moving"), "EndMotion: idle");
            Eq(0, Count("axis_state") + Count("manual_activity"), "the backlog read at start emits no events");
            Eq("not_supported", ExpectError(() => Exec(a, "move_rel", J.Obj("dx", 0.1))), "the satellite does not move this stage");
            a.Shutdown();

            // 2. Live rows: half a line waits; BeginMotion -> moving and stop refuses; -0.00; Configured; a newer session file wins.
            var d2 = Path.Combine(_tmp, "coords-live");
            Directory.CreateDirectory(d2);
            var t2 = DateTime.Now.AddSeconds(-20);
            var f2 = Path.Combine(d2, Name(t2));
            File.WriteAllText(f2, header + Row(t2, "EndMotion", 7.27, 0, 9), Encoding.ASCII);
            var b = new MotionManipulatorDevice(Cfg(d2), host);
            b.Start();
            Eq(DeviceBase.StateReady, b.State, "read-only xyz is ready");
            Check(Exec(b, "stop") != null, "stop is fine while the CSV shows the stage idle");
            int axis0 = Count("axis_state"), man0 = Count("manual_activity");
            var begin = Row(t2.AddSeconds(5), "BeginMotion", 7.27, 0, 9);
            File.AppendAllText(f2, begin.Substring(0, 20), Encoding.ASCII);
            Thread.Sleep(300);
            Eq("EndMotion", J.Str(b.Status(false), "position_event"), "half a line is not parsed");
            File.AppendAllText(f2, begin.Substring(20), Encoding.ASCII);
            WaitUntil(() => J.Str(b.Status(false), "position_event") == "BeginMotion", 3, "BeginMotion seen");
            Eq(true, J.Get(b.Status(false), "moving"), "BeginMotion: moving");
            Check(Count("manual_activity") > man0 && Count("axis_state") > axis0, "manual_activity and axis_state for a live motion");
            Eq("not_supported", ExpectError(() => Exec(b, "stop")), "stop does not claim success while the stage may move");
            Check(ExpectThrows(() => b.EmergencyStop("selftest")), "E-STOP reports an error for this stage instead of 'stopped'");
            File.AppendAllText(f2, Row(t2.AddSeconds(8), "EndMotion", -0.001, 0.02, 9), Encoding.ASCII);
            WaitUntil(() => J.Str(b.Status(false), "position_event") == "EndMotion", 3, "EndMotion seen");
            var s2 = b.Status(false);
            Check(J.Num(s2, "x_mm") == 0 && Equals(J.Get(s2, "moving"), false), "-0.00 reads as 0 and the stage is idle again: " + Json.Serialize(s2));
            File.AppendAllText(f2, Row(t2.AddSeconds(10), "Configured", 10.22, 0, 9), Encoding.ASCII);
            WaitUntil(() => J.Str(b.Status(false), "position_event") == "Configured", 3, "Configured seen");
            lock (host.Events)
                Check(host.Events.Any(e => e.kind == "manual_activity" && J.Str((Dictionary<string, object>)e.data, "what") == "xyz_coordinates_redefined"),
                      "Configured is reported as a coordinate redefinition, not a motion");
            var t3 = t2.AddSeconds(15);
            File.WriteAllText(Path.Combine(d2, Name(t3)), header + Row(t3, "EndMotion", 3, 3, 3), Encoding.ASCII);
            WaitUntil(() => J.Num(b.Status(false), "x_mm", double.NaN) == 3, 3, "switched to the newer session file");
            b.Shutdown();

            // 3. A BeginMotion that never ended long ago: moving is unknown, not true forever.
            var d3 = Path.Combine(_tmp, "coords-stale");
            Directory.CreateDirectory(d3);
            var t4 = DateTime.Now.AddMinutes(-10);
            File.WriteAllText(Path.Combine(d3, Name(t4)), header + Row(t4, "BeginMotion", 1, 1, 1), Encoding.ASCII);
            var c = new MotionManipulatorDevice(Cfg(d3), host);
            c.Start();
            Check(J.Get(c.Status(false), "moving") == null, "stale BeginMotion: moving unknown");
            c.Shutdown();

            // 4. A header that differs from the known one: nothing reported, the problem is.
            var d4 = Path.Combine(_tmp, "coords-format");
            Directory.CreateDirectory(d4);
            File.WriteAllText(Path.Combine(d4, Name(t4)), "Time,State,X,Y,Z\r\n" + Row(t4, "EndMotion", 1, 1, 1), Encoding.ASCII);
            var e4 = new MotionManipulatorDevice(Cfg(d4), host);
            e4.Start();
            var s4 = e4.Status(false);
            Check(J.Get(s4, "x_mm") == null && J.Str(s4, "coordinates_problem") != null && e4.State == DeviceBase.StateError, "unknown header: no position, problem reported");
            e4.Shutdown();

            // 5. Real sample files copied from the instrument PC (if present in the repository).
            var real = FindUp(Path.Combine("local-samples", "team", "spectrasense-hw", "raw", "manip"));
            var sample = real != null ? Path.Combine(real, "Coordinates_20260630165125.csv") : null;
            if (sample == null || !File.Exists(sample)) return;
            var d5 = Path.Combine(_tmp, "coords-real");
            Directory.CreateDirectory(d5);
            File.Copy(sample, Path.Combine(d5, Path.GetFileName(sample)));
            var r5 = new MotionManipulatorDevice(Cfg(d5), host);
            r5.Start();
            var s5 = r5.Status(false);
            Check(J.Long(s5, "rows") == 1007 && J.Long(s5, "bad_rows") == 0, "all 1007 rows of the rig1 file parse: " + Json.Serialize(s5));
            Check(J.Num(s5, "x_mm") == -1.05 && J.Num(s5, "y_mm") == 0.02 && J.Num(s5, "z_mm") == 0.05 && J.Str(s5, "position_event") == "EndMotion",
                  "last row of the rig1 file: " + Json.Serialize(s5));
            Eq(Clock.UnixMsFromDateTime(new DateTime(2026, 6, 30, 22, 24, 7, DateTimeKind.Local)), J.Long(s5, "position_at_ms"), "rig1 file: last row time");
            r5.Shutdown();
        }

        private static bool ExpectThrows(Action a)
        {
            try { a(); return false; }
            catch (Exception) { return true; }
        }

        // ------------------------------------------------------------------ protocol tests against FakeHub

        private static (SatelliteCore core, FakeHub hub) StartPair(string name, Action<FakeHub> setupHub = null, params Dictionary<string, object>[] devices)
        {
            var hub = new FakeHub();
            setupHub?.Invoke(hub);
            var state = Path.Combine(_tmp, name);
            Directory.CreateDirectory(state);
            var devs = devices.Length > 0 ? devices.Cast<object>().ToList() : new List<object>
            {
                J.Obj("id", "ccd", "driver", "lightfield", "sim", true, "readout_ms", 5.0),
                J.Obj("id", "pol", "driver", "kinesis_kdc101", "sim", true, "speed_deg_s", 30.0, "sim_start_homed", true),
                J.Obj("id", "pico", "driver", "picomotor8742", "sim", true),
            };
            var cfg = SatelliteConfig.FromJson(J.Obj(
                "satellite_id", "selftest",
                "hub", J.Obj("url", hub.Url, "backoff_max_s", 1.0),
                "state_dir", state,
                "upload", J.Obj("max_bytes_per_s", 400000, "chunk_bytes", 8192),
                "devices", devs));
            var errs = cfg.Validate(allowPlainLoopback: true);
            Check(errs.Count == 0, "config: " + string.Join("; ", errs));
            var core = new SatelliteCore(cfg, hub.ExpectedToken);
            core.Start();
            return (core, hub);
        }

        private static Dictionary<string, object> Must(Dictionary<string, object> m, string what)
        {
            Check(m != null, "timed out waiting for " + what);
            return m;
        }

        private static Func<Dictionary<string, object>, bool> Msg(string t, string id = null)
        {
            return m => J.Str(m, "t") == t && (id == null || J.Str(m, "id") == id);
        }

        private static void Cmd(FakeHub hub, string id, string device, string action, Dictionary<string, object> args = null, double timeout = 30)
        {
            hub.Send(J.Obj("t", "cmd", "id", id, "device", device, "action", action, "args", args ?? J.Obj(), "timeout_s", timeout));
        }

        private static void TestProtocol()
        {
            var (core, hub) = StartPair("proto");
            try
            {
                var hello = Must(hub.WaitFor(Msg("hello"), 10), "hello");
                Eq(1L, J.Long(hello, "proto"), "hello.proto");
                Eq("selftest", J.Str(hello, "satellite_id"), "hello.satellite_id");
                Eq(core.BootId, J.Str(hello, "boot_id"), "hello.boot_id");
                Eq(3, J.List(hello, "devices").Count, "hello.devices");
                Check(J.Has(hello, "evt_seq") && J.Has(hello, "time_ms") && J.Has(hello, "sw"), "hello fields");
                Eq("Bearer " + hub.ExpectedToken, hub.LastRequestHeaders["Authorization"], "Authorization header");
                Eq("selftest", hub.LastRequestHeaders["X-MAST-Satellite-Id"], "X-MAST-Satellite-Id");
                Eq("1", hub.LastRequestHeaders["X-MAST-Satellite-Proto"], "X-MAST-Satellite-Proto");

                hub.StartPinging(300);
                var pong = Must(hub.WaitFor(Msg("pong"), 5), "pong");
                Check(J.Long(pong, "t1") <= J.Long(pong, "t2") && J.Has(pong, "t0") && J.Has(pong, "n"), "pong fields t0/t1/t2");
                Must(hub.WaitFor(m => J.Str(m, "t") == "evt" && J.Str(m, "kind") == "device_state" && J.Str(m, "device") == "ccd", 10), "device_state for ccd");

                // acquire -> accepted, frames, result
                int mark = hub.Mark();
                Cmd(hub, "cmd-1", "ccd", "acquire", J.Obj("exposure_ms", 30.0, "frames", 2, "save", false));
                var ack = Must(hub.WaitFor(Msg("ack", "cmd-1"), 5, mark), "ack");
                Eq("accepted", J.Str(ack, "state"), "ack.state");
                var res = Must(hub.WaitFor(Msg("result", "cmd-1"), 10, mark), "result");
                Check(J.Bool(res, "ok"), "acquire ok: " + Json.Serialize(res));
                Eq(2L, J.Long(J.Dict(res, "value"), "frames"), "result.value.frames");
                Check(J.Has(res, "started_ms") && J.Has(res, "finished_ms"), "result timestamps");
                var frames = hub.Received().Skip(mark).Where(m => J.Str(m, "t") == "evt" && J.Str(m, "kind") == "spectrum").ToList();
                Eq(2, frames.Count, "spectrum events");
                Eq("cmd-1", J.Str(J.Dict(frames[0], "data"), "cmd_id"), "frame carries cmd_id");
                var seqs = hub.Received().Where(m => J.Str(m, "t") == "evt").Select(m => J.Long(m, "seq")).ToList();
                Check(seqs.Zip(seqs.Skip(1), (x, y) => y == x + 1).All(b => b), "event seq contiguous");

                // duplicate after done: duplicate_done + the cached result
                mark = hub.Mark();
                Cmd(hub, "cmd-1", "ccd", "acquire", J.Obj("frames", 2, "save", false));
                Eq("duplicate_done", J.Str(Must(hub.WaitFor(Msg("ack", "cmd-1"), 5, mark), "dup ack"), "state"), "dup ack state");
                Must(hub.WaitFor(Msg("result", "cmd-1"), 5, mark), "cached result resent");

                // duplicate while running, then cancel
                mark = hub.Mark();
                Cmd(hub, "cmd-2", "ccd", "acquire", J.Obj("exposure_ms", 1000.0, "frames", 20, "save", false));
                Must(hub.WaitFor(Msg("ack", "cmd-2"), 5, mark), "ack 2");
                Cmd(hub, "cmd-2", "ccd", "acquire", J.Obj());
                Must(hub.WaitFor(m => Msg("ack", "cmd-2")(m) && J.Str(m, "state") == "duplicate_running", 5, mark), "duplicate_running");
                hub.Send(J.Obj("t", "cmd_status", "ids", new List<object> { "cmd-1", "cmd-2", "nope" }));
                var cs = Must(hub.WaitFor(Msg("cmd_status_reply"), 5, mark), "cmd_status_reply");
                var items = J.List(cs, "items").Cast<Dictionary<string, object>>().ToDictionary(i => J.Str(i, "id"));
                Eq("done", J.Str(items["cmd-1"], "state"), "status cmd-1");
                Eq("running", J.Str(items["cmd-2"], "state"), "status cmd-2");
                Eq("unknown", J.Str(items["nope"], "state"), "status unknown");
                hub.Send(J.Obj("t", "cancel", "id", "cmd-2"));
                var r2 = Must(hub.WaitFor(Msg("result", "cmd-2"), 5, mark), "cancelled result");
                Check(!J.Bool(r2, "ok") && J.Str(J.Dict(r2, "error"), "code") == "cancelled", "cancel -> cancelled: " + Json.Serialize(r2));

                // unknown device / unsupported action -> rejected + result
                mark = hub.Mark();
                Cmd(hub, "cmd-3", "nosuch", "status");
                var rejAck = Must(hub.WaitFor(Msg("ack", "cmd-3"), 5, mark), "reject ack");
                Eq("rejected", J.Str(rejAck, "state"), "unknown device rejected");
                Eq("invalid_args", J.Str(rejAck, "reason"), "rejected ack.reason is the bare error code (the hub reads the code from it)");
                Eq("invalid_args", J.Str(J.Dict(Must(hub.WaitFor(Msg("result", "cmd-3"), 5, mark), "reject result"), "error"), "code"), "unknown device code");
                Cmd(hub, "cmd-4", "pol", "acquire");
                Eq("not_supported", J.Str(J.Dict(Must(hub.WaitFor(Msg("result", "cmd-4"), 5, mark), "ns result"), "error"), "code"), "unsupported action");

                // operator hold: refuses motion, admits stop
                core.SetOperatorHold("pol", true);
                mark = hub.Mark();
                Cmd(hub, "cmd-5", "pol", "move_rel", J.Obj("deg", 1.0));
                Eq("operator_hold", J.Str(J.Dict(Must(hub.WaitFor(Msg("result", "cmd-5"), 5, mark), "hold result"), "error"), "code"), "hold code");
                Cmd(hub, "cmd-6", "pol", "stop");
                var r6 = Must(hub.WaitFor(Msg("result", "cmd-6"), 5, mark), "stop under hold");
                Check(J.Bool(r6, "ok") && J.Bool(J.Dict(r6, "value"), "operator_hold"), "stop allowed under hold and returns the device status");
                Cmd(hub, "cmd-6s", "pol", "safe", J.Obj("reason", "selftest"));
                var r6s = Must(hub.WaitFor(Msg("result", "cmd-6s"), 5, mark), "safe under hold");
                Check(J.Bool(r6s, "ok") && J.List(J.Dict(r6s, "value"), "stopped").Count == 0, "safe admitted under hold: " + Json.Serialize(r6s));
                Must(hub.WaitFor(m => J.Str(m, "kind") == "device_state" && J.Str(m, "device") == "pol" && J.Bool(J.Dict(m, "data"), "operator_hold"), 5, mark), "device_state.operator_hold");
                core.SetOperatorHold("pol", false);

                // timeout
                mark = hub.Mark();
                Cmd(hub, "cmd-7", "pol", "move_rel", J.Obj("deg", 300.0), timeout: 0.5);
                var r7 = Must(hub.WaitFor(Msg("result", "cmd-7"), 5, mark), "timeout result");
                Eq("timeout", J.Str(J.Dict(r7, "error"), "code"), "timeout code");
            }
            finally
            {
                core.Stop();
                hub.Dispose();
            }
        }

        private static void TestReconnectResend()
        {
            var (core, hub) = StartPair("resend");
            try
            {
                Must(hub.WaitFor(Msg("hello"), 10), "hello");
                hub.StartPinging(300);
                hub.AutoAckEvents = false; // nothing acked: everything must come again
                Cmd(hub, "r-1", "ccd", "acquire", J.Obj("exposure_ms", 10.0, "frames", 3, "save", false));
                Must(hub.WaitFor(Msg("result", "r-1"), 10), "result");
                Thread.Sleep(200);
                long last = hub.Received().Where(m => J.Str(m, "t") == "evt").Max(m => J.Long(m, "seq"));
                Check(last >= 5, "some events");
                hub.WelcomeEvtAck = 2;
                int mark = hub.Mark();
                hub.Drop();
                var hello2 = Must(hub.WaitFor(Msg("hello"), 10, mark), "second hello");
                Eq(core.BootId, J.Str(hello2, "boot_id"), "same boot_id after reconnect");
                Check(J.Long(hello2, "evt_seq") >= last, "hello.evt_seq >= last seq");
                var firstResent = Must(hub.WaitFor(Msg("evt"), 10, mark), "resent event");
                Eq(3L, J.Long(firstResent, "seq"), "resend starts right after welcome.evt_ack");
                Thread.Sleep(500);
                var resent = hub.Received().Skip(mark).Where(m => J.Str(m, "t") == "evt").Select(m => J.Long(m, "seq")).ToList();
                Check(Enumerable.Range(3, (int)last - 2).All(s => resent.Contains(s)), "all unacked events resent");
                // same request id after reconnect: still idempotent
                Cmd(hub, "r-1", "ccd", "acquire", J.Obj());
                Eq("duplicate_done", J.Str(Must(hub.WaitFor(Msg("ack", "r-1"), 5, mark), "dup ack"), "state"), "idempotent across reconnects");
            }
            finally
            {
                core.Stop();
                hub.Dispose();
            }
        }

        private static void TestDeadman()
        {
            var (core, hub) = StartPair("deadman", h => h.DeadmanS = 1.0);
            try
            {
                Must(hub.WaitFor(Msg("hello"), 10), "hello");
                hub.StartPinging(200);
                Thread.Sleep(500);
                int mark = hub.Mark();
                Cmd(hub, "d-1", "pol", "move_rel", J.Obj("deg", 200.0), timeout: 60); // ~7 s at 30 deg/s
                Must(hub.WaitFor(Msg("ack", "d-1"), 5, mark), "ack");
                Thread.Sleep(500);
                hub.StopPinging();
                var r = Must(hub.WaitFor(Msg("result", "d-1"), 6, mark), "result after deadman");
                Check(!J.Bool(r, "ok") && J.Str(J.Dict(r, "error"), "code") == "cancelled" && (J.Str(J.Dict(r, "error"), "message") ?? "").Contains("deadman"), "deadman ends the move: " + Json.Serialize(r));
                var dm = Must(hub.WaitFor(m => J.Str(m, "kind") == "deadman", 3, mark), "deadman event");
                Check(J.List(J.Dict(dm, "data"), "stopped").Contains("pol"), "deadman stopped pol");
                Eq("", J.Str(dm, "device"), "satellite-level event has device \"\"");
            }
            finally
            {
                core.Stop();
                hub.Dispose();
            }
        }

        private static void TestEstop()
        {
            var (core, hub) = StartPair("estop");
            try
            {
                Must(hub.WaitFor(Msg("hello"), 10), "hello");
                hub.StartPinging(300);
                int mark = hub.Mark();
                Cmd(hub, "e-1", "ccd", "acquire", J.Obj("exposure_ms", 1000.0, "frames", 30, "save", false), timeout: 60);
                Cmd(hub, "e-2", "pol", "move_rel", J.Obj("deg", 200.0), timeout: 60);
                Must(hub.WaitFor(Msg("ack", "e-2"), 5, mark), "ack");
                Thread.Sleep(300);
                hub.Send(J.Obj("t", "estop", "reason", "selftest"));
                var ea = Must(hub.WaitFor(Msg("estop_ack"), 5, mark), "estop_ack");
                var stopped = J.List(ea, "stopped").Select(o => (string)o).ToList();
                Check(new[] { "ccd", "pol", "pico" }.All(stopped.Contains), "stopped lists every device, the idle pico included: " + Json.Serialize(ea));
                var active = (J.List(ea, "active") ?? new List<object>()).Select(o => (string)o).OrderBy(x => x, StringComparer.Ordinal).ToList();
                Eq("ccd,pol", string.Join(",", active), "active = the devices that were acquiring or moving");
                Eq(0, J.List(ea, "errors")?.Count ?? -1, "no errors");
                Check(!J.Bool(Must(hub.WaitFor(Msg("result", "e-1"), 5, mark), "e-1"), "ok"), "acquisition ended");
                Check(!J.Bool(Must(hub.WaitFor(Msg("result", "e-2"), 5, mark), "e-2"), "ok"), "move ended");
            }
            finally
            {
                core.Stop();
                hub.Dispose();
            }
        }

        private static void TestUploadResume()
        {
            var watch = Path.Combine(_tmp, "watch-root");
            Directory.CreateDirectory(Path.Combine(watch, "2026", "20261019"));
            var (core, hub) = StartPair("upload", h => { h.DropAfterChunks = 3; h.MaxUploadBytesPerS = 400000; },
                J.Obj("id", "spe", "driver", "spe_watcher", "root", watch, "backfill_hours", 24.0, "stable_s", 0.5, "scan_interval_s", 2.0));
            try
            {
                Must(hub.WaitFor(Msg("hello"), 10), "hello");
                hub.StartPinging(300);
                var file = Path.Combine(watch, "2026", "20261019", "Raman_ 1.spe");
                var frames = Enumerable.Range(0, 40).Select(f => Enumerable.Range(0, 1340).Select(i => (ushort)((i * 31 + f) & 0xFFFF)).ToArray()).ToList();
                SpeFile.WriteU16(file, frames, Enumerable.Range(0, 1340).Select(i => 600.0 + i * 0.05).ToArray(), 633, null, DateTimeOffset.Now);
                string sha = SpeWatcherDevice.Sha256File(file);
                var offer = Must(hub.WaitFor(Msg("file_offer"), 15), "file_offer");
                Eq(sha, J.Str(offer, "file_id"), "file_id is the sha256");
                Eq("2026/20261019/Raman_ 1.spe", J.Str(offer, "rel_path"), "rel_path with forward slashes");
                Eq("spe", J.Str(offer, "kind"), "kind");
                Eq(40L, J.Long(J.Dict(offer, "meta"), "frames"), "meta from the SPE footer");
                var done = Must(hub.WaitFor(m => J.Str(m, "t") == "file_end", 30), "file_end after resume");
                Thread.Sleep(300);
                byte[] got;
                lock (hub.Files) got = hub.Files[sha].ToArray();
                Check(got.SequenceEqual(File.ReadAllBytes(file)), "hub copy identical");
                Check(hub.Connections >= 2, "the link was dropped and re-established");
                long resumeAt;
                lock (hub.WantOffsets)
                {
                    Check(hub.WantOffsets.Count >= 2 && hub.WantOffsets.Last() > 0, "second offer resumed at offset " + string.Join(",", hub.WantOffsets));
                    resumeAt = hub.WantOffsets.Last();
                }
                lock (hub.ChunkOffsets)
                {
                    // After the reconnect the satellite must continue where the hub stopped, not resend from 0.
                    int lastConn = hub.ChunkLog.Max(c => c.conn);
                    var after = hub.ChunkLog.Where(c => c.conn == lastConn).Select(c => c.offset).ToList();
                    Check(after.Count > 0 && after[0] == resumeAt, $"first chunk after reconnect at {(after.Count > 0 ? after[0] : -1)}, resume offset {resumeAt}");
                    Check(after.All(o => o >= resumeAt), "no chunk below the resume offset after reconnect");
                }
                // manifest: no second upload of the same file after a restart
                var spe = (SpeWatcherDevice)core.Devices.First(d => d.Id == "spe");
                var deadline = DateTime.UtcNow.AddSeconds(5);
                while (DateTime.UtcNow < deadline && J.Long(spe.Status(false), "uploaded") < 1) Thread.Sleep(100);
                Eq(1L, J.Long(spe.Status(false), "uploaded"), "watcher counts the upload");
            }
            finally
            {
                core.Stop();
                hub.Dispose();
            }
            var (core2, hub2) = StartPair("upload", null,
                J.Obj("id", "spe", "driver", "spe_watcher", "root", watch, "backfill_hours", 24.0, "stable_s", 0.5, "scan_interval_s", 1.0));
            try
            {
                Must(hub2.WaitFor(Msg("hello"), 10), "hello after restart");
                hub2.StartPinging(300);
                Check(hub2.WaitFor(Msg("file_offer"), 4) == null, "manifest prevents a second upload after restart");
            }
            finally
            {
                core2.Stop();
                hub2.Dispose();
            }
        }

        /// <summary>A driver whose vendor call ignores cancellation (blocks 4 s no matter what).</summary>
        private sealed class HangDevice : DeviceBase
        {
            public HangDevice(DeviceConfig c, IDeviceHost h) : base(c, h, "pmt") { }
            public override IReadOnlyList<string> Caps => new[] { "status", "stop", "safe", "read" };
            public override void Start() { SetState(StateReady); }
            protected override Dictionary<string, object> BuildStatus(bool fresh) { return J.Obj(); }
            protected override object ExecuteAction(CommandContext ctx)
            {
                using (BeginExclusive(ctx, "read", hubMotion: true)) Thread.Sleep(4000);
                return J.Obj("late", true);
            }
            protected override void StopHardware(bool emergency, bool hubOnly) { }
        }

        private static void TestHungCommand()
        {
            var hub = new FakeHub();
            var state = Path.Combine(_tmp, "hung");
            var cfg = SatelliteConfig.FromJson(J.Obj("satellite_id", "selftest", "hub", J.Obj("url", hub.Url), "state_dir", state,
                "devices", new List<object> { J.Obj("id", "pmt", "driver", "test_hang") }));
            var old = SatelliteCore.HungGraceS;
            SatelliteCore.HungGraceS = 1.0;
            var core = new SatelliteCore(cfg, hub.ExpectedToken, (c, h) => new HangDevice(c, h));
            core.Start();
            try
            {
                Must(hub.WaitFor(Msg("hello"), 10), "hello");
                hub.StartPinging(300);
                int mark = hub.Mark();
                Cmd(hub, "h-1", "pmt", "read", J.Obj(), timeout: 0.5);
                var r = Must(hub.WaitFor(Msg("result", "h-1"), 4, mark), "result for the hung command");
                Check(!J.Bool(r, "ok") && J.Str(J.Dict(r, "error"), "code") == "timeout" && (J.Str(J.Dict(r, "error"), "message") ?? "").Contains("hung"),
                      "hung command reported as timeout: " + Json.Serialize(r));
                Eq(DeviceBase.StateError, core.Devices[0].State, "device marked error");
                Thread.Sleep(3500); // the vendor call returns now; its late result must not be sent
                Eq(1, hub.Received().Skip(mark).Count(m => Msg("result", "h-1")(m)), "exactly one result");
                hub.Send(J.Obj("t", "cmd_status", "ids", new List<object> { "h-1" }));
                var cs = Must(hub.WaitFor(Msg("cmd_status_reply"), 5, mark), "cmd_status_reply");
                Eq("done", J.Str((Dictionary<string, object>)J.List(cs, "items")[0], "state"), "journal says done");
            }
            finally
            {
                SatelliteCore.HungGraceS = old;
                core.Stop();
                hub.Dispose();
            }
        }

        private static void TestRejections()
        {
            var (core, hub) = StartPair("reject401", h => h.RejectWithHttp = 401);
            try
            {
                var end = DateTime.UtcNow.AddSeconds(3);
                while (DateTime.UtcNow < end && hub.HandshakeAttempts < 1) Thread.Sleep(50);
                Thread.Sleep(2500);
                Check(hub.HandshakeAttempts == 1, "401 must back off (60 s), attempts=" + hub.HandshakeAttempts);
                Eq(LinkState.Rejected, core.Link.State, "link state after 401");
            }
            finally
            {
                core.Stop();
                hub.Dispose();
            }
            var (core2, hub2) = StartPair("reject4406", h => h.CloseAfterHelloWith = 4406);
            try
            {
                var end = DateTime.UtcNow.AddSeconds(4);
                while (DateTime.UtcNow < end && core2.Link.State != LinkState.Rejected) Thread.Sleep(50);
                Eq(LinkState.Rejected, core2.Link.State, "link state after 4406");
                Thread.Sleep(1500);
                Check(hub2.HandshakeAttempts == 1, "4406 must back off, attempts=" + hub2.HandshakeAttempts);
            }
            finally
            {
                core2.Stop();
                hub2.Dispose();
            }
            // 503: the hub is shutting down - an outage, not a credential problem: plain backoff, not the 60 s auth wait.
            var (core5, hub5) = StartPair("hub503", h => h.RejectWithHttp = 503);
            try
            {
                var end = DateTime.UtcNow.AddSeconds(6);
                while (DateTime.UtcNow < end && hub5.HandshakeAttempts < 3) Thread.Sleep(50);
                Check(hub5.HandshakeAttempts >= 3, "503 is retried with the normal backoff, attempts=" + hub5.HandshakeAttempts);
                Check(core5.Link.State != LinkState.Rejected, "503 is not shown as rejected: " + core5.Link.State);
            }
            finally
            {
                core5.Stop();
                hub5.Dispose();
            }
            // 4409: the hub gave this id to a newer connection. Reconnecting at once would start a tug of war.
            var (core3, hub3) = StartPair("replaced4409");
            try
            {
                var end = DateTime.UtcNow.AddSeconds(5);
                while (DateTime.UtcNow < end && core3.Link.State != LinkState.Connected) Thread.Sleep(50);
                Eq(LinkState.Connected, core3.Link.State, "connected before 4409");
                int attempts = hub3.HandshakeAttempts;
                hub3.CloseWith(4409);
                end = DateTime.UtcNow.AddSeconds(3);
                while (DateTime.UtcNow < end && core3.Link.State == LinkState.Connected) Thread.Sleep(50);
                Eq(LinkState.Backoff, core3.Link.State, "link state while waiting after 4409");
                Thread.Sleep(2500);
                Check(hub3.HandshakeAttempts == attempts, $"4409 must wait auth_retry_s before reconnecting, attempts {attempts} -> {hub3.HandshakeAttempts}");
            }
            finally
            {
                core3.Stop();
                hub3.Dispose();
            }
        }
    }
}
