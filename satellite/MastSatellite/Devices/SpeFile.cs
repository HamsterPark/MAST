using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text;
using System.Xml;
using MastSatellite.Core;

namespace MastSatellite.Devices
{
    /// <summary>
    /// SPE 3.0 (Teledyne Princeton Instruments "SPE 3.0 File Format Specification", Issue 7):
    /// 4100-byte binary header (xml_footer_offset u64 @678, file_header_ver f32 @1992), binary
    /// frames from offset 4100, XML footer. Reads the footer into a compact metadata summary;
    /// also writes minimal SPE 3.0 files for the simulated spectrometer.
    /// </summary>
    public sealed class SpeInfo
    {
        public float HeaderVersion;
        public long FooterOffset;
        public long FileSize;
        public int Frames;
        public int Width;
        public int Height;
        public string PixelFormat;
        public long FrameSize;
        public long FrameStride;
        public double? LaserLineNm;
        public double[] Wavelength;
        public Dictionary<string, object> Meta = new Dictionary<string, object>(StringComparer.Ordinal);
    }

    public static class SpeFile
    {
        public const int HeaderSize = 4100;
        private const string SpeNs = "http://www.princetoninstruments.com/spe/2009";

        public static SpeInfo Read(string path, bool keepWavelength = false, int maxFooterBytes = 16 * 1024 * 1024)
        {
            using (var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
                return Read(fs, keepWavelength, maxFooterBytes);
        }

        public static SpeInfo Read(Stream fs, bool keepWavelength, int maxFooterBytes)
        {
            var header = new byte[HeaderSize];
            ReadExactly(fs, 0, header, HeaderSize);
            var info = new SpeInfo
            {
                FileSize = fs.Length,
                HeaderVersion = BitConverter.ToSingle(header, 1992),
                FooterOffset = (long)BitConverter.ToUInt64(header, 678),
            };
            // SPE 2.x compatible fields (LightField fills these too).
            int xdim = BitConverter.ToUInt16(header, 42);
            int ydim = BitConverter.ToUInt16(header, 656);
            int nframes = BitConverter.ToInt32(header, 1446);
            short datatype = BitConverter.ToInt16(header, 108);
            info.Width = xdim;
            info.Height = ydim;
            info.Frames = nframes;
            info.PixelFormat = datatype == 3 ? "MonochromeUnsigned16" : datatype == 8 ? "MonochromeUnsigned32" : datatype == 0 ? "MonochromeFloating32" : "datatype" + datatype;

            if (info.HeaderVersion >= 3.0f && info.FooterOffset > HeaderSize && info.FooterOffset < info.FileSize)
            {
                long n = info.FileSize - info.FooterOffset;
                if (n > maxFooterBytes) throw new InvalidDataException($"SPE footer of {n} bytes exceeds the limit");
                var buf = new byte[n];
                ReadExactly(fs, info.FooterOffset, buf, (int)n);
                ParseFooter(info, DecodeXml(buf), keepWavelength);
            }
            else
            {
                info.Meta["spe_version"] = info.HeaderVersion;
                info.Meta["footer"] = false;
            }
            int bpp = info.PixelFormat == "MonochromeUnsigned16" ? 2 : 4;
            if (info.FrameSize == 0) info.FrameSize = (long)info.Width * info.Height * bpp;
            if (info.FrameStride == 0) info.FrameStride = info.FrameSize;
            info.Meta["frames"] = info.Frames;
            info.Meta["width"] = info.Width;
            info.Meta["height"] = info.Height;
            info.Meta["pixel_format"] = info.PixelFormat;
            info.Meta["frame_stride"] = info.FrameStride;
            return info;
        }

        private static string DecodeXml(byte[] buf)
        {
            int start = 0;
            if (buf.Length >= 3 && buf[0] == 0xEF && buf[1] == 0xBB && buf[2] == 0xBF) start = 3;
            int end = buf.Length;
            while (end > start && (buf[end - 1] == 0 || buf[end - 1] == '\n' || buf[end - 1] == '\r' || buf[end - 1] == ' ')) end--;
            return Encoding.UTF8.GetString(buf, start, end - start);
        }

        private static void ReadExactly(Stream s, long offset, byte[] buf, int count)
        {
            s.Position = offset;
            int got = 0;
            while (got < count)
            {
                int r = s.Read(buf, got, count - got);
                if (r <= 0) throw new EndOfStreamException("SPE file truncated");
                got += r;
            }
        }

        public static void ParseFooter(SpeInfo info, string xml, bool keepWavelength)
        {
            var doc = new XmlDocument { XmlResolver = null };
            using (var sr = new StringReader(xml))
            using (var xr = XmlReader.Create(sr, new XmlReaderSettings { DtdProcessing = DtdProcessing.Prohibit, XmlResolver = null }))
                doc.Load(xr);
            var root = doc.DocumentElement;
            if (root == null || root.LocalName != "SpeFormat") throw new InvalidDataException("SPE footer root is not SpeFormat");
            var m = info.Meta;
            m["spe_version"] = root.GetAttribute("version");
            m["footer"] = true;

            var frame = Find(root, "DataFormat", "DataBlock");
            if (frame != null && frame.GetAttribute("type") == "Frame")
            {
                info.Frames = AttrInt(frame, "count", info.Frames);
                info.PixelFormat = Attr(frame, "pixelFormat") ?? info.PixelFormat;
                info.FrameSize = AttrInt(frame, "size", 0);
                info.FrameStride = AttrInt(frame, "stride", 0);
                var regions = frame.ChildNodes.OfType<XmlElement>().Where(e => e.LocalName == "DataBlock" && e.GetAttribute("type") == "Region").ToList();
                if (regions.Count > 0)
                {
                    info.Width = AttrInt(regions[0], "width", info.Width);
                    info.Height = regions.Sum(r => AttrInt(r, "height", 0));
                    m["regions"] = regions.Count;
                }
            }
            var meta = Find(root, "MetaFormat");
            if (meta != null)
            {
                m["frame_metadata"] = meta.ChildNodes.OfType<XmlElement>()
                    .Select(e => (e.GetAttribute("type") + " " + e.GetAttribute("event")).Trim()).ToList();
            }

            var wm = Find(root, "Calibrations", "WavelengthMapping");
            if (wm != null)
            {
                if (double.TryParse(wm.GetAttribute("laserLine"), NumberStyles.Float, CultureInfo.InvariantCulture, out var ll))
                {
                    info.LaserLineNm = ll;
                    m["laser_line_nm"] = ll;
                }
                m["wavelength_calibration_date"] = Attr(wm, "date");
                var wl = Find(wm, "Wavelength") ?? Find(wm, "WavelengthError");
                if (wl != null)
                {
                    var arr = ParseDoubles(wl.InnerText);
                    if (arr.Length > 0)
                    {
                        m["wavelength_count"] = arr.Length;
                        m["wavelength_first_nm"] = arr[0];
                        m["wavelength_last_nm"] = arr[arr.Length - 1];
                        if (keepWavelength) info.Wavelength = arr;
                    }
                }
            }
            var sm = Find(root, "Calibrations", "SensorMapping");
            if (sm != null)
            {
                m["sensor_roi"] = J.Obj("x", AttrInt(sm, "x", 0), "y", AttrInt(sm, "y", 0), "width", AttrInt(sm, "width", 0),
                                        "height", AttrInt(sm, "height", 0), "x_binning", AttrInt(sm, "xBinning", 1), "y_binning", AttrInt(sm, "yBinning", 1));
            }

            var origin = Find(root, "DataHistories", "DataHistory", "Origin");
            if (origin != null)
            {
                m["acquired_created"] = Attr(origin, "created");
                m["software"] = (Attr(origin, "software") + " " + Attr(origin, "softwareVersion")).Trim();
                var exp = Find(origin, "Experiment");
                if (exp != null) ExtractExperiment(exp, m);
            }
            var fi = Find(root, "GeneralInformation", "FileInformation");
            if (fi != null)
            {
                m["file_created"] = Attr(fi, "created");
                m["file_last_modified"] = Attr(fi, "lastModified");
            }
        }

        // Settings that matter for every spectrum (契约 §11.3); paths mirror the setting keys.
        private static readonly string[][] ExperimentFields =
        {
            new[] { "exposure_ms", "Devices/Cameras/Camera/ShutterTiming/ExposureTime" },
            new[] { "shutter_mode", "Devices/Cameras/Camera/ShutterTiming/Mode" },
            new[] { "frames_to_store", "Devices/Cameras/Camera/Experiment/Acquisition/FramesToStore" },
            new[] { "temperature_setpoint_c", "Devices/Cameras/Camera/Sensor/Temperature/SetPoint" },
            new[] { "temperature_c", "Devices/Cameras/Camera/Sensor/Temperature/Reading" },
            new[] { "temperature_status", "Devices/Cameras/Camera/Sensor/Temperature/Status" },
            new[] { "adc_mhz", "Devices/Cameras/Camera/Adc/Speed" },
            new[] { "adc_quality", "Devices/Cameras/Camera/Adc/Quality" },
            new[] { "adc_gain", "Devices/Cameras/Camera/Adc/AnalogGain" },
            new[] { "readout_ms", "Devices/Cameras/Camera/ReadoutControl/Time" },
            new[] { "roi_selection", "Devices/Cameras/Camera/ReadoutControl/RegionsOfInterest/Selection" },
            new[] { "trigger_response", "Devices/Cameras/Camera/HardwareIO/Trigger/Response" },
            new[] { "trigger_edge", "Devices/Cameras/Camera/HardwareIO/Trigger/Determination" },
            new[] { "output_signal", "Devices/Cameras/Camera/HardwareIO/OutputSignal" },
            new[] { "timestamps", "Devices/Cameras/Camera/Acquisition/TimeStamping/Stamps" },
            new[] { "frame_tracking", "Devices/Cameras/Camera/Acquisition/FrameTracking/Enabled" },
            new[] { "base_file_name", "Devices/Cameras/Camera/Experiment/FileNameGeneration/BaseFileName" },
            new[] { "directory", "Devices/Cameras/Camera/Experiment/FileNameGeneration/Directory" },
            new[] { "grating", "Devices/Spectrometers/Spectrometer/Grating/Selected" },
            new[] { "center_wavelength_nm", "Devices/Spectrometers/Spectrometer/Grating/CenterWavelength" },
            new[] { "exit_port", "Devices/Spectrometers/Spectrometer/OpticalPort/Exit/Selected" },
        };

        private static void ExtractExperiment(XmlElement exp, Dictionary<string, object> m)
        {
            foreach (var f in ExperimentFields)
            {
                var e = Find(exp, f[1].Split('/'));
                if (e == null) continue;
                var text = e.InnerText;
                if (string.IsNullOrEmpty(text)) continue;
                var type = e.GetAttribute("type");
                if ((type == "Double" || type == "Int32" || type == "Int64") &&
                    double.TryParse(text, NumberStyles.Float, CultureInfo.InvariantCulture, out var d)) m[f[0]] = d;
                else if (type == "Boolean" && bool.TryParse(text, out var b)) m[f[0]] = b;
                else m[f[0]] = text;
            }
            var cam = Find(exp, "System", "Cameras", "Camera");
            if (cam != null)
            {
                m["camera_model"] = Attr(cam, "model");
                m["camera_serial"] = Attr(cam, "serialNumber");
            }
            var spec = Find(exp, "System", "Spectrometers", "Spectrometer");
            if (spec != null)
            {
                m["spectrometer_model"] = Attr(spec, "model");
                m["spectrometer_serial"] = Attr(spec, "serialNumber");
            }
        }

        /// <summary>Descends by local names (namespace-agnostic), taking the first match at each level.</summary>
        private static XmlElement Find(XmlElement start, params string[] names)
        {
            var cur = start;
            foreach (var n in names)
            {
                XmlElement next = null;
                foreach (XmlNode c in cur.ChildNodes)
                {
                    if (c is XmlElement e && e.LocalName == n) { next = e; break; }
                }
                if (next == null) return null;
                cur = next;
            }
            return cur;
        }

        private static string Attr(XmlElement e, string name)
        {
            var v = e.GetAttribute(name);
            return string.IsNullOrEmpty(v) ? null : v;
        }

        private static int AttrInt(XmlElement e, string name, int def)
        {
            return int.TryParse(e.GetAttribute(name), NumberStyles.Integer, CultureInfo.InvariantCulture, out var v) ? v : def;
        }

        public static double[] ParseDoubles(string text)
        {
            var parts = text.Split(new[] { ',', ' ', '\n', '\r', '\t' }, StringSplitOptions.RemoveEmptyEntries);
            var r = new List<double>(parts.Length);
            foreach (var p in parts)
                if (double.TryParse(p, NumberStyles.Float, CultureInfo.InvariantCulture, out var d)) r.Add(d);
            return r.ToArray();
        }

        /// <summary>Reads frame i (first region, u16 data) as a flat row-major array.</summary>
        public static ushort[] ReadFrameU16(string path, SpeInfo info, int frameIndex)
        {
            if (info.PixelFormat != "MonochromeUnsigned16") throw new NotSupportedException(info.PixelFormat);
            int n = info.Width * Math.Max(1, info.Height);
            var bytes = new byte[n * 2];
            using (var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
                ReadExactly(fs, HeaderSize + frameIndex * info.FrameStride, bytes, bytes.Length);
            var r = new ushort[n];
            Buffer.BlockCopy(bytes, 0, r, 0, bytes.Length);
            return r;
        }

        /// <summary>Writes a minimal but spec-conformant SPE 3.0 file (one 1-row region per frame, u16).</summary>
        public static void WriteU16(string path, IList<ushort[]> frames, double[] wavelengthNm, double laserLineNm,
                                    IDictionary<string, string> cameraSettings, DateTimeOffset created)
        {
            int width = frames[0].Length;
            int frameBytes = width * 2;
            long footerOffset = HeaderSize + (long)frameBytes * frames.Count;
            var header = new byte[HeaderSize];
            PutU16(header, 6, width);
            PutU16(header, 18, 1);
            PutI16(header, 34, -1);
            PutU16(header, 42, width);
            PutI16(header, 108, 3);
            PutU16(header, 656, 1);
            PutI16(header, 658, 1);
            PutI32(header, 664, -1);
            Buffer.BlockCopy(BitConverter.GetBytes((ulong)footerOffset), 0, header, 678, 8);
            PutI32(header, 1446, frames.Count);
            Buffer.BlockCopy(BitConverter.GetBytes(3.0f), 0, header, 1992, 4);
            PutI32(header, 2996, 0x01234567);
            PutI16(header, 4098, 0x5555);

            var inv = CultureInfo.InvariantCulture;
            var sb = new StringBuilder(64 * 1024);
            sb.Append("<SpeFormat version=\"3.0\" xmlns=\"").Append(SpeNs).Append("\"><DataFormat>");
            sb.AppendFormat(inv, "<DataBlock type=\"Frame\" count=\"{0}\" pixelFormat=\"MonochromeUnsigned16\" size=\"{1}\" stride=\"{1}\" calibrations=\"1\">", frames.Count, frameBytes);
            sb.AppendFormat(inv, "<DataBlock type=\"Region\" calibrations=\"2,3\" count=\"1\" width=\"{0}\" height=\"1\" size=\"{1}\" stride=\"{1}\" />", width, frameBytes);
            sb.Append("</DataBlock></DataFormat><Calibrations>");
            sb.AppendFormat(inv, "<WavelengthMapping id=\"1\" date=\"{0}\" orientation=\"Normal\" laserLine=\"{1}\"><Wavelength xml:space=\"preserve\">",
                created.ToString("o", inv), laserLineNm.ToString("R", inv));
            for (int i = 0; i < wavelengthNm.Length; i++)
            {
                if (i > 0) sb.Append(',');
                sb.Append(wavelengthNm[i].ToString("R", inv));
            }
            sb.Append("</Wavelength></WavelengthMapping>");
            sb.AppendFormat(inv, "<SensorInformation id=\"2\" orientation=\"Normal\" width=\"{0}\" height=\"100\" />", width);
            sb.AppendFormat(inv, "<SensorMapping id=\"3\" x=\"0\" y=\"25\" height=\"50\" width=\"{0}\" xBinning=\"1\" yBinning=\"50\" />", width);
            sb.Append("</Calibrations><DataHistories><DataHistory id=\"1\">");
            sb.AppendFormat(inv, "<Origin creator=\"MastSatellite\" created=\"{0}\" software=\"MastSatellite (simulated LightField)\" softwareVersion=\"1.0\">", created.ToString("o", inv));
            sb.Append("<Experiment xmlns=\"http://www.princetoninstruments.com/experiment/2009\" version=\"12\"><System>");
            sb.Append("<Spectrometers count=\"1\"><Spectrometer deviceID=\"1\" model=\"SIM-HRS-300\" serialNumber=\"SIM0001\" computerInterface=\"USB\" /></Spectrometers>");
            sb.Append("<Cameras count=\"1\"><Camera deviceID=\"2\" model=\"SIM-PyLoN-100B\" serialNumber=\"SIM0002\" computerInterface=\"Gigabit Ethernet\" demo=\"True\" /></Cameras>");
            sb.Append("</System><Devices><Cameras count=\"1\"><Camera deviceID=\"2\">");
            AppendSettings(sb, cameraSettings, "Camera.");
            sb.Append("</Camera></Cameras><Spectrometers count=\"1\"><Spectrometer deviceID=\"1\">");
            AppendSettings(sb, cameraSettings, "Spectrometer.");
            sb.Append("</Spectrometer></Spectrometers></Devices></Experiment></Origin></DataHistory></DataHistories>");
            sb.AppendFormat(inv, "<GeneralInformation><FileInformation creator=\"MastSatellite\" created=\"{0}\" lastModified=\"{0}\" /></GeneralInformation></SpeFormat>", created.ToString("o", inv));

            var dir = Path.GetDirectoryName(Path.GetFullPath(path));
            if (!string.IsNullOrEmpty(dir)) Directory.CreateDirectory(dir);
            using (var fs = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.Read))
            {
                fs.Write(header, 0, header.Length);
                var buf = new byte[frameBytes];
                foreach (var f in frames)
                {
                    Buffer.BlockCopy(f, 0, buf, 0, frameBytes);
                    fs.Write(buf, 0, frameBytes);
                }
                var x = Encoding.UTF8.GetBytes(sb.ToString());
                fs.Write(x, 0, x.Length);
            }
        }

        private sealed class Node
        {
            public readonly SortedDictionary<string, Node> Children = new SortedDictionary<string, Node>(StringComparer.Ordinal);
            public string Value;
            public string Type;
        }

        /// <summary>"Camera.ShutterTiming.ExposureTime" = "1000|Double" -> nested elements under the device (one tree, like LightField).</summary>
        private static void AppendSettings(StringBuilder sb, IDictionary<string, string> settings, string prefix)
        {
            if (settings == null) return;
            var root = new Node();
            foreach (var kv in settings.Where(k => k.Key.StartsWith(prefix, StringComparison.Ordinal)))
            {
                var cur = root;
                foreach (var p in kv.Key.Substring(prefix.Length).Split('.'))
                {
                    if (!cur.Children.TryGetValue(p, out var nx)) cur.Children[p] = nx = new Node();
                    cur = nx;
                }
                var vt = kv.Value.Split(new[] { '|' }, 2);
                cur.Value = vt[0];
                cur.Type = vt.Length > 1 ? vt[1] : "String";
            }
            AppendNode(sb, root);
        }

        private static void AppendNode(StringBuilder sb, Node n)
        {
            foreach (var kv in n.Children)
            {
                sb.Append('<').Append(kv.Key);
                if (kv.Value.Value != null)
                {
                    sb.Append(" type=\"").Append(kv.Value.Type).Append("\">").Append(System.Security.SecurityElement.Escape(kv.Value.Value));
                }
                else
                {
                    sb.Append('>');
                    AppendNode(sb, kv.Value);
                }
                sb.Append("</").Append(kv.Key).Append('>');
            }
        }

        private static void PutU16(byte[] b, int off, int v) { b[off] = (byte)v; b[off + 1] = (byte)(v >> 8); }
        private static void PutI16(byte[] b, int off, int v) { PutU16(b, off, v & 0xFFFF); }
        private static void PutI32(byte[] b, int off, int v) { Buffer.BlockCopy(BitConverter.GetBytes(v), 0, b, off, 4); }
    }
}
