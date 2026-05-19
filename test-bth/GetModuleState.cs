using System;
using FunctionsManager;
using ConnectivityConstants;
using static ConnectivityConstants.DelegateCallbacks;

class Program {
    static void Main() {
        try {
            MyWhoosh.BT_RegisterDelegates(
                msg             => Console.WriteLine($"[LOG] {msg}"),
                (dt, r, di, ft) => Console.WriteLine($"[CONNECT] {dt} {r}"),
                (dt, r)         => Console.WriteLine($"[DISCONNECT] {dt} {r}"),
                sd              => {},
                antId           => {},
                cd              => {}
            );
            MyWhoosh.BT_InitBluetoothManager();
        } catch (Exception ex) {
            Console.WriteLine($"[INIT] {ex.GetType().Name}: {ex.Message}");
        }

        bool state = MyWhoosh.BT_GetModuleState();
        Console.WriteLine($"[BT_GetModuleState] => {state}");
    }
}
