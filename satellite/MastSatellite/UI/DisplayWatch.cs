using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;
using System.Windows.Forms;
using MastSatellite.Core;

namespace MastSatellite.UI
{
    /// <summary>
    /// Records display events of the interactive session in the satellite log, so a reported
    /// "screen went black" can be matched against what Windows saw: topology / mode changes,
    /// monitor arrival or removal, and the console display state (on / off / dimmed).
    /// Observes only; changes nothing. Lines are written only when something changed.
    /// </summary>
    public sealed class DisplayWatch : NativeWindow, IDisposable
    {
        private const int WM_DISPLAYCHANGE = 0x007E;
        private const int WM_SETTINGCHANGE = 0x001A;
        private const int WM_DEVICECHANGE = 0x0219;
        private const int WM_POWERBROADCAST = 0x0218;
        private const int PBT_POWERSETTINGCHANGE = 0x8013;
        private const int DBT_DEVNODES_CHANGED = 0x0007;
        private const int WS_EX_TOOLWINDOW = 0x00000080;
        private const int ENUM_CURRENT_SETTINGS = -1;

        private static readonly Guid ConsoleDisplayState = new Guid("6FE69556-704A-47A0-8F24-C28D936FDA47");

        private readonly Timer _poll = new Timer { Interval = 2000 };
        private IntPtr _powerNotify;
        private string _lastTopology;
        private string _lastMonitors;
        private int _lastConsoleState = -1;

        public DisplayWatch()
        {
            CreateHandle(new CreateParams { Caption = "MastSatellite.DisplayWatch", ExStyle = WS_EX_TOOLWINDOW });
            var guid = ConsoleDisplayState;
            try { _powerNotify = RegisterPowerSettingNotification(Handle, ref guid, 0); } catch { _powerNotify = IntPtr.Zero; }
            _lastTopology = DescribeDisplays();
            _lastMonitors = DescribeMonitors();
            Log.Info("[display] watching: " + _lastTopology + " | monitors: " + _lastMonitors);
            _poll.Tick += (s, e) => Check("poll");
            _poll.Start();
        }

        protected override void WndProc(ref Message m)
        {
            try
            {
                switch (m.Msg)
                {
                    case WM_DISPLAYCHANGE:
                        Check(string.Format(CultureInfo.InvariantCulture, "WM_DISPLAYCHANGE {0}x{1} {2}bpp",
                            (long)m.LParam & 0xFFFF, ((long)m.LParam >> 16) & 0xFFFF, (long)m.WParam));
                        break;
                    case WM_DEVICECHANGE:
                        if ((long)m.WParam == DBT_DEVNODES_CHANGED) Check("devnodes changed");
                        break;
                    case WM_SETTINGCHANGE:
                        Check("setting change");
                        break;
                    case WM_POWERBROADCAST:
                        if ((long)m.WParam == PBT_POWERSETTINGCHANGE && m.LParam != IntPtr.Zero) OnPowerSetting(m.LParam);
                        break;
                }
            }
            catch (Exception ex)
            {
                Log.Debug("[display] handler failed: " + ex.Message);
            }
            base.WndProc(ref m);
        }

        private void OnPowerSetting(IntPtr lParam)
        {
            var ps = (POWERBROADCAST_SETTING)Marshal.PtrToStructure(lParam, typeof(POWERBROADCAST_SETTING));
            if (ps.PowerSetting != ConsoleDisplayState || ps.DataLength < 4) return;
            int state = Marshal.ReadInt32(lParam, 20);
            if (state == _lastConsoleState) return;
            string text = state == 0 ? "off" : state == 1 ? "on" : state == 2 ? "dimmed" : state.ToString(CultureInfo.InvariantCulture);
            if (_lastConsoleState >= 0 || state != 1) Log.Info("[display] console display state: " + text);
            _lastConsoleState = state;
        }

        private void Check(string why)
        {
            var topo = DescribeDisplays();
            var mons = DescribeMonitors();
            if (topo == _lastTopology && mons == _lastMonitors) return;
            var sb = new StringBuilder("[display] changed (" + why + "): ");
            if (topo != _lastTopology) sb.Append(_lastTopology + " -> " + topo);
            if (mons != _lastMonitors)
            {
                if (topo != _lastTopology) sb.Append(" | ");
                sb.Append("monitors " + _lastMonitors + " -> " + mons);
            }
            Log.Info(sb.ToString());
            _lastTopology = topo;
            _lastMonitors = mons;
        }

        /// <summary>Active screens with their current mode, e.g. "DISPLAY1 2560x1440@60 primary; DISPLAY2 1920x1080@60".</summary>
        public static string DescribeDisplays()
        {
            try
            {
                var parts = new List<string>();
                foreach (var s in Screen.AllScreens.OrderBy(x => x.DeviceName, StringComparer.Ordinal))
                {
                    var dm = new DEVMODE { dmSize = (short)Marshal.SizeOf(typeof(DEVMODE)) };
                    string mode = EnumDisplaySettings(s.DeviceName, ENUM_CURRENT_SETTINGS, ref dm)
                        ? string.Format(CultureInfo.InvariantCulture, "{0}x{1}@{2}", dm.dmPelsWidth, dm.dmPelsHeight, dm.dmDisplayFrequency)
                        : s.Bounds.Width + "x" + s.Bounds.Height;
                    parts.Add(string.Format(CultureInfo.InvariantCulture, "{0} {1} at {2},{3}{4}",
                        s.DeviceName.TrimStart('\\', '.'), mode, s.Bounds.X, s.Bounds.Y, s.Primary ? " primary" : ""));
                }
                return parts.Count == 0 ? "no screens" : string.Join("; ", parts);
            }
            catch (Exception ex)
            {
                return "unavailable (" + ex.GetType().Name + ")";
            }
        }

        /// <summary>Attached monitors per adapter output, from EnumDisplayDevices.</summary>
        public static string DescribeMonitors()
        {
            try
            {
                var parts = new List<string>();
                for (uint i = 0; ; i++)
                {
                    var ad = new DISPLAY_DEVICE { cb = Marshal.SizeOf(typeof(DISPLAY_DEVICE)) };
                    if (!EnumDisplayDevices(null, i, ref ad, 0)) break;
                    if ((ad.StateFlags & 0x1) == 0) continue; // DISPLAY_DEVICE_ATTACHED_TO_DESKTOP
                    for (uint j = 0; ; j++)
                    {
                        var mon = new DISPLAY_DEVICE { cb = Marshal.SizeOf(typeof(DISPLAY_DEVICE)) };
                        if (!EnumDisplayDevices(ad.DeviceName, j, ref mon, 0)) break;
                        if ((mon.StateFlags & 0x1) == 0) continue; // DISPLAY_DEVICE_ACTIVE
                        parts.Add(ad.DeviceName.TrimStart('\\', '.') + "=" + mon.DeviceString);
                    }
                }
                return parts.Count == 0 ? "none" : string.Join(", ", parts);
            }
            catch (Exception ex)
            {
                return "unavailable (" + ex.GetType().Name + ")";
            }
        }

        public void Dispose()
        {
            _poll.Stop();
            _poll.Dispose();
            if (_powerNotify != IntPtr.Zero) { try { UnregisterPowerSettingNotification(_powerNotify); } catch { } _powerNotify = IntPtr.Zero; }
            if (Handle != IntPtr.Zero) DestroyHandle();
        }

        [StructLayout(LayoutKind.Sequential, Pack = 4)]
        private struct POWERBROADCAST_SETTING
        {
            public Guid PowerSetting;
            public int DataLength;
        }

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct DISPLAY_DEVICE
        {
            public int cb;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string DeviceName;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string DeviceString;
            public int StateFlags;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string DeviceID;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string DeviceKey;
        }

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct DEVMODE
        {
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string dmDeviceName;
            public short dmSpecVersion, dmDriverVersion, dmSize, dmDriverExtra;
            public int dmFields;
            public int dmPositionX, dmPositionY, dmDisplayOrientation, dmDisplayFixedOutput;
            public short dmColor, dmDuplex, dmYResolution, dmTTOption, dmCollate;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string dmFormName;
            public short dmLogPixels;
            public int dmBitsPerPel, dmPelsWidth, dmPelsHeight, dmDisplayFlags, dmDisplayFrequency;
            public int dmICMMethod, dmICMIntent, dmMediaType, dmDitherType, dmReserved1, dmReserved2, dmPanningWidth, dmPanningHeight;
        }

        [DllImport("user32.dll", SetLastError = true)]
        private static extern IntPtr RegisterPowerSettingNotification(IntPtr hRecipient, [In] ref Guid powerSettingGuid, int flags);

        [DllImport("user32.dll", SetLastError = true)]
        private static extern bool UnregisterPowerSettingNotification(IntPtr handle);

        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        private static extern bool EnumDisplaySettings(string deviceName, int modeNum, ref DEVMODE devMode);

        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        private static extern bool EnumDisplayDevices(string device, uint devNum, ref DISPLAY_DEVICE displayDevice, uint flags);
    }
}
