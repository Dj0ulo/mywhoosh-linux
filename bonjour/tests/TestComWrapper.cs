using System;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Windows.Forms;

[ComImport]
[Guid("29DE265F-8402-474F-833A-D4653B23458F")]
[InterfaceType(ComInterfaceType.InterfaceIsDual)]
public interface IBonjourService
{
    void _VtblGap1_1();
    [DispId(2)]
    [return: MarshalAs(UnmanagedType.Interface)]
    IBonjourService Browse(uint flags, uint interfaceIndex, string registrationType,
        string domain, [MarshalAs(UnmanagedType.Interface)] IBonjourEventManager eventManager);
    void _VtblGap2_9();
    [DispId(12)] void Stop();
}

[ComImport]
[Guid("7FD72324-63E1-45AD-B337-4D525BD98DAD")]
[InterfaceType(ComInterfaceType.InterfaceIsDual)]
public interface IBonjourEventManager { }

public enum TestFlags { None = 0, Add = 2 }
public delegate void FoundHandler(IBonjourService service, TestFlags flags, uint interfaceIndex,
    string name, string registrationType, string domain);

[ComEventInterface(typeof(IBonjourEvents), typeof(IBonjourEvents))]
public interface ITestEvents
{
    event FoundHandler ServiceFound;
}

public static class TestComWrapper
{
    private static ApplicationContext context;
    private static string expectedName;
    private static int received;

    [DllImport("dnssd.dll", CallingConvention = CallingConvention.StdCall, CharSet = CharSet.Ansi)]
    private static extern int DNSServiceRegister(out IntPtr service, uint flags,
        uint interfaceIndex, string name, string registrationType, string domain,
        string host, ushort port, ushort textLength, IntPtr text,
        IntPtr callback, IntPtr callbackContext);

    [DllImport("dnssd.dll", CallingConvention = CallingConvention.StdCall)]
    private static extern void DNSServiceRefDeallocate(IntPtr service);

    public static void Receive<TService, TFlags>(TService service, TFlags flags,
        uint interfaceIndex, string name, string registrationType, string domain)
    {
        if (name != expectedName)
            return;
        if (service == null || !Marshal.IsComObject(service) || !typeof(TFlags).IsEnum)
            throw new InvalidOperationException("The callback did not deliver the expected COM object and enum.");
        received++;
        Console.WriteLine("Typed callback received: " + name + ", flags=" + flags);
        context.ExitThread();
    }

    [STAThread]
    public static int Main(string[] arguments)
    {
        object manager = null;
        IBonjourService service = null;
        IBonjourService browser = null;
        IntPtr registration = IntPtr.Zero;
        EventInfo eventInfo = null;
        Delegate handler = null;
        try
        {
            if (arguments.Length > 1)
                throw new ArgumentException("Supply at most one WindowsConnectivity.dll path.");
            Console.WriteLine("System.Core: " + typeof(ComAwareEventInfo).Assembly.Location);
            var assembly = arguments.Length == 1 ? Assembly.LoadFrom(arguments[0]) : null;
            var eventInterface = assembly == null ? typeof(ITestEvents) : assembly.GetType("Bonjour._IDNSSDEvents_Event", true);
            eventInfo = new ComAwareEventInfo(eventInterface, "ServiceFound");
            var parameters = eventInfo.EventHandlerType.GetMethod("Invoke").GetParameters();
            var receiver = typeof(TestComWrapper).GetMethod("Receive").MakeGenericMethod(
                parameters[0].ParameterType, parameters[1].ParameterType);
            handler = Delegate.CreateDelegate(eventInfo.EventHandlerType, receiver);
            manager = Activator.CreateInstance(Type.GetTypeFromCLSID(new Guid("BEEB932A-8D4A-4619-AEFE-A836F988B221"), true));
            eventInfo.AddEventHandler(manager, handler);
            eventInfo.AddEventHandler(manager, handler);
            eventInfo.RemoveEventHandler(manager, handler);
            using (context = new ApplicationContext())
            using (var timeout = new Timer())
            {
                expectedName = "MyWhooshWrapperProbe" + System.Diagnostics.Process.GetCurrentProcess().Id;
                int result = DNSServiceRegister(out registration, 0, uint.MaxValue,
                    expectedName, "_mywhoosh-probe._tcp", "local.", null,
                    0x0900, 0, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero);
                if (result != 0)
                    throw new InvalidOperationException("DNSServiceRegister returned " + result);
                service = (IBonjourService)Activator.CreateInstance(Type.GetTypeFromCLSID(
                    new Guid("24CD4DE9-FF84-4701-9DC1-9B69E0D1090A"), true));
                browser = service.Browse(0, uint.MaxValue, "_mywhoosh-probe._tcp", "local.", (IBonjourEventManager)manager);
                timeout.Interval = 10000;
                timeout.Tick += delegate { context.ExitThread(); };
                timeout.Start();
                Application.Run(context);
                timeout.Stop();
            }
            if (received != 1)
                throw new InvalidOperationException("Expected one callback after removing a duplicate handler; received " + received);
            eventInfo.RemoveEventHandler(manager, handler);
            handler = null;
            Console.WriteLine("PASS: real local-only Bonjour callback, typed delegate, duplicate removal and unsubscribe.");
            TestDispatch(manager, service);
            if (assembly != null)
            {
                var managerType = assembly.GetType("OpenBike.OpenBikeManager", true);
                var openBike = Activator.CreateInstance(managerType, true);
                var enabled = managerType.GetField("isBonjourEnabled", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
                if (enabled == null || !(bool)enabled.GetValue(openBike))
                    throw new InvalidOperationException("OpenBike does not see Bonjour enabled; this would skip the required code path.");
                managerType.GetMethod("OBM_Initialize", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic).Invoke(openBike, null);
                Console.WriteLine("PASS: original OBM_Initialize completed with Bonjour enabled under Mono.");
            }
            return 0;
        }
        catch (Exception error)
        {
            Console.Error.WriteLine(error);
            return 1;
        }
        finally
        {
            if (handler != null && manager != null)
                eventInfo.RemoveEventHandler(manager, handler);
            if (browser != null)
            {
                browser.Stop();
                Marshal.ReleaseComObject(browser);
            }
            if (service != null)
                Marshal.ReleaseComObject(service);
            if (registration != IntPtr.Zero)
                DNSServiceRefDeallocate(registration);
            if (manager != null)
                Marshal.ReleaseComObject(manager);
        }
    }

    private static void TestDispatch(object manager, IBonjourService service)
    {
        int lost = 0, resolved = 0, failed = 0;
        using (var bridge = new BonjourBridge(manager))
        {
            FoundHandler lostHandler = delegate(IBonjourService callbackService, TestFlags flags, uint index,
                string name, string type, string domain) {
                if (callbackService != null && flags == TestFlags.None && name == "lost") lost++;
            };
            Action<IBonjourService, TestFlags, uint, string, string, ushort, object> resolvedHandler =
                delegate(IBonjourService callbackService, TestFlags flags, uint index, string fullName,
                    string host, ushort port, object text) {
                    if (callbackService != null && port == 9 && host == "localhost.") resolved++;
                };
            Action<IBonjourService, TestFlags> failedHandler = delegate(IBonjourService callbackService, TestFlags error) {
                if (callbackService != null && (int)error == -65537) failed++;
            };
            bridge.Add("ServiceLost", lostHandler);
            bridge.Add("ServiceResolved", resolvedHandler);
            bridge.Add("OperationFailed", failedHandler);
            bridge.ServiceLost(service, 0, uint.MaxValue, "lost", "_mywhoosh-probe._tcp", "local.");
            bridge.ServiceResolved(service, 0, uint.MaxValue, "probe.local.", "localhost.", 9, null);
            bridge.OperationFailed(service, -65537);
            bridge.Remove("ServiceLost", lostHandler);
            bridge.ServiceLost(service, 0, uint.MaxValue, "lost", "_mywhoosh-probe._tcp", "local.");
        }
        if (lost != 1 || resolved != 1 || failed != 1)
            throw new InvalidOperationException("Synthetic event dispatch or removal failed.");
        Console.WriteLine("PASS: synthetic ServiceLost, ServiceResolved and OperationFailed dispatch.");
    }
}