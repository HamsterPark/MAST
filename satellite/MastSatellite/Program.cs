using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net.Security;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using System.Text;
using System.Threading;
using System.Windows.Forms;
using MastSatellite.Core;
using MastSatellite.Protocol;
using MastSatellite.UI;

namespace MastSatellite
{
    internal static class Program
    {
        private const string Usage =
@"MastSatellite " + SatelliteCore.SoftwareVersion + @" - MAST satellite node

  MastSatellite.exe [--config <satellite.json>] [--headless] [--allow-plain-loopback]
      Run the satellite. Without --config: %ProgramData%\MAST-Satellite\satellite.json,
      else satellite.json next to the exe. The status window needs a desktop session;
      without one (or with --headless) it runs windowless.
  MastSatellite.exe --stop [--config <file>] [--timeout <s>]
      Ask the running satellite to shut down gracefully and wait for it.
  MastSatellite.exe --protect-token --out <file> [--scope CurrentUser|LocalMachine]
      Read a token from stdin and write it DPAPI-protected (same format as set-token.ps1).
  MastSatellite.exe --print-spki <host:port>
      Connect once with TLS and print the server's SPKI SHA-256 (compare it with the value
      the hub's admin tool prints before putting it into hub.spki_sha256).
  MastSatellite.exe --check-config [--config <file>] [--allow-plain-loopback]
      Validate the config, decrypt the token, and check that every enabled driver's vendor
      files exist (nothing is loaded or opened). Exit code 0 = ready to start.
  MastSatellite.exe --selftest [--spe-samples <dir>]
      Offline self-test (protocol, journal, rate limiter, SPE parser, simulated devices).
  MastSatellite.exe --ui-demo [--ui-scene connected|reconnecting|rejected|trouble] [--ui-size <w>x<h>]
      The status window on stand-in data (no config, no lock, no hub). Size in 96-dpi pixels.
  MastSatellite.exe --ui-shot <file.png> [--ui-shot-after <s>] [--ui-scene ...] [--ui-size ...]
      Same, then save the window as a PNG and exit.
  MastSatellite.exe --ui-audit
      Count repaints over a still and a changing 10 s phase; exit code 0 = no needless repaint.
  MastSatellite.exe --version";

        [STAThread]
        private static int Main(string[] args)
        {
            var a = new Args(args);
            bool cli = a.Has("--selftest") || a.Has("--stop") || a.Has("--protect-token") || a.Has("--print-spki") ||
                       a.Has("--check-config") || a.Has("--version") || a.Has("--help") || a.Has("-h") || a.Has("/?") ||
                       a.Has("--ui-audit") || a.Has("--ui-shot");
            if (cli || a.Has("--headless")) AttachParentConsole();
            try
            {
                if (a.Has("--help") || a.Has("-h") || a.Has("/?")) { Console.WriteLine(Usage); return 0; }
                if (a.Has("--version")) { Console.WriteLine("MastSatellite " + SatelliteCore.SoftwareVersion); return 0; }
                if (a.Has("--selftest")) return SelfTest.SelfTests.Run(a.Value("--spe-samples"), a.Has("--verbose"));
                if (a.Has("--protect-token")) return ProtectToken(a);
                if (a.Has("--print-spki")) return PrintSpki(a.Value("--print-spki"));
                if (a.Has("--check-config")) return CheckConfig(a);
                if (a.Has("--stop")) return StopRunning(a);
                if (a.Has("--ui-demo") || a.Has("--ui-shot") || a.Has("--ui-audit")) return UiDemo(a);
                return Run(a);
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine("fatal: " + ex);
                try { Log.Error("fatal", ex); } catch { }
                return 1;
            }
            finally
            {
                Log.Close();
            }
        }

        private static void AttachParentConsole()
        {
            // A WinExe has no console. If stdout is not redirected, borrow the parent's console.
            if (Native.GetStdHandle(Native.STD_OUTPUT_HANDLE) == IntPtr.Zero) Native.AttachConsole(Native.ATTACH_PARENT_PROCESS);
            try
            {
                var stdout = new StreamWriter(Console.OpenStandardOutput(), new UTF8Encoding(false)) { AutoFlush = true };
                Console.SetOut(stdout);
                var stderr = new StreamWriter(Console.OpenStandardError(), new UTF8Encoding(false)) { AutoFlush = true };
                Console.SetError(stderr);
            }
            catch { }
        }

        private static string DefaultConfigPath()
        {
            var pd = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), "MAST-Satellite", "satellite.json");
            if (File.Exists(pd)) return pd;
            return Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "satellite.json");
        }

        private static SatelliteConfig LoadConfig(Args a, bool allowPlain)
        {
            var path = a.Value("--config") ?? DefaultConfigPath();
            if (!File.Exists(path)) throw new FileNotFoundException("config not found: " + path);
            var cfg = SatelliteConfig.Load(path);
            var errs = cfg.Validate(allowPlain);
            if (errs.Count > 0) throw new InvalidDataException("invalid config " + path + ":\n  - " + string.Join("\n  - ", errs));
            return cfg;
        }

        private static int Run(Args a)
        {
            bool allowPlain = a.Has("--allow-plain-loopback");
            var cfg = LoadConfig(a, allowPlain);
            Directory.CreateDirectory(cfg.StateDir);
            Log.Init(Path.Combine(cfg.StateDir, "logs"), cfg.LogKeepDays);
            Log.MinLevel = cfg.ParsedLogLevel();
            bool headless = a.Has("--headless") || !SessionInfo.HasDesktop;
            Log.EchoToConsole = headless;

            FileStream lockFile;
            try
            {
                lockFile = new FileStream(Path.Combine(cfg.StateDir, "satellite.lock"), FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None);
            }
            catch (IOException)
            {
                Log.Error("another MastSatellite is already running with state_dir " + cfg.StateDir);
                Console.Error.WriteLine("another MastSatellite is already running with state_dir " + cfg.StateDir);
                return 3;
            }
            var stopFile = Path.Combine(cfg.StateDir, "stop.request");
            try { File.Delete(stopFile); } catch { }
            using (lockFile)
            {
                var lockText = Encoding.ASCII.GetBytes($"pid={System.Diagnostics.Process.GetCurrentProcess().Id}\r\n");
                lockFile.SetLength(0);
                lockFile.Write(lockText, 0, lockText.Length);
                lockFile.Flush(true);

                string token;
                try { token = TokenStore.Load(cfg.Hub.TokenFile); }
                catch (Exception ex)
                {
                    Log.Error("cannot load the hub token: " + ex.Message);
                    if (!headless) MessageBox.Show("无法读取连接 MAST 的令牌：\n" + ex.Message + "\n\n请用 deploy\\set-token.ps1 写入令牌后重新启动卫星。", "MAST 卫星", MessageBoxButtons.OK, MessageBoxIcon.Error);
                    return 4;
                }

                Log.Info($"MastSatellite {SatelliteCore.SoftwareVersion} starting, config {cfg.SourcePath}, {(headless ? "headless" : "with window")}, {SessionInfo.Describe()}");
                var core = new SatelliteCore(cfg, token);
                core.Start();

                using (var stopEvent = new EventWaitHandle(false, EventResetMode.ManualReset, @"Local\MastSatellite." + cfg.SatelliteId + ".stop"))
                {
                    StatusForm form = null;
                    var stopRequested = new ManualResetEventSlim(false);
                    var watcher = new Thread(() =>
                    {
                        while (!stopRequested.IsSet)
                        {
                            if (stopEvent.WaitOne(1000) || File.Exists(stopFile))
                            {
                                Log.Info("stop requested (--stop)");
                                try { File.Delete(stopFile); } catch { }
                                stopRequested.Set();
                                form?.ExitFromRemote();
                            }
                        }
                    }) { IsBackground = true, Name = "stop-watcher" };
                    watcher.Start();
                    Console.CancelKeyPress += (s, e) => { e.Cancel = true; stopRequested.Set(); form?.ExitFromRemote(); };

                    if (headless)
                    {
                        Microsoft.Win32.SystemEvents.SessionEnding += (s, e) => stopRequested.Set();
                        stopRequested.Wait();
                    }
                    else
                    {
                        Application.EnableVisualStyles();
                        Application.SetCompatibleTextRenderingDefault(false);
                        // Monitors can go dark now and then: log what Windows saw, to match against reports.
                        DisplayWatch display = null;
                        try { display = new DisplayWatch(); }
                        catch (Exception ex) { Log.Warn("[display] watch not started: " + ex.Message); }
                        form = new StatusForm(core, cfg.StartMinimized);
                        Application.Run(form);
                        stopRequested.Set();
                        display?.Dispose();
                    }
                }
                core.Stop();
            }
            return 0;
        }

        private static int UiDemo(Args a)
        {
            System.Drawing.Size? size = null;
            var sz = a.Value("--ui-size");
            if (!string.IsNullOrEmpty(sz))
            {
                var p = sz.ToLowerInvariant().Split('x');
                if (p.Length != 2 || !int.TryParse(p[0], out var w) || !int.TryParse(p[1], out var h) || w < 200 || h < 200)
                    throw new ArgumentException("--ui-size <w>x<h>, e.g. 900x600");
                size = new System.Drawing.Size(w, h);
            }
            double after = double.TryParse(a.Value("--ui-shot-after") ?? "2.5", System.Globalization.NumberStyles.Float,
                System.Globalization.CultureInfo.InvariantCulture, out var s) ? s : 2.5;
            return DemoHarness.Run(a.Value("--ui-scene"), size, a.Value("--ui-shot"), after, a.Has("--ui-audit"));
        }

        /// <summary>Pre-flight for the maintenance window: config, token, vendor files. Loads and opens nothing.</summary>
        private static int CheckConfig(Args a)
        {
            var path = a.Value("--config") ?? DefaultConfigPath();
            Console.WriteLine("config     " + path);
            if (!File.Exists(path)) { Console.WriteLine("FAIL       config file not found"); return 1; }
            SatelliteConfig cfg;
            try { cfg = SatelliteConfig.Load(path); }
            catch (Exception ex) { Console.WriteLine("FAIL       cannot parse: " + ex.Message); return 1; }
            int bad = 0;
            foreach (var e in cfg.Validate(a.Has("--allow-plain-loopback"))) { Console.WriteLine("FAIL       " + e); bad++; }
            Console.WriteLine($"satellite  {cfg.SatelliteId}  hub {cfg.Hub.Url}  state_dir {cfg.StateDir}");
            Console.WriteLine($"session    {SessionInfo.Describe()}");
            try
            {
                TokenStore.Load(cfg.Hub.TokenFile);
                Console.WriteLine("ok         token decrypts (" + cfg.Hub.TokenFile + ")");
            }
            catch (Exception ex) { Console.WriteLine("FAIL       token: " + ex.Message); bad++; }
            foreach (var d in cfg.Devices)
            {
                if (!d.Enabled) { Console.WriteLine($"off        {d.Id} ({d.Driver})"); continue; }
                if (d.Sim) { Console.WriteLine($"sim        {d.Id} ({d.Driver})"); continue; }
                var need = new List<string>();
                int before = bad;
                switch (d.Driver)
                {
                    case "lightfield":
                    {
                        var root = d.Path("lightfield_root") ?? Environment.GetEnvironmentVariable("LIGHTFIELD_ROOT") ?? @"C:\Program Files\Princeton Instruments\LightField\";
                        need.Add(Path.Combine(root, "PrincetonInstruments.LightField.AutomationV5.dll"));
                        need.Add(Path.Combine(root, "AddInViews", "PrincetonInstruments.LightFieldViewV5.dll"));
                        need.Add(Path.Combine(root, "PrincetonInstruments.LightFieldAddInSupportServices.dll"));
                        if (!SessionInfo.HasDesktop) { Console.WriteLine($"WARN       {d.Id}: no desktop session here; LightField will refuse to start in this session"); }
                        break;
                    }
                    case "kinesis_kdc101":
                        if (string.IsNullOrEmpty(d.Str("serial", ""))) { Console.WriteLine($"FAIL       {d.Id}: 'serial' is not set"); bad++; }
                        need.Add(Path.Combine(d.Path("kinesis_dir", @"C:\Program Files\Thorlabs\Kinesis"), "Thorlabs.MotionControl.KCube.DCServo.dll"));
                        break;
                    case "picomotor8742":
                        if (d.Str("transport", "usb") == "usb") need.Add(d.Path("usb_dll", @"C:\Program Files\Newport\Newport USB Driver\Bin\usbdll.dll"));
                        if (d.Str("transport", "usb") == "cmdlib") need.Add(d.Path("cmdlib_path", @"C:\Program Files\New Focus\New Focus Picomotor Application\Bin\CmdLib8742.dll"));
                        break;
                    case "spe_watcher":
                    {
                        var root = d.Path("root", null);
                        if (string.IsNullOrEmpty(root)) { Console.WriteLine($"FAIL       {d.Id}: 'root' is not set"); bad++; }
                        else if (!Directory.Exists(root)) { Console.WriteLine($"FAIL       {d.Id}: watch root not found: {root}"); bad++; }
                        break;
                    }
                    case "ds1":
                    {
                        var port = d.Str("port", "");
                        if (string.IsNullOrEmpty(port)) Console.WriteLine($"WARN       {d.Id}: 'port' is not set");
                        else if (!System.IO.Ports.SerialPort.GetPortNames().Contains(port, StringComparer.OrdinalIgnoreCase))
                            Console.WriteLine($"WARN       {d.Id}: {port} not present (the port is not opened by this check)");
                        break;
                    }
                }
                var missing = need.Where(p => !File.Exists(p)).ToList();
                foreach (var m in missing) { Console.WriteLine($"FAIL       {d.Id}: missing {m}"); bad++; }
                if (bad == before) Console.WriteLine($"ok         {d.Id} ({d.Driver})");
            }
            Console.WriteLine(bad == 0 ? "READY" : bad + " problem(s)");
            return bad == 0 ? 0 : 1;
        }

        private static int StopRunning(Args a)
        {
            var cfg = LoadConfig(a, allowPlain: true);
            double timeout = double.TryParse(a.Value("--timeout") ?? "60", System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out var t) ? t : 60;
            var lockPath = Path.Combine(cfg.StateDir, "satellite.lock");
            if (!IsLocked(lockPath)) { Console.WriteLine("no running satellite for " + cfg.StateDir); return 2; }
            File.WriteAllText(Path.Combine(cfg.StateDir, "stop.request"), DateTime.UtcNow.ToString("o"));
            try
            {
                if (EventWaitHandle.TryOpenExisting(@"Local\MastSatellite." + cfg.SatelliteId + ".stop", out var ev))
                    using (ev) ev.Set();
            }
            catch { }
            var end = DateTime.UtcNow.AddSeconds(timeout);
            while (DateTime.UtcNow < end)
            {
                if (!IsLocked(lockPath)) { Console.WriteLine("satellite stopped"); return 0; }
                Thread.Sleep(250);
            }
            Console.Error.WriteLine("satellite did not stop within " + timeout + " s");
            return 1;
        }

        private static bool IsLocked(string path)
        {
            if (!File.Exists(path)) return false;
            try
            {
                using (new FileStream(path, FileMode.Open, FileAccess.ReadWrite, FileShare.None)) return false;
            }
            catch (IOException) { return true; }
            catch (UnauthorizedAccessException) { return true; }
        }

        private static int ProtectToken(Args a)
        {
            var output = a.Value("--out") ?? throw new ArgumentException("--out <file> is required");
            var scope = string.Equals(a.Value("--scope"), "LocalMachine", StringComparison.OrdinalIgnoreCase)
                ? DataProtectionScope.LocalMachine : DataProtectionScope.CurrentUser;
            var token = Console.In.ReadToEnd().Trim();
            if (token.Length < 16) { Console.Error.WriteLine("token on stdin is missing or too short"); return 2; }
            TokenStore.Save(output, token, scope);
            Console.WriteLine("token written to " + output + " (" + scope + ")");
            return 0;
        }

        private static int PrintSpki(string hostPort)
        {
            if (string.IsNullOrEmpty(hostPort)) throw new ArgumentException("--print-spki <host:port>");
            var idx = hostPort.LastIndexOf(':');
            var host = hostPort.Substring(0, idx);
            var port = int.Parse(hostPort.Substring(idx + 1), System.Globalization.CultureInfo.InvariantCulture);
            X509Certificate seen = null;
            using (var tcp = new TcpClient())
            {
                tcp.Connect(host, port);
                using (var ssl = new SslStream(tcp.GetStream(), false, (s, cert, chain, err) => { seen = cert; return true; }))
                {
                    ssl.AuthenticateAsClient(host);
                }
            }
            if (seen == null) { Console.Error.WriteLine("no certificate presented"); return 1; }
            Console.WriteLine("subject     " + seen.Subject);
            Console.WriteLine("issuer      " + seen.Issuer);
            Console.WriteLine("spki_sha256 " + Spki.Sha256Hex(seen));
            Console.WriteLine("(trust-on-first-use value: confirm it against the hub's own admin output before using it)");
            return 0;
        }

        private sealed class Args
        {
            private readonly List<string> _a;
            public Args(string[] a) { _a = a.ToList(); }
            public bool Has(string k) { return _a.Any(x => string.Equals(x, k, StringComparison.OrdinalIgnoreCase)); }
            public string Value(string k)
            {
                for (int i = 0; i + 1 < _a.Count; i++)
                    if (string.Equals(_a[i], k, StringComparison.OrdinalIgnoreCase)) return _a[i + 1];
                return null;
            }
        }
    }
}
