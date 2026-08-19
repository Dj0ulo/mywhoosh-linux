// Does anything ever *reach* the emitted sink?
//
// ../winemono/ Advises a sink on Bonjour's connection point and the Advise
// succeeds, but Bonjour under Wine receives no packets, so it has never called
// back.  Before writing a replacement COM server (the fake sensor of step 2)
// we have to know how that server is supposed to deliver an event:
//
//   * through IDispatch::Invoke, the way a real dispinterface source does, or
//   * early-bound through the interface vtable, if Mono's CCW has no IDispatch.
//
// This probe answers it without any native code: it builds the sink, takes its
// CCW with Marshal.GetIUnknownForObject, and calls into it through raw vtable
// slots -- exactly what a COM source would do from C.
//
// Build: ../winemono/build.sh (needs the game's WindowsConnectivity.dll)
// Run:   inside a prefix carrying the patched runtime (see install.sh)

using System;
using System.Reflection;
using System.Runtime.InteropServices;
using Bonjour;
using MyWhoosh.ComEventShim;

class SinkInvokeProbe
{
    static Guid IID_IDispatch = new Guid("00020400-0000-0000-C000-000000000046");
    static Guid IID_NULL      = Guid.Empty;

    const int DISPATCH_METHOD = 1;

    // Slot numbers in an IDispatch-derived vtable.
    const int SLOT_GETIDSOFNAMES = 5;
    const int SLOT_INVOKE        = 6;
    const int SLOT_FIRST_MEMBER  = 7;   // first dispinterface method, if the vtable has one

    [StructLayout(LayoutKind.Sequential)]
    struct DISPPARAMS
    {
        public IntPtr rgvarg;
        public IntPtr rgdispidNamedArgs;
        public uint cArgs;
        public uint cNamedArgs;
    }

    delegate int InvokeFn(IntPtr self, int dispIdMember, ref Guid riid, int lcid, ushort flags,
                          ref DISPPARAMS args, IntPtr varResult, IntPtr excepInfo, IntPtr argErr);

    delegate int GetIDsOfNamesFn(IntPtr self, ref Guid riid, IntPtr names, uint cNames,
                                 int lcid, IntPtr dispIds);

    // ServiceFound(DNSSDService, DNSSDFlags, uint, BSTR, BSTR, BSTR), early-bound.
    delegate int ServiceFoundFn(IntPtr self, IntPtr browser, int flags, uint ifIndex,
                                IntPtr serviceName, IntPtr regType, IntPtr domain);

    // OperationFailed(DNSSDService, DNSSDError), early-bound -- two args, nothing to marshal.
    delegate int OperationFailedFn(IntPtr self, IntPtr service, int error);

    [DllImport("oleaut32.dll", CharSet = CharSet.Unicode)]
    static extern IntPtr SysAllocString(string s);
    [DllImport("oleaut32.dll")]
    static extern void SysFreeString(IntPtr bstr);

    static int fired;
    static string lastCall = "(none)";
    static int runtimeDispid = -1;      // what the CCW's own GetIDsOfNames answers

    static void Say(string tag, string msg) { Console.WriteLine("{0,-6} {1}", tag, msg); Console.Out.Flush(); }

    static void OnServiceFound(DNSSDService browser, DNSSDFlags flags, uint ifIndex,
                               string serviceName, string regType, string domain)
    {
        fired++;
        lastCall = string.Format("ServiceFound(browser={0}, flags={1}, ifIndex={2}, \"{3}\", \"{4}\", \"{5}\")",
                                 browser == null ? "null" : "rcw", flags, ifIndex, serviceName, regType, domain);
        Say("SINK", lastCall);
    }

    static void OnOperationFailed(DNSSDService service, DNSSDError error)
    {
        fired++;
        lastCall = string.Format("OperationFailed(service={0}, error={1})",
                                 service == null ? "null" : "rcw", error);
        Say("SINK", lastCall);
    }

    [STAThread]
    static int Main()
    {
        Say("ENV", "runtime : " + typeof(object).Assembly.Location);
        Say("ENV", "shim    : " + typeof(ComEvents).Assembly.Location);

        Guid iid; string[] names; int[] dispids;
        object sink = ComEventShimDiagnostics.BuildSink(typeof(_IDNSSDEvents_Event),
                                                       typeof(_IDNSSDEvents),
                                                       out iid, out names, out dispids);
        Say("SINK", "type " + sink.GetType().FullName);
        Say("SINK", "iid  " + iid.ToString("B"));
        for (int i = 0; i < names.Length; i++)
            Say("SINK", string.Format("  slot {0}: {1} dispid={2}", i, names[i], dispids[i]));

        // The shim keeps the handlers in a Delegate[] the emitted methods index into.
        FieldInfo slotsField = sink.GetType().GetField("Slots");
        var slots = (Delegate[])slotsField.GetValue(sink);
        slots[Array.IndexOf(names, "ServiceFound")] =
            new _IDNSSDEvents_ServiceFoundEventHandler(OnServiceFound);
        slots[Array.IndexOf(names, "OperationFailed")] =
            new _IDNSSDEvents_OperationFailedEventHandler(OnOperationFailed);

        // Sanity: the managed call path works regardless of any COM plumbing.
        Say("STEP", "calling the sink as a managed object");
        sink.GetType().GetMethod("OperationFailed").Invoke(sink, new object[] { null, DNSSDError.kDNSSDError_NoError });
        Say(fired == 1 ? "OK" : "FAIL", "managed dispatch fired=" + fired);

        IntPtr unk = Marshal.GetIUnknownForObject(sink);
        Say("CCW", "IUnknown at 0x" + unk.ToString("x"));

        IntPtr disp = QueryInterface(unk, ref IID_IDispatch, "IDispatch");
        IntPtr ev   = QueryInterface(unk, ref iid, "_IDNSSDEvents");

        if (disp != IntPtr.Zero) TryDispatch(disp, names, dispids);
        else Say("NOTE", "no IDispatch on the CCW: a dispinterface source cannot call this sink");

        if (ev != IntPtr.Zero) TryVtable(ev, disp, names);
        else Say("NOTE", "no vtable for the event IID either");

        // GetIDispatchForObject is the managed shortcut for the same question.
        try { Say("CCW", "GetIDispatchForObject -> 0x" + Marshal.GetIDispatchForObject(sink).ToString("x")); }
        catch (Exception e) { Say("CCW", "GetIDispatchForObject -> " + e.GetType().Name + ": " + e.Message); }

        Say("RESULT", "sink invocations delivered: " + fired + " (1 is the managed baseline)");
        return fired > 1 ? 0 : 1;
    }

    static IntPtr QueryInterface(IntPtr unk, ref Guid iid, string what)
    {
        IntPtr p;
        int hr = Marshal.QueryInterface(unk, ref iid, out p);
        Say(hr == 0 ? "OK" : "FAIL", string.Format("QueryInterface({0}) -> 0x{1:x8}{2}",
            what, hr, hr == 0 ? " at 0x" + p.ToString("x") : ""));
        return hr == 0 ? p : IntPtr.Zero;
    }

    static T Slot<T>(IntPtr obj, int slot) where T : class
    {
        IntPtr vtbl = Marshal.ReadIntPtr(obj);
        IntPtr fn = Marshal.ReadIntPtr(vtbl, slot * IntPtr.Size);
        return Marshal.GetDelegateForFunctionPointer(fn, typeof(T)) as T;
    }

    static void TryDispatch(IntPtr disp, string[] names, int[] dispids)
    {
        int slot = Array.IndexOf(names, "ServiceFound");
        int dispid = dispids[slot];

        // A source that has a type library knows the dispid; one that does not
        // asks.  Both are worth knowing about.
        try
        {
            var byName = Slot<GetIDsOfNamesFn>(disp, SLOT_GETIDSOFNAMES);
            IntPtr name = SysAllocString("ServiceFound");
            IntPtr namePtr = Marshal.AllocCoTaskMem(IntPtr.Size);
            IntPtr idPtr = Marshal.AllocCoTaskMem(4);
            Marshal.WriteIntPtr(namePtr, name);
            int hr = byName(disp, ref IID_NULL, namePtr, 1, 0, idPtr);
            Say(hr == 0 ? "OK" : "FAIL", string.Format("GetIDsOfNames(\"ServiceFound\") -> 0x{0:x8}{1}",
                hr, hr == 0 ? " dispid=" + Marshal.ReadInt32(idPtr) : ""));
            if (hr == 0) runtimeDispid = Marshal.ReadInt32(idPtr);
            SysFreeString(name);
            Marshal.FreeCoTaskMem(namePtr);
            Marshal.FreeCoTaskMem(idPtr);
        }
        catch (Exception e) { Say("FAIL", "GetIDsOfNames threw " + e.GetType().Name + ": " + e.Message); }

        // Bonjour would call the dispid its own type library declares (3).  A
        // source that asks the sink instead gets whatever the CCW answers.  Try
        // both: the difference decides what our replacement server has to do.
        if (dispid >= 0) Invoke(disp, dispid, "type library");
        if (runtimeDispid >= 0 && runtimeDispid != dispid) Invoke(disp, runtimeDispid, "GetIDsOfNames");
        if (dispid < 0 && runtimeDispid < 0)
            Say("SKIP", "no dispid for ServiceFound from either source: cannot Invoke");
    }

    /// <summary>ServiceFound through IDispatch::Invoke, the way a dispinterface source calls.</summary>
    static void Invoke(IntPtr disp, int dispid, string where)
    {
        // VARIANT is 24 bytes on x64; args go in reverse order, as COM requires.
        const int VARIANT_SIZE = 24;
        object[] args = { null, DNSSDFlags.kDNSSDFlagsDefault, (uint)0, "FakeHR", "_wahoo-fitness-tnp._tcp.", "local." };
        IntPtr rgvarg = Marshal.AllocCoTaskMem(VARIANT_SIZE * args.Length);
        for (int i = 0; i < args.Length; i++)
        {
            IntPtr v = new IntPtr(rgvarg.ToInt64() + VARIANT_SIZE * i);
            for (int b = 0; b < VARIANT_SIZE; b++) Marshal.WriteByte(v, b, 0);
            Marshal.GetNativeVariantForObject(args[args.Length - 1 - i], v);
        }

        var pars = new DISPPARAMS { rgvarg = rgvarg, rgdispidNamedArgs = IntPtr.Zero,
                                    cArgs = (uint)args.Length, cNamedArgs = 0 };
        int before = fired;
        try
        {
            var invoke = Slot<InvokeFn>(disp, SLOT_INVOKE);
            int hr = invoke(disp, dispid, ref IID_NULL, 0, DISPATCH_METHOD, ref pars,
                            IntPtr.Zero, IntPtr.Zero, IntPtr.Zero);
            Say(hr == 0 ? "OK" : "FAIL", string.Format("Invoke(dispid={0} from {1}) -> 0x{2:x8}",
                                                       dispid, where, hr));
        }
        catch (Exception e) { Say("FAIL", "Invoke threw " + e.GetType().Name + ": " + e.Message); }

        Say(fired > before ? "OK" : "FAIL",
            string.Format("Invoke(dispid={0}) reached the handler: {1}", dispid, fired > before));
        Marshal.FreeCoTaskMem(rgvarg);
    }

    static void TryVtable(IntPtr ev, IntPtr disp, string[] names)
    {
        // The emitted interface is declared InterfaceIsIDispatch, so its vtable
        // should carry IDispatch's four slots before the first event method --
        // slot 7.  Getting this wrong calls the wrong function and takes the
        // process down, so it is settable rather than guessed twice.
        int first = SLOT_FIRST_MEMBER;
        string env = Environment.GetEnvironmentVariable("SINK_VTABLE_BASE");
        if (env != null) first = int.Parse(env);
        Say("NOTE", string.Format("event methods assumed to start at vtable slot {0}"
                                  + " (SINK_VTABLE_BASE overrides)", first));

        int slot = Array.IndexOf(names, "OperationFailed");
        int before = fired;
        try
        {
            var call = Slot<OperationFailedFn>(ev, first + slot);
            int hr = call(ev, IntPtr.Zero, 0);
            Say(hr == 0 ? "OK" : "FAIL", string.Format("vtable OperationFailed(slot {0}) -> 0x{1:x8}",
                                                       first + slot, hr));
        }
        catch (Exception e) { Say("FAIL", "vtable call threw " + e.GetType().Name + ": " + e.Message); }

        Say(fired > before ? "OK" : "FAIL", "early-bound call reached the handler: " + (fired > before));

        // The call the replacement server actually has to make: three BSTRs and
        // a null interface pointer, which the CCW has to turn into managed
        // strings and a null RCW.
        int fslot = Array.IndexOf(names, "ServiceFound");
        IntPtr name = SysAllocString("FakeHR");
        IntPtr regType = SysAllocString("_wahoo-fitness-tnp._tcp.");
        IntPtr domain = SysAllocString("local.");
        before = fired;
        try
        {
            var call = Slot<ServiceFoundFn>(ev, first + fslot);
            int hr = call(ev, IntPtr.Zero, 2 /* kDNSServiceFlagsAdd */, 0, name, regType, domain);
            Say(hr == 0 ? "OK" : "FAIL", string.Format("vtable ServiceFound(slot {0}) -> 0x{1:x8}",
                                                       first + fslot, hr));
        }
        catch (Exception e) { Say("FAIL", "vtable ServiceFound threw " + e.GetType().Name + ": " + e.Message); }
        Say(fired > before ? "OK" : "FAIL", "early-bound ServiceFound reached the handler: " + (fired > before));
        SysFreeString(name); SysFreeString(regType); SysFreeString(domain);
    }
}
