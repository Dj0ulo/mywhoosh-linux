// Minimal IL disassembler for WindowsConnectivity.dll.
//
//   mono ILDump.exe <assembly> <Type.Name> [MethodName]
//
// Uses reflection + Module.Resolve* so it works on any assembly Mono can load
// the *metadata* of, even when referenced assemblies (Windows.winmd) are absent
// -- types that fail to load are simply skipped.

using System;
using System.Linq;
using System.Reflection;
using System.Reflection.Emit;
using System.Collections.Generic;

class ILDump
{
    static Dictionary<short, OpCode> ops = new Dictionary<short, OpCode>();

    static void Main(string[] args)
    {
        foreach (var f in typeof(OpCodes).GetFields(BindingFlags.Public | BindingFlags.Static))
        {
            var op = (OpCode)f.GetValue(null);
            ops[op.Value] = op;
        }

        // Some BCL assemblies the target references (System.ServiceProcess) are not
        // in the host Mono's GAC.  Without them, resolving a method's local-variable
        // signature throws before we ever see the IL, so satisfy them from any
        // directory listed in ILDUMP_PROBE (colon-separated) or next to this tool.
        AppDomain.CurrentDomain.AssemblyResolve += (s2, e2) => {
            string simple = new AssemblyName(e2.Name).Name;
            var dirs = new List<string> { AppDomain.CurrentDomain.BaseDirectory };
            string extra = Environment.GetEnvironmentVariable("ILDUMP_PROBE");
            if (!string.IsNullOrEmpty(extra)) dirs.AddRange(extra.Split(':'));
            foreach (var d in dirs) {
                string cand = System.IO.Path.Combine(d, simple + ".dll");
                if (System.IO.File.Exists(cand)) { try { return Assembly.LoadFrom(cand); } catch { } }
            }
            return null;
        };

        var asm = Assembly.LoadFrom(args[0]);
        Type[] types;
        try { types = asm.GetTypes(); }
        catch (ReflectionTypeLoadException e) { types = e.Types.Where(t => t != null).ToArray(); }

        string typeFilter = args.Length > 1 ? args[1] : null;
        string methFilter = args.Length > 2 ? args[2] : null;

        foreach (var t in types)
        {
            if (typeFilter != null && !t.FullName.Contains(typeFilter)) continue;
            const BindingFlags all = BindingFlags.Public | BindingFlags.NonPublic
                                   | BindingFlags.Static | BindingFlags.Instance
                                   | BindingFlags.DeclaredOnly;
            MethodBase[] methods;
            try { methods = t.GetMethods(all).Cast<MethodBase>()
                             .Concat(t.GetConstructors(all).Cast<MethodBase>()).ToArray(); }
            catch (Exception e) { Console.WriteLine("// " + t.FullName + ": " + e.Message); continue; }

            foreach (var m in methods)
            {
                if (methFilter != null && !m.Name.Contains(methFilter)) continue;
                Console.WriteLine("\n=== " + t.FullName + "::" + m.Name);
                try { Disassemble(m, asm.ManifestModule); }
                catch (Exception e) { Console.WriteLine("  !! " + e.GetType().Name + ": " + e.Message); }
            }
        }
    }

    static void Disassemble(MethodBase m, Module mod)
    {
        var body = m.GetMethodBody();
        if (body == null) { Console.WriteLine("  <no body>"); return; }
        byte[] il = body.GetILAsByteArray();
        var gtA = m.DeclaringType.IsGenericType ? m.DeclaringType.GetGenericArguments() : null;
        var gtM = m.IsGenericMethod ? m.GetGenericArguments() : null;

        int i = 0;
        while (i < il.Length)
        {
            int at = i;
            short code = il[i++];
            if (code == 0xFE) code = (short)(0xFE00 | il[i++]);
            OpCode op;
            if (!ops.TryGetValue(code, out op)) { Console.WriteLine($"  IL_{at:x4}: <{code:x2}?>"); continue; }

            string arg = "";
            switch (op.OperandType)
            {
                case OperandType.InlineNone: break;
                case OperandType.ShortInlineBrTarget:
                    arg = $"IL_{(i + 1 + (sbyte)il[i]):x4}"; i += 1; break;
                case OperandType.InlineBrTarget:
                    arg = $"IL_{(i + 4 + BitConverter.ToInt32(il, i)):x4}"; i += 4; break;
                case OperandType.ShortInlineI:  arg = il[i].ToString(); i += 1; break;
                case OperandType.ShortInlineVar: arg = il[i].ToString(); i += 1; break;
                case OperandType.InlineI:       arg = BitConverter.ToInt32(il, i).ToString(); i += 4; break;
                case OperandType.InlineI8:      arg = BitConverter.ToInt64(il, i).ToString(); i += 8; break;
                case OperandType.InlineVar:     arg = BitConverter.ToInt16(il, i).ToString(); i += 2; break;
                case OperandType.ShortInlineR:  arg = BitConverter.ToSingle(il, i).ToString(); i += 4; break;
                case OperandType.InlineR:       arg = BitConverter.ToDouble(il, i).ToString(); i += 8; break;
                case OperandType.InlineSwitch: {
                    int n = BitConverter.ToInt32(il, i); i += 4;
                    var t = new List<string>();
                    int baseAddr = i + n * 4;
                    for (int k = 0; k < n; k++) { t.Add($"IL_{(baseAddr + BitConverter.ToInt32(il, i)):x4}"); i += 4; }
                    arg = string.Join(", ", t); break;
                }
                case OperandType.InlineString:
                    arg = "\"" + mod.ResolveString(BitConverter.ToInt32(il, i)) + "\""; i += 4; break;
                default: {
                    int tok = BitConverter.ToInt32(il, i); i += 4;
                    try {
                        var mem = mod.ResolveMember(tok, gtA, gtM);
                        arg = (mem.DeclaringType != null ? mem.DeclaringType.FullName + "::" : "") + mem.Name;
                    } catch (Exception e) { arg = $"token(0x{tok:x8}) /* {e.GetType().Name} */"; }
                    break;
                }
            }
            Console.WriteLine($"  IL_{at:x4}: {op.Name,-14} {arg}");
        }
    }
}
