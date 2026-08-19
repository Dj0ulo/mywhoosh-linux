// Minimal reproducer for the wine-mono bug the shim has to route around, and a
// map of what reflection over embedded interop types can and cannot do.
//
// Bonjour._IDNSSDEvents is an embedded ([TypeIdentifier]) dispinterface with
// _VtblGapN_M placeholder slots.  Resolving any of its methods trips
//
//   * Assertion at .../mono/metadata/icall.c:4348, condition `method->slot < nslots' not met
//
// which aborts the process instead of throwing, so it cannot be caught.  The
// crashing check is last on purpose: everything above it is what the shim relies
// on instead.

using System;
using System.Reflection;
using System.Runtime.InteropServices;

class ReflProbe
{
    static void Try(string what, Action a)
    {
        Console.Write("  " + what + " ... "); Console.Out.Flush();
        try { a(); Console.WriteLine("ok"); }
        catch (Exception e) { Console.WriteLine("FAIL " + e.GetType().Name + ": " + e.Message); }
        Console.Out.Flush();
    }

    static void Main()
    {
        Type src = Type.GetType("Bonjour._IDNSSDEvents, WindowsConnectivity", true);
        Type evt = Type.GetType("Bonjour._IDNSSDEvents_Event, WindowsConnectivity", true);

        Try("src.GUID", () => Console.Write(src.GUID + " "));
        Try("Marshal.GenerateGuidForType(src)", () => Console.Write(Marshal.GenerateGuidForType(src) + " "));
        Try("evt.GetEvents()", () => {
            foreach (EventInfo e in evt.GetEvents()) Console.Write(e.Name + " ");
        });
        Try("evt.GetEvent(\"ServiceFound\").EventHandlerType", () => {
            Type t = evt.GetEvent("ServiceFound").EventHandlerType;
            Console.Write(t.Name + "/" + t.GetMethod("Invoke").GetParameters().Length + " args ");
        });

        Console.WriteLine("  -- everything below aborts the runtime --");
        Try("src.GetMethod(\"ServiceFound\")", () => {
            MethodInfo m = src.GetMethod("ServiceFound");
            Console.Write((m == null ? "null" : m.Name) + " ");
        });
        Try("src.GetMethods()", () => { foreach (MethodInfo m in src.GetMethods()) Console.Write(m.Name + " "); });
    }
}
