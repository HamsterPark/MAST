using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Imaging;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Windows.Forms;
using MastSatellite.Core;
using MastSatellite.UI.Controls;

namespace MastSatellite.UI
{
    /// <summary>
    /// --ui-demo / --ui-shot / --ui-audit: the status window on stand-in data, without config,
    /// lock, token or hub. The audit proves the no-flicker rule with paint counts.
    /// </summary>
    internal static class DemoHarness
    {
        public static int Run(string scene, Size? logicalSize, string shotPath, double shotAfterS, bool audit)
        {
            var dir = Path.Combine(Path.GetTempPath(), "MastSatellite-ui-demo");
            Log.Init(Path.Combine(dir, "logs"), 1);
            Log.MinLevel = LogLevel.Info;
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);

            var demo = new DemoStatusSource(scene);
            bool still = audit || shotPath != null;
            demo.Changing = !still;
            int exit = 0;
            using (var form = new StatusForm(demo, false, "rig1（演示）") { NoActivate = still })
            {
                if (logicalSize.HasValue)
                {
                    form.StartPosition = FormStartPosition.Manual;
                    var wa = Screen.PrimaryScreen.WorkingArea;
                    int w = Theme.Px(form.DeviceDpi, logicalSize.Value.Width), h = Theme.Px(form.DeviceDpi, logicalSize.Value.Height);
                    form.Bounds = new Rectangle(wa.Left + (wa.Width - w) / 2, wa.Top + (wa.Height - h) / 2, w, h);
                }
                if (audit)
                {
                    var run = new Audit(form, demo);
                    form.Shown += (s, e) => run.Start();
                    Application.Run(form);
                    exit = run.Passed ? 0 : 1;
                }
                else if (shotPath != null)
                {
                    var t = new Timer { Interval = Math.Max(200, (int)(shotAfterS * 1000)) };
                    t.Tick += (s, e) =>
                    {
                        t.Stop();
                        try
                        {
                            Capture(form, shotPath);
                            Console.WriteLine("saved " + shotPath);
                        }
                        catch (Exception ex)
                        {
                            Console.Error.WriteLine("screenshot failed: " + ex.Message);
                            exit = 1;
                        }
                        form.ExitFromRemote();
                    };
                    form.Shown += (s, e) => t.Start();
                    Application.Run(form);
                    t.Dispose();
                }
                else Application.Run(form);
            }
            return exit;
        }

        /// <summary>
        /// Two 10 s phases. Still: nothing changes, so only the connected-for clock may repaint the
        /// header (once a second) and the device list and the log must not repaint at all.
        /// Changing: one device and one log line per second; the list repaints only that row.
        /// </summary>
        private sealed class Audit
        {
            private readonly StatusForm _form;
            private readonly DemoStatusSource _demo;
            private readonly Timer _t = new Timer { Interval = 100 };
            private DateTime _phaseStart;
            private int _phase;          // 0 settle, 1 still, 2 changing
            private Dictionary<string, int> _still, _changing;
            private int _stillMaxRowInvalid, _changingMaxRowInvalid;
            private const double PhaseS = 10;

            public bool Passed { get; private set; }

            public Audit(StatusForm form, DemoStatusSource demo)
            {
                _form = form;
                _demo = demo;
                _t.Tick += (s, e) => Step();
            }

            public void Start()
            {
                _form.IgnoreMouse(true);
                _phaseStart = DateTime.UtcNow;
                _t.Start();
            }

            private void Step()
            {
                double el = (DateTime.UtcNow - _phaseStart).TotalSeconds;
                if (_phase == 0 && el >= 2)
                {
                    PaintAudit.Reset();
                    _phase = 1;
                    _phaseStart = DateTime.UtcNow;
                }
                else if (_phase == 1 && el >= PhaseS)
                {
                    _still = PaintAudit.All();
                    _stillMaxRowInvalid = PaintAudit.MaxInvalidHeight("devices");
                    PaintAudit.Reset();
                    _demo.Changing = true;
                    _phase = 2;
                    _phaseStart = DateTime.UtcNow;
                }
                else if (_phase == 2 && el >= PhaseS)
                {
                    _changing = PaintAudit.All();
                    _changingMaxRowInvalid = PaintAudit.MaxInvalidHeight("devices");
                    _t.Stop();
                    Report();
                    _form.ExitFromRemote();
                }
            }

            private static int N(Dictionary<string, int> d, string k) => d.TryGetValue(k, out var v) ? v : 0;

            private void Report()
            {
                int row = _form.DeviceRowHeight;
                var checks = new List<(string name, bool ok, string detail)>
                {
                    ("still: header <= 1/s", N(_still, "header") <= PhaseS + 1, N(_still, "header") + " paints"),
                    ("still: devices = 0", N(_still, "devices") == 0, N(_still, "devices") + " paints"),
                    ("still: log = 0", N(_still, "log") == 0, N(_still, "log") + " paints"),
                    // the upper bounds mean nothing unless the changes reached the screen at all
                    ("changing: devices repainted", N(_changing, "devices") >= PhaseS / 2, N(_changing, "devices") + " paints"),
                    ("changing: devices <= 2/s", N(_changing, "devices") <= 2 * PhaseS + 1, N(_changing, "devices") + " paints"),
                    ("changing: device invalidation is one row", _changingMaxRowInvalid > 0 && _changingMaxRowInvalid <= row, _changingMaxRowInvalid + " px (row " + row + " px)"),
                    ("changing: log repainted", N(_changing, "log") >= PhaseS / 2, N(_changing, "log") + " paints"),
                    ("changing: log <= 2/s", N(_changing, "log") <= 2 * PhaseS + 1, N(_changing, "log") + " paints"),
                };
                Passed = checks.All(c => c.ok);
                string Obj(Dictionary<string, int> d) => "{" + string.Join(",", d.OrderBy(kv => kv.Key, StringComparer.Ordinal).Select(kv => "\"" + kv.Key + "\":" + kv.Value)) + "}";
                Console.WriteLine("{\"dpi\":" + _form.DeviceDpi + ",\"phase_s\":" + PhaseS.ToString(CultureInfo.InvariantCulture)
                    + ",\"still\":" + Obj(_still) + ",\"changing\":" + Obj(_changing)
                    + ",\"device_row_px\":" + row + ",\"max_device_invalidation_px\":" + _changingMaxRowInvalid
                    + ",\"passed\":" + (Passed ? "true" : "false") + "}");
                foreach (var c in checks) Console.WriteLine((c.ok ? "PASS  " : "FAIL  ") + c.name + "  (" + c.detail + ")");
            }
        }

        /// <summary>Saves the window as drawn by DWM (title bar included), even when another window covers it.</summary>
        private static void Capture(Form form, string path)
        {
            var dir = Path.GetDirectoryName(Path.GetFullPath(path));
            if (!string.IsNullOrEmpty(dir)) Directory.CreateDirectory(dir);
            GetWindowRect(form.Handle, out var wr);
            RECT fr;
            if (DwmGetWindowAttribute(form.Handle, 9 /* DWMWA_EXTENDED_FRAME_BOUNDS */, out fr, Marshal.SizeOf(typeof(RECT))) != 0) fr = wr;
            using (var full = new Bitmap(wr.Right - wr.Left, wr.Bottom - wr.Top, PixelFormat.Format32bppArgb))
            {
                bool ok;
                using (var g = Graphics.FromImage(full))
                {
                    var hdc = g.GetHdc();
                    try { ok = PrintWindow(form.Handle, hdc, 2 /* PW_RENDERFULLCONTENT */); }
                    finally { g.ReleaseHdc(hdc); }
                    if (!ok) g.CopyFromScreen(wr.Left, wr.Top, 0, 0, full.Size);
                }
                var crop = new Rectangle(fr.Left - wr.Left, fr.Top - wr.Top, fr.Right - fr.Left, fr.Bottom - fr.Top);
                crop.Intersect(new Rectangle(Point.Empty, full.Size));
                using (var outBmp = full.Clone(crop, PixelFormat.Format32bppArgb)) outBmp.Save(path, ImageFormat.Png);
            }
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct RECT { public int Left, Top, Right, Bottom; }

        [DllImport("user32.dll")]
        private static extern bool GetWindowRect(IntPtr hwnd, out RECT rect);

        [DllImport("user32.dll")]
        private static extern bool PrintWindow(IntPtr hwnd, IntPtr hdc, uint flags);

        [DllImport("dwmapi.dll")]
        private static extern int DwmGetWindowAttribute(IntPtr hwnd, int attr, out RECT value, int size);
    }
}
