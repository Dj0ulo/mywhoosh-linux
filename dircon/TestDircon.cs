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
// Build: ./build.sh      Run: ./run.sh [scan_seconds] [read_seconds]
//
// With ../fakesensor/ installed in the prefix this runs the whole loop:
// discovery, connection, and live power/heart-rate readings.

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

    // The unmanaged export, called exactly as the game calls it -- through the
    // vtable-fixup slot ../exportshim/ rewrites, not through the managed method.
    //
    // Resolved by hand rather than with [DllImport]: mono resolves a DllImport
    // when it compiles the method that calls it, and asking it to dlopen the
    // game's own mixed-mode assembly as a native library takes the runtime
    // down before any of this runs (measured -- a native crash in
    // WD_StartScanning, well before the call).  GetProcAddress asks Wine
    // instead, which is what the game does too.
    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    delegate int GetListFn(out IntPtr devices);

    [DllImport("kernel32", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern IntPtr GetModuleHandleW(string name);

    [DllImport("kernel32", CharSet = CharSet.Ansi, ExactSpelling = true, SetLastError = true)]
    static extern IntPtr GetProcAddress(IntPtr module, string name);

    static GetListFn ScannedDevicesExport()
    {
        IntPtr m = GetModuleHandleW("WindowsConnectivity.dll");
        if (m == IntPtr.Zero) return null;
        IntPtr p = GetProcAddress(m, "WD_GetScannedDevicesList");
        if (p == IntPtr.Zero) return null;
        return (GetListFn)Marshal.GetDelegateForFunctionPointer(p, typeof(GetListFn));
    }

    [STAThread]
    static void Main(string[] args)
    {
        int scanSeconds = args.Length > 0 ? int.Parse(args[0]) : 20;
        int readSeconds = args.Length > 1 ? int.Parse(args[1]) : 10;

        Say("HARNESS", "MyWhoosh Dircon probe -- scanning for " + scanSeconds + "s, "
                     + "then reading for " + readSeconds + "s");

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
        // Scan *as a power source*: GetAllScannedDevices only reports the Dircon
        // scan list when scanDeviceType is E_PowerSource or E_SecondaryPower, and
        // only WD_StartScanning sets it -- WD_StartScanningAll leaves it None and
        // the list comes back empty however many services were resolved.
        Try("WD_OnPairWidgetOpen", () => MyWhoosh.WD_OnPairWidgetOpen());
        Try("WD_StartScanning", () => MyWhoosh.WD_StartScanning(EDeviceTypeEnum.E_PowerSource));

        DeviceInformationStruct? target = null;
        // A second sensor, if one turned up: the game pairs each slot
        // separately, so a heart-rate strap is its own device rather than
        // another claim on the trainer.
        DeviceInformationStruct? strap = null;
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
                if (found != null && found.Length > 0 && target == null)
                {
                    target = found[0];
                    foreach (var d in found)
                        if (d.deviceName != target.Value.deviceName) { strap = d; break; }
                }
            });
            if (target != null) break;
        }

        // --- 5b. The heart-rate slot, the way the game asks for it -------------
        // MyWhoosh polls the *export*, and the export is where
        // ../exportshim/'s ScanRescue lives.  GetAllScannedDevices answers from
        // the scan list for E_PowerSource and E_SecondaryPower only -- ask it
        // for E_HeartRate and it walks paired sensors instead -- so without the
        // rescue this comes back empty however many straps resolved.  Stop
        // scanning first: StartScan clears the list and only re-browses when it
        // is not already scanning, which is also what the game does when it
        // moves from one slot to the next.
        // Both slots, so the other half shows too: the shim hides a sensor whose
        // capabilities cannot fill the slot being searched, which is the only
        // thing standing between a strap and the trainer list.
        int esize = Marshal.SizeOf(typeof(DeviceInformationStruct));
        GetListFn poll = ScannedDevicesExport();
        if (poll == null) Say("SLOT", "WD_GetScannedDevicesList is not exported");
        foreach (EDeviceTypeEnum slot in
                 new[] { EDeviceTypeEnum.E_HeartRate, EDeviceTypeEnum.E_PowerSource })
        {
            if (target == null || poll == null) break;
            EDeviceTypeEnum s = slot;
            // No WD_StopScanning first, on purpose: that is what the game does
            // when it moves from one slot's search to the next, and it is the
            // hard case.  StartScan clears the scan list and re-browses only
            // when it is not already scanning, so the entries the last resolve
            // wrote are gone and no new ones arrive -- which is what the shim's
            // Announce puts back.
            Try("WD_StartScanning(" + s + ")", () => MyWhoosh.WD_StartScanning(s));
            for (int i = 1; i <= 8; i++)
            {
                Pump(1000);
                int n = 0;
                Try("WD_GetScannedDevicesList(export)", () => {
                    IntPtr block;
                    n = poll(out block);
                    Say("SLOT", s + " t+" + i + "s: " + n + " device(s)");
                    for (int k = 0; k < n; k++)
                        Say("SLOT", "   " + Describe((DeviceInformationStruct)
                            Marshal.PtrToStructure(new IntPtr(block.ToInt64() + (long)k * esize),
                                                   typeof(DeviceInformationStruct))));
                });
                if (n > 0) break;
            }
        }

        // --- 6. Connect to whatever turned up, then read it --------------------
        // Stop scanning first, as the game does when you pick a device out of
        // the list: connecting while a browse is still cycling leaves the
        // sensor's own connection racing the resolves and it never comes up.
        Try("WD_StopScanning", () => MyWhoosh.WD_StopScanning());

        if (target == null)
        {
            Say("HARNESS", "nothing discovered -- stopping here");
        }
        else
        {
            DeviceInformationStruct d = target.Value;
            Say("HARNESS", "connecting to " + Describe(d));
            Try("WD_ConnectToDevice(power)", () => MyWhoosh.WD_ConnectToDevice(ref d));

            // Slots are independent: WD_GetHeart reads the sensor paired as
            // E_HeartRate, so a device has to be claimed there too or the heart
            // rate it is already reporting stays invisible.  With two sensors
            // advertised that is the second one -- which is the case that
            // matters, since a strap is never the trainer.
            DeviceInformationStruct hr = strap ?? d;
            if (strap != null) Say("HARNESS", "heart rate from " + Describe(hr));
            hr.deviceType = EDeviceTypeEnum.E_HeartRate;
            Try("WD_ConnectToDevice(heart)", () => MyWhoosh.WD_ConnectToDevice(ref hr));

            for (int i = 1; i <= readSeconds; i++)
            {
                Pump(1000);
                Try("WD_Get*", () => Say("READ", string.Format(
                    "t+{0}s power={1}W cadence={2} speed={3} hr={4} connected={5}",
                    i, MyWhoosh.WD_GetPower(), MyWhoosh.WD_GetCadence(),
                    MyWhoosh.WD_GetSpeed(), MyWhoosh.WD_GetHeart(),
                    MyWhoosh.WD_IsDeviceConnected(EDeviceTypeEnum.E_PowerSource))));
            }

            Try("WD_DisconnectAll", () => MyWhoosh.WD_DisconnectAll());
        }

        Try("WD_StopScanning", () => MyWhoosh.WD_StopScanning());
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
