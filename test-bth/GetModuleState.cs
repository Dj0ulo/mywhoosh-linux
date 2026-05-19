using System;
using System.Threading;
using FunctionsManager;
using ConnectivityConstants;
using static ConnectivityConstants.DelegateCallbacks;

class Program {
    static void Main() {
        // --- Init ---
        try {
            MyWhoosh.BT_RegisterDelegates(
                msg             => Console.WriteLine($"[LOG] {msg}"),
                (dt, r, di, ft) => Console.WriteLine($"[CONNECT] {dt} result={r} name={di.deviceName}"),
                (dt, r)         => Console.WriteLine($"[DISCONNECT] {dt} result={r}"),
                sd              => {},
                antId           => {},
                cd              => {}
            );
            MyWhoosh.BT_InitBluetoothManager();
            Console.WriteLine("[INIT] BluetoothManager initialized");
        } catch (Exception ex) {
            Console.WriteLine($"[INIT] {ex.GetType().Name}: {ex.Message}");
            return;
        }

        // --- Module state ---
        try {
            bool state = MyWhoosh.BT_GetModuleState();
            Console.WriteLine($"[MODULE STATE] => {state}");
            if (!state) { Console.WriteLine("[MODULE STATE] Module not ready, exiting."); return; }
        } catch (Exception ex) {
            Console.WriteLine($"[MODULE STATE] {ex.GetType().Name}: {ex.Message}");
            return;
        }

        // --- Scan ---
        Console.WriteLine("[SCAN] Starting scan for all device types...");
        try {
            MyWhoosh.BT_StartScanningAll();
        } catch (Exception ex) {
            Console.WriteLine($"[SCAN] {ex.GetType().Name}: {ex.Message}");
            return;
        }

        Console.WriteLine("[SCAN] Waiting 5s...");
        Thread.Sleep(5000);

        // --- Results ---
        try {
            DeviceInformationStruct[] devices = null;
            int count = MyWhoosh.BT_GetScannedDevicesList(out devices);
            Console.WriteLine($"\n[SCANNED DEVICES] count={count}");
            if (devices != null)
                foreach (var d in devices)
                    PrintDevice(d);
        } catch (Exception ex) {
            Console.WriteLine($"[SCANNED DEVICES] {ex.GetType().Name}: {ex.Message}");
        }

        try {
            DeviceInformationStruct[] devices = null;
            int count = MyWhoosh.BT_GetConnectedDevicesList(out devices);
            Console.WriteLine($"\n[CONNECTED DEVICES] count={count}");
            if (devices != null)
                foreach (var d in devices)
                    PrintDevice(d);
        } catch (Exception ex) {
            Console.WriteLine($"[CONNECTED DEVICES] {ex.GetType().Name}: {ex.Message}");
        }

        MyWhoosh.BT_StopScanningAll();
    }

    static void PrintDevice(DeviceInformationStruct d) {
        Console.WriteLine($"  Name={d.deviceName} UUID={d.deviceUuid} Type={d.deviceType} Protocol={d.hardwareProtocolType}");
        Console.WriteLine($"    Connected={d.isConnected} Power={d.hasPower} Cadence={d.hasCadence} Heart={d.hasHeart} Speed={d.hasSpeed} Controllable={d.hasControllable}");
        Console.WriteLine($"    Model={d.modelNumber} Serial={d.serialNumber} Manufacturer={d.manufactureName} Display={d.displayValue}");
    }
}
