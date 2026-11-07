using System;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Security;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using System.Text;

namespace MastSatellite.Core
{
    /// <summary>
    /// The satellite token, DPAPI-protected at rest. The blob is what
    /// deploy\set-token.ps1 writes: ProtectedData.Protect(utf8(token), Entropy, scope).
    /// </summary>
    public static class TokenStore
    {
        /// <summary>Fixed entropy shared with deploy\set-token.ps1 (ASCII).</summary>
        public static readonly byte[] Entropy = Encoding.ASCII.GetBytes("MAST-Satellite-token-v1");

        public static string Load(string path)
        {
            if (string.IsNullOrEmpty(path)) throw new InvalidOperationException("hub.token_file is not configured");
            if (!File.Exists(path)) throw new FileNotFoundException("token file not found (run deploy\\set-token.ps1)", path);
            var blob = File.ReadAllBytes(path);
            byte[] plain;
            try
            {
                plain = ProtectedData.Unprotect(blob, Entropy, DataProtectionScope.CurrentUser);
            }
            catch (CryptographicException first)
            {
                try { plain = ProtectedData.Unprotect(blob, Entropy, DataProtectionScope.LocalMachine); }
                catch (CryptographicException)
                {
                    throw new CryptographicException("cannot decrypt the token file; it must be written by set-token.ps1 as the same Windows user (or with -Scope LocalMachine): " + first.Message);
                }
            }
            var token = Encoding.UTF8.GetString(plain).Trim();
            Array.Clear(plain, 0, plain.Length);
            if (token.Length == 0) throw new InvalidOperationException("token file is empty");
            return token;
        }

        public static void Save(string path, string token, DataProtectionScope scope)
        {
            var dir = Path.GetDirectoryName(Path.GetFullPath(path));
            if (!string.IsNullOrEmpty(dir)) Directory.CreateDirectory(dir);
            var plain = Encoding.UTF8.GetBytes(token.Trim());
            var blob = ProtectedData.Protect(plain, Entropy, scope);
            Array.Clear(plain, 0, plain.Length);
            File.WriteAllBytes(path, blob);
        }
    }

    /// <summary>
    /// SHA-256 over the DER SubjectPublicKeyInfo of a certificate (the "SPKI pin").
    /// .NET Framework has no ExportSubjectPublicKeyInfo, so the SPKI is cut out of the
    /// certificate DER directly.
    /// </summary>
    public static class Spki
    {
        public static string Sha256Hex(X509Certificate cert)
        {
            var der = cert.GetRawCertData();
            var spki = ExtractSpki(der);
            using (var sha = SHA256.Create()) return Hex(sha.ComputeHash(spki));
        }

        public static string Hex(byte[] b)
        {
            var sb = new StringBuilder(b.Length * 2);
            foreach (var x in b) sb.Append(x.ToString("x2"));
            return sb.ToString();
        }

        /// <summary>Certificate ::= SEQUENCE { tbsCertificate, ... }; SPKI is the 7th field of tbs (6th without version).</summary>
        public static byte[] ExtractSpki(byte[] der)
        {
            int pos = 0;
            var cert = ReadTlv(der, ref pos);
            if (cert.Tag != 0x30) throw new FormatException("certificate is not a SEQUENCE");
            int p = cert.ContentStart;
            var tbs = ReadTlv(der, ref p);
            if (tbs.Tag != 0x30) throw new FormatException("tbsCertificate is not a SEQUENCE");
            int q = tbs.ContentStart;
            var field = ReadTlv(der, ref q);
            if (field.Tag == 0xA0) field = ReadTlv(der, ref q); // [0] version -> serialNumber
            // serialNumber already read; now signature, issuer, validity, subject
            for (int i = 0; i < 4; i++) ReadTlv(der, ref q);
            var spki = ReadTlv(der, ref q);
            if (spki.Tag != 0x30) throw new FormatException("subjectPublicKeyInfo is not a SEQUENCE");
            var r = new byte[spki.TotalLength];
            Buffer.BlockCopy(der, spki.Start, r, 0, r.Length);
            return r;
        }

        internal struct Tlv
        {
            public int Tag, Start, ContentStart, ContentLength;
            public int TotalLength => ContentStart - Start + ContentLength;
        }

        internal static Tlv ReadTlv(byte[] d, ref int pos)
        {
            var t = new Tlv { Start = pos };
            if (pos + 2 > d.Length) throw new FormatException("truncated DER");
            t.Tag = d[pos++];
            int len = d[pos++];
            if ((len & 0x80) != 0)
            {
                int n = len & 0x7F;
                if (n == 0 || n > 4 || pos + n > d.Length) throw new FormatException("bad DER length");
                len = 0;
                for (int i = 0; i < n; i++) len = (len << 8) | d[pos++];
            }
            if (len < 0 || pos + len > d.Length) throw new FormatException("DER length out of range");
            t.ContentStart = pos;
            t.ContentLength = len;
            pos += len;
            return t;
        }

        /// <summary>Re-encode SPKI from the parsed public key; used by the self-test to cross-check ExtractSpki.</summary>
        public static byte[] EncodeSpki(PublicKey pk)
        {
            var oid = EncodeOid(pk.Oid.Value);
            var parms = pk.EncodedParameters?.RawData;
            var algBody = parms == null || parms.Length == 0 ? oid : oid.Concat(parms).ToArray();
            var alg = Wrap(0x30, algBody);
            var key = pk.EncodedKeyValue.RawData;
            var bits = Wrap(0x03, new byte[] { 0 }.Concat(key).ToArray());
            return Wrap(0x30, alg.Concat(bits).ToArray());
        }

        private static byte[] EncodeOid(string dotted)
        {
            var parts = dotted.Split('.').Select(ulong.Parse).ToArray();
            var body = new System.Collections.Generic.List<byte> { (byte)(parts[0] * 40 + parts[1]) };
            for (int i = 2; i < parts.Length; i++)
            {
                var v = parts[i];
                var stack = new System.Collections.Generic.Stack<byte>();
                stack.Push((byte)(v & 0x7F));
                v >>= 7;
                while (v > 0) { stack.Push((byte)(0x80 | (v & 0x7F))); v >>= 7; }
                body.AddRange(stack);
            }
            return Wrap(0x06, body.ToArray());
        }

        private static byte[] Wrap(int tag, byte[] body)
        {
            byte[] len;
            if (body.Length < 0x80) len = new[] { (byte)body.Length };
            else if (body.Length < 0x100) len = new byte[] { 0x81, (byte)body.Length };
            else if (body.Length < 0x10000) len = new byte[] { 0x82, (byte)(body.Length >> 8), (byte)body.Length };
            else len = new byte[] { 0x83, (byte)(body.Length >> 16), (byte)(body.Length >> 8), (byte)body.Length };
            return new[] { (byte)tag }.Concat(len).Concat(body).ToArray();
        }
    }

    /// <summary>
    /// Certificate validation for the hub link. .NET Framework's ClientWebSocket has no per-socket
    /// callback, so this goes through ServicePointManager: connections to the hub endpoint are
    /// accepted only when the SPKI pin matches (no CA chain, no host-name check); anything else
    /// keeps normal validation.
    /// </summary>
    public static class TlsPinning
    {
        private static readonly object Gate = new object();
        private static string _host;
        private static int _port;
        private static string _pin;
        private static bool _installed;

        /// <summary>The pin of the last certificate the hub presented (for diagnostics and --print-spki).</summary>
        public static string LastSeenPin { get; private set; }
        public static string LastSeenSubject { get; private set; }

        public static void Configure(Uri hub, string pinHex)
        {
            lock (Gate)
            {
                _host = hub.Host;
                _port = hub.Port;
                _pin = (pinHex ?? "").Trim().ToLowerInvariant();
                if (!_installed)
                {
                    ServicePointManager.ServerCertificateValidationCallback += Validate;
                    _installed = true;
                }
            }
        }

        private static bool Validate(object sender, X509Certificate cert, X509Chain chain, SslPolicyErrors errors)
        {
            Uri target = (sender as HttpWebRequest)?.RequestUri;
            string pin = null;
            try
            {
                if (cert != null)
                {
                    pin = Spki.Sha256Hex(cert);
                    LastSeenPin = pin;
                    LastSeenSubject = cert.Subject;
                }
            }
            catch (Exception ex)
            {
                Log.Warn("SPKI extraction failed: " + ex.Message);
            }
            string want;
            string host;
            int port;
            lock (Gate) { want = _pin; host = _host; port = _port; }
            bool isHub = target != null && string.Equals(target.Host, host, StringComparison.OrdinalIgnoreCase) && target.Port == port;
            bool pinOk = pin != null && !string.IsNullOrEmpty(want) && FixedTimeEquals(pin, want);
            if (isHub)
            {
                if (!pinOk) Log.Error($"hub certificate pin mismatch: presented spki_sha256={pin ?? "?"} configured={want}");
                return pinOk;
            }
            return pinOk || errors == SslPolicyErrors.None;
        }

        public static bool FixedTimeEquals(string a, string b)
        {
            if (a == null || b == null || a.Length != b.Length) return false;
            int diff = 0;
            for (int i = 0; i < a.Length; i++) diff |= a[i] ^ b[i];
            return diff == 0;
        }
    }
}
