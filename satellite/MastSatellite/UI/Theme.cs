using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Text;
using System.Linq;
using System.Runtime.InteropServices;
using System.Windows.Forms;

namespace MastSatellite.UI
{
    /// <summary>
    /// Palette for a darkened optics lab: graphite like anodised breadboard, one bright element
    /// (the operator-hold switch), status colours used only where they carry meaning.
    /// </summary>
    internal static class Theme
    {
        public static readonly Color Graphite = Rgb(0x1C2127);
        public static readonly Color Surface = Rgb(0x232A31);
        public static readonly Color SurfaceHover = Rgb(0x29313A);
        public static readonly Color Hairline = Rgb(0x343D47);
        public static readonly Color Text = Rgb(0xE3E7EB);
        public static readonly Color TextMuted = Rgb(0x8E99A5);
        public static readonly Color TextDim = Rgb(0x66717D);
        public static readonly Color SignalGreen = Rgb(0x3FBF87);
        public static readonly Color SteelBlue = Rgb(0x3A9FD6);
        public static readonly Color Amber = Rgb(0xF2A93B);
        public static readonly Color LaserRed = Rgb(0xEF5350);
        public static readonly Color AmberInk = Rgb(0x1C2127);
        public static readonly Color SteelTint = Rgb(0x274A5E);   // "MAST 可控" selected: the normal state stays quiet
        public static readonly Color SteelInk = Rgb(0xB3DCF2);
        public static readonly Color TextSoft = Rgb(0xC3CAD1);    // log lines at INFO

        public static Color Rgb(int rgb) => Color.FromArgb(0xFF, (rgb >> 16) & 0xFF, (rgb >> 8) & 0xFF, rgb & 0xFF);

        public static int Px(int dpi, float v) => (int)Math.Round(v * dpi / 96f);

        public static GraphicsPath RoundRect(RectangleF r, float radius)
        {
            var p = new GraphicsPath();
            float d = Math.Min(radius * 2, Math.Min(r.Width, r.Height));
            if (d <= 0.5f) { p.AddRectangle(r); return p; }
            p.AddArc(r.X, r.Y, d, d, 180, 90);
            p.AddArc(r.Right - d, r.Y, d, d, 270, 90);
            p.AddArc(r.Right - d, r.Bottom - d, d, d, 0, 90);
            p.AddArc(r.X, r.Bottom - d, d, d, 90, 90);
            p.CloseFigure();
            return p;
        }

        /// <summary>Single-line text, vertically centred in <paramref name="r"/>, end-ellipsis when it does not fit.</summary>
        public static void Text1(Graphics g, string s, Font f, Color c, Rectangle r, bool right = false, bool center = false)
        {
            if (string.IsNullOrEmpty(s) || r.Width <= 0) return;
            var flags = TextFormatFlags.SingleLine | TextFormatFlags.VerticalCenter | TextFormatFlags.NoPrefix |
                        TextFormatFlags.EndEllipsis | TextFormatFlags.NoPadding;
            if (right) flags |= TextFormatFlags.Right;
            else if (center) flags |= TextFormatFlags.HorizontalCenter;
            TextRenderer.DrawText(g, s, f, r, c, flags);
        }

        private static Graphics _measure;

        /// <summary>
        /// Text measurement without a window handle (UI thread only). Fonts are sized in pixels,
        /// so the 1x1 bitmap's resolution does not change the result.
        /// </summary>
        public static Graphics Measure => _measure ?? (_measure = Graphics.FromImage(new Bitmap(1, 1)));

        public static int TextWidth(Graphics g, string s, Font f)
        {
            if (string.IsNullOrEmpty(s)) return 0;
            return TextRenderer.MeasureText(g, s, f, new Size(int.MaxValue, int.MaxValue), TextFormatFlags.SingleLine | TextFormatFlags.NoPrefix | TextFormatFlags.NoPadding).Width;
        }

        /// <summary>Asks Windows 11 for a dark caption that matches the window; ignored where unsupported.</summary>
        public static void DarkCaption(IntPtr hwnd)
        {
            try
            {
                int on = 1;
                DwmSetWindowAttribute(hwnd, 20, ref on, 4); // DWMWA_USE_IMMERSIVE_DARK_MODE
                int bgr = Graphite.R | (Graphite.G << 8) | (Graphite.B << 16);
                DwmSetWindowAttribute(hwnd, 35, ref bgr, 4); // DWMWA_CAPTION_COLOR (Windows 11)
                int text = Text.R | (Text.G << 8) | (Text.B << 16);
                DwmSetWindowAttribute(hwnd, 36, ref text, 4); // DWMWA_TEXT_COLOR
            }
            catch { }
        }

        [DllImport("dwmapi.dll")]
        private static extern int DwmSetWindowAttribute(IntPtr hwnd, int attr, ref int value, int size);

        [DllImport("user32.dll")]
        private static extern bool DestroyIcon(IntPtr hIcon);

        /// <summary>
        /// The satellite mark: a graphite disc, a small body with two solar panels on the diagonal,
        /// and a status dot (null for the plain window icon).
        /// </summary>
        public static Icon MakeIcon(int size, Color? status)
        {
            using (var bmp = new Bitmap(size, size))
            {
                using (var g = Graphics.FromImage(bmp))
                {
                    g.SmoothingMode = SmoothingMode.AntiAlias;
                    g.PixelOffsetMode = PixelOffsetMode.HighQuality;
                    g.Clear(Color.Transparent);
                    float s = size / 32f;
                    using (var disc = new SolidBrush(Graphite))
                    using (var ring = new Pen(Hairline, Math.Max(1f, 1.5f * s)))
                    {
                        g.FillEllipse(disc, 1 * s, 1 * s, 30 * s, 30 * s);
                        g.DrawEllipse(ring, 1 * s, 1 * s, 30 * s, 30 * s);
                    }
                    var state = g.Save();
                    g.TranslateTransform(16 * s, 16 * s);
                    g.RotateTransform(-45);
                    using (var panel = new SolidBrush(SteelBlue))
                    using (var body = new SolidBrush(Text))
                    using (var strut = new Pen(TextMuted, Math.Max(1f, 1.2f * s)))
                    {
                        g.DrawLine(strut, -11 * s, 0, 11 * s, 0);
                        g.FillRectangle(panel, -12.5f * s, -4.5f * s, 7 * s, 9 * s);
                        g.FillRectangle(panel, 5.5f * s, -4.5f * s, 7 * s, 9 * s);
                        g.FillEllipse(body, -3.5f * s, -3.5f * s, 7 * s, 7 * s);
                    }
                    g.Restore(state);
                    if (status.HasValue)
                    {
                        using (var dot = new SolidBrush(status.Value))
                        using (var edge = new Pen(Graphite, Math.Max(1f, 2f * s)))
                        {
                            var r = new RectangleF(19 * s, 19 * s, 12 * s, 12 * s);
                            g.FillEllipse(dot, r);
                            g.DrawEllipse(edge, r);
                        }
                    }
                }
                IntPtr h = bmp.GetHicon();
                try { using (var tmp = Icon.FromHandle(h)) return (Icon)tmp.Clone(); }
                finally { DestroyIcon(h); }
            }
        }

        public static ToolStripRenderer MenuRenderer() => new ToolStripProfessionalRenderer(new DarkMenuColors()) { RoundedEdges = false };

        private sealed class DarkMenuColors : ProfessionalColorTable
        {
            public override Color ToolStripDropDownBackground => Surface;
            public override Color MenuBorder => Hairline;
            public override Color MenuItemBorder => Hairline;
            public override Color MenuItemSelected => SurfaceHover;
            public override Color MenuItemSelectedGradientBegin => SurfaceHover;
            public override Color MenuItemSelectedGradientEnd => SurfaceHover;
            public override Color ImageMarginGradientBegin => Surface;
            public override Color ImageMarginGradientMiddle => Surface;
            public override Color ImageMarginGradientEnd => Surface;
            public override Color SeparatorDark => Hairline;
            public override Color SeparatorLight => Surface;
        }
    }

    /// <summary>
    /// Fonts for one DPI, sized in pixels so layout arithmetic and drawing agree. Rebuilt (and the
    /// old set disposed) when the window moves to a monitor with another DPI.
    /// </summary>
    internal sealed class ThemeFonts : IDisposable
    {
        public readonly int Dpi;
        public readonly Font Ui;          // body text, Chinese
        public readonly Font UiSmall;     // hints, labels
        public readonly Font Section;     // "设备", "日志"
        public readonly Font Title;       // link status
        public readonly Font DeviceName;  // device ids
        public readonly Font Metric;      // numbers
        public readonly Font Mono;        // log lines

        public ThemeFonts(int dpi)
        {
            Dpi = dpi;
            string ui = Pick("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI");
            string din = Pick("Bahnschrift SemiBold", "Bahnschrift", "Segoe UI Semibold", "Segoe UI");
            string dinRegular = Pick("Bahnschrift", "Segoe UI");
            string mono = Pick("Cascadia Mono", "Consolas", "Courier New");
            Ui = Make(ui, 9f, FontStyle.Regular);
            UiSmall = Make(ui, 8.25f, FontStyle.Regular);
            Section = Make(ui, 10.5f, FontStyle.Bold);
            Title = Make(ui, 11f, FontStyle.Bold);
            DeviceName = Make(din, 12f, din.StartsWith("Bahnschrift SemiBold", StringComparison.Ordinal) ? FontStyle.Regular : FontStyle.Bold);
            Metric = Make(dinRegular, 10f, FontStyle.Regular);
            Mono = Make(mono, 8.5f, FontStyle.Regular);
        }

        private Font Make(string family, float points, FontStyle style) => new Font(family, points * Dpi / 72f, style, GraphicsUnit.Pixel);

        private static readonly Lazy<string[]> Installed = new Lazy<string[]>(() =>
        {
            using (var installed = new InstalledFontCollection()) return installed.Families.Select(f => f.Name).ToArray();
        });

        private static string Pick(params string[] names)
        {
            foreach (var n in names) if (Installed.Value.Contains(n, StringComparer.OrdinalIgnoreCase)) return n;
            return SystemFonts.MessageBoxFont.FontFamily.Name;
        }

        public void Dispose()
        {
            Ui.Dispose(); UiSmall.Dispose(); Section.Dispose(); Title.Dispose(); DeviceName.Dispose(); Metric.Dispose(); Mono.Dispose();
        }
    }
}
