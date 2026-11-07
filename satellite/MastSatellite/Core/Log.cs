using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text;

namespace MastSatellite.Core
{
    public enum LogLevel { Debug = 0, Info = 1, Warn = 2, Error = 3 }

    /// <summary>One recorded line; Seq increases by one per line for the life of the process.</summary>
    public struct LogEntry
    {
        public long Seq;
        public LogLevel Level;
        public string Line;
    }

    /// <summary>
    /// Process-wide log: daily file under &lt;state_dir&gt;\logs, stdout when one is attached,
    /// and an in-memory tail for the status window. Never throws.
    /// </summary>
    public static class Log
    {
        private static readonly object Gate = new object();
        private static readonly LinkedList<LogEntry> Tail = new LinkedList<LogEntry>();
        private const int TailMax = 1000;
        private static long _seq;
        private static string _dir;
        private static string _currentFile;
        private static StreamWriter _writer;
        private static int _keepDays = 14;

        public static LogLevel MinLevel { get; set; } = LogLevel.Info;
        public static bool EchoToConsole { get; set; }

        /// <summary>Raised after each line is recorded (level, formatted line). Handlers must not block.</summary>
        public static event Action<LogLevel, string> Line;

        public static void Init(string directory, int keepDays)
        {
            lock (Gate)
            {
                _dir = directory;
                _keepDays = Math.Max(1, keepDays);
                try { Directory.CreateDirectory(directory); } catch { }
                Prune();
            }
        }

        public static void Debug(string msg) { Write(LogLevel.Debug, msg); }
        public static void Info(string msg) { Write(LogLevel.Info, msg); }
        public static void Warn(string msg) { Write(LogLevel.Warn, msg); }
        public static void Error(string msg) { Write(LogLevel.Error, msg); }

        public static void Error(string msg, Exception ex)
        {
            Write(LogLevel.Error, msg + ": " + ex.GetType().Name + ": " + ex.Message);
            Write(LogLevel.Debug, ex.ToString());
        }

        public static string[] TailLines(int max)
        {
            lock (Gate) return Tail.Skip(Math.Max(0, Tail.Count - max)).Select(e => e.Line).ToArray();
        }

        /// <summary>Lines recorded after <paramref name="afterSeq"/>, oldest first, at most <paramref name="max"/> (the newest).</summary>
        public static LogEntry[] TailSince(long afterSeq, int max)
        {
            lock (Gate)
            {
                var picked = new List<LogEntry>();
                for (var n = Tail.Last; n != null && n.Value.Seq > afterSeq && picked.Count < max; n = n.Previous) picked.Add(n.Value);
                picked.Reverse();
                return picked.ToArray();
            }
        }

        public static void Write(LogLevel level, string msg)
        {
            if (level < MinLevel) return;
            string line;
            try
            {
                line = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss.fff", CultureInfo.InvariantCulture)
                       + " " + level.ToString().ToUpperInvariant().PadRight(5) + " " + msg;
            }
            catch { return; }
            lock (Gate)
            {
                Tail.AddLast(new LogEntry { Seq = ++_seq, Level = level, Line = line });
                while (Tail.Count > TailMax) Tail.RemoveFirst();
                WriteFile(line);
                if (EchoToConsole)
                {
                    try { Console.Out.WriteLine(line); Console.Out.Flush(); } catch { }
                }
            }
            try { Line?.Invoke(level, line); } catch { }
        }

        private static void WriteFile(string line)
        {
            if (_dir == null) return;
            try
            {
                string name = Path.Combine(_dir, "satellite-" + DateTime.Now.ToString("yyyyMMdd", CultureInfo.InvariantCulture) + ".log");
                if (!string.Equals(name, _currentFile, StringComparison.OrdinalIgnoreCase))
                {
                    _writer?.Dispose();
                    var fs = new FileStream(name, FileMode.Append, FileAccess.Write, FileShare.ReadWrite | FileShare.Delete);
                    _writer = new StreamWriter(fs, new UTF8Encoding(false)) { AutoFlush = true };
                    _currentFile = name;
                    Prune();
                }
                _writer.WriteLine(line);
            }
            catch
            {
                // A full disk or a locked file must never take the satellite down.
            }
        }

        private static void Prune()
        {
            if (_dir == null) return;
            try
            {
                var cutoff = DateTime.Now.AddDays(-_keepDays);
                foreach (var f in Directory.GetFiles(_dir, "satellite-*.log"))
                {
                    if (File.GetLastWriteTime(f) < cutoff) File.Delete(f);
                }
            }
            catch { }
        }

        public static void Close()
        {
            lock (Gate)
            {
                try { _writer?.Dispose(); } catch { }
                _writer = null;
                _currentFile = null;
            }
        }
    }
}
