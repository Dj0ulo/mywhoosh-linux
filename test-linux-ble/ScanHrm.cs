// Wine BLE Heart Rate Monitor Scanner
// Compile: mcs ScanHrm.cs -out:ScanHrm.exe
// Run:     wine ScanHrm.exe
//
// Tests whether Wine's WinRT BLE stack can discover nearby Heart Rate Monitors.
// Uses reflection to activate WinRT types — no Windows SDK reference needed at compile time.

using System;
using System.Collections;
using System.Reflection;
using System.Reflection.Emit;
using System.Runtime.InteropServices;
using System.Threading;

class ScanHrm
{
    const string HRM_UUID    = "0000180d-0000-1000-8000-00805f9b34fb";
    const int    SCAN_SECONDS = 15;

    static int foundCount = 0;

    // ── Win32 Bluetooth radio check ──────────────────────────────────────────

    [StructLayout(LayoutKind.Sequential)]
    struct BLUETOOTH_FIND_RADIO_PARAMS { public uint dwSize; }

    [DllImport("BluetoothAPIs.dll", SetLastError = true)]
    static extern IntPtr BluetoothFindFirstRadio(ref BLUETOOTH_FIND_RADIO_PARAMS p, out IntPtr phRadio);

    [DllImport("BluetoothAPIs.dll", SetLastError = true)]
    static extern bool BluetoothFindRadioClose(IntPtr hFind);

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool CloseHandle(IntPtr h);

    // ────────────────────────────────────────────────────────────────────────

    static void Main()
    {
        Console.Title = "Wine BLE HRM Scanner";
        Console.WriteLine("=== Wine BLE Heart Rate Monitor Scanner ===");
        Console.WriteLine("Heart Rate Service UUID: " + HRM_UUID);
        Console.WriteLine(new string('-', 55));

        CheckBluetoothRadio();
        Console.WriteLine();
        ScanBleAdvertisements();

        Console.WriteLine("\nPress Enter to exit.");
        Console.ReadLine();
    }

    // ── Step 1: verify adapter is visible via classic Win32 API ─────────────

    static void CheckBluetoothRadio()
    {
        Console.WriteLine("[1/2] Checking Bluetooth radio (Win32 API)...");
        try
        {
            var p = new BLUETOOTH_FIND_RADIO_PARAMS
            {
                dwSize = (uint)Marshal.SizeOf(typeof(BLUETOOTH_FIND_RADIO_PARAMS))
            };
            IntPtr hRadio;
            IntPtr hFind = BluetoothFindFirstRadio(ref p, out hRadio);
            if (hFind == IntPtr.Zero)
            {
                int err = Marshal.GetLastWin32Error();
                Console.WriteLine("  FAIL: No Bluetooth radio detected (Win32 error " + err + ").");
            }
            else
            {
                Console.WriteLine("  OK: Bluetooth radio detected.");
                CloseHandle(hRadio);
                BluetoothFindRadioClose(hFind);
            }
        }
        catch (Exception ex)
        {
            Console.WriteLine("  ERROR: " + ex.Message);
        }
    }

    // ── Step 2: WinRT BLE advertisement scan ────────────────────────────────

    static void ScanBleAdvertisements()
    {
        Console.WriteLine("[2/2] Starting WinRT BLE advertisement scan...");

        // Activate WinRT type via reflection — ContentType=WindowsRuntime tells
        // the CLR to use RoActivateInstance instead of a normal constructor.
        Type watcherType = Type.GetType(
            "Windows.Devices.Bluetooth.Advertisement.BluetoothLEAdvertisementWatcher, " +
            "Windows, Version=255.255.255.255, Culture=neutral, " +
            "PublicKeyToken=null, ContentType=WindowsRuntime");

        if (watcherType == null)
        {
            Console.WriteLine("  FAIL: WinRT BLE type unavailable.");
            Console.WriteLine("  Wine may not support BLE WinRT APIs in this configuration.");
            return;
        }
        Console.WriteLine("  OK: WinRT type loaded.");

        object watcher = Activator.CreateInstance(watcherType);
        Console.WriteLine("  OK: BluetoothLEAdvertisementWatcher created.");

        // ScanningMode = Active (1) — request scan-response packets
        try
        {
            PropertyInfo scanModeProp = watcherType.GetProperty("ScanningMode");
            if (scanModeProp != null)
                scanModeProp.SetValue(watcher, Enum.ToObject(scanModeProp.PropertyType, 1), null);
        }
        catch { /* non-critical */ }

        // Hook the Received event using a DynamicMethod trampoline so we can
        // bind our static callback to the generic TypedEventHandler<,> delegate
        // without knowing its concrete type at compile time.
        EventInfo receivedEvent = watcherType.GetEvent("Received");
        if (receivedEvent != null)
        {
            try
            {
                Type handlerType = receivedEvent.EventHandlerType;
                ParameterInfo[] invokeParams = handlerType.GetMethod("Invoke").GetParameters();
                Type senderParamType = invokeParams[0].ParameterType;
                Type argsParamType   = invokeParams[1].ParameterType;

                MethodInfo callback = typeof(ScanHrm).GetMethod(
                    "OnAdvertisementReceived",
                    BindingFlags.Static | BindingFlags.NonPublic);

                var dm = new DynamicMethod(
                    "_BleReceivedTrampoline",
                    typeof(void),
                    new[] { senderParamType, argsParamType },
                    typeof(ScanHrm),
                    skipVisibility: true);

                ILGenerator il = dm.GetILGenerator();
                il.Emit(OpCodes.Ldarg_0);
                il.Emit(OpCodes.Ldarg_1);
                il.EmitCall(OpCodes.Call, callback, null);
                il.Emit(OpCodes.Ret);

                Delegate del = dm.CreateDelegate(handlerType);
                receivedEvent.AddEventHandler(watcher, del);
                Console.WriteLine("  OK: Advertisement event handler attached.");
            }
            catch (Exception ex)
            {
                Console.WriteLine("  WARNING: Could not attach event handler: " + ex.Message);
            }
        }

        // Start scan
        try
        {
            watcherType.GetMethod("Start").Invoke(watcher, null);
            Console.WriteLine("  Scanning for " + SCAN_SECONDS + "s — bring your HRM sensor close...\n");
        }
        catch (Exception ex)
        {
            Console.WriteLine("  FAIL: Could not start BLE scan: " + ex.GetBaseException().Message);
            return;
        }

        Thread.Sleep(SCAN_SECONDS * 1000);

        try { watcherType.GetMethod("Stop").Invoke(watcher, null); } catch { }

        Console.WriteLine("\n  Scan complete. HRM devices found: " + foundCount);
        if (foundCount == 0)
            Console.WriteLine("  (None found — make sure your HRM is powered on and nearby.)");
    }

    // Called for every BLE advertisement received during the scan.
    static void OnAdvertisementReceived(object sender, object args)
    {
        try
        {
            Type argsType = args.GetType();

            // Check ServiceUuids in the advertisement
            object adv = argsType.GetProperty("Advertisement").GetValue(args, null);
            var uuids  = adv.GetType().GetProperty("ServiceUuids").GetValue(adv, null) as IEnumerable;

            bool isHrm = false;
            if (uuids != null)
                foreach (var uuid in uuids)
                    if (string.Equals(uuid.ToString(), HRM_UUID, StringComparison.OrdinalIgnoreCase))
                    { isHrm = true; break; }

            if (!isHrm) return;

            // MAC address
            ulong address = (ulong)argsType.GetProperty("BluetoothAddress").GetValue(args, null);
            string mac = string.Format("{0:X2}:{1:X2}:{2:X2}:{3:X2}:{4:X2}:{5:X2}",
                (address >> 40) & 0xFF, (address >> 32) & 0xFF, (address >> 24) & 0xFF,
                (address >> 16) & 0xFF, (address >> 8)  & 0xFF, address & 0xFF);

            // RSSI
            short rssi = 0;
            try { rssi = (short)argsType.GetProperty("RawSignalStrengthInDBm").GetValue(args, null); } catch { }

            // Local name (may be empty in advertisement packets)
            string name = "<unknown>";
            try
            {
                string n = (string)adv.GetType().GetProperty("LocalName").GetValue(adv, null);
                if (!string.IsNullOrEmpty(n)) name = n;
            }
            catch { }

            Interlocked.Increment(ref foundCount);
            Console.WriteLine("  [HRM FOUND] " + name + " | " + mac + " | RSSI: " + rssi + " dBm");
        }
        catch (Exception ex)
        {
            Console.WriteLine("  [!] Parse error in event: " + ex.Message);
        }
    }
}
