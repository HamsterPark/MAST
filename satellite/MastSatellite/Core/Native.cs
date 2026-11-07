using System;
using System.Diagnostics;
using System.Runtime.InteropServices;

namespace MastSatellite.Core
{
    internal static class Native
    {
        [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
        internal static extern IntPtr LoadLibraryEx(string lpFileName, IntPtr hFile, uint dwFlags);

        [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Ansi, ExactSpelling = true)]
        internal static extern IntPtr GetProcAddress(IntPtr hModule, string procName);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool FreeLibrary(IntPtr hModule);

        [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
        [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool SetDllDirectory(string lpPathName);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool AttachConsole(int dwProcessId);

        [DllImport("kernel32.dll", SetLastError = true)]
        internal static extern IntPtr GetStdHandle(int nStdHandle);

        [DllImport("kernel32.dll")]
        internal static extern void GetSystemTimePreciseAsFileTime(out long fileTime);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool ProcessIdToSessionId(uint dwProcessId, out uint pSessionId);

        [DllImport("kernel32.dll")]
        internal static extern uint WTSGetActiveConsoleSessionId();

        internal const uint LOAD_WITH_ALTERED_SEARCH_PATH = 0x00000008;
        internal const int ATTACH_PARENT_PROCESS = -1;
        internal const int STD_OUTPUT_HANDLE = -11;

        /// <summary>
        /// Loads a native DLL by absolute path so its own folder is used to resolve its
        /// dependencies (vendor DLLs sit next to their helpers).
        /// </summary>
        internal static IntPtr LoadVendorDll(string fullPath)
        {
            var h = LoadLibraryEx(fullPath, IntPtr.Zero, LOAD_WITH_ALTERED_SEARCH_PATH);
            if (h == IntPtr.Zero)
                throw new DllNotFoundException($"LoadLibraryEx failed for {fullPath} (win32 error {Marshal.GetLastWin32Error()})");
            return h;
        }

        internal static T GetFunction<T>(IntPtr module, string name) where T : class
        {
            var p = GetProcAddress(module, name);
            if (p == IntPtr.Zero)
                throw new EntryPointNotFoundException($"export {name} not found");
            return Marshal.GetDelegateForFunctionPointer(p, typeof(T)) as T;
        }

        internal static T TryGetFunction<T>(IntPtr module, string name) where T : class
        {
            var p = GetProcAddress(module, name);
            return p == IntPtr.Zero ? null : Marshal.GetDelegateForFunctionPointer(p, typeof(T)) as T;
        }
    }

    /// <summary>Wall clock (unix, sub-ms precise) and a monotonic clock.</summary>
    public static class Clock
    {
        private const long UnixEpochFileTime = 116444736000000000L;
        private static readonly Stopwatch Mono = Stopwatch.StartNew();
        private static readonly bool HasPrecise = ProbePrecise();

        private static bool ProbePrecise()
        {
            try { Native.GetSystemTimePreciseAsFileTime(out _); return true; }
            catch { return false; }
        }

        /// <summary>Unix time in microseconds.</summary>
        public static long UnixUs()
        {
            long ft;
            if (HasPrecise) Native.GetSystemTimePreciseAsFileTime(out ft);
            else ft = DateTime.UtcNow.ToFileTimeUtc();
            return (ft - UnixEpochFileTime) / 10;
        }

        public static long UnixMs() { return UnixUs() / 1000; }

        public static double MonoSeconds => Mono.Elapsed.TotalSeconds;

        public static long MonoMs => Mono.ElapsedMilliseconds;

        public static long UnixMsFromDateTime(DateTime utc)
        {
            return (utc.ToUniversalTime().ToFileTimeUtc() - UnixEpochFileTime) / 10000;
        }
    }

    /// <summary>Which Windows session we run in; LightField / SpectraSense need the desktop.</summary>
    public static class SessionInfo
    {
        public static uint SessionId
        {
            get
            {
                try
                {
                    if (Native.ProcessIdToSessionId((uint)Process.GetCurrentProcess().Id, out var sid)) return sid;
                }
                catch { }
                return uint.MaxValue;
            }
        }

        /// <summary>True when there is a usable interactive desktop (not session 0, not a service).</summary>
        public static bool HasDesktop => SessionId != 0 && Environment.UserInteractive;

        public static string Describe()
        {
            return $"session={SessionId} interactive={Environment.UserInteractive} console_session={SafeConsoleSession()}";
        }

        private static uint SafeConsoleSession()
        {
            try { return Native.WTSGetActiveConsoleSessionId(); } catch { return uint.MaxValue; }
        }
    }
}
