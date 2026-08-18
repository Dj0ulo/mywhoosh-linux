// Isolates which part of MyWhoosh's Bonjour COM activation wine-mono cannot do.
//
// WahooProgram.WFTNP_Init() performs, in order:
//   1. Marshal.GetTypeFromCLSID(guid)         -- CLSID -> System.__ComObject type
//   2. Activator.CreateInstance(type)         -- CoCreateInstance
//   3. new ComAwareEventInfo(iface, "ServiceFound").AddEventHandler(...)
//                                             -- IConnectionPoint sink wiring
//   4. IDNSSDService.Browse(...)              -- late-bound COM call
//
// Each step is probed separately so a failure names the exact missing piece.

using System;
using System.Runtime.InteropServices;

class ComProbe
{
    static readonly Guid CLSID_DNSSDService      = new Guid("24CD4DE9-FF84-4701-9DC1-9B69E0D1090A");
    static readonly Guid CLSID_DNSSDEventManager = new Guid("BEEB932A-8D4A-4619-AEFE-A836F988B221");

    static void Main()
    {
        Type tSvc = Step("Marshal.GetTypeFromCLSID(DNSSDService)",
                         () => Marshal.GetTypeFromCLSID(CLSID_DNSSDService));

        object svc = tSvc == null ? null
                   : Step("Activator.CreateInstance(DNSSDService)", () => Activator.CreateInstance(tSvc));

        Type tEvt = Step("Marshal.GetTypeFromCLSID(DNSSDEventManager)",
                         () => Marshal.GetTypeFromCLSID(CLSID_DNSSDEventManager));

        object evt = tEvt == null ? null
                   : Step("Activator.CreateInstance(DNSSDEventManager)", () => Activator.CreateInstance(tEvt));

        // Fallback path: CoCreateInstance directly, bypassing the Type-based route.
        Step("CoCreateInstance(DNSSDService) via ole32", () => {
            Guid iid = new Guid("00000000-0000-0000-C000-000000000046"); // IID_IUnknown
            IntPtr p;
            int hr = CoCreateInstance(ref Unsafe(CLSID_DNSSDService), IntPtr.Zero, 5 /*INPROC|LOCAL*/, ref iid, out p);
            if (hr != 0) throw new COMException("CoCreateInstance", hr);
            return "IUnknown* = 0x" + p.ToString("x");
        });

        if (svc != null)
            Step("IDispatch Browse() on DNSSDService", () =>
                svc.GetType().InvokeMember("Browse", System.Reflection.BindingFlags.InvokeMethod,
                    null, svc, new object[] { 0, 0, "_wahoo-fitness-tnp._tcp.", null, evt }));

        Console.WriteLine("\nprobe complete");
    }

    static Guid _tmp;
    static ref Guid Unsafe(Guid g) { _tmp = g; return ref _tmp; }

    [DllImport("ole32.dll")]
    static extern int CoCreateInstance(ref Guid clsid, IntPtr outer, uint ctx, ref Guid iid, out IntPtr obj);

    static T Step<T>(string what, Func<T> f) where T : class
    {
        try { var r = f(); Console.WriteLine("  OK   " + what + " -> " + (r ?? (object)"null")); return r; }
        catch (Exception e)
        {
            var inner = e; while (inner.InnerException != null) inner = inner.InnerException;
            Console.WriteLine("  FAIL " + what + " -> " + inner.GetType().Name + ": " + inner.Message);
            return null;
        }
    }
}
