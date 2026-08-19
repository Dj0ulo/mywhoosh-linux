// A real implementation of System.Runtime.InteropServices.ComAwareEventInfo,
// which wine-mono ships as a NotImplementedException stub in System.Core.dll,
// and which MyWhoosh's Dircon stack needs.
//
// The game wires its four Bonjour callbacks like this:
//
//   new ComAwareEventInfo(typeof(_IDNSSDEvents_Event), "ServiceFound")
//       .AddEventHandler(eventManager, new _IDNSSDEvents_ServiceFoundEventHandler(...))
//
// On .NET that walks ComEventInterfaceAttribute from the _Event interface to the
// source dispinterface, reads the DispIdAttribute of the matching method, and
// hands both to ComEventsHelper, which Advises an IDispatch sink on the object's
// connection point.  None of it exists in wine-mono, so the Advise happens here
// and System.Core's ComAwareEventInfo is patched to forward into it (see
// PatchSystemCore.cs).  ComEventsHelper itself -- a stub in mscorlib -- is left
// alone: nothing on this path calls it, and its (rcw, iid, dispid) signature
// carries no interface to build a sink from.
//
// Two deviations from .NET, both forced by wine-mono:
//
//  * The sink cannot implement the interop interface itself.  MyWhoosh embeds
//    the Bonjour interop types, so _IDNSSDEvents carries _VtblGapN_M placeholder
//    slots that Mono refuses to lay out in a managed class.  An equivalent
//    [ComImport] dispinterface -- same IID, same method names and signatures,
//    gaps dropped -- is emitted at run time instead, and the sink implements
//    that.  Callers reach it through IDispatch::Invoke, so the missing vtable
//    slots do not matter.
//
//  * Nothing here may reflect over the source dispinterface's *methods*.  Those
//    same gaps make `method->slot < nslots` fail inside mono's icall layer,
//    which aborts the process rather than throwing (see README).  Signatures
//    therefore come from the event interface's delegate types, which reflect
//    cleanly, and dispids from the registered type library.

using System;
using System.Collections.Generic;
using System.Reflection;
using System.Reflection.Emit;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using Microsoft.Win32;

namespace MyWhoosh.ComEventShim
{
    /// <summary>Entry points called by the patched ComAwareEventInfo.</summary>
    public static class ComEvents
    {
        /// <summary>Backing EventInfo for ComAwareEventInfo(type, eventName).</summary>
        public static EventInfo GetEventInfo(Type type, string eventName)
        {
            if (type == null) throw new ArgumentNullException("type");
            if (eventName == null) throw new ArgumentNullException("eventName");

            EventInfo ei = type.GetEvent(eventName, BindingFlags.Public | BindingFlags.NonPublic
                                                  | BindingFlags.Instance | BindingFlags.Static);
            if (ei == null)
                throw new ArgumentException("event '" + eventName + "' not found on " + type.FullName,
                                           "eventName");
            return ei;
        }

        public static void AddEventHandler(EventInfo inner, object target, Delegate handler)
        {
            Check(inner, target, handler);

            if (!Marshal.IsComObject(target)) { inner.AddEventHandler(target, handler); return; }

            ComEventSinks.Combine(target, inner.DeclaringType, SourceInterface(inner), inner.Name, handler);
        }

        public static void RemoveEventHandler(EventInfo inner, object target, Delegate handler)
        {
            Check(inner, target, handler);

            if (!Marshal.IsComObject(target)) { inner.RemoveEventHandler(target, handler); return; }

            ComEventSinks.Remove(target, inner.DeclaringType, SourceInterface(inner), inner.Name, handler);
        }

        static void Check(EventInfo inner, object target, Delegate handler)
        {
            if (inner == null) throw new InvalidOperationException("ComAwareEventInfo is uninitialised");
            if (target == null) throw new ArgumentNullException("target");
            if (handler == null) throw new ArgumentNullException("handler");
        }

        static Type SourceInterface(EventInfo inner)
        {
            Type declaring = inner.DeclaringType;
            var attr = (ComEventInterfaceAttribute)Attribute.GetCustomAttribute(
                declaring, typeof(ComEventInterfaceAttribute));
            if (attr == null)
                throw new InvalidOperationException(declaring.FullName + " has no ComEventInterfaceAttribute");
            return attr.SourceInterface;
        }
    }

    /// <summary>Hooks for the probes: exercise one piece of the machinery in isolation.</summary>
    public static class ComEventShimDiagnostics
    {
        /// <summary>Emit the sink type for one event interface and instantiate it.</summary>
        public static object BuildSink(Type eventInterface, Type source,
                                       out Guid iid, out string[] names, out int[] dispids)
        {
            SinkKind kind = SinkKind.For(eventInterface, source);
            iid = kind.Iid;
            names = kind.Names;
            dispids = kind.DispIds;
            return kind.CreateSink(new Delegate[kind.SlotCount]);
        }
    }

    /// <summary>
    /// One Advised sink per (COM object, event interface), holding one combined
    /// delegate per event -- the equivalent of ComEventsHelper's sink table.
    /// </summary>
    internal static class ComEventSinks
    {
        sealed class Entry
        {
            internal IntPtr Identity;          // IUnknown of the RCW, for lookup
            internal Type EventInterface;
            internal SinkKind Kind;
            internal object Sink;
            internal Delegate[] Slots;         // by sink slot, shared with the sink object
            internal IConnectionPoint Point;
            internal int Cookie;
        }

        static readonly object sync = new object();
        static readonly List<Entry> live = new List<Entry>();

        internal static void Combine(object rcw, Type eventInterface, Type source,
                                     string eventName, Delegate handler)
        {
            lock (sync)
            {
                Entry e = Find(rcw, eventInterface) ?? Advise(rcw, eventInterface, source);
                int slot = e.Kind.SlotOf(eventName);
                e.Slots[slot] = Delegate.Combine(e.Slots[slot], handler);
            }
        }

        internal static void Remove(object rcw, Type eventInterface, Type source,
                                    string eventName, Delegate handler)
        {
            lock (sync)
            {
                Entry e = Find(rcw, eventInterface);
                if (e == null) return;

                int slot = e.Kind.SlotOf(eventName);
                e.Slots[slot] = Delegate.Remove(e.Slots[slot], handler);

                foreach (Delegate d in e.Slots) if (d != null) return;

                try { e.Point.Unadvise(e.Cookie); } catch { }
                try { Marshal.ReleaseComObject(e.Point); } catch { }
                live.Remove(e);
            }
        }

        static Entry Find(object rcw, Type eventInterface)
        {
            IntPtr id = Identity(rcw);
            foreach (Entry e in live)
                if (e.Identity == id && e.EventInterface == eventInterface) return e;
            return null;
        }

        static Entry Advise(object rcw, Type eventInterface, Type source)
        {
            SinkKind kind = SinkKind.For(eventInterface, source);

            var container = rcw as IConnectionPointContainer;
            if (container == null)
                throw new InvalidOperationException("COM object does not implement IConnectionPointContainer");

            IConnectionPoint point;
            Guid iid = kind.Iid;
            container.FindConnectionPoint(ref iid, out point);
            if (point == null)
                throw new InvalidOperationException("no connection point for " + iid);

            var slots = new Delegate[kind.SlotCount];
            object sink = kind.CreateSink(slots);

            int cookie;
            point.Advise(sink, out cookie);

            var entry = new Entry {
                Identity = Identity(rcw), EventInterface = eventInterface, Kind = kind,
                Sink = sink, Slots = slots, Point = point, Cookie = cookie,
            };
            live.Add(entry);          // keeps sink and slots alive for as long as Advised
            return entry;
        }

        static IntPtr Identity(object rcw)
        {
            IntPtr unk = Marshal.GetIUnknownForObject(rcw);
            Marshal.Release(unk);
            return unk;
        }
    }

    /// <summary>
    /// The emitted sink type for one event interface: a [ComImport] dispinterface
    /// carrying the source IID, plus a class implementing it that forwards each
    /// method to the delegate in its slot.
    /// </summary>
    internal sealed class SinkKind
    {
        static readonly Dictionary<Type, SinkKind> cache = new Dictionary<Type, SinkKind>();
        static ModuleBuilder module;

        internal Guid Iid;
        internal int SlotCount { get { return names.Length; } }
        internal string[] Names { get { return (string[])names.Clone(); } }
        internal int[] DispIds { get { return (int[])dispids.Clone(); } }

        string[] names;                // slot -> event name
        int[] dispids;                 // slot -> dispid, or -1 when the type library is unavailable
        Type sinkType;
        FieldInfo slotsField;

        internal static SinkKind For(Type eventInterface, Type source)
        {
            lock (cache)
            {
                SinkKind k;
                if (!cache.TryGetValue(eventInterface, out k))
                    cache[eventInterface] = k = Build(eventInterface, source);
                return k;
            }
        }

        internal int SlotOf(string eventName)
        {
            for (int i = 0; i < names.Length; i++) if (names[i] == eventName) return i;
            throw new InvalidOperationException("event " + eventName + " is not on the sink interface");
        }

        internal object CreateSink(Delegate[] slots)
        {
            object sink = Activator.CreateInstance(sinkType);
            slotsField.SetValue(sink, slots);
            return sink;
        }

        static SinkKind Build(Type eventInterface, Type source)
        {
            // Metadata order, so the emitted interface matches the source's slot order.
            var events = new List<EventInfo>(eventInterface.GetEvents());
            events.Sort((a, b) => a.MetadataToken.CompareTo(b.MetadataToken));

            var kind = new SinkKind {
                Iid = Marshal.GenerateGuidForType(source),
                names = new string[events.Count],
                dispids = new int[events.Count],
            };
            var signatures = new MethodInfo[events.Count];
            for (int i = 0; i < events.Count; i++)
            {
                kind.names[i] = events[i].Name;
                signatures[i] = events[i].EventHandlerType.GetMethod("Invoke");
            }
            TypeLib.DispIdsOf(kind.Iid, kind.names, kind.dispids);

            ModuleBuilder mod = Module();
            string tag = source.Name + "_" + Guid.NewGuid().ToString("N");

            Type iface = EmitInterface(mod, "Sink_" + tag + "_Interface", kind.Iid,
                                       kind.names, signatures, kind.dispids);
            kind.sinkType = EmitSink(mod, "Sink_" + tag, iface, kind.names, signatures,
                                     out kind.slotsField);
            return kind;
        }

        static ModuleBuilder Module()
        {
            if (module == null)
            {
                var name = new AssemblyName("MyWhoosh.ComEventShim.Sinks");
                AssemblyBuilder asm = AppDomain.CurrentDomain.DefineDynamicAssembly(
                    name, AssemblyBuilderAccess.Run);
                module = asm.DefineDynamicModule("Sinks");
            }
            return module;
        }

        static Type EmitInterface(ModuleBuilder mod, string name, Guid iid,
                                  string[] names, MethodInfo[] signatures, int[] dispids)
        {
            TypeBuilder tb = mod.DefineType(name, TypeAttributes.Public | TypeAttributes.Interface
                                                | TypeAttributes.Abstract | TypeAttributes.Import);
            tb.SetCustomAttribute(Attr(typeof(GuidAttribute), new Type[] { typeof(string) },
                                       new object[] { iid.ToString() }));
            tb.SetCustomAttribute(Attr(typeof(InterfaceTypeAttribute), new Type[] { typeof(ComInterfaceType) },
                                       new object[] { ComInterfaceType.InterfaceIsIDispatch }));

            for (int i = 0; i < names.Length; i++)
            {
                MethodBuilder mb = tb.DefineMethod(names[i],
                    MethodAttributes.Public | MethodAttributes.Abstract | MethodAttributes.Virtual
                    | MethodAttributes.NewSlot | MethodAttributes.HideBySig,
                    signatures[i].ReturnType, ParamTypes(signatures[i]));
                if (dispids[i] >= 0)
                    mb.SetCustomAttribute(Attr(typeof(DispIdAttribute), new Type[] { typeof(int) },
                                               new object[] { dispids[i] }));
            }
            return tb.CreateType();
        }

        static Type EmitSink(ModuleBuilder mod, string name, Type iface,
                             string[] names, MethodInfo[] signatures, out FieldInfo slotsField)
        {
            TypeBuilder tb = mod.DefineType(name, TypeAttributes.Public | TypeAttributes.Class
                                                | TypeAttributes.Sealed,
                                            typeof(object), new Type[] { iface });
            tb.SetCustomAttribute(Attr(typeof(ComVisibleAttribute), new Type[] { typeof(bool) },
                                       new object[] { true }));
            tb.SetCustomAttribute(Attr(typeof(ClassInterfaceAttribute), new Type[] { typeof(ClassInterfaceType) },
                                       new object[] { ClassInterfaceType.None }));

            FieldBuilder slots = tb.DefineField("Slots", typeof(Delegate[]), FieldAttributes.Public);
            MethodInfo dynamicInvoke = typeof(Delegate).GetMethod("DynamicInvoke");

            for (int slot = 0; slot < names.Length; slot++)
            {
                MethodInfo signature = signatures[slot];
                Type[] ps = ParamTypes(signature);

                MethodBuilder mb = tb.DefineMethod(names[slot],
                    MethodAttributes.Public | MethodAttributes.Virtual | MethodAttributes.Final
                    | MethodAttributes.NewSlot | MethodAttributes.HideBySig,
                    signature.ReturnType, ps);
                ILGenerator il = mb.GetILGenerator();

                LocalBuilder ret = signature.ReturnType == typeof(void)
                    ? null : il.DeclareLocal(signature.ReturnType);
                Label done = il.DefineLabel();
                Label call = il.DefineLabel();

                il.Emit(OpCodes.Ldarg_0);
                il.Emit(OpCodes.Ldfld, slots);
                il.Emit(OpCodes.Ldc_I4, slot);
                il.Emit(OpCodes.Ldelem_Ref);
                il.Emit(OpCodes.Dup);
                il.Emit(OpCodes.Brtrue, call);
                il.Emit(OpCodes.Pop);                 // no handler attached: ignore the callback
                il.Emit(OpCodes.Br, done);

                il.MarkLabel(call);
                il.Emit(OpCodes.Ldc_I4, ps.Length);
                il.Emit(OpCodes.Newarr, typeof(object));
                for (int i = 0; i < ps.Length; i++)
                {
                    il.Emit(OpCodes.Dup);
                    il.Emit(OpCodes.Ldc_I4, i);
                    il.Emit(OpCodes.Ldarg, i + 1);
                    if (ps[i].IsValueType) il.Emit(OpCodes.Box, ps[i]);
                    il.Emit(OpCodes.Stelem_Ref);
                }
                il.Emit(OpCodes.Callvirt, dynamicInvoke);
                if (ret == null) il.Emit(OpCodes.Pop);
                else { il.Emit(OpCodes.Unbox_Any, signature.ReturnType); il.Emit(OpCodes.Stloc, ret); }

                il.MarkLabel(done);
                if (ret != null) il.Emit(OpCodes.Ldloc, ret);
                il.Emit(OpCodes.Ret);

                tb.DefineMethodOverride(mb, iface.GetMethod(names[slot]));
            }

            Type built = tb.CreateType();
            slotsField = built.GetField("Slots");
            return built;
        }

        static Type[] ParamTypes(MethodInfo m)
        {
            ParameterInfo[] ps = m.GetParameters();
            var types = new Type[ps.Length];
            for (int i = 0; i < ps.Length; i++) types[i] = ps[i].ParameterType;
            return types;
        }

        static CustomAttributeBuilder Attr(Type type, Type[] ctor, object[] args)
        {
            return new CustomAttributeBuilder(type.GetConstructor(ctor), args);
        }
    }

    /// <summary>
    /// Dispids for a dispinterface, read from the type library the interface is
    /// registered against -- the source assembly's own metadata is off limits
    /// (see the header).  Best effort: slots stay at -1 when the library or a
    /// name cannot be resolved, and the emitted method then carries no
    /// DispIdAttribute.
    /// </summary>
    internal static class TypeLib
    {
        [DllImport("oleaut32.dll", PreserveSig = false)]
        static extern void LoadRegTypeLib(ref Guid libid, ushort major, ushort minor, int lcid,
                                          [MarshalAs(UnmanagedType.Interface)] out ITypeLib lib);

        internal static void DispIdsOf(Guid iid, string[] names, int[] dispids)
        {
            for (int i = 0; i < dispids.Length; i++) dispids[i] = -1;

            try
            {
                Guid libid; ushort major, minor;
                if (!RegisteredTypeLib(iid, out libid, out major, out minor)) return;

                ITypeLib lib;
                LoadRegTypeLib(ref libid, major, minor, 0, out lib);

                ITypeInfo info;
                lib.GetTypeInfoOfGuid(ref iid, out info);

                var ids = new int[1];
                for (int i = 0; i < names.Length; i++)
                {
                    var one = new string[] { names[i] };
                    try { info.GetIDsOfNames(one, 1, ids); dispids[i] = ids[0]; } catch { }
                }
                Marshal.ReleaseComObject(info);
                Marshal.ReleaseComObject(lib);
            }
            catch { }        // dispids stay unknown; see the class comment
        }

        static bool RegisteredTypeLib(Guid iid, out Guid libid, out ushort major, out ushort minor)
        {
            libid = Guid.Empty; major = 1; minor = 0;

            using (RegistryKey key = Registry.ClassesRoot.OpenSubKey(
                       @"Interface\" + iid.ToString("B") + @"\TypeLib"))
            {
                if (key == null) return false;
                var id = key.GetValue(null) as string;
                if (id == null) return false;
                libid = new Guid(id);

                var version = key.GetValue("Version") as string;
                if (version != null)
                {
                    string[] parts = version.Split('.');
                    ushort.TryParse(parts[0], out major);
                    if (parts.Length > 1) ushort.TryParse(parts[1], out minor);
                }
                return true;
            }
        }
    }
}
