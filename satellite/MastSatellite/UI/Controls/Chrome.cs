using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Windows.Forms;

namespace MastSatellite.UI.Controls
{
    /// <summary>Section title ("设备", "日志") with an optional summary and an optional check toggle on the right.</summary>
    internal sealed class SectionBar : FlatControl
    {
        private readonly string _name;
        private string _title = "";
        private string _summary = "";
        private string _toggleText;
        private bool _toggleOn;
        private bool _toggleHot;

        public SectionBar(string auditName) { _name = auditName; }

        protected override string AuditName => _name;

        public event Action<bool> ToggleChanged;

        public string Title { get => _title; set { if (value != _title) { _title = value ?? ""; Invalidate(); } } }
        public string Summary { get => _summary; set { if ((value ?? "") != _summary) { _summary = value ?? ""; Invalidate(); } } }
        public string ToggleText { get => _toggleText; set { _toggleText = value; Invalidate(); } }

        public bool ToggleOn
        {
            get => _toggleOn;
            set { if (value != _toggleOn) { _toggleOn = value; Invalidate(); ToggleChanged?.Invoke(value); } }
        }

        private Rectangle ToggleRect(Graphics g)
        {
            if (string.IsNullOrEmpty(_toggleText)) return Rectangle.Empty;
            int box = Px(13);
            int w = box + Px(8) + Theme.TextWidth(g, _toggleText, Fonts.UiSmall);
            return new Rectangle(Width - Px(16) - w, 0, w, Height);
        }

        protected override void Draw(Graphics g, Rectangle clip)
        {
            g.Clear(Theme.Graphite);
            int x = Px(16);
            int tw = Theme.TextWidth(g, _title, Fonts.Section);
            Theme.Text1(g, _title, Fonts.Section, Theme.Text, new Rectangle(x, 0, tw + 1, Height));
            var t = ToggleRect(g);
            int right = t.IsEmpty ? Width - Px(16) : t.Left - Px(20);
            if (!string.IsNullOrEmpty(_summary))
                Theme.Text1(g, _summary, Fonts.UiSmall, Theme.TextMuted, new Rectangle(x + tw + Px(14), 0, Math.Max(0, right - x - tw - Px(14)), Height));
            if (t.IsEmpty) return;
            int box = Px(13);
            var b = new Rectangle(t.Left, (Height - box) / 2, box, box);
            g.SmoothingMode = SmoothingMode.AntiAlias;
            using (var path = Theme.RoundRect(new RectangleF(b.X + 0.5f, b.Y + 0.5f, b.Width - 1, b.Height - 1), Px(3)))
            {
                if (_toggleOn) using (var f = new SolidBrush(Theme.TextMuted)) g.FillPath(f, path);
                using (var p = new Pen(_toggleOn ? Theme.TextMuted : (_toggleHot ? Theme.TextMuted : Theme.TextDim))) g.DrawPath(p, path);
            }
            if (_toggleOn)
            {
                using (var p = new Pen(Theme.Graphite, Math.Max(1.5f, Px(2))) { StartCap = LineCap.Round, EndCap = LineCap.Round, LineJoin = LineJoin.Round })
                    g.DrawLines(p, new[]
                    {
                        new PointF(b.X + box * 0.25f, b.Y + box * 0.52f),
                        new PointF(b.X + box * 0.43f, b.Y + box * 0.70f),
                        new PointF(b.X + box * 0.76f, b.Y + box * 0.32f),
                    });
            }
            g.SmoothingMode = SmoothingMode.None;
            Theme.Text1(g, _toggleText, Fonts.UiSmall, _toggleHot || _toggleOn ? Theme.Text : Theme.TextMuted,
                new Rectangle(b.Right + Px(8), 0, t.Right - b.Right - Px(8) + 1, Height));
        }

        protected override void OnMouseMove(MouseEventArgs e)
        {
            base.OnMouseMove(e);
            if (IgnoreMouse || Fonts == null) return;
            bool hot = ToggleRect(Theme.Measure).Contains(e.Location);
            if (hot != _toggleHot) { _toggleHot = hot; Cursor = hot ? Cursors.Hand : Cursors.Default; Invalidate(); }
        }

        protected override void OnMouseLeave(EventArgs e)
        {
            base.OnMouseLeave(e);
            if (_toggleHot) { _toggleHot = false; Cursor = Cursors.Default; Invalidate(); }
        }

        protected override void OnMouseDown(MouseEventArgs e)
        {
            base.OnMouseDown(e);
            if (e.Button != MouseButtons.Left || Fonts == null) return;
            if (ToggleRect(Theme.Measure).Contains(e.Location)) ToggleOn = !ToggleOn;
        }
    }

    /// <summary>The draggable gap between the device list and the log. Draws a short grip line.</summary>
    internal sealed class SplitHandle : FlatControl
    {
        private bool _hot, _drag;
        private int _grabY;

        public SplitHandle() { Cursor = Cursors.HSplit; }

        protected override string AuditName => "split";

        /// <summary>Raised while dragging with the vertical offset (in pixels) from where the drag started.</summary>
        public event Action<int> Dragged;
        public event Action DragStarted;

        protected override void Draw(Graphics g, Rectangle clip)
        {
            g.Clear(Theme.Graphite);
            int w = Px(36), y = Height / 2;
            using (var p = new Pen(_hot || _drag ? Theme.TextMuted : Theme.Hairline, Math.Max(1, Px(2))))
                g.DrawLine(p, (Width - w) / 2, y, (Width + w) / 2, y);
        }

        protected override void OnMouseEnter(EventArgs e) { base.OnMouseEnter(e); if (!IgnoreMouse) { _hot = true; Invalidate(); } }
        protected override void OnMouseLeave(EventArgs e) { base.OnMouseLeave(e); if (_hot) { _hot = false; Invalidate(); } }

        protected override void OnMouseDown(MouseEventArgs e)
        {
            base.OnMouseDown(e);
            if (e.Button != MouseButtons.Left) return;
            _drag = true;
            _grabY = PointToScreen(e.Location).Y;
            Capture = true;
            DragStarted?.Invoke();
            Invalidate();
        }

        protected override void OnMouseMove(MouseEventArgs e)
        {
            base.OnMouseMove(e);
            if (_drag) Dragged?.Invoke(PointToScreen(e.Location).Y - _grabY);
        }

        protected override void OnMouseUp(MouseEventArgs e)
        {
            base.OnMouseUp(e);
            if (!_drag) return;
            _drag = false;
            Capture = false;
            Invalidate();
        }
    }

    /// <summary>One quiet line of guidance at the bottom of the window.</summary>
    internal sealed class FooterBar : FlatControl
    {
        private string _text = "";

        protected override string AuditName => "footer";

        public override string Text { get => _text; set { if ((value ?? "") != _text) { _text = value ?? ""; Invalidate(); } } }

        protected override void Draw(Graphics g, Rectangle clip)
        {
            g.Clear(Theme.Graphite);
            using (var p = new Pen(Theme.Hairline)) g.DrawLine(p, 0, 0, Width, 0);
            Theme.Text1(g, _text, Fonts.UiSmall, Theme.TextDim, new Rectangle(Px(16), 1, Width - Px(32), Height - 1));
        }
    }
}
