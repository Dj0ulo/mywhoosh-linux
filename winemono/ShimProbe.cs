// Checks the patched ComAwareEventInfo without needing Bonjour or the game:
// the managed (non-COM) event path, which must behave exactly like plain
// EventInfo, and the reflection surface the runtime itself uses.
//
// The COM path -- IConnectionPoint::Advise against a real dispinterface -- is
// covered by ../dircon/BonjourProbe.cs, which needs Bonjour installed.

using System;
using System.Reflection;
using System.Runtime.InteropServices;

public delegate void Ping(string what);

public interface IPingSource { event Ping Pinged; }

public class Source : IPingSource
{
    public event Ping Pinged;
    public void Raise(string what) { if (Pinged != null) Pinged(what); }
}

class ShimProbe
{
    static int fails;
    static string last;

    static void Main()
    {
        Console.WriteLine("runtime : " + typeof(object).Assembly.Location);
        Console.WriteLine("System.Core: " + typeof(ComAwareEventInfo).Assembly.Location);
        Console.WriteLine();

        var evt = Step("construct ComAwareEventInfo(IPingSource, \"Pinged\")",
                       () => new ComAwareEventInfo(typeof(IPingSource), "Pinged"));
        if (evt == null) { Done(); return; }

        Check("Name", () => evt.Name, "Pinged");
        Check("DeclaringType", () => evt.DeclaringType.Name, "IPingSource");
        Check("EventHandlerType", () => evt.EventHandlerType.Name, "Ping");
        Check("GetAddMethod().Name", () => evt.GetAddMethod().Name, "add_Pinged");
        Check("Attributes", () => evt.Attributes.ToString(), "None");
        Check("IsDefined(ObsoleteAttribute)", () => evt.IsDefined(typeof(ObsoleteAttribute), false).ToString(), "False");
        Check("GetCustomAttributes().Length", () => evt.GetCustomAttributes(false).Length.ToString(), "0");

        var source = new Source();
        var handler = new Ping(OnPing);

        Step("AddEventHandler (managed target)", () => { evt.AddEventHandler(source, handler); return "ok"; });
        last = null; source.Raise("first");
        Check("handler fired", () => last, "first");

        Step("RemoveEventHandler (managed target)", () => { evt.RemoveEventHandler(source, handler); return "ok"; });
        last = null; source.Raise("second");
        Check("handler detached", () => last == null ? "<null>" : last, "<null>");

        Done();
    }

    static void OnPing(string what) { last = what; }

    static void Done()
    {
        Console.WriteLine();
        Console.WriteLine(fails == 0 ? "all checks passed" : fails + " check(s) failed");
        Environment.Exit(fails == 0 ? 0 : 1);
    }

    static T Step<T>(string what, Func<T> f) where T : class
    {
        try { T r = f(); Console.WriteLine("  OK   " + what); return r; }
        catch (Exception e)
        {
            Exception inner = e; while (inner.InnerException != null) inner = inner.InnerException;
            Console.WriteLine("  FAIL " + what + " -> " + inner.GetType().Name + ": " + inner.Message);
            fails++;
            return null;
        }
    }

    static void Check(string what, Func<string> f, string expected)
    {
        string got;
        try { got = f(); }
        catch (Exception e)
        {
            Exception inner = e; while (inner.InnerException != null) inner = inner.InnerException;
            Console.WriteLine("  FAIL " + what + " -> " + inner.GetType().Name + ": " + inner.Message);
            fails++;
            return;
        }
        if (got == expected) Console.WriteLine("  OK   " + what + " = " + got);
        else { Console.WriteLine("  FAIL " + what + " = " + got + " (expected " + expected + ")"); fails++; }
    }
}
