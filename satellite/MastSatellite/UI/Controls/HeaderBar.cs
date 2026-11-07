using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Globalization;
using System.Text;
using System.Windows.Forms;
using MastSatellite.Protocol;
using LinkState = MastSatellite.Protocol.LinkState;

namespace MastSatellite.UI.Controls
{
    /// <summary>
    /// Link status and a few numbers. Everything shown is folded into one key; the bar repaints
    /// only when that key changes (in practice once a second, for the connected-for clock).
    /// </summary>
    internal sealed class HeaderBar : FlatControl
    {
        private readonly ToolTip _tip = new ToolTip { InitialDelay = 400, AutoPopDelay = 20000 };
        private string _key = "";
        private string _tipText;
        private string _title = "";
        private string _sub = "";
        private Color _accent = Theme.TextDim;
        private List<KeyValuePair<string, string>> _row1 = new List<KeyValuePair<string, string>>();
        private List<KeyValuePair<string, string>> _row2 = new List<KeyValuePair<string, string>>();

        protected override string AuditName => "header";

        public void Show(SatelliteSnapshot s)
        {
            string title;
            Color accent;
            switch (s.Link)
            {
                case LinkState.Connected: title = "已连接 MAST 枢纽"; accent = Theme.SignalGreen; break;
                case LinkState.Connecting: title = "正在连接 MAST 枢纽"; accent = Theme.Amber; break;
                case LinkState.Backoff: title = "与 MAST 枢纽断开，正在重连"; accent = Theme.Amber; break;
                case LinkState.Rejected: title = "MAST 枢纽拒绝了连接：检查令牌和证书钉值"; accent = Theme.LaserRed; break;
                case LinkState.Stopped: title = "卫星已停止"; accent = Theme.TextDim; break;
                default: title = "卫星未启动"; accent = Theme.TextDim; break;
            }
            string sub = HostPort(s.HubUrl) + "    卫星 " + s.SatelliteId;
            if (s.Link != LinkState.Connected && !string.IsNullOrEmpty(s.LinkError)) sub += "    " + s.LinkError;

            var ci = CultureInfo.InvariantCulture;
            string since = s.Link == LinkState.Connected && s.ConnectedSinceUtc.HasValue ? Duration(DateTime.UtcNow - s.ConnectedSinceUtc.Value) : "—";
            string ping = s.PingAgeS < 0 ? "—" : s.PingAgeS < 2 ? "正常" : ((int)Math.Round(s.PingAgeS)).ToString(ci) + " s 前";
            var row1 = new List<KeyValuePair<string, string>> { Pair("连接时长", since), Pair("心跳", ping) };
            var row2 = new List<KeyValuePair<string, string>> { Pair("重连", s.Reconnects.ToString(ci)),
                Pair("上传", s.FilesUploaded.ToString(ci) + " 个，" + (s.UploadBytes / 1048576.0).ToString("0.0", ci) + " MB") };
            if (s.UploadQueue > 0) row2.Add(Pair("队列", s.UploadQueue.ToString(ci)));

            var key = new StringBuilder().Append(title).Append('|').Append(sub).Append('|').Append(accent.ToArgb());
            foreach (var kv in row1) key.Append('|').Append(kv.Key).Append('=').Append(kv.Value);
            foreach (var kv in row2) key.Append('|').Append(kv.Key).Append('=').Append(kv.Value);
            if (key.ToString() != _key)
            {
                _key = key.ToString();
                _title = title; _sub = sub; _accent = accent; _row1 = row1; _row2 = row2;
                Invalidate();
            }

            string tip = string.Format(ci, "卫星 boot {0}\n枢纽 {1}\n枢纽 boot {2}\n事件 seq {3}，已确认 {4}，缓冲 {5}，丢弃 {6}{7}",
                Short(s.BootId), s.HubUrl, s.HubBootId ?? "—", s.EventsLastSeq, s.EventsAcked, s.EventsBuffered, s.EventsDropped,
                string.IsNullOrEmpty(s.LinkError) ? "" : "\n最近错误 " + s.LinkError);
            if (tip != _tipText) { _tipText = tip; _tip.SetToolTip(this, tip); }
        }

        private static KeyValuePair<string, string> Pair(string k, string v) => new KeyValuePair<string, string>(k, v);
        private static string Short(string id) => string.IsNullOrEmpty(id) ? "—" : id.Substring(0, Math.Min(8, id.Length));

        private static string HostPort(string url)
        {
            if (Uri.TryCreate(url ?? "", UriKind.Absolute, out var u)) return u.Host + ":" + u.Port.ToString(CultureInfo.InvariantCulture);
            return url ?? "";
        }

        private static string Duration(TimeSpan t)
        {
            if (t < TimeSpan.Zero) t = TimeSpan.Zero;
            var hms = string.Format(CultureInfo.InvariantCulture, "{0}:{1:00}:{2:00}", t.Hours, t.Minutes, t.Seconds);
            return t.TotalDays >= 1 ? ((int)t.TotalDays).ToString(CultureInfo.InvariantCulture) + " 天 " + hms : hms;
        }

        protected override void Draw(Graphics g, Rectangle clip)
        {
            g.Clear(Theme.Graphite);
            int pad = Px(16);
            int titleH = Fonts.Title.Height;
            int subH = Fonts.UiSmall.Height;
            int top = (Height - titleH - Px(4) - subH) / 2;

            // status lamp
            int d = Px(10);
            g.SmoothingMode = SmoothingMode.AntiAlias;
            using (var b = new SolidBrush(_accent)) g.FillEllipse(b, pad, top + (titleH - d) / 2f, d, d);
            g.SmoothingMode = SmoothingMode.None;

            // metrics, right-aligned, two rows; measured first so the title can give way
            int metricsW = Math.Max(RowWidth(g, _row1), RowWidth(g, _row2));
            int right = Width - pad;
            DrawMetricRow(g, _row1, right, top, titleH);
            DrawMetricRow(g, _row2, right, top + titleH + Px(4), subH + Px(2));

            int textX = pad + d + Px(10);
            int textW = Math.Max(0, right - metricsW - Px(24) - textX);
            // connected is the normal state: the lamp says it, the words stay neutral
            Color titleColor = _accent == Theme.SignalGreen ? Theme.Text : _accent == Theme.TextDim ? Theme.TextMuted : _accent;
            Theme.Text1(g, _title, Fonts.Title, titleColor, new Rectangle(textX, top, textW, titleH));
            Theme.Text1(g, _sub, Fonts.UiSmall, Theme.TextMuted, new Rectangle(textX, top + titleH + Px(4), textW, subH));

            using (var p = new Pen(Theme.Hairline)) g.DrawLine(p, 0, Height - 1, Width, Height - 1);
        }

        private int RowWidth(Graphics g, List<KeyValuePair<string, string>> row)
        {
            int w = 0;
            foreach (var kv in row)
                w += Theme.TextWidth(g, kv.Key, Fonts.UiSmall) + Px(6) + Theme.TextWidth(g, kv.Value, Fonts.Metric) + Px(18);
            return Math.Max(0, w - Px(18));
        }

        private void DrawMetricRow(Graphics g, List<KeyValuePair<string, string>> row, int right, int y, int h)
        {
            int x = right;
            for (int i = row.Count - 1; i >= 0; i--)
            {
                var kv = row[i];
                int vw = Theme.TextWidth(g, kv.Value, Fonts.Metric);
                x -= vw;
                Theme.Text1(g, kv.Value, Fonts.Metric, Theme.Text, new Rectangle(x, y, vw + 1, h));
                int lw = Theme.TextWidth(g, kv.Key, Fonts.UiSmall);
                x -= Px(6) + lw;
                Theme.Text1(g, kv.Key, Fonts.UiSmall, Theme.TextMuted, new Rectangle(x, y, lw + 1, h));
                x -= Px(18);
            }
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing) _tip.Dispose();
            base.Dispose(disposing);
        }
    }
}
