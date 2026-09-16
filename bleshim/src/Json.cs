// A JSON reader and writer small enough to read in one sitting.
//
// The shim talks to blehelper.py in newline-delimited JSON (see Backend.cs for
// why there is a helper at all).  wine-mono's BCL has no JSON of any kind --
// System.Text.Json is .NET Core, and DataContractJsonSerializer wants types and
// contracts we would only be fighting -- and pulling a NuGet assembly into an
// assembly that has to load out of the prefix's mono tree buys a second thing
// to install for a format we own both ends of.
//
// So: objects are Dictionary<string, object>, arrays are List<object>, numbers
// are double, and everything else is string, bool or null.  Byte payloads never
// travel as JSON arrays -- they are hex strings, which halves the parsing of
// the one message that arrives at 10 Hz per sensor.

using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text;

namespace MyWhoosh.Ble
{
    static class Json
    {
        // ------------------------------------------------------------ read

        public static Dictionary<string, object> ParseObject(string text)
        {
            int i = 0;
            object v = Parse(text, ref i);
            var o = v as Dictionary<string, object>;
            if (o == null) throw new FormatException("not a JSON object: " + Clip(text));
            return o;
        }

        static object Parse(string s, ref int i)
        {
            SkipWs(s, ref i);
            if (i >= s.Length) throw new FormatException("truncated JSON");
            char c = s[i];
            switch (c)
            {
                case '{': return ParseObject(s, ref i);
                case '[': return ParseArray(s, ref i);
                case '"': return ParseString(s, ref i);
                case 't': Expect(s, ref i, "true"); return true;
                case 'f': Expect(s, ref i, "false"); return false;
                case 'n': Expect(s, ref i, "null"); return null;
                default: return ParseNumber(s, ref i);
            }
        }

        static Dictionary<string, object> ParseObject(string s, ref int i)
        {
            var o = new Dictionary<string, object>();
            i++;                                        // '{'
            SkipWs(s, ref i);
            if (i < s.Length && s[i] == '}') { i++; return o; }
            while (true)
            {
                SkipWs(s, ref i);
                string key = ParseString(s, ref i);
                SkipWs(s, ref i);
                if (i >= s.Length || s[i] != ':') throw new FormatException("expected ':'");
                i++;
                o[key] = Parse(s, ref i);
                SkipWs(s, ref i);
                if (i >= s.Length) throw new FormatException("truncated object");
                if (s[i] == ',') { i++; continue; }
                if (s[i] == '}') { i++; return o; }
                throw new FormatException("expected ',' or '}'");
            }
        }

        static List<object> ParseArray(string s, ref int i)
        {
            var a = new List<object>();
            i++;                                        // '['
            SkipWs(s, ref i);
            if (i < s.Length && s[i] == ']') { i++; return a; }
            while (true)
            {
                a.Add(Parse(s, ref i));
                SkipWs(s, ref i);
                if (i >= s.Length) throw new FormatException("truncated array");
                if (s[i] == ',') { i++; continue; }
                if (s[i] == ']') { i++; return a; }
                throw new FormatException("expected ',' or ']'");
            }
        }

        static string ParseString(string s, ref int i)
        {
            if (i >= s.Length || s[i] != '"') throw new FormatException("expected a string");
            i++;
            var sb = new StringBuilder();
            while (true)
            {
                if (i >= s.Length) throw new FormatException("truncated string");
                char c = s[i++];
                if (c == '"') return sb.ToString();
                if (c != '\\') { sb.Append(c); continue; }
                if (i >= s.Length) throw new FormatException("truncated escape");
                char e = s[i++];
                switch (e)
                {
                    case '"': sb.Append('"'); break;
                    case '\\': sb.Append('\\'); break;
                    case '/': sb.Append('/'); break;
                    case 'b': sb.Append('\b'); break;
                    case 'f': sb.Append('\f'); break;
                    case 'n': sb.Append('\n'); break;
                    case 'r': sb.Append('\r'); break;
                    case 't': sb.Append('\t'); break;
                    case 'u':
                        if (i + 4 > s.Length) throw new FormatException("truncated \\u");
                        sb.Append((char)ushort.Parse(s.Substring(i, 4), NumberStyles.HexNumber,
                                                    CultureInfo.InvariantCulture));
                        i += 4;
                        break;
                    default: throw new FormatException("bad escape \\" + e);
                }
            }
        }

        static object ParseNumber(string s, ref int i)
        {
            int start = i;
            while (i < s.Length && "+-.eE0123456789".IndexOf(s[i]) >= 0) i++;
            if (i == start) throw new FormatException("expected a value at " + Clip(s.Substring(start)));
            return double.Parse(s.Substring(start, i - start), CultureInfo.InvariantCulture);
        }

        static void Expect(string s, ref int i, string word)
        {
            if (i + word.Length > s.Length || s.Substring(i, word.Length) != word)
                throw new FormatException("expected " + word);
            i += word.Length;
        }

        static void SkipWs(string s, ref int i)
        {
            while (i < s.Length && (s[i] == ' ' || s[i] == '\t' || s[i] == '\r' || s[i] == '\n')) i++;
        }

        static string Clip(string s)
        {
            return s.Length <= 60 ? s : s.Substring(0, 60) + "...";
        }

        // ----------------------------------------------------------- write

        /// Write an object from alternating key/value arguments, which is how
        /// every request in this shim is built:  Write("op", "read", "char", u)
        public static string Write(params object[] pairs)
        {
            if (pairs.Length % 2 != 0) throw new ArgumentException("keys and values must pair up");
            var sb = new StringBuilder("{");
            for (int i = 0; i < pairs.Length; i += 2)
            {
                if (i > 0) sb.Append(',');
                WriteString(sb, Convert.ToString(pairs[i], CultureInfo.InvariantCulture));
                sb.Append(':');
                WriteValue(sb, pairs[i + 1]);
            }
            return sb.Append('}').ToString();
        }

        static void WriteValue(StringBuilder sb, object v)
        {
            if (v == null) { sb.Append("null"); return; }
            if (v is bool) { sb.Append((bool)v ? "true" : "false"); return; }
            if (v is string) { WriteString(sb, (string)v); return; }
            if (v is int || v is long || v is uint || v is ulong || v is double || v is float)
            {
                sb.Append(Convert.ToString(v, CultureInfo.InvariantCulture));
                return;
            }
            WriteString(sb, Convert.ToString(v, CultureInfo.InvariantCulture));
        }

        static void WriteString(StringBuilder sb, string s)
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
                    default:
                        if (c < 0x20) sb.Append("\\u").Append(((int)c).ToString("x4", CultureInfo.InvariantCulture));
                        else sb.Append(c);
                        break;
                }
            }
            sb.Append('"');
        }

        // ------------------------------------------------------- accessors

        public static string Str(Dictionary<string, object> o, string key, string fallback = null)
        {
            object v;
            if (o == null || !o.TryGetValue(key, out v) || v == null) return fallback;
            return v as string ?? Convert.ToString(v, CultureInfo.InvariantCulture);
        }

        public static double Num(Dictionary<string, object> o, string key, double fallback = 0)
        {
            object v;
            if (o == null || !o.TryGetValue(key, out v) || !(v is double)) return fallback;
            return (double)v;
        }

        public static bool Bool(Dictionary<string, object> o, string key, bool fallback = false)
        {
            object v;
            if (o == null || !o.TryGetValue(key, out v) || !(v is bool)) return fallback;
            return (bool)v;
        }

        public static List<object> Arr(Dictionary<string, object> o, string key)
        {
            object v;
            if (o == null || !o.TryGetValue(key, out v)) return new List<object>();
            return v as List<object> ?? new List<object>();
        }

        public static Dictionary<string, object> Obj(object v)
        {
            return v as Dictionary<string, object>;
        }

        // Byte payloads travel as hex; BitConverter's dashes are not worth the
        // bytes and DataWriter hands us plain arrays.
        public static string ToHex(byte[] bytes)
        {
            var sb = new StringBuilder(bytes.Length * 2);
            foreach (byte b in bytes) sb.Append(b.ToString("x2", CultureInfo.InvariantCulture));
            return sb.ToString();
        }

        public static byte[] FromHex(string hex)
        {
            if (string.IsNullOrEmpty(hex)) return new byte[0];
            if (hex.Length % 2 != 0) throw new FormatException("odd-length hex: " + Clip(hex));
            var bytes = new byte[hex.Length / 2];
            for (int i = 0; i < bytes.Length; i++)
                bytes[i] = byte.Parse(hex.Substring(i * 2, 2), NumberStyles.HexNumber,
                                      CultureInfo.InvariantCulture);
            return bytes;
        }
    }
}
