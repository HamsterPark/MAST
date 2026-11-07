using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Linq;
using System.Text;
using System.Windows.Forms;
using MastSatellite.Core;

namespace MastSatellite.UI.Controls
{
    /// <summary>
    /// Log tail, owner-drawn and virtualised: new lines are appended to a ring of at most
    /// <see cref="Capacity"/>; only visible lines are drawn. Follows the bottom while the operator
    /// is there; scrolled up, the view stays put and a small marker offers the newest lines.
    /// </summary>
    internal sealed class LogView : FlatControl
    {
        public const int Capacity = 3000;

        private sealed class Entry
        {
            public long Seq;
            public LogLevel Level;
            public string Raw;
            public string Time;
            public string Source;
            public string Message;
        }

        private readonly List<Entry> _all = new List<Entry>();
        private readonly List<Entry> _view = new List<Entry>();
        private readonly ToolTip _tip = new ToolTip { InitialDelay = 600, AutoPopDelay = 30000, ReshowDelay = 200 };
        private readonly ContextMenuStrip _menu = new ContextMenuStrip();
        private readonly ToolStripMenuItem _copySel;
        private readonly ToolStripMenuItem _openDir;
        private string _tipFor;
        private int _top;            // index in _view of the first visible line
        private bool _follow = true;
        private bool _unseen;        // lines arrived below while scrolled up
        private bool _warnOnly;
        private long _selFrom = -1, _selTo = -1; // seq range (inclusive), -1 = none
        private long _anchor = -1;
        private bool _dragging;
        private int _timeW, _sourceW;

        public LogView()
        {
            SetStyle(ControlStyles.Selectable, true);
            TabStop = true;
            _menu.Renderer = Theme.MenuRenderer();
            _menu.ForeColor = Theme.Text;
            _menu.BackColor = Theme.Surface;
            _copySel = new ToolStripMenuItem("复制选中的行", null, (s, e) => CopySelected()) { ShortcutKeyDisplayString = "Ctrl+C", ForeColor = Theme.Text };
            var copyVisible = new ToolStripMenuItem("复制屏幕上的行", null, (s, e) => CopyVisible()) { ForeColor = Theme.Text };
            _openDir = new ToolStripMenuItem("打开日志文件夹", null, (s, e) => OpenDirectory()) { ForeColor = Theme.Text };
            _menu.Items.AddRange(new ToolStripItem[] { _copySel, copyVisible, new ToolStripSeparator(), _openDir });
            _menu.Opening += (s, e) =>
            {
                _copySel.Enabled = _selFrom >= 0;
                _openDir.Enabled = !string.IsNullOrEmpty(LogDirectory);
            };
            ContextMenuStrip = _menu;
        }

        protected override string AuditName => "log";

        public string LogDirectory { get; set; }

        private int LineH => Fonts == null ? 16 : Fonts.Mono.Height + Px(5);
        private int TopPad => Px(6);
        private int VisibleLines => Math.Max(1, (Height - TopPad) / LineH);
        private int MaxTop => Math.Max(0, _view.Count - VisibleLines);

        public bool WarnOnly
        {
            get => _warnOnly;
            set
            {
                if (_warnOnly == value) return;
                _warnOnly = value;
                _view.Clear();
                _view.AddRange(_all.Where(Passes));
                _top = MaxTop;
                _follow = true;
                _unseen = false;
                Invalidate();
            }
        }

        private bool Passes(Entry e) => !_warnOnly || e.Level >= LogLevel.Warn;

        // ------------------------------------------------------------------ data

        public void Append(IReadOnlyList<LogEntry> entries)
        {
            if (entries == null || entries.Count == 0) return;
            bool atBottom = _follow;
            int addedVisible = 0;
            foreach (var le in entries)
            {
                var e = Parse(le);
                _all.Add(e);
                if (Passes(e)) { _view.Add(e); addedVisible++; }
            }
            int dropped = 0;
            if (_all.Count > Capacity)
            {
                int n = _all.Count - Capacity;
                long firstKept = _all[n].Seq;
                _all.RemoveRange(0, n);
                while (_view.Count > 0 && _view[0].Seq < firstKept) { _view.RemoveAt(0); dropped++; }
            }
            if (addedVisible == 0 && dropped == 0) return;
            if (atBottom)
            {
                _top = MaxTop;
                Invalidate();
            }
            else
            {
                // keep the same lines on screen: lines dropped from the front shift the index
                _top = Math.Max(0, _top - dropped);
                bool wasUnseen = _unseen;
                if (addedVisible > 0) _unseen = true;
                if (dropped > 0 || _unseen != wasUnseen) Invalidate();
                else InvalidateThumb();
            }
        }

        private static Entry Parse(LogEntry le)
        {
            var e = new Entry { Seq = le.Seq, Level = le.Level, Raw = le.Line ?? "" };
            string s = e.Raw;
            // "yyyy-MM-dd HH:mm:ss.fff LEVEL message"
            if (s.Length >= 30 && s[4] == '-' && s[10] == ' ' && s[13] == ':')
            {
                e.Time = s.Substring(11, 8);
                s = s.Substring(30);
            }
            else e.Time = "";
            if (s.Length > 2 && s[0] == '[')
            {
                int close = s.IndexOf(']');
                if (close > 1 && close <= 16 && close + 1 < s.Length && s[close + 1] == ' ')
                {
                    e.Source = s.Substring(1, close - 1);
                    s = s.Substring(close + 2);
                }
            }
            e.Message = s.IndexOf('\n') >= 0 || s.IndexOf('\r') >= 0 ? s.Replace("\r\n", " ↵ ").Replace('\n', ' ').Replace('\r', ' ') : s;
            return e;
        }

        // ------------------------------------------------------------------ paint

        protected override void OnFontsChanged()
        {
            _timeW = Theme.TextWidth(Theme.Measure, "00:00:00", Fonts.Mono);
            _sourceW = Theme.TextWidth(Theme.Measure, "display", Fonts.Mono);
            _top = _follow ? MaxTop : Math.Min(_top, MaxTop);
        }

        protected override void OnSizeChanged(EventArgs e)
        {
            base.OnSizeChanged(e);
            if (_follow) _top = MaxTop;
            else _top = Math.Min(_top, MaxTop);
        }

        private int MessageX => Px(16) + _timeW + Px(14) + _sourceW + Px(14);

        protected override void Draw(Graphics g, Rectangle clip)
        {
            g.Clear(Theme.Surface);
            int x0 = Px(16);
            int srcX = x0 + _timeW + Px(14);
            int msgX = MessageX;
            int right = Width - Px(16);
            int n = Math.Min(_view.Count - _top, VisibleLines + 1);
            for (int i = 0; i < n; i++)
            {
                var e = _view[_top + i];
                int y = TopPad + i * LineH;
                var lineRect = new Rectangle(0, y, Width, LineH);
                if (!lineRect.IntersectsWith(clip)) continue;
                if (Selected(e.Seq))
                    using (var sel = new SolidBrush(Theme.Hairline)) g.FillRectangle(sel, Px(8), y, Width - Px(16), LineH);
                Theme.Text1(g, e.Time, Fonts.Mono, Theme.TextDim, new Rectangle(x0, y, _timeW + 1, LineH));
                if (e.Source != null) Theme.Text1(g, e.Source, Fonts.Mono, Theme.TextMuted, new Rectangle(srcX, y, _sourceW + 1, LineH));
                Color c;
                switch (e.Level)
                {
                    case LogLevel.Error: c = Theme.LaserRed; break;
                    case LogLevel.Warn: c = Theme.Amber; break;
                    case LogLevel.Debug: c = Theme.TextDim; break;
                    default: c = Theme.TextSoft; break;
                }
                Theme.Text1(g, e.Message, Fonts.Mono, c, new Rectangle(msgX, y, Math.Max(0, right - msgX), LineH));
            }
            if (_view.Count == 0)
                Theme.Text1(g, _warnOnly ? "没有警告或错误" : "还没有日志", Fonts.Ui, Theme.TextDim, ClientRectangle, center: true);
            if (MaxTop > 0) DrawThumb(g);
            if (_unseen && !_follow) DrawNewMarker(g);
        }

        private Rectangle ThumbTrack => new Rectangle(Width - Px(8), Px(4), Px(4), Height - Px(8));

        private void DrawThumb(Graphics g)
        {
            var t = ThumbTrack;
            int thumb = Math.Max(Px(24), t.Height * VisibleLines / Math.Max(1, _view.Count));
            int y = t.Top + (t.Height - thumb) * _top / Math.Max(1, MaxTop);
            g.SmoothingMode = SmoothingMode.AntiAlias;
            using (var path = Theme.RoundRect(new RectangleF(t.Left, y, Px(3), thumb), Px(1.5f)))
            using (var b = new SolidBrush(Theme.Hairline))
                g.FillPath(b, path);
            g.SmoothingMode = SmoothingMode.None;
        }

        private void InvalidateThumb()
        {
            PaintAudit.Invalidated(AuditName, ThumbTrack.Height);
            Invalidate(ThumbTrack);
        }

        private Rectangle NewMarkerRect
        {
            get
            {
                int w = Px(112), h = Px(24);
                return new Rectangle(Width - w - Px(20), Height - h - Px(10), w, h);
            }
        }

        private void DrawNewMarker(Graphics g)
        {
            var r = NewMarkerRect;
            g.SmoothingMode = SmoothingMode.AntiAlias;
            using (var path = Theme.RoundRect(new RectangleF(r.X, r.Y, r.Width, r.Height), Px(4)))
            {
                using (var b = new SolidBrush(Theme.Graphite)) g.FillPath(b, path);
                using (var p = new Pen(Theme.Hairline)) g.DrawPath(p, path);
            }
            g.SmoothingMode = SmoothingMode.None;
            Theme.Text1(g, "有新日志  ↓", Fonts.UiSmall, Theme.Text, r, center: true);
        }

        // ------------------------------------------------------------------ scrolling and selection

        private void ScrollTo(int top)
        {
            int t = Math.Max(0, Math.Min(MaxTop, top));
            bool follow = t >= MaxTop;
            bool unseen = follow ? false : _unseen;
            if (t == _top && follow == _follow && unseen == _unseen) return;
            _top = t;
            _follow = follow;
            _unseen = unseen;
            Invalidate();
        }

        protected override void OnMouseWheel(MouseEventArgs e)
        {
            base.OnMouseWheel(e);
            ScrollTo(_top - e.Delta / 120 * 3);
        }

        private int IndexAt(int y)
        {
            int i = _top + (y - TopPad) / LineH;
            return Math.Max(0, Math.Min(_view.Count - 1, i));
        }

        private bool Selected(long seq) => _selFrom >= 0 && seq >= _selFrom && seq <= _selTo;

        private void Select(long a, long b)
        {
            long from = Math.Min(a, b), to = Math.Max(a, b);
            if (from == _selFrom && to == _selTo) return;
            _selFrom = from;
            _selTo = to;
            Invalidate();
        }

        protected override void OnMouseDown(MouseEventArgs e)
        {
            base.OnMouseDown(e);
            Focus();
            if (_unseen && !_follow && NewMarkerRect.Contains(e.Location) && e.Button == MouseButtons.Left)
            {
                ScrollTo(MaxTop);
                return;
            }
            if (_view.Count == 0) return;
            var seq = _view[IndexAt(e.Y)].Seq;
            if (e.Button == MouseButtons.Right)
            {
                if (!Selected(seq)) { _anchor = seq; Select(seq, seq); }
                return;
            }
            if (e.Button != MouseButtons.Left) return;
            if ((ModifierKeys & Keys.Shift) != 0 && _anchor >= 0) Select(_anchor, seq);
            else { _anchor = seq; Select(seq, seq); }
            _dragging = true;
        }

        protected override void OnMouseMove(MouseEventArgs e)
        {
            base.OnMouseMove(e);
            if (IgnoreMouse || _view.Count == 0) return;
            if (_dragging && _anchor >= 0)
            {
                if (e.Y < 0) ScrollTo(_top - 1);
                else if (e.Y > Height) ScrollTo(_top + 1);
                Select(_anchor, _view[IndexAt(e.Y)].Seq);
            }
            string tip = null;
            int row = (e.Y - TopPad) / LineH;
            if (e.Y >= TopPad && row < VisibleLines && _top + row < _view.Count && e.X >= MessageX)
            {
                var entry = _view[_top + row];
                if (Theme.TextWidth(Theme.Measure, entry.Message, Fonts.Mono) > Width - Px(16) - MessageX) tip = entry.Message;
            }
            if (tip != _tipFor)
            {
                _tipFor = tip;
                _tip.SetToolTip(this, tip ?? "");
                if (tip == null) _tip.Hide(this);
            }
        }

        protected override void OnMouseUp(MouseEventArgs e)
        {
            base.OnMouseUp(e);
            _dragging = false;
        }

        protected override bool IsInputKey(Keys keyData)
        {
            switch (keyData & Keys.KeyCode)
            {
                case Keys.Up: case Keys.Down: case Keys.PageUp: case Keys.PageDown: case Keys.Home: case Keys.End: return true;
            }
            return base.IsInputKey(keyData);
        }

        protected override void OnKeyDown(KeyEventArgs e)
        {
            base.OnKeyDown(e);
            if (e.Control && e.KeyCode == Keys.C) { CopySelected(); e.Handled = true; return; }
            if (e.Control && e.KeyCode == Keys.A && _view.Count > 0) { _anchor = _view[0].Seq; Select(_view[0].Seq, _view[_view.Count - 1].Seq); e.Handled = true; return; }
            switch (e.KeyCode)
            {
                case Keys.Up: ScrollTo(_top - 1); break;
                case Keys.Down: ScrollTo(_top + 1); break;
                case Keys.PageUp: ScrollTo(_top - VisibleLines + 1); break;
                case Keys.PageDown: ScrollTo(_top + VisibleLines - 1); break;
                case Keys.Home: ScrollTo(0); break;
                case Keys.End: ScrollTo(MaxTop); break;
                default: return;
            }
            e.Handled = true;
        }

        private void CopySelected()
        {
            if (_selFrom < 0) return;
            SetClipboard(_view.Where(x => Selected(x.Seq)).Select(x => x.Raw));
        }

        private void CopyVisible()
        {
            SetClipboard(_view.Skip(_top).Take(VisibleLines).Select(x => x.Raw));
        }

        private static void SetClipboard(IEnumerable<string> lines)
        {
            var text = string.Join(Environment.NewLine, lines);
            if (text.Length == 0) return;
            try { Clipboard.SetText(text); } catch (Exception ex) { Log.Warn("copy to clipboard failed: " + ex.Message); }
        }

        private void OpenDirectory()
        {
            if (string.IsNullOrEmpty(LogDirectory)) return;
            try { Process.Start(new ProcessStartInfo("explorer.exe", "\"" + LogDirectory + "\"") { UseShellExecute = true }); }
            catch (Exception ex) { Log.Warn("open log folder failed: " + ex.Message); }
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing) { _tip.Dispose(); _menu.Dispose(); }
            base.Dispose(disposing);
        }
    }
}
