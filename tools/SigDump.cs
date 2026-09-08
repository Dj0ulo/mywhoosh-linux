// Dump the exact marshalling surface of the byref-array exports and the struct
// they carry, so blocker 4 is worked from the game's own metadata.
//
//   mono SigDump.exe <assembly>
using System;
using System.Linq;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Collections.Generic;

class SigDump
{
    static void Attrs(object[] ca, string indent)
    {
        foreach (var a in ca) {
            var m = a as MarshalAsAttribute;
            if (m != null)
                Console.WriteLine(indent + "[MarshalAs(" + m.Value
                    + " SizeConst=" + m.SizeConst
                    + " SizeParamIndex=" + m.SizeParamIndex
                    + " ArraySubType=" + m.ArraySubType + ")]");
            else
                Console.WriteLine(indent + "[" + a.GetType().Name + "]");
        }
    }

    static void DumpStruct(Type t, HashSet<Type> seen)
    {
        if (t == null || !seen.Add(t)) return;
        Console.WriteLine();
        Console.WriteLine("struct " + t.FullName + "  layout=" + t.StructLayoutAttribute.Value
            + " pack=" + t.StructLayoutAttribute.Pack
            + " charset=" + t.StructLayoutAttribute.CharSet
            + " size=" + t.StructLayoutAttribute.Size);
        try { Console.WriteLine("  Marshal.SizeOf = " + Marshal.SizeOf(t)); }
        catch (Exception e) { Console.WriteLine("  Marshal.SizeOf threw: " + e.Message); }
        var nested = new List<Type>();
        foreach (var f in t.GetFields(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance)) {
            Console.WriteLine("  " + f.FieldType + " " + f.Name
                + "  offset=" + (t.StructLayoutAttribute.Value == LayoutKind.Explicit
                                 ? Marshal.OffsetOf(t, f.Name).ToString() : "seq"));
            Attrs(f.GetCustomAttributes(false), "      ");
            if (f.FieldType.IsValueType && !f.FieldType.IsPrimitive && !f.FieldType.IsEnum)
                nested.Add(f.FieldType);
        }
        foreach (var n in nested) DumpStruct(n, seen);
    }

    static void Main(string[] args)
    {
        AppDomain.CurrentDomain.AssemblyResolve += (s, e) => {
            string simple = new AssemblyName(e.Name).Name;
            string cand = System.IO.Path.Combine(AppDomain.CurrentDomain.BaseDirectory, simple + ".dll");
            return System.IO.File.Exists(cand) ? Assembly.LoadFrom(cand) : null;
        };
        var asm = Assembly.LoadFrom(args[0]);
        Type[] types;
        try { types = asm.GetTypes(); }
        catch (ReflectionTypeLoadException e) { types = e.Types.Where(x => x != null).ToArray(); }

        var seen = new HashSet<Type>();
        foreach (var t in types) {
            const BindingFlags all = BindingFlags.Public | BindingFlags.NonPublic
                                   | BindingFlags.Static | BindingFlags.Instance
                                   | BindingFlags.DeclaredOnly;
            MethodInfo[] ms;
            try { ms = t.GetMethods(all); } catch { continue; }
            foreach (var mi in ms) {
                if (!mi.Name.Contains("DevicesList")) continue;
                Console.WriteLine("=== " + t.FullName + "." + mi.Name);
                Console.WriteLine("  returns " + mi.ReturnType);
                Console.WriteLine("  attributes " + mi.Attributes + " | impl " + mi.GetMethodImplementationFlags());
                Attrs(mi.ReturnParameter.GetCustomAttributes(false), "    ret ");
                foreach (var p in mi.GetParameters()) {
                    Console.WriteLine("  param " + p.Position + ": " + p.ParameterType
                        + " " + p.Name + "  attrs=" + p.Attributes);
                    Attrs(p.GetCustomAttributes(false), "      ");
                    var pt = p.ParameterType;
                    if (pt.IsByRef) pt = pt.GetElementType();
                    if (pt.IsArray) pt = pt.GetElementType();
                    if (pt.IsValueType && !pt.IsPrimitive) DumpStruct(pt, seen);
                }
            }
        }
    }
}
