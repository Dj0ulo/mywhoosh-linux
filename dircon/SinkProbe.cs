// wine-mono's System.Core has ComAwareEventInfo as a throw-only stub, which is
// what kills WahooProgram.WFTNP_Init().  ComAwareEventInfo is only a convenience
// wrapper over IConnectionPointContainer::Advise, so this probe answers the
// question that decides whether the Dircon path is salvageable from outside the
// runtime: can Mono Advise a *managed* sink object on a COM connection point?
//
// If Advise succeeds and events arrive, the missing piece can be supplied by a
// shipped helper assembly instead of a patched wine-mono.

using System;
using System.Linq;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Threading;
using Bonjour;

// _IDNSSDEvents is a pure dispinterface, so the connection point calls the sink
// through IDispatch::Invoke.  Declaring our own copy avoids the interop type's
// vtable-gap placeholders, which Mono refuses to lay out in a managed class.
[ComImport, Guid("21ae8d7f-d5fe-45cf-b632-cfa2c2c6b498")]
[InterfaceType(ComInterfaceType.InterfaceIsIDispatch)]
public interface IDnssdEventsSink
{
    void ServiceFound(DNSSDService b, DNSSDFlags f, uint ifIndex, string name, string regtype, string domain);
    void ServiceLost(DNSSDService b, DNSSDFlags f, uint ifIndex, string name, string regtype, string domain);
    void ServiceResolved(DNSSDService s, DNSSDFlags f, uint ifIndex, string full, string host, ushort port, TXTRecord txt);
    void OperationFailed(DNSSDService s, DNSSDError e);
}

[ComVisible(true)]
[ClassInterface(ClassInterfaceType.None)]
public class Sink : IDnssdEventsSink
{
    public static int found;

    public void ServiceFound(DNSSDService b, DNSSDFlags f, uint ifIndex, string name, string regtype, string domain)
    {
        found++;
        Console.WriteLine("  FOUND  \"" + name + "\" " + regtype + domain + " if=" + ifIndex);
        Console.Out.Flush();
    }
    public void ServiceLost(DNSSDService b, DNSSDFlags f, uint ifIndex, string name, string regtype, string domain)
        { Console.WriteLine("  LOST   \"" + name + "\""); }
    public void ServiceResolved(DNSSDService s, DNSSDFlags f, uint ifIndex, string full, string host, ushort port, TXTRecord txt)
        { Console.WriteLine("  RESOLVED " + full + " -> " + host + ":" + port); Console.Out.Flush(); }
    public void OperationFailed(DNSSDService s, DNSSDError e)
        { Console.WriteLine("  FAILED " + e); }

}

class SinkProbe
{
    [StructLayout(LayoutKind.Sequential)]
    struct MSG { public IntPtr hwnd; public uint message; public IntPtr wParam, lParam; public uint time; public int x, y; }

    [DllImport("user32.dll")] static extern bool PeekMessage(out MSG m, IntPtr hWnd, uint min, uint max, uint remove);
    [DllImport("user32.dll")] static extern bool TranslateMessage(ref MSG m);
    [DllImport("user32.dll")] static extern IntPtr DispatchMessage(ref MSG m);

    static readonly Guid CLSID_DNSSDService      = new Guid("24CD4DE9-FF84-4701-9DC1-9B69E0D1090A");
    static readonly Guid CLSID_DNSSDEventManager = new Guid("BEEB932A-8D4A-4619-AEFE-A836F988B221");

    // Bonjour's COM objects are apartment-threaded; from an MTA thread the call
    // is marshalled to a host apartment that never pumps, and Browse() fails with
    // RPC_S_SERVER_UNAVAILABLE.
    [STAThread]
    static void Main(string[] args)
    {
        int seconds = args.Length > 0 ? int.Parse(args[0]) : 20;

        Guid iidEvents = typeof(IDnssdEventsSink).GUID;
        Console.WriteLine("  _IDNSSDEvents IID = " + iidEvents);

        object em = Activator.CreateInstance(Marshal.GetTypeFromCLSID(CLSID_DNSSDEventManager));
        Console.WriteLine("  OK   DNSSDEventManager created");

        var cpc = em as IConnectionPointContainer;
        if (cpc == null) { Console.WriteLine("  FAIL cast to IConnectionPointContainer"); return; }
        Console.WriteLine("  OK   IConnectionPointContainer");

        IConnectionPoint cp;
        cpc.FindConnectionPoint(ref iidEvents, out cp);
        Console.WriteLine("  OK   FindConnectionPoint");

        int cookie;
        try { cp.Advise(new Sink(), out cookie); }
        catch (Exception e) { Console.WriteLine("  FAIL Advise -> " + e.GetType().Name + ": " + e.Message); return; }
        Console.WriteLine("  OK   Advise, cookie=" + cookie);

        Console.WriteLine("  ...  creating DNSSDService"); Console.Out.Flush();
        object svc = Activator.CreateInstance(Marshal.GetTypeFromCLSID(CLSID_DNSSDService));
        Console.WriteLine("  OK   DNSSDService created"); Console.Out.Flush();

        Console.WriteLine("  ...  casting to IDNSSDService"); Console.Out.Flush();
        IDNSSDService isvc = (IDNSSDService)svc;
        Console.WriteLine("  OK   cast"); Console.Out.Flush();

        Console.WriteLine("  ...  casting eventManager to DNSSDEventManager"); Console.Out.Flush();
        DNSSDEventManager dem = (DNSSDEventManager)em;
        Console.WriteLine("  OK   cast"); Console.Out.Flush();

        Console.WriteLine("  ...  calling Browse"); Console.Out.Flush();
        try { isvc.Browse(0, 0, "_wahoo-fitness-tnp._tcp.", null, dem); }
        catch (Exception e) { Console.WriteLine("  FAIL Browse -> " + e.GetType().Name + ": " + e.Message); return; }
        Console.WriteLine("  OK   Browse started, waiting " + seconds + "s\n");

        // In an STA, COM callbacks are delivered through the thread's message
        // queue, so the sink only fires while we pump.  The game (Unreal) pumps
        // its own message loop, which is why this matches its behaviour.
        var deadline = DateTime.Now.AddSeconds(seconds);
        MSG msg;
        while (DateTime.Now < deadline)
        {
            while (PeekMessage(out msg, IntPtr.Zero, 0, 0, 1 /*PM_REMOVE*/))
            {
                TranslateMessage(ref msg);
                DispatchMessage(ref msg);
            }
            Thread.Sleep(20);
        }
        Console.WriteLine("\nprobe complete (" + Sink.found + " service(s) found)");
        Environment.Exit(0);
    }
}
