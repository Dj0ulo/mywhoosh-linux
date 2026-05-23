using System;
using System.Threading;
using FunctionsManager;
using ConnectivityConstants;
using static ConnectivityConstants.DelegateCallbacks;

class Program {
    static void Main() {
        try {
            MyWhoosh.BT_RegisterDelegates(
                msg             => Console.WriteLine($"[LOG] {msg}"),
                (dt, r, di, ft) => Console.WriteLine($"[CONNECT] {dt} result={r} name={di.deviceName}"),
                (dt, r)         => Console.WriteLine($"[DISCONNECT] {dt} result={r}"),
                sd              => {},
                antId           => {},
                cd              => Console.WriteLine($"[DATA] HR={cd.heartRate} Power={cd.power} Cadence={cd.cadence}")
            );
            MyWhoosh.BT_InitBluetoothManager();
            Console.WriteLine("[INIT] BluetoothManager initialized");
        } catch (Exception ex) {
            Console.WriteLine($"[INIT] {ex.GetType().Name}: {ex.Message}");
            return;
        }

        try {
            bool state = MyWhoosh.BT_GetModuleState();
            Console.WriteLine($"[MODULE STATE] => {state}");
            if (!state) { Console.WriteLine("[MODULE STATE] Module not ready, exiting."); return; }
        } catch (Exception ex) {
            Console.WriteLine($"[MODULE STATE] {ex.GetType().Name}: {ex.Message}");
            return;
        }

        Console.WriteLine("[SCAN] Scanning for heart rate sensors...");
        try {
            MyWhoosh.BT_StartScanning(EDeviceTypeEnum.E_HeartRate);
        } catch (Exception ex) {
            Console.WriteLine($"[SCAN] {ex.GetType().Name}: {ex.Message}");
            return;
        }

        Console.WriteLine("[SCAN] Waiting 15s...");
        Thread.Sleep(15000);

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

        MyWhoosh.BT_StopScanning();
    }

    static void PrintDevice(DeviceInformationStruct d) {
        Console.WriteLine($"  Name={d.deviceName} UUID={d.deviceUuid} Type={d.deviceType} Protocol={d.hardwareProtocolType}");
        Console.WriteLine($"    Connected={d.isConnected} Heart={d.hasHeart} Power={d.hasPower} Cadence={d.hasCadence}");
        Console.WriteLine($"    Manufacturer={d.manufactureName} Model={d.modelNumber} Display={d.displayValue}");
    }
}
