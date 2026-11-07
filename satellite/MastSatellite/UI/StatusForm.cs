using System;
using System.Collections.Generic;
using System.Drawing;
using System.Linq;
using System.Windows.Forms;
using MastSatellite.Core;
using MastSatellite.Devices;
using MastSatellite.Protocol;
using MastSatellite.UI.Controls;
using LinkState = MastSatellite.Protocol.LinkState;

namespace MastSatellite.UI
{
    /// <summary>
    /// Local status window: link state, devices with the operator-hold ("人工接管") switch, log tail.
    /// Closing the window hides it to the tray; only the tray menu (or --stop) ends the satellite.
    /// A 250 ms timer only compares the new snapshot with what is on screen; controls repaint just
    /// the parts that changed, so an idle satellite draws nothing but the connected-for clock.
    /// </summary>
    public sealed class StatusForm : Form
    {
        private const int WS_EX_COMPOSITED = 0x02000000;

        private readonly IStatusSource _src;
        private readonly HeaderBar _header = new HeaderBar();
        private readonly SectionBar _devTitle = new SectionBar("devices-title") { Title = "设备" };
        private readonly DeviceList _devices = new DeviceList();
        private readonly SplitHandle _split = new SplitHandle();
        private readonly SectionBar _logTitle = new SectionBar("log-title") { Title = "日志", ToggleText = "只看警告和错误" };
        private readonly LogView _log = new LogView();
        private readonly FooterBar _footer = new FooterBar
        {
            Text = "人工接管：MAST 只能对该设备执行停止、安全和只读查询，正在执行的命令会被停下。关闭窗口会收到托盘，右键托盘图标可退出卫星。",
        };
        private readonly NotifyIcon _tray = new NotifyIcon();
        private readonly ContextMenuStrip _trayMenu = new ContextMenuStrip();
        private readonly Timer _timer = new Timer { Interval = 250 };
        private readonly string _name;
        private ThemeFonts _fonts;
        private Icon _appIcon, _trayOk, _trayWarn, _trayBad;
        private bool _exitRequested;
        private bool _hintShown;
        private LinkState? _lastLink;
        private string _lastTrayText;
        private long _logSeq;
        private double _devFraction = -1;   // share of the device/log space given to devices; -1 = fit the rows
        private int _dragStartHeight;
        private int _laidOutRows = -1;

        public StatusForm(SatelliteCore core, bool startHidden) : this(new CoreStatusSource(core), startHidden, core.Config.SatelliteId) { }

        public StatusForm(IStatusSource src, bool startHidden, string name)
        {
            _src = src;
            _name = name;
            Text = "MAST 卫星 " + name;
            AutoScaleMode = AutoScaleMode.None;
            BackColor = Theme.Graphite;
            ForeColor = Theme.Text;
            DoubleBuffered = true;
            StartPosition = FormStartPosition.CenterScreen;
            _fonts = new ThemeFonts(DeviceDpi);
            Font = _fonts.Ui;
            var wa = Screen.PrimaryScreen.WorkingArea;
            Width = Math.Min(wa.Width, Math.Max(Px(1040), wa.Width / 2));
            Height = Math.Min(wa.Height, Math.Max(Px(820), wa.Height * 7 / 10));
            MinimumSize = new Size(Px(720), Px(480));

            _appIcon = Theme.MakeIcon(32, null);
            int tray = SystemInformation.SmallIconSize.Width;
            _trayOk = Theme.MakeIcon(tray, Theme.SignalGreen);
            _trayWarn = Theme.MakeIcon(tray, Theme.Amber);
            _trayBad = Theme.MakeIcon(tray, Theme.LaserRed);
            Icon = _appIcon;

            Controls.AddRange(new Control[] { _header, _devTitle, _devices, _split, _logTitle, _log, _footer });
            ApplyFonts();
            _log.LogDirectory = src.LogDirectory;
            _logTitle.ToggleChanged += on => _log.WarnOnly = on;
            _devices.HoldRequested += OnHoldRequested;
            _devices.ActionRequested += OnActionRequested;
            _split.DragStarted += () => _dragStartHeight = _devices.Height;
            _split.Dragged += dy =>
            {
                int shared = SharedHeight();
                if (shared <= 0) return;
                _devFraction = Math.Max(0.1, Math.Min(0.9, (_dragStartHeight + dy) / (double)shared));
                PerformLayout();
            };

            _trayMenu.Renderer = Theme.MenuRenderer();
            _trayMenu.BackColor = Theme.Surface;
            _trayMenu.ForeColor = Theme.Text;
            _trayMenu.Items.Add(new ToolStripMenuItem("打开窗口", null, (s, e) => ShowFromTray()) { ForeColor = Theme.Text });
            _trayMenu.Items.Add(new ToolStripSeparator());
            _trayMenu.Items.Add(new ToolStripMenuItem("退出卫星", null, (s, e) => RequestExit()) { ForeColor = Theme.Text });
            _tray.ContextMenuStrip = _trayMenu;
            _tray.Icon = _trayWarn;
            _tray.Text = Clip63("MAST 卫星 " + name);
            _tray.Visible = true;
            _tray.DoubleClick += (s, e) => ShowFromTray();

            RefreshView();
            _timer.Tick += (s, e) => RefreshView();
            _timer.Start();
            Resize += (s, e) =>
            {
                if (WindowState == FormWindowState.Minimized && !_exitRequested) HideToTray(showTip: false);
            };
            if (startHidden)
            {
                WindowState = FormWindowState.Minimized;
                ShowInTaskbar = false;
                Load += (s, e) => Hide();
            }
        }

        /// <summary>Demo and audit runs: show without taking focus from the operator's window.</summary>
        internal bool NoActivate { get; set; }

        protected override bool ShowWithoutActivation => NoActivate;

        internal int DeviceRowHeight => _devices.RowHeight;

        internal void IgnoreMouse(bool on)
        {
            foreach (var c in Controls.OfType<FlatControl>()) c.IgnoreMouse = on;
        }

        protected override CreateParams CreateParams
        {
            get
            {
                var cp = base.CreateParams;
                cp.ExStyle |= WS_EX_COMPOSITED;
                return cp;
            }
        }

        private int Px(float v) => Theme.Px(_fonts?.Dpi ?? DeviceDpi, v);

        private static string Clip63(string s) => s.Length <= 63 ? s : s.Substring(0, 62) + "…";

        // ------------------------------------------------------------------ DPI and layout

        protected override void OnHandleCreated(EventArgs e)
        {
            base.OnHandleCreated(e);
            Theme.DarkCaption(Handle);
            if (DeviceDpi != _fonts.Dpi) RebuildFonts(DeviceDpi);
            if (_exitRequested) BeginInvoke((Action)Close);
        }

        protected override void OnDpiChanged(DpiChangedEventArgs e)
        {
            base.OnDpiChanged(e);
            RebuildFonts(e.DeviceDpiNew);
        }

        private void RebuildFonts(int dpi)
        {
            var old = _fonts;
            _fonts = new ThemeFonts(dpi);
            Font = _fonts.Ui;
            MinimumSize = new Size(Px(720), Px(480));
            ApplyFonts();
            old.Dispose();
            PerformLayout();
        }

        private void ApplyFonts()
        {
            foreach (var c in Controls.OfType<FlatControl>()) c.ApplyFonts(_fonts);
        }

        private int HeaderH => Px(72);
        private int SectionH => Px(36);
        private int SplitH => Px(12);
        private int FooterH => Px(30);

        private int SharedHeight() => ClientSize.Height - HeaderH - Px(6) - SectionH - SplitH - SectionH - Px(10) - FooterH;

        protected override void OnLayout(LayoutEventArgs levent)
        {
            base.OnLayout(levent);
            if (_fonts == null) return;
            int w = ClientSize.Width, h = ClientSize.Height;
            if (w <= 0 || h <= 0) return;
            _header.SetBounds(0, 0, w, HeaderH);
            int y = HeaderH + Px(6);
            _devTitle.SetBounds(0, y, w, SectionH);
            y += SectionH;
            int shared = Math.Max(0, SharedHeight());
            int content = Math.Max(Px(64), _devices.RowCount * _devices.RowHeight);
            // automatic: show every device if the log still keeps about eight lines
            int devH = _devFraction < 0 ? Math.Min(content, Math.Max((int)(shared * 0.6), shared - Px(170))) : (int)(shared * _devFraction);
            devH = Math.Max(Math.Min(Px(64), shared), Math.Min(devH, shared - Px(72)));
            _devices.SetBounds(0, y, w, Math.Max(0, devH));
            y += Math.Max(0, devH);
            _split.SetBounds(0, y, w, SplitH);
            y += SplitH;
            _logTitle.SetBounds(0, y, w, SectionH);
            y += SectionH;
            int logH = Math.Max(0, h - FooterH - Px(10) - y);
            _log.SetBounds(Px(16), y, Math.Max(0, w - Px(32)), logH);
            _footer.SetBounds(0, h - FooterH, w, FooterH);
            _laidOutRows = _devices.RowCount;
        }

        // ------------------------------------------------------------------ refresh

        private void RefreshView()
        {
            SatelliteSnapshot s;
            try { s = _src.Snapshot(); }
            catch { return; }

            _header.Show(s);
            _devices.Show(s.Devices, _src.LocalActions);
            if (_devices.RowCount != _laidOutRows) PerformLayout();
            _devTitle.Summary = Summary(s.Devices);

            var fresh = Log.TailSince(_logSeq, 500);
            if (fresh.Length > 0)
            {
                _logSeq = fresh[fresh.Length - 1].Seq;
                _log.Append(fresh);
            }

            if (s.Link != _lastLink)
            {
                _lastLink = s.Link;
                _tray.Icon = s.Link == LinkState.Connected ? _trayOk
                    : s.Link == LinkState.Rejected || s.Link == LinkState.Stopped ? _trayBad : _trayWarn;
            }
            var tip = Clip63("MAST 卫星 " + s.SatelliteId + "：" + LinkWord(s.Link));
            if (tip != _lastTrayText) { _lastTrayText = tip; _tray.Text = tip; }
        }

        private static string Summary(IReadOnlyList<DeviceRow> devices)
        {
            if (devices.Count == 0) return "";
            var words = new List<string> { devices.Count + " 台" };
            void Add(string word, Func<DeviceRow, bool> pick)
            {
                int n = devices.Count(pick);
                if (n > 0) words.Add(word + " " + n);
            }
            Add("就绪", d => !d.OperatorHold && d.State == DeviceBase.StateReady);
            Add("忙碌", d => !d.OperatorHold && d.State == DeviceBase.StateBusy);
            Add("人工接管", d => d.OperatorHold);
            Add("错误", d => !d.OperatorHold && d.State == DeviceBase.StateError);
            Add("离线", d => !d.OperatorHold && d.State == DeviceBase.StateOffline);
            Add("未启动", d => !d.OperatorHold && d.State == DeviceBase.StateNotStarted);
            return string.Join("    ", words);
        }

        private static string LinkWord(LinkState s)
        {
            switch (s)
            {
                case LinkState.Connected: return "已连接";
                case LinkState.Connecting: return "正在连接";
                case LinkState.Backoff: return "未连接，等待重连";
                case LinkState.Rejected: return "被枢纽拒绝";
                case LinkState.Stopped: return "已停止";
                default: return "未启动";
            }
        }

        private void OnHoldRequested(string id, bool on)
        {
            try { _src.SetOperatorHold(id, on); }
            catch (Exception ex) { Log.Error($"[{id}] operator hold change failed", ex); }
            RefreshView(); // redraw the row at once, not on the next tick
        }

        private void OnActionRequested(string id, string action)
        {
            var msg = "对 " + id + " 执行“" + action + "”？\n正在进行的操作可能被打断。";
            if (MessageBox.Show(this, msg, "本机操作", MessageBoxButtons.OKCancel, MessageBoxIcon.Question) != DialogResult.OK) return;
            try { _src.RunLocalAction(id, action); }
            catch (Exception ex) { Log.Error($"[{id}] local action '{action}' failed", ex); }
        }

        // ------------------------------------------------------------------ tray and exit

        private void ShowFromTray()
        {
            ShowInTaskbar = true;
            Show();
            WindowState = FormWindowState.Normal;
            Activate();
        }

        private void HideToTray(bool showTip)
        {
            ShowInTaskbar = false;
            Hide();
            if (showTip && !_hintShown)
            {
                _hintShown = true;
                _tray.ShowBalloonTip(3500, "MAST 卫星仍在运行", "已收起到托盘。双击图标打开窗口；右键可退出。", ToolTipIcon.Info);
            }
        }

        private void RequestExit()
        {
            var busy = _src.BusyDeviceIds();
            var msg = "退出后 MAST 将无法控制本机设备，LightField 也会随之关闭。"
                + (busy.Count > 0 ? "\n正在执行命令的设备：" + string.Join(", ", busy) : "")
                + "\n\n确定退出卫星吗？";
            if (MessageBox.Show(this, msg, "退出 MAST 卫星", MessageBoxButtons.OKCancel, MessageBoxIcon.Warning) != DialogResult.OK)
                return;
            _exitRequested = true;
            Close();
        }

        /// <summary>Called from --stop / stop file (any thread): close without asking.</summary>
        public void ExitFromRemote()
        {
            if (!IsHandleCreated) { _exitRequested = true; return; } // OnHandleCreated closes it
            if (InvokeRequired) { BeginInvoke((Action)ExitFromRemote); return; }
            _exitRequested = true;
            Close();
        }

        protected override void OnFormClosing(FormClosingEventArgs e)
        {
            if (!_exitRequested && e.CloseReason == CloseReason.UserClosing)
            {
                e.Cancel = true;
                HideToTray(showTip: true);
                return;
            }
            _timer.Stop();
            _tray.Visible = false;
            base.OnFormClosing(e);
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing)
            {
                _timer.Dispose();
                _tray.Dispose();
                _trayMenu.Dispose();
                _appIcon?.Dispose();
                _trayOk?.Dispose();
                _trayWarn?.Dispose();
                _trayBad?.Dispose();
            }
            base.Dispose(disposing);
            if (disposing) _fonts?.Dispose();
        }
    }
}
