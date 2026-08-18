// Talks to Bonjour's dnssd.dll C API directly, bypassing the COM wrapper.
// Separates "is mDNSResponder working under Wine" from "does Mono's COM
// eventing work" -- the two failure modes the Dircon path can hit.

using System;
using System.Runtime.InteropServices;

class DnssdProbe
{
    delegate void BrowseReply(IntPtr sdRef, uint flags, uint ifIndex, int errorCode,
                              IntPtr serviceName, IntPtr regtype, IntPtr replyDomain, IntPtr context);

    [DllImport("dnssd.dll")] static extern int DNSServiceBrowse(
        out IntPtr sdRef, uint flags, uint ifIndex,
        [MarshalAs(UnmanagedType.LPStr)] string regtype,
        [MarshalAs(UnmanagedType.LPStr)] string domain,
        BrowseReply cb, IntPtr context);

    delegate void RegisterReply(IntPtr sdRef, uint flags, int errorCode,
                                IntPtr name, IntPtr regtype, IntPtr domain, IntPtr context);

    [DllImport("dnssd.dll")] static extern int DNSServiceRegister(
        out IntPtr sdRef, uint flags, uint ifIndex,
        [MarshalAs(UnmanagedType.LPStr)] string name,
        [MarshalAs(UnmanagedType.LPStr)] string regtype,
        [MarshalAs(UnmanagedType.LPStr)] string domain,
        [MarshalAs(UnmanagedType.LPStr)] string host,
        ushort port, ushort txtLen, IntPtr txtRecord,
        RegisterReply cb, IntPtr context);

    [DllImport("dnssd.dll")] static extern int  DNSServiceProcessResult(IntPtr sdRef);
    [DllImport("dnssd.dll")] static extern int  DNSServiceRefSockFD(IntPtr sdRef);
    [DllImport("dnssd.dll")] static extern void DNSServiceRefDeallocate(IntPtr sdRef);
    [DllImport("dnssd.dll")] static extern int  DNSServiceGetProperty(
        [MarshalAs(UnmanagedType.LPStr)] string prop, out uint result, ref uint size);

    static int found;

    static void Main(string[] args)
    {
        // usage: DnssdProbe [seconds] [regtype] [register]
        int seconds = args.Length > 0 ? int.Parse(args[0]) : 20;
        string type = args.Length > 1 ? args[1] : "_wahoo-fitness-tnp._tcp";
        bool register = args.Length > 2 && args[2] == "register";

        uint version = 0, size = 4;
        int hr = DNSServiceGetProperty("DaemonVersion", out version, ref size);
        Console.WriteLine("DNSServiceGetProperty(DaemonVersion) -> err=" + hr + " version=" + version);
        if (hr != 0) { Console.WriteLine("mDNSResponder is not reachable -- the daemon is not serving clients."); return; }

        IntPtr sdRef;
        BrowseReply   bcb = OnBrowse;                 // keep alive across the call
        RegisterReply rcb = OnRegister;

        if (register)
        {
            // Registering through the same daemon lets us exercise browse/resolve
            // without needing port 5353, which the host's avahi-daemon already owns.
            hr = DNSServiceRegister(out sdRef, 0, 0, "FakeTrainer", type, null, null,
                                    unchecked((ushort)((36866 >> 8) | (36866 << 8))), 0, IntPtr.Zero,
                                    rcb, IntPtr.Zero);
            Console.WriteLine("DNSServiceRegister(FakeTrainer." + type + ") -> err=" + hr);
        }
        else
        {
            hr = DNSServiceBrowse(out sdRef, 0, 0, type, null, bcb, IntPtr.Zero);
            Console.WriteLine("DNSServiceBrowse(" + type + ") -> err=" + hr);
        }
        if (hr != 0) return;

        Console.WriteLine("browsing for " + seconds + "s...\n");
        var deadline = DateTime.Now.AddSeconds(seconds);
        while (DateTime.Now < deadline)
        {
            // DNSServiceProcessResult blocks until a reply is available; the socket
            // is polled first so the loop can still time out.
            if (Poll(DNSServiceRefSockFD(sdRef), 500)) DNSServiceProcessResult(sdRef);
        }
        DNSServiceRefDeallocate(sdRef);
        GC.KeepAlive(bcb); GC.KeepAlive(rcb);
        Console.WriteLine("\nprobe complete (" + found + " service(s) found)");
    }

    static void OnBrowse(IntPtr sdRef, uint flags, uint ifIndex, int err,
                         IntPtr name, IntPtr regtype, IntPtr domain, IntPtr ctx)
    {
        found++;
        Console.WriteLine("  FOUND \"" + Marshal.PtrToStringAnsi(name) + "\" "
                        + Marshal.PtrToStringAnsi(regtype) + Marshal.PtrToStringAnsi(domain)
                        + " if=" + ifIndex + " err=" + err);
        Console.Out.Flush();
    }

    static void OnRegister(IntPtr sdRef, uint flags, int err, IntPtr name, IntPtr regtype, IntPtr domain, IntPtr ctx)
    {
        Console.WriteLine("  REGISTERED \"" + Marshal.PtrToStringAnsi(name) + "\" "
                        + Marshal.PtrToStringAnsi(regtype) + Marshal.PtrToStringAnsi(domain) + " err=" + err);
        Console.Out.Flush();
    }

    [StructLayout(LayoutKind.Sequential)]
    struct TimeVal { public int tv_sec, tv_usec; }
    [StructLayout(LayoutKind.Sequential)]
    struct FdSet { public uint count; public IntPtr fd; }

    [DllImport("ws2_32.dll")] static extern int select(int nfds, ref FdSet read, IntPtr w, IntPtr e, ref TimeVal t);

    static bool Poll(int fd, int ms)
    {
        var set = new FdSet { count = 1, fd = (IntPtr)fd };
        var tv  = new TimeVal { tv_sec = ms / 1000, tv_usec = (ms % 1000) * 1000 };
        return select(0, ref set, IntPtr.Zero, IntPtr.Zero, ref tv) > 0;
    }
}
