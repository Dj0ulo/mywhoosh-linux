// Drive the shim the way the game does, without the game.
//
// Every step here is one the game takes in BluetoothProgram or BluetoothSensor,
// in the same order and through the same WinRT members, so a failure shows up
// as a printed line rather than as a sensor that quietly never appears.  It
// runs under the host's mono against a running ./blehelper.py, which is a far
// shorter loop than launching MyWhoosh -- and under wine-mono inside the prefix
// too, which is where the answers actually have to be right.
//
//   ./blehelper.py &
//   mcs -out:build/TestBle.exe -r:build/Windows.dll TestBle.cs
//   mono build/TestBle.exe                       # scan only
//   mono build/TestBle.exe FA:55:E5:BE:21:A5     # scan, connect, subscribe
//   mono build/TestBle.exe FA:55:E5:BE:21:A5 --control   # ... and take FTMS control
//
// With a MAC it connects, walks the GATT tree and subscribes to everything that
// notifies, printing values for 30 seconds -- which is exactly what the game
// does once a sensor is paired.

using System;
using System.Collections.Generic;
using System.Threading;
using Windows.Devices.Bluetooth;
using Windows.Devices.Bluetooth.Advertisement;
using Windows.Devices.Bluetooth.GenericAttributeProfile;
using Windows.Devices.Radios;
using Windows.Storage.Streams;

static class TestBle
{
    static readonly Dictionary<ulong, string> Seen = new Dictionary<ulong, string>();

    static int Main(string[] args)
    {
        Console.WriteLine("== radios (BluetoothProgram::CheckRadioState asks this first)");
        var radios = Radio.GetRadiosAsync().Task.Result;
        foreach (var r in radios)
            Console.WriteLine("   {0}  kind={1} state={2}", r.Name, r.Kind, r.State);
        if (radios.Count == 0)
            Console.WriteLine("   none -- the game would report Bluetooth off");

        Console.WriteLine();
        Console.WriteLine("== scanning for 10s");
        var watcher = new BluetoothLEAdvertisementWatcher();
        watcher.put_ScanningMode(BluetoothLEScanningMode.Active);
        watcher.add_Received((w, e) => OnAdvert(e));
        watcher.add_Stopped((w, e) => Console.WriteLine("   watcher stopped"));
        watcher.Start();
        Thread.Sleep(10000);
        watcher.Stop();
        Console.WriteLine("   {0} device(s) advertising", Seen.Count);

        if (args.Length == 0)
        {
            Console.WriteLine();
            Console.WriteLine("pass a MAC to connect to one of them");
            return 0;
        }

        ulong address = ToAddress(args[0]);
        Console.WriteLine();
        Console.WriteLine("== connecting to {0} (0x{1:x12})", args[0], address);
        var device = BluetoothLEDevice.FromBluetoothAddressAsync(address).Task.Result;
        if (device == null)
        {
            Console.WriteLine("   failed -- see the helper's log");
            return 1;
        }
        Console.WriteLine("   connected: name={0} status={1}", device.Name, device.ConnectionStatus);
        device.add_ConnectionStatusChanged(
            (d, o) => Console.WriteLine("   connection status is now {0}", d.ConnectionStatus));

        Console.WriteLine();
        Console.WriteLine("== services");
        var services = device.GetGattServicesAsync(BluetoothCacheMode.Uncached).Task.Result;
        Console.WriteLine("   status={0}, {1} service(s)", services.Status, services.Services.Count);

        int subscribed = 0;
        foreach (var service in services.Services)
        {
            Console.WriteLine("   {0}", service.Uuid);
            var chars = service.GetCharacteristicsAsync(BluetoothCacheMode.Uncached).Task.Result;
            foreach (var c in chars.Characteristics)
            {
                Console.WriteLine("      {0}  {1}", c.Uuid, c.CharacteristicProperties);

                if ((c.CharacteristicProperties & GattCharacteristicProperties.Read) != 0)
                {
                    var read = c.ReadValueAsync(BluetoothCacheMode.Uncached).Task.Result;
                    if (read.Status == GattCommunicationStatus.Success)
                        Console.WriteLine("         read {0}", Hex(read.Value));
                }

                bool notifies = (c.CharacteristicProperties &
                                 (GattCharacteristicProperties.Notify |
                                  GattCharacteristicProperties.Indicate)) != 0;
                if (!notifies) continue;

                var characteristic = c;
                characteristic.add_ValueChanged((sender, e) =>
                    Console.WriteLine("   {0:HH:mm:ss.fff} {1} -> {2}",
                                      DateTime.Now, sender.Uuid, Hex(e.CharacteristicValue)));
                var status = characteristic.WriteClientCharacteristicConfigurationDescriptorAsync(
                    GattClientCharacteristicConfigurationDescriptorValue.Notify).Task.Result;
                Console.WriteLine("         subscribe -> {0}", status);
                if (status == GattCommunicationStatus.Success) subscribed++;
            }
        }

        if (Array.IndexOf(args, "--control") >= 0) RequestControl(services);

        Console.WriteLine();
        Console.WriteLine("== {0} subscription(s); listening for 30s", subscribed);
        Thread.Sleep(30000);

        device.Dispose();
        Console.WriteLine("== done");
        return 0;
    }

    /// The one operation notifications and reads do not cover: a write, to the
    /// characteristic the game writes to for resistance.  FTMS Request Control
    /// (0x00 to 0x2AD9) is the standard opening move and changes nothing about
    /// the ride -- the trainer answers on the same characteristic with 0x80 0x00
    /// 0x01, which is an indication arriving through the same path as every
    /// other notification.
    static void RequestControl(GattDeviceServicesResult services)
    {
        Console.WriteLine();
        Console.WriteLine("== FTMS control point");
        foreach (var service in services.Services)
        {
            if (service.Uuid != new Guid("00001826-0000-1000-8000-00805f9b34fb")) continue;
            foreach (var c in service.GetCharacteristicsAsync(BluetoothCacheMode.Uncached)
                                     .Task.Result.Characteristics)
            {
                if (c.Uuid != new Guid("00002ad9-0000-1000-8000-00805f9b34fb")) continue;

                c.add_ValueChanged((sender, e) =>
                    Console.WriteLine("   control point answered {0}", Hex(e.CharacteristicValue)));
                Console.WriteLine("   subscribe -> {0}",
                    c.WriteClientCharacteristicConfigurationDescriptorAsync(
                        GattClientCharacteristicConfigurationDescriptorValue.Indicate).Task.Result);

                var writer = new DataWriter();
                writer.put_ByteOrder(ByteOrder.LittleEndian);
                writer.WriteByte(0x00);                     // Request Control
                var result = c.WriteValueWithResultAsync(writer.DetachBuffer()).Task.Result;
                Console.WriteLine("   request control -> {0}", result.Status);
                return;
            }
        }
        Console.WriteLine("   no FTMS control point on this device");
    }

    static void OnAdvert(BluetoothLEAdvertisementReceivedEventArgs e)
    {
        // The game keeps one entry per address and ignores an advertisement
        // with no service UUIDs, so print the same way: once per device, with
        // what decided its fate.
        if (Seen.ContainsKey(e.BluetoothAddress)) return;
        var uuids = new List<string>();
        foreach (var u in e.Advertisement.ServiceUuids) uuids.Add(u.ToString().Substring(4, 4));
        string name = string.IsNullOrEmpty(e.Advertisement.LocalName)
                    ? "(no name -- the game invents one)" : e.Advertisement.LocalName;
        Seen[e.BluetoothAddress] = name;
        Console.WriteLine("   {0}  {1,-32} {2}", ToMac(e.BluetoothAddress), name,
                          uuids.Count == 0 ? "no service UUIDs -- IGNORED by the game"
                                           : string.Join(" ", uuids.ToArray()));
    }

    static string Hex(IBuffer buffer)
    {
        var bytes = new byte[buffer.Length];
        DataReader.FromBuffer(buffer).ReadBytes(bytes);
        return BitConverter.ToString(bytes).Replace("-", "").ToLowerInvariant();
    }

    static ulong ToAddress(string mac)
    {
        ulong value = 0;
        foreach (string part in mac.Split(':'))
            value = (value << 8) | Convert.ToByte(part, 16);
        return value;
    }

    static string ToMac(ulong address)
    {
        var parts = new string[6];
        for (int i = 0; i < 6; i++) parts[i] = ((byte)(address >> (40 - i * 8))).ToString("X2");
        return string.Join(":", parts);
    }
}
