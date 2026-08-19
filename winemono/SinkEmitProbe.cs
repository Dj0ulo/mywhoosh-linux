// Emits the sink type for MyWhoosh's embedded Bonjour dispinterface without
// touching COM, so an emission failure is separable from an Advise failure.
// Also shows whether the dispids came out of Bonjour's registered type library.

using System;
using MyWhoosh.ComEventShim;

class SinkEmitProbe
{
    static void Main()
    {
        Type events = Type.GetType("Bonjour._IDNSSDEvents_Event, WindowsConnectivity", true);
        Type source = Type.GetType("Bonjour._IDNSSDEvents, WindowsConnectivity", true);

        Guid iid; string[] names; int[] dispids;
        object sink = ComEventShimDiagnostics.BuildSink(events, source, out iid, out names, out dispids);

        Console.WriteLine("source IID : " + iid);
        for (int i = 0; i < names.Length; i++)
            Console.WriteLine("  slot " + i + "   : " + names[i]
                              + " dispid=" + (dispids[i] < 0 ? "unknown" : dispids[i].ToString()));

        Console.WriteLine("sink type  : " + sink.GetType().Name);
        foreach (Type i in sink.GetType().GetInterfaces())
            Console.WriteLine("  implements: " + i.Name + " " + i.GUID);
        Console.WriteLine("ok");
    }
}
