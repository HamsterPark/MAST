using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Drawing;
using System.Linq;
using System.Windows.Forms;

namespace MastSatellite.UI.Controls
{
    /// <summary>
    /// Paint counters per control, read by --ui-audit to prove that unchanged data causes no
    /// repaint and a changed device repaints only its own row.
    /// </summary>
    internal static class PaintAudit
    {
        private static readonly ConcurrentDictionary<string, int> Counts = new ConcurrentDictionary<string, int>();
        private static readonly ConcurrentDictionary<string, int> MaxInvalid = new ConcurrentDictionary<string, int>();

        public static void Painted(string name) => Counts.AddOrUpdate(name, 1, (k, v) => v + 1);
        public static void Invalidated(string name, int height) => MaxInvalid.AddOrUpdate(name, height, (k, v) => Math.Max(v, height));
        public static int MaxInvalidHeight(string name) => MaxInvalid.TryGetValue(name, out var v) ? v : 0;
        public static int Get(string name) => Counts.TryGetValue(name, out var v) ? v : 0;
        public static Dictionary<string, int> All() => Counts.ToDictionary(kv => kv.Key, kv => kv.Value);

        public static void Reset()
        {
            Counts.Clear();
            MaxInvalid.Clear();
        }
    }

    /// <summary>
    /// Owner-drawn, double-buffered base: paints only in WM_PAINT into an off-screen buffer, so a
    /// repaint never shows an erased background. Fonts come from the window (one set per DPI).
    /// </summary>
    internal abstract class FlatControl : Control
    {
        protected FlatControl()
        {
            SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer |
                     ControlStyles.ResizeRedraw, true);
            BackColor = Theme.Graphite;
            ForeColor = Theme.Text;
        }

        protected ThemeFonts Fonts { get; private set; }

        /// <summary>True during --ui-audit: mouse hover must not add repaints to the count.</summary>
        public bool IgnoreMouse { get; set; }

        protected abstract string AuditName { get; }

        public void ApplyFonts(ThemeFonts fonts)
        {
            Fonts = fonts;
            OnFontsChanged();
            Invalidate();
        }

        protected virtual void OnFontsChanged() { }

        protected int Px(float v) => Theme.Px(Fonts?.Dpi ?? DeviceDpi, v);

        protected override void OnPaint(PaintEventArgs e)
        {
            PaintAudit.Painted(AuditName);
            if (Fonts == null) { e.Graphics.Clear(BackColor); return; }
            Draw(e.Graphics, e.ClipRectangle);
        }

        protected abstract void Draw(Graphics g, Rectangle clip);
    }
}
