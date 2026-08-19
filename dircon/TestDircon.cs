// Test harness for MyWhoosh's Wahoo Dircon (network sensor) code path.
//
// MyWhoosh exposes two independent sensor stacks in WindowsConnectivity.dll:
//   BT_*  -> Windows.Devices.Bluetooth (WinRT) -- unusable under wine-mono
//   WD_*  -> Wahoo Direct Connect: GATT over TCP, discovered over mDNS/Bonjour
//
// The WD_ path touches no WinRT at all, so it is the only stack that can work
// under Wine without reimplementing the WinRT projection.  This harness drives
// it directly, without launching the game, and prints everything the manager
// logs so we can see how far it gets.
//
// Build: ./build.sh      Run: ./run.sh

using System;
using System.Runtime.InteropServices;
using System.Threading;
using ConnectivityConstants;
using static ConnectivityConstants.DelegateCallbacks;
using FunctionsManager;

class TestDircon
{
    static readonly DateTime start = DateTime.Now;

    [StructLayout(LayoutKind.Sequential)]
    struct MSG { public IntPtr hwnd; public uint message; public IntPtr wParam, lParam; public uint time; public int x, y; }

    [DllImport("user32.dll")] static extern bool PeekMessage(out MSG m, IntPtr hWnd, uint min, uint max, uint remove);
    [DllImport("user32.dll")] static extern bool TranslateMessage(ref MSG m);
    [DllImport("user32.dll")] static extern IntPtr DispatchMessage(ref MSG m);

    // Bonjour's COM objects are apartment-threaded and deliver their callbacks
    // through the thread's message queue: from an MTA thread the browse crashes
    // in the marshaller.  The game is Unreal and pumps its own loop, so the
    // harness has to be an STA that pumps too.
    static void Pump(int ms)
    {
        DateTime until = DateTime.Now.AddMilliseconds(ms);
        MSG msg;
        while (DateTime.Now < until)
        {
            while (PeekMessage(out msg, IntPtr.Zero, 0, 0, 1 /*PM_REMOVE*/))
            {
                TranslateMessage(ref msg);
                DispatchMessage(ref msg);
            }
            Thread.Sleep(20);
        }
    }

    static void Say(string tag, string msg)
    {
        Console.WriteLine("[{0,7:0.000}] {1,-6} {2}", (DateTime.Now - start).TotalSeconds, tag, msg);
        Console.Out.Flush();
    }

    [STAThread]
    static void Main(string[] args)
    {
        int scanSeconds = args.Length > 0 ? int.Parse(args[0]) : 20;

        Say("HARNESS", "MyWhoosh Dircon probe -- scanning for " + scanSeconds + "s");

        // --- 1. Bring the manager up first: every other WD_ call dereferences it --
        Try("WD_InitWahooDirconManager", () => MyWhoosh.WD_InitWahooDirconManager());

        Try("WD_GetDirconServiceAvailability", () =>
            Say("PROBE", "DirconServiceAvailability = " + MyWhoosh.WD_GetDirconServiceAvailability()));

        // --- 2. Register callbacks --------------------------------------------
        LogCallBackDelegate      log        = m  => Say("LOG", m);
        ConnectDelegate          connect    = (t, ok, d, fail)
                                              => Say("CONN", t + " " + ok + " " + Describe(d) + " fail=" + fail);
        DisconnectDelegate       disconnect = (t, ok) => Say("DISC", t + " " + ok);
        SteerModeInput           steer      = s  => Say("STEER", s.SteerDirection + " " + s.SteerMagnitude);
        ConnectivityDataInput    data       = c  => Say("DATA", "power=" + c.power + " cadence=" + c.cadence
                                                     + " speed=" + c.speed + " hr=" + c.heartRate);

        Try("WD_RegisterDelegates", () =>
            MyWhoosh.WD_RegisterDelegates(log, connect, disconnect, steer, data));

        // --- 3. Enable the Dircon feature flag --------------------------------
        // IsWahooDirconAllowed gates the whole WD_ stack.
        Try("WD_UpdateFeatures", () => {
            ConnectivityFeatures f = new ConnectivityFeatures();
            f.IsPowerSourceAllowed   = true;
            f.IsSpeedSensorAllowed   = true;
            f.IsSecondaryPowerAllowed= true;
            f.IsTreadmillAllowed     = true;
            f.IsBluetoothAllowed     = false;   // keep the WinRT stack out of this
            f.IsANTAllowed           = false;
            f.IsWahooDirconAllowed   = true;
            f.IsDevelopment          = true;
            f.scanType               = EScanMechanism.ScanOnly;
            MyWhoosh.WD_UpdateFeatures(f);
        });

        Try("WD_UpdateSlots", () => {
            PairedAvailableSlots s = new PairedAvailableSlots();
            s.PowerSource = s.Controllable = s.Cadence = s.HeartRate = true;
            s.SecondaryPower = s.RunSpeedSensor = s.Treadmill = s.Steering = s.CycleSpeedSensor = true;
            MyWhoosh.WD_UpdateSlots(s);
        });

        Try("WD_UpdateMultiplier", () => {
            WhooshTrainerMultiplierStruct m = new WhooshTrainerMultiplierStruct();
            m.powerMultiplier = m.cadenceMultiplier = m.speedMultiplier = m.gradeMultiplier = 1.0f;
            MyWhoosh.WD_UpdateMultiplier(m);
        });

        // --- 4. State ----------------------------------------------------------
        Try("WD_GetNetworkState", () => Say("PROBE", "NetworkState = " + MyWhoosh.WD_GetNetworkState()));

        // --- 5. Browse for _wahoo-fitness-tnp._tcp ----------------------------
        Try("WD_OnPairWidgetOpen", () => MyWhoosh.WD_OnPairWidgetOpen());
        Try("WD_StartScanningAll", () => MyWhoosh.WD_StartScanningAll());

        for (int i = 1; i <= scanSeconds; i++)
        {
            Pump(1000);
            if (i % 5 != 0) continue;
            Try("WD_GetScannedDevicesList", () => {
                DeviceInformationStruct[] found;
                int n = MyWhoosh.WD_GetScannedDevicesList(out found);
                Say("SCAN", "t+" + i + "s: " + n + " device(s)");
                if (found != null)
                    foreach (var d in found) Say("SCAN", "   " + Describe(d));
            });
        }

        Try("WD_StopScanningAll", () => MyWhoosh.WD_StopScanningAll());
        Try("WD_OnPairWidgetClose", () => MyWhoosh.WD_OnPairWidgetClose());
        Say("HARNESS", "done");
        Environment.Exit(0);   // manager keeps background threads alive
    }

    static string Describe(DeviceInformationStruct d)
    {
        return "\"" + d.deviceName + "\" uuid=" + d.deviceUuid
             + " proto=" + d.hardwareProtocolType + " type=" + d.deviceType
             + " connected=" + d.isConnected
             + " caps=[" + (d.hasPower ? "power " : "") + (d.hasCadence ? "cadence " : "")
             + (d.hasHeart ? "heart " : "") + (d.hasSpeed ? "speed " : "")
             + (d.hasControllable ? "controllable" : "") + "]";
    }

    static void Try(string what, Action a)
    {
        try { a(); Say("CALL", what + " -> ok"); }
        catch (Exception e)
        {
            Say("FAIL", what + " -> " + e.GetType().Name + ": " + e.Message);
            if (Environment.GetEnvironmentVariable("DIRCON_TRACE") != null)
                foreach (var line in e.ToString().Split('\n')) Say("TRACE", line.TrimEnd());
        }
    }
}
