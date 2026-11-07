using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Linq;
using System.Windows.Forms;
using MastSatellite.Devices;
using MastSatellite.Protocol;

namespace MastSatellite.UI.Controls
{
    /// <summary>
    /// Device rows, owner-drawn. Each row keeps the text it last painted; a new snapshot
    /// invalidates only the rows whose text changed. The operator-hold switch on the right is the
    /// one bright element of the window.
    /// </summary>
    internal sealed class DeviceList : FlatControl
    {
        private sealed class Row
        {
            public string Id;
            public string Key;          // everything painted, folded; equal key = identical pixels
            public string Kind;
            public string State;
            public string StateWord;
            public Color Accent;
            public bool Sim;
            public bool Hold;
            public string Detail;       // second line
            public Color DetailColor;
            public IReadOnlyList<string> Actions = Array.Empty<string>();
            // layout, filled by Layout()
            public Rectangle Bounds;
            public Rectangle Action;
            public Rectangle SwitchMast;
            public Rectangle SwitchHold;
            public Rectangle DetailRect;
        }

        private enum Part { None, Action, Mast, Hold }

        private readonly List<Row> _rows = new List<Row>();
        private readonly ToolTip _tip = new ToolTip { InitialDelay = 500, AutoPopDelay = 30000, ReshowDelay = 200 };
        private string _tipFor;
        private int _scroll;
        private int _focus = -1;
        private int _hoverRow = -1;
        private Part _hoverPart = Part.None;
        private bool _keyboardFocusVisible;
        private bool _spaceDown;

        /// <summary>Raised when the operator clicks the other half of a switch (or presses space).</summary>
        public event Action<string, bool> HoldRequested;

        /// <summary>Raised when the operator clicks a row's local-action button.</summary>
        public event Action<string, string> ActionRequested;

        public DeviceList()
        {
            SetStyle(ControlStyles.Selectable, true);
            TabStop = true;
        }

        protected override string AuditName => "devices";

        public int RowHeight => RowH;
        public int RowCount => _rows.Count;
        private int RowH => Px(56);
        private int Pad => Px(16);
        private int ContentHeight => _rows.Count * RowH;
        private int MaxScroll => Math.Max(0, ContentHeight - Height);

        // ------------------------------------------------------------------ data

        public void Show(IReadOnlyList<DeviceRow> devices, Func<string, IReadOnlyList<string>> actionsOf)
        {
            bool structural = devices.Count != _rows.Count;
            for (int i = 0; !structural && i < devices.Count; i++) structural = devices[i].Id != _rows[i].Id;
            if (structural)
            {
                _rows.Clear();
                foreach (var d in devices) _rows.Add(new Row { Id = d.Id, Actions = actionsOf(d.Id) ?? Array.Empty<string>() });
                foreach (var r in _rows.Zip(devices, (r, d) => (r, d))) Fill(r.r, r.d);
                _scroll = Math.Min(_scroll, MaxScroll);
                if (_focus >= _rows.Count) _focus = _rows.Count - 1;
                LayoutRows();
                Invalidate();
                return;
            }
            for (int i = 0; i < devices.Count; i++)
            {
                var r = _rows[i];
                string before = r.Key;
                Fill(r, devices[i]);
                if (r.Key != before) InvalidateRow(i);
            }
        }

        private static void Fill(Row r, DeviceRow d)
        {
            r.Kind = KindName(d);
            r.State = d.OperatorHold ? DeviceBase.StateOperatorHold : d.State;
            r.Sim = d.Sim;
            r.Hold = d.OperatorHold;
            switch (r.State)
            {
                case DeviceBase.StateReady: r.StateWord = "就绪"; r.Accent = Theme.SignalGreen; break;
                case DeviceBase.StateBusy: r.StateWord = "忙碌"; r.Accent = Theme.SteelBlue; break;
                case DeviceBase.StateOffline: r.StateWord = "离线"; r.Accent = Theme.TextDim; break;
                case DeviceBase.StateError: r.StateWord = "错误"; r.Accent = Theme.LaserRed; break;
                case DeviceBase.StateNotStarted: r.StateWord = "未启动"; r.Accent = Theme.TextDim; break;
                case DeviceBase.StateOperatorHold: r.StateWord = "人工接管"; r.Accent = Theme.Amber; break;
                default: r.StateWord = d.State ?? "—"; r.Accent = Theme.TextDim; break;
            }
            if (!string.IsNullOrEmpty(d.Active)) { r.Detail = "正在执行  " + d.Active; r.DetailColor = Theme.SteelBlue; }
            else if (r.State == DeviceBase.StateError && !string.IsNullOrEmpty(d.Reason ?? d.LastError)) { r.Detail = d.Reason ?? d.LastError; r.DetailColor = Theme.LaserRed; }
            else if (r.Hold) { r.Detail = "人工接管中：MAST 只能停止、安全和只读查询" + (string.IsNullOrEmpty(d.Reason) ? "" : "    " + d.Reason); r.DetailColor = Theme.Amber; }
            else if (!string.IsNullOrEmpty(d.Reason)) { r.Detail = d.Reason; r.DetailColor = Theme.TextMuted; }
            else if (!string.IsNullOrEmpty(d.LastError)) { r.Detail = "上次错误  " + d.LastError; r.DetailColor = Theme.TextMuted; }
            else { r.Detail = ""; r.DetailColor = Theme.TextMuted; }
            r.Key = string.Join("\u001f", r.Kind, r.State, r.StateWord, r.Sim ? "1" : "0", r.Hold ? "1" : "0", r.Detail, r.DetailColor.ToArgb().ToString(), string.Join("\u001e", r.Actions));
        }

        private static string KindName(DeviceRow d)
        {
            switch (d.Driver)
            {
                case "lightfield": return "光谱仪  LightField";
                case "kinesis_kdc101": return "旋转台  KDC101";
                case "motion_manipulator": return "三维台  Motion Manipulator";
                case "picomotor8742": return "皮可电机  8742";
                case "ds1": return "AOTF  DS1";
                case "spectrasense": return "PMT  SpectraSense";
                case "spe_watcher": return "光谱文件监视";
            }
            switch (d.Kind)
            {
                case "spectrometer": return "光谱仪";
                case "rotation_stage": return "旋转台";
                case "xyz_stage": return "三维台";
                case "openloop_stepper": return "开环电机";
                case "aotf": return "AOTF";
                case "pmt": return "PMT";
                case "file_watcher": return "文件监视";
            }
            return d.Kind ?? "";
        }

        // ------------------------------------------------------------------ layout

        protected override void OnFontsChanged() { LayoutRows(); }

        protected override void OnSizeChanged(EventArgs e)
        {
            base.OnSizeChanged(e);
            _scroll = Math.Min(_scroll, MaxScroll);
            LayoutRows();
        }

        private void LayoutRows()
        {
            if (Fonts == null) return;
            int w = Width;
            int swW = Px(176), swH = Px(28);
            for (int i = 0; i < _rows.Count; i++)
            {
                var r = _rows[i];
                int y = i * RowH - _scroll;
                r.Bounds = new Rectangle(0, y, w, RowH);
                int cy = y + (RowH - swH) / 2;
                r.SwitchMast = new Rectangle(w - Pad - Px(12) - swW, cy, swW / 2, swH);
                r.SwitchHold = new Rectangle(r.SwitchMast.Right, cy, swW - swW / 2, swH);
                if (r.Actions.Count > 0)
                {
                    int aw = Theme.TextWidth(Theme.Measure, r.Actions[0], Fonts.UiSmall) + Px(24);
                    r.Action = new Rectangle(r.SwitchMast.Left - Px(12) - aw, cy, aw, swH);
                }
                else r.Action = Rectangle.Empty;
                int textX = Pad + Px(15);
                int right = (r.Action.IsEmpty ? r.SwitchMast.Left : r.Action.Left) - Px(16);
                r.DetailRect = new Rectangle(textX, y + RowH / 2 + Px(1), Math.Max(0, right - textX), RowH / 2 - Px(8));
            }
        }

        private void InvalidateRow(int i)
        {
            if (i < 0 || i >= _rows.Count) return;
            var b = _rows[i].Bounds;
            if (b.Bottom < 0 || b.Top > Height) return;
            PaintAudit.Invalidated(AuditName, b.Height);
            Invalidate(b);
        }

        // ------------------------------------------------------------------ paint

        protected override void Draw(Graphics g, Rectangle clip)
        {
            g.Clear(Theme.Graphite);
            if (_rows.Count == 0)
            {
                Theme.Text1(g, "配置里没有启用的设备", Fonts.Ui, Theme.TextMuted, ClientRectangle, center: true);
                return;
            }
            for (int i = 0; i < _rows.Count; i++)
            {
                var r = _rows[i];
                if (!r.Bounds.IntersectsWith(clip)) continue;
                DrawRow(g, r, i);
            }
            if (MaxScroll > 0) DrawScrollThumb(g);
        }

        private void DrawRow(Graphics g, Row r, int index)
        {
            var b = r.Bounds;
            bool hover = index == _hoverRow;
            using (var bg = new SolidBrush(hover ? Theme.SurfaceHover : Theme.Surface))
                g.FillRectangle(bg, Pad, b.Top, b.Width - 2 * Pad, b.Height - 1);
            using (var line = new Pen(Theme.Graphite))
                g.DrawLine(line, Pad, b.Bottom - 1, b.Width - Pad, b.Bottom - 1);
            using (var bar = new SolidBrush(r.Accent))
                g.FillRectangle(bar, Pad, b.Top, Px(3), b.Height - 1);

            int x = Pad + Px(15);
            int line1H = RowH / 2 - Px(6);
            // one line only: centre it, so the row is not top-heavy next to the centred switch
            int line1Y = string.IsNullOrEmpty(r.Detail) ? b.Top + (RowH - line1H) / 2 : b.Top + Px(8);

            // device id, then kind, then a status word in a fixed column
            int idW = Px(64);
            Theme.Text1(g, r.Id, Fonts.DeviceName, Theme.Text, new Rectangle(x, line1Y, idW, line1H));
            int kindX = x + idW;
            int stateX = Math.Max(kindX + Px(170), Math.Min(kindX + Px(200), r.DetailRect.Right - Px(120)));
            Theme.Text1(g, r.Kind, Fonts.Ui, Theme.TextMuted, new Rectangle(kindX, line1Y, stateX - kindX - Px(12), line1H));
            int sw = Theme.TextWidth(g, r.StateWord, Fonts.Ui);
            Theme.Text1(g, r.StateWord, Fonts.Ui, r.State == DeviceBase.StateOffline || r.State == DeviceBase.StateNotStarted ? Theme.TextMuted : r.Accent,
                new Rectangle(stateX, line1Y, sw + 1, line1H));
            if (r.Sim) DrawTag(g, "模拟", stateX + sw + Px(8), line1Y, line1H);

            Theme.Text1(g, r.Detail, Fonts.UiSmall, r.DetailColor, r.DetailRect);

            if (!r.Action.IsEmpty) DrawActionButton(g, r, hover && _hoverPart == Part.Action);
            DrawSwitch(g, r, index);
            if (index == _focus && Focused && _keyboardFocusVisible)
            {
                using (var p = new Pen(Theme.TextMuted) { DashStyle = DashStyle.Dot })
                    g.DrawRectangle(p, Pad + Px(4), b.Top + Px(2), b.Width - 2 * Pad - Px(8), b.Height - Px(5));
            }
        }

        private void DrawTag(Graphics g, string text, int x, int y, int h)
        {
            int w = Theme.TextWidth(g, text, Fonts.UiSmall) + Px(10);
            int th = Fonts.UiSmall.Height + Px(2);
            var r = new Rectangle(x, y + (h - th) / 2, w, th);
            g.SmoothingMode = SmoothingMode.AntiAlias;
            using (var path = Theme.RoundRect(new RectangleF(r.X + 0.5f, r.Y + 0.5f, r.Width - 1, r.Height - 1), Px(4)))
            using (var p = new Pen(Theme.TextDim))
                g.DrawPath(p, path);
            g.SmoothingMode = SmoothingMode.None;
            Theme.Text1(g, text, Fonts.UiSmall, Theme.TextMuted, r, center: true);
        }

        private void DrawActionButton(Graphics g, Row r, bool hot)
        {
            var a = r.Action;
            g.SmoothingMode = SmoothingMode.AntiAlias;
            using (var path = Theme.RoundRect(new RectangleF(a.X + 0.5f, a.Y + 0.5f, a.Width - 1, a.Height - 1), Px(4)))
            {
                if (hot) using (var f = new SolidBrush(Theme.Hairline)) g.FillPath(f, path);
                using (var p = new Pen(hot ? Theme.TextMuted : Theme.Hairline)) g.DrawPath(p, path);
            }
            g.SmoothingMode = SmoothingMode.None;
            Theme.Text1(g, r.Actions[0], Fonts.UiSmall, hot ? Theme.Text : Theme.TextMuted, a, center: true);
        }

        private void DrawSwitch(Graphics g, Row r, int index)
        {
            var whole = Rectangle.Union(r.SwitchMast, r.SwitchHold);
            var on = r.Hold ? r.SwitchHold : r.SwitchMast;
            var off = r.Hold ? r.SwitchMast : r.SwitchHold;
            bool hotOff = index == _hoverRow && (_hoverPart == (r.Hold ? Part.Mast : Part.Hold));
            float rad = Px(4);
            g.SmoothingMode = SmoothingMode.AntiAlias;
            using (var outline = Theme.RoundRect(new RectangleF(whole.X + 0.5f, whole.Y + 0.5f, whole.Width - 1, whole.Height - 1), rad))
            {
                if (hotOff)
                {
                    var state = g.Save();
                    g.SetClip(off);
                    using (var f = new SolidBrush(Theme.Hairline)) g.FillPath(f, outline);
                    g.Restore(state);
                }
                using (var p = new Pen(Theme.Hairline)) g.DrawPath(p, outline);
            }
            using (var fill = Theme.RoundRect(new RectangleF(on.X, on.Y, on.Width, on.Height), rad))
            using (var b = new SolidBrush(r.Hold ? Theme.Amber : Theme.SteelTint))
                g.FillPath(b, fill);
            g.SmoothingMode = SmoothingMode.None;
            Theme.Text1(g, "MAST 可控", Fonts.UiSmall, r.Hold ? (hotOff ? Theme.Text : Theme.TextMuted) : Theme.SteelInk, r.SwitchMast, center: true);
            Theme.Text1(g, "人工接管", Fonts.UiSmall, r.Hold ? Theme.AmberInk : (hotOff ? Theme.Text : Theme.TextMuted), r.SwitchHold, center: true);
        }

        private void DrawScrollThumb(Graphics g)
        {
            int track = Height - Px(8);
            int thumb = Math.Max(Px(24), track * Height / Math.Max(1, ContentHeight));
            int y = Px(4) + (track - thumb) * _scroll / Math.Max(1, MaxScroll);
            g.SmoothingMode = SmoothingMode.AntiAlias;
            using (var path = Theme.RoundRect(new RectangleF(Width - Px(7), y, Px(3), thumb), Px(1.5f)))
            using (var b = new SolidBrush(Theme.Hairline))
                g.FillPath(b, path);
            g.SmoothingMode = SmoothingMode.None;
        }

        // ------------------------------------------------------------------ input

        private (int row, Part part) HitTest(Point p)
        {
            for (int i = 0; i < _rows.Count; i++)
            {
                var r = _rows[i];
                if (!r.Bounds.Contains(p)) continue;
                if (r.SwitchMast.Contains(p)) return (i, Part.Mast);
                if (r.SwitchHold.Contains(p)) return (i, Part.Hold);
                if (!r.Action.IsEmpty && r.Action.Contains(p)) return (i, Part.Action);
                return (i, Part.None);
            }
            return (-1, Part.None);
        }

        protected override void OnMouseMove(MouseEventArgs e)
        {
            base.OnMouseMove(e);
            if (IgnoreMouse) return;
            var (row, part) = HitTest(e.Location);
            if (row != _hoverRow || part != _hoverPart)
            {
                int old = _hoverRow;
                _hoverRow = row;
                _hoverPart = part;
                InvalidateRow(old);
                if (row != old) InvalidateRow(row);
                Cursor = part == Part.Action || (row >= 0 && part == (_rows[row].Hold ? Part.Mast : Part.Hold)) ? Cursors.Hand : Cursors.Default;
            }
            string tip = null;
            if (row >= 0)
            {
                var r = _rows[row];
                if (part == Part.Mast || part == Part.Hold)
                    tip = r.Hold ? "点「MAST 可控」交还给 MAST" : "点「人工接管」：MAST 只能对 " + r.Id + " 执行停止、安全和只读查询，正在执行的命令会被停下";
                else if (part == Part.Action) tip = "在本机执行：" + r.Actions[0];
                else if (r.DetailRect.Contains(e.Location) && !string.IsNullOrEmpty(r.Detail)) tip = r.Detail;
            }
            if (tip != _tipFor)
            {
                _tipFor = tip;
                _tip.SetToolTip(this, tip ?? "");
                if (tip == null) _tip.Hide(this);
            }
        }

        protected override void OnMouseLeave(EventArgs e)
        {
            base.OnMouseLeave(e);
            if (_hoverRow >= 0)
            {
                int old = _hoverRow;
                _hoverRow = -1;
                _hoverPart = Part.None;
                InvalidateRow(old);
            }
            Cursor = Cursors.Default;
        }

        protected override void OnMouseDown(MouseEventArgs e)
        {
            base.OnMouseDown(e);
            Focus();
            _keyboardFocusVisible = false;
            if (e.Button != MouseButtons.Left) return;
            var (row, part) = HitTest(e.Location);
            if (row < 0) return;
            SetFocusRow(row);
            // the second press of a double click is not a second request (the old ListView's
            // double click flipped the hold check box; a quick double click produced repeated flips)
            if (e.Clicks > 1) return;
            var r = _rows[row];
            if (part == Part.Mast && r.Hold) RequestHold(row, false);
            else if (part == Part.Hold && !r.Hold) RequestHold(row, true);
            else if (part == Part.Action && r.Actions.Count > 0) ActionRequested?.Invoke(r.Id, r.Actions[0]);
        }

        private void RequestHold(int row, bool on)
        {
            HoldRequested?.Invoke(_rows[row].Id, on);
        }

        protected override void OnMouseWheel(MouseEventArgs e)
        {
            base.OnMouseWheel(e);
            ScrollTo(_scroll - e.Delta * RowH / 120);
        }

        private void ScrollTo(int value)
        {
            int v = Math.Max(0, Math.Min(MaxScroll, value));
            if (v == _scroll) return;
            _scroll = v;
            LayoutRows();
            Invalidate();
        }

        private void SetFocusRow(int row)
        {
            if (row == _focus) return;
            int old = _focus;
            _focus = row;
            InvalidateRow(old);
            InvalidateRow(row);
            if (row >= 0)
            {
                var b = _rows[row].Bounds;
                if (b.Top < 0) ScrollTo(_scroll + b.Top);
                else if (b.Bottom > Height) ScrollTo(_scroll + b.Bottom - Height);
            }
        }

        protected override bool IsInputKey(Keys keyData)
        {
            switch (keyData & Keys.KeyCode)
            {
                case Keys.Up: case Keys.Down: case Keys.Space: return true;
            }
            return base.IsInputKey(keyData);
        }

        protected override void OnKeyDown(KeyEventArgs e)
        {
            base.OnKeyDown(e);
            if (_rows.Count == 0) return;
            _keyboardFocusVisible = true;
            if (e.KeyCode == Keys.Down) { SetFocusRow(Math.Min(_rows.Count - 1, _focus + 1)); InvalidateRow(_focus); e.Handled = true; }
            else if (e.KeyCode == Keys.Up) { SetFocusRow(Math.Max(0, _focus - 1)); InvalidateRow(_focus); e.Handled = true; }
            else if (e.KeyCode == Keys.Space && _focus >= 0)
            {
                // a held space bar auto-repeats; one press is one switch
                if (!_spaceDown) { _spaceDown = true; RequestHold(_focus, !_rows[_focus].Hold); }
                e.Handled = true;
            }
        }

        protected override void OnKeyUp(KeyEventArgs e)
        {
            base.OnKeyUp(e);
            if (e.KeyCode == Keys.Space) _spaceDown = false;
        }

        protected override void OnGotFocus(EventArgs e)
        {
            base.OnGotFocus(e);
            if (_focus < 0 && _rows.Count > 0) _focus = 0;
            InvalidateRow(_focus);
        }

        protected override void OnLostFocus(EventArgs e)
        {
            base.OnLostFocus(e);
            _spaceDown = false;
            InvalidateRow(_focus);
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing) _tip.Dispose();
            base.Dispose(disposing);
        }
    }
}
