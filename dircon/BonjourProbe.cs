// Replicates WahooProgram.WFTNP_Init() / WFTNP_StartScan() step by step, using
// the very same embedded-interop Bonjour types that ship inside
// WindowsConnectivity.dll -- so a failure here is exactly the failure the game hits.
//
//   1. CoCreateInstance DNSSDEventManager  {BEEB932A-8D4A-4619-AEFE-A836F988B221}
//   2. ComAwareEventInfo(...).AddEventHandler  -- IConnectionPoint sink wiring
//   3. CoCreateInstance DNSSDService       {24CD4DE9-FF84-4701-9DC1-9B69E0D1090A}
//   4. IDNSSDService.Browse("_wahoo-fitness-tnp._tcp.")
//
// Then it sits in a message loop so ServiceFound callbacks can arrive.

using System;
using System.Runtime.InteropServices;
using System.Threading;
using Bonjour;

class BonjourProbe
{
    const string SERVICE_TYPE = "_wahoo-fitness-tnp._tcp.";

    static readonly Guid CLSID_DNSSDService      = new Guid("24CD4DE9-FF84-4701-9DC1-9B69E0D1090A");
    static readonly Guid CLSID_DNSSDEventManager = new Guid("BEEB932A-8D4A-4619-AEFE-A836F988B221");

    static void Main(string[] args)
    {
        int seconds = args.Length > 0 ? int.Parse(args[0]) : 20;

        DNSSDEventManager em = Step("create DNSSDEventManager", () =>
            (DNSSDEventManager)Activator.CreateInstance(Marshal.GetTypeFromCLSID(CLSID_DNSSDEventManager)));
        if (em == null) return;

        // The game wires its handlers through ComAwareEventInfo rather than the
        // C# event syntax, so probe that exact route.
        Hook(em, "ServiceFound",   new _IDNSSDEvents_ServiceFoundEventHandler(OnFound));
        Hook(em, "ServiceLost",    new _IDNSSDEvents_ServiceLostEventHandler(OnLost));
        Hook(em, "ServiceResolved",new _IDNSSDEvents_ServiceResolvedEventHandler(OnResolved));
        Hook(em, "OperationFailed",new _IDNSSDEvents_OperationFailedEventHandler(OnFailed));

        DNSSDService main = Step("create DNSSDService", () =>
            (DNSSDService)Activator.CreateInstance(Marshal.GetTypeFromCLSID(CLSID_DNSSDService)));
        if (main == null) return;

        DNSSDService browser = Step("Browse(" + SERVICE_TYPE + ")", () =>
            ((IDNSSDService)main).Browse(0, 0, SERVICE_TYPE, null, em));
        if (browser == null) return;

        Console.WriteLine("\nbrowsing for " + seconds + "s -- publish a service on the host with:");
        Console.WriteLine("  avahi-publish -s FakeTrainer _wahoo-fitness-tnp._tcp 36866\n");
        for (int i = 0; i < seconds; i++) { Thread.Sleep(1000); Console.Out.Flush(); }

        Console.WriteLine("\nprobe complete (" + found + " service(s) found)");
        Environment.Exit(0);
    }

    static int found;

    static void Hook(DNSSDEventManager em, string name, Delegate handler)
    {
        Step("hook " + name, () => {
            var evt = new ComAwareEventInfo(typeof(_IDNSSDEvents_Event), name);
            evt.AddEventHandler(em, handler);
            return "wired";
        });
    }

    static void OnFound(DNSSDService b, DNSSDFlags f, uint ifIndex, string name, string regtype, string domain)
    {
        found++;
        Console.WriteLine("  FOUND  \"" + name + "\" " + regtype + " " + domain + " if=" + ifIndex + " flags=" + f);
        Console.Out.Flush();
    }
    static void OnLost(DNSSDService b, DNSSDFlags f, uint ifIndex, string name, string regtype, string domain)
        { Console.WriteLine("  LOST   \"" + name + "\""); }
    static void OnResolved(DNSSDService s, DNSSDFlags f, uint ifIndex, string full, string host, ushort port, TXTRecord txt)
        { Console.WriteLine("  RESOLVED " + full + " -> " + host + ":" + port); }
    static void OnFailed(DNSSDService s, DNSSDError e)
        { Console.WriteLine("  FAILED  " + e); }

    static T Step<T>(string what, Func<T> f) where T : class
    {
        try { var r = f(); Console.WriteLine("  OK   " + what); return r; }
        catch (Exception e)
        {
            var inner = e; while (inner.InnerException != null) inner = inner.InnerException;
            Console.WriteLine("  FAIL " + what + " -> " + inner.GetType().Name + ": " + inner.Message);
            Console.WriteLine(inner.StackTrace);
            return null;
        }
    }
}
