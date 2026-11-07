using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.Text;
using System.Web.Script.Serialization;

namespace MastSatellite.Core
{
    /// <summary>
    /// JSON in and out. Parsing goes through JavaScriptSerializer (System.Web.Extensions) and is
    /// normalised to Dictionary&lt;string, object&gt; / List&lt;object&gt; / string / long / double /
    /// bool / null. Writing is our own so NaN/Infinity become null and doubles round-trip.
    /// </summary>
    public static class Json
    {
        private static readonly JavaScriptSerializer Serializer = new JavaScriptSerializer
        {
            MaxJsonLength = int.MaxValue,
            RecursionLimit = 64,
        };

        public static object Parse(string text)
        {
            return Normalize(Serializer.DeserializeObject(text));
        }

        public static Dictionary<string, object> ParseObject(string text)
        {
            if (Parse(text) is Dictionary<string, object> d) return d;
            throw new FormatException("JSON object expected");
        }

        private static object Normalize(object o)
        {
            switch (o)
            {
                case null:
                    return null;
                case Dictionary<string, object> d:
                {
                    var r = new Dictionary<string, object>(d.Count, StringComparer.Ordinal);
                    foreach (var kv in d) r[kv.Key] = Normalize(kv.Value);
                    return r;
                }
                case object[] a:
                {
                    var l = new List<object>(a.Length);
                    foreach (var x in a) l.Add(Normalize(x));
                    return l;
                }
                case ArrayList al:
                {
                    var l = new List<object>(al.Count);
                    foreach (var x in al) l.Add(Normalize(x));
                    return l;
                }
                case int i:
                    return (long)i;
                case long l2:
                    return l2;
                case decimal m:
                    return (double)m;
                case float f:
                    return (double)f;
                default:
                    return o;
            }
        }

        public static string Serialize(object o)
        {
            var sb = new StringBuilder(256);
            Write(sb, o);
            return sb.ToString();
        }

        private static void Write(StringBuilder sb, object o)
        {
            switch (o)
            {
                case null:
                    sb.Append("null");
                    return;
                case string s:
                    WriteString(sb, s);
                    return;
                case bool b:
                    sb.Append(b ? "true" : "false");
                    return;
                case double d:
                    WriteDouble(sb, d);
                    return;
                case float f:
                    WriteDouble(sb, f);
                    return;
                case decimal m:
                    sb.Append(m.ToString(CultureInfo.InvariantCulture));
                    return;
                case int _:
                case long _:
                case short _:
                case ushort _:
                case uint _:
                case ulong _:
                case byte _:
                case sbyte _:
                    sb.Append(Convert.ToString(o, CultureInfo.InvariantCulture));
                    return;
                case Enum e:
                    WriteString(sb, e.ToString());
                    return;
                case char c:
                    WriteString(sb, c.ToString());
                    return;
                case Guid g:
                    WriteString(sb, g.ToString("N"));
                    return;
                case DateTime dt:
                    WriteString(sb, dt.ToUniversalTime().ToString("o", CultureInfo.InvariantCulture));
                    return;
                case DateTimeOffset dto:
                    WriteString(sb, dto.ToString("o", CultureInfo.InvariantCulture));
                    return;
                case TimeSpan ts:
                    WriteDouble(sb, ts.TotalMilliseconds);
                    return;
                case RawJson raw:
                    sb.Append(raw.Text);
                    return;
                case IDictionary<string, object> dict:
                {
                    sb.Append('{');
                    bool first = true;
                    foreach (var kv in dict)
                    {
                        if (!first) sb.Append(',');
                        first = false;
                        WriteString(sb, kv.Key);
                        sb.Append(':');
                        Write(sb, kv.Value);
                    }
                    sb.Append('}');
                    return;
                }
                case IDictionary dict2:
                {
                    sb.Append('{');
                    bool first = true;
                    foreach (DictionaryEntry kv in dict2)
                    {
                        if (!first) sb.Append(',');
                        first = false;
                        WriteString(sb, Convert.ToString(kv.Key, CultureInfo.InvariantCulture));
                        sb.Append(':');
                        Write(sb, kv.Value);
                    }
                    sb.Append('}');
                    return;
                }
                case IEnumerable seq:
                {
                    sb.Append('[');
                    bool first = true;
                    foreach (var x in seq)
                    {
                        if (!first) sb.Append(',');
                        first = false;
                        Write(sb, x);
                    }
                    sb.Append(']');
                    return;
                }
                default:
                    WriteString(sb, Convert.ToString(o, CultureInfo.InvariantCulture));
                    return;
            }
        }

        private static void WriteDouble(StringBuilder sb, double d)
        {
            if (double.IsNaN(d) || double.IsInfinity(d)) { sb.Append("null"); return; }
            sb.Append(d.ToString("R", CultureInfo.InvariantCulture));
        }

        private static void WriteString(StringBuilder sb, string s)
        {
            sb.Append('"');
            foreach (char c in s)
            {
                switch (c)
                {
                    case '"': sb.Append("\\\""); break;
                    case '\\': sb.Append("\\\\"); break;
                    case '\n': sb.Append("\\n"); break;
                    case '\r': sb.Append("\\r"); break;
                    case '\t': sb.Append("\\t"); break;
                    case '\b': sb.Append("\\b"); break;
                    case '\f': sb.Append("\\f"); break;
                    default:
                        if (c < 0x20 || c == (char)0x2028 || c == (char)0x2029)
                            sb.Append("\\u").Append(((int)c).ToString("x4", CultureInfo.InvariantCulture));
                        else
                            sb.Append(c);
                        break;
                }
            }
            sb.Append('"');
        }
    }

    /// <summary>A pre-serialised JSON fragment, written verbatim.</summary>
    public sealed class RawJson
    {
        public RawJson(string text) { Text = text; }
        public string Text { get; }
    }

    /// <summary>Typed access to parsed JSON objects. Missing keys give the default.</summary>
    public static class J
    {
        public static Dictionary<string, object> Obj(params object[] kv)
        {
            var d = new Dictionary<string, object>(StringComparer.Ordinal);
            for (int i = 0; i + 1 < kv.Length; i += 2) d[(string)kv[i]] = kv[i + 1];
            return d;
        }

        public static bool Has(IDictionary<string, object> d, string key)
        {
            return d != null && d.TryGetValue(key, out var v) && v != null;
        }

        public static object Get(IDictionary<string, object> d, string key)
        {
            return d != null && d.TryGetValue(key, out var v) ? v : null;
        }

        public static string Str(IDictionary<string, object> d, string key, string def = null)
        {
            var v = Get(d, key);
            if (v == null) return def;
            return v is string s ? s : Convert.ToString(v, CultureInfo.InvariantCulture);
        }

        public static double Num(IDictionary<string, object> d, string key, double def = 0)
        {
            return ToDouble(Get(d, key), def);
        }

        public static double? NumOpt(IDictionary<string, object> d, string key)
        {
            var v = Get(d, key);
            if (v == null) return null;
            double r = ToDouble(v, double.NaN);
            return double.IsNaN(r) ? (double?)null : r;
        }

        public static long Long(IDictionary<string, object> d, string key, long def = 0)
        {
            var v = Get(d, key);
            if (v == null) return def;
            try { return Convert.ToInt64(ToDouble(v, def), CultureInfo.InvariantCulture); }
            catch { return def; }
        }

        public static int Int(IDictionary<string, object> d, string key, int def = 0)
        {
            return (int)Math.Max(int.MinValue, Math.Min(int.MaxValue, Long(d, key, def)));
        }

        public static bool Bool(IDictionary<string, object> d, string key, bool def = false)
        {
            var v = Get(d, key);
            switch (v)
            {
                case null: return def;
                case bool b: return b;
                case string s:
                    if (bool.TryParse(s, out var p)) return p;
                    return s == "1" || s.Equals("yes", StringComparison.OrdinalIgnoreCase) || s.Equals("on", StringComparison.OrdinalIgnoreCase);
                default:
                    return ToDouble(v, 0) != 0;
            }
        }

        public static Dictionary<string, object> Dict(IDictionary<string, object> d, string key)
        {
            return Get(d, key) as Dictionary<string, object>;
        }

        public static List<object> List(IDictionary<string, object> d, string key)
        {
            return Get(d, key) as List<object>;
        }

        public static double ToDouble(object v, double def)
        {
            switch (v)
            {
                case null: return def;
                case double d: return d;
                case long l: return l;
                case int i: return i;
                case float f: return f;
                case decimal m: return (double)m;
                case bool b: return b ? 1 : 0;
                case string s:
                    return double.TryParse(s, NumberStyles.Float, CultureInfo.InvariantCulture, out var r) ? r : def;
                default:
                    try { return Convert.ToDouble(v, CultureInfo.InvariantCulture); }
                    catch { return def; }
            }
        }
    }
}
