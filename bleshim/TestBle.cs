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
//   mono build/TestBle.exe                          # scan only
//   mono build/TestBle.exe AA:BB:CC:DD:EE:FF        # scan, connect, subscribe
//   mono build/TestBle.exe AA:BB:CC:DD:EE:FF --control   # ... and take FTMS control
//
// With a MAC it connects, walks the GATT tree and subscribes to everything that
// notifies, printing values for 30 seconds -- which is exactly what the game
// does once a sensor is paired.
//
// Three automated scenarios run without any hardware or a real helper, added
// when the connect path was made asynchronous and radio polls were served from
// a cache, to keep the failure they fixed from coming back:
//
//   mono build/TestBle.exe --stub             # full walk against fakehelper.py,
//                                             # with a 6s connect and a slow read;
//                                             # asserts the calling thread is
//                                             # never blocked by either
//   mono build/TestBle.exe --stub --slow-radios
//                                             # .. and that a radio poll through
//                                             # an unresponsive helper is cut off
//                                             # at Backend's 2.5s bound instead of
//                                             # hanging with it
//   mono build/TestBle.exe --quiescence       # against the real blehelper.py on a
//                                             # private port: a device that is
//                                             # nowhere must fail slowly the first
//                                             # time and fast the second -- the
//                                             # ladder that stops a phone being
//                                             # hammered with pairing prompts
//   mono build/TestBle.exe --thin             # the radios/scan/async-connect walk
//                                             # against a helper started by the
//                                             # caller (env MYWHOOSH_BLE_PORT);
//                                             # never spawns anything, which is
//                                             # what makes it the mode to run
//                                             # under wine-mono inside the prefix,
//                                             # where the BCL answers for
//                                             # Task.Factory.StartNew where it
//                                             # actually has to
//
// Exit code 0 only if every assertion held.

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
        var names = new List<string>(args);
        bool stub = names.Remove("--stub");
        bool slowRadios = names.Remove("--slow-radios");
        bool quiescence = names.Remove("--quiescence");
        bool thin = names.Remove("--thin");

        if (thin) return ThinRun();
        if (stub) return StubRun(slowRadios);
        if (quiescence) return QuiescenceRun();

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

    // ================================================== automated scenarios
    //
    // Everything below is the regression ladder for the fixes behind it: a
    // connect that takes seconds must never slow a radio poll or a scan
    // request; a helper that will not answer must cost the bound, not the
    // silence; and the real helper's quiescence ladder must engage so
    // unreachable devices are not re-asked at five-second intervals.

    static int stubFails;

    sealed class Counter
    {
        int n;
        public void Hit<TSender, TArgs>(TSender a, TArgs b) { lock (this) n++; }
        public int Value { get { lock (this) return n; } }
    }

    static void Check(bool held, string what)
    {
        Console.WriteLine("   {0} {1}", held ? "ok  " : "FAIL", what);
        if (!held) stubFails++;
    }

    static bool WaitUntil(Func<bool> condition, int timeoutMs)
    {
        int waited = 0;
        while (waited < timeoutMs)
        {
            if (condition()) return true;
            Thread.Sleep(50);
            waited += 50;
        }
        return condition();
    }

    static string FindScript(string name)
    {
        string baseDir = null;
        try { baseDir = System.IO.Path.GetDirectoryName(typeof(TestBle).Assembly.Location); }
        catch { }
        foreach (string root in new[] { baseDir, baseDir != null ? System.IO.Path.Combine(baseDir, "..") : null, "." })
        {
            if (root == null) continue;
            string candidate = System.IO.Path.Combine(root, name);
            if (System.IO.File.Exists(candidate)) return System.IO.Path.GetFullPath(candidate);
        }
        return name;                      // let python's own path resolution try
    }

    static System.Diagnostics.Process StartHelper(string scriptArgs)
    {
        var psi = new System.Diagnostics.ProcessStartInfo("python3", scriptArgs)
        { UseShellExecute = false };
        return System.Diagnostics.Process.Start(psi);
    }

    static void WaitForPort(int port, int timeoutMs)
    {
        int waited = 0;
        while (waited < timeoutMs)
        {
            try
            {
                var probe = new System.Net.Sockets.TcpClient();
                probe.Connect("127.0.0.1", port);
                probe.Close();
                return;
            }
            catch (System.Net.Sockets.SocketException) { }
            Thread.Sleep(100);
            waited += 100;
        }
        throw new Exception("nothing listening on 127.0.0.1:" + port + " after " + timeoutMs + "ms");
    }

    static int StubRun(bool slowRadios)
    {
        int port = 27031;
        string env = Environment.GetEnvironmentVariable("MYWHOOSH_TEST_PORT");
        if (!string.IsNullOrEmpty(env)) int.TryParse(env, out port);
        // The shim finds its helper through this variable, and it must be set
        // before the first member of the shim is constructed.
        Environment.SetEnvironmentVariable("MYWHOOSH_BLE_PORT", port.ToString());

        string script = FindScript("fakehelper.py");
        string args = "--port " + port
            + (slowRadios ? " --slow radios:9000"
                          : " --slow connect:6000 --slow read:350");
        Console.WriteLine("== scenario against {0} {1}", script, args);
        stubFails = 0;
        var helper = StartHelper(script + " " + args);
        try
        {
            WaitForPort(port, 8000);
            return slowRadios ? StubSlowRadios() : StubScenario();
        }
        catch (Exception e)
        {
            FailNow("harness itself failed: " + e);
            return 1;
        }
        finally
        {
            try { helper.Kill(); } catch { }
        }
    }

    static void FailNow(string what)
    {
        Console.WriteLine("   FAIL {0}", what);
        stubFails++;
    }

    /// The walk the harness can do against a helper the caller started --
    /// nothing spawned, nothing killed, which is what makes this the mode for
    /// wine-mono: the assertions there say Task.Factory.StartNew, Task.Wait
    /// and the rest of the BCL the shim leans on exist where the game runs.
    /// Point MYWHOOSH_BLE_PORT at a running fakehelper before launching.
    static int ThinRun()
    {
        stubFails = 0;
        Console.WriteLine("== thin scenario against a caller-started helper (port " +
                          Environment.GetEnvironmentVariable("MYWHOOSH_BLE_PORT") + ")");

        var quiet = System.Diagnostics.Stopwatch.StartNew();
        var radios = Radio.GetRadiosAsync().Task.Result;
        quiet.Stop();
        Check(radios.Count >= 1, "radios answered (" + (int)quiet.ElapsedMilliseconds + "ms)");

        var seen = new Counter();
        var watcher = new BluetoothLEAdvertisementWatcher();
        watcher.add_Received((w, e) => seen.Hit(w, e));
        watcher.Start();
        Check(WaitUntil(() => seen.Value >= 1, 6000), "adverts on the wire (" + seen.Value + ")");
        watcher.Stop();

        const string addr = "AA:BB:CC:DD:EE:01";
        ulong trainer = 0;
        foreach (string part in addr.Split(':'))
            trainer = (trainer << 8) | Convert.ToByte(part, 16);

        var connectWatch = System.Diagnostics.Stopwatch.StartNew();
        var op = BluetoothLEDevice.FromBluetoothAddressAsync(trainer);
        int declared = (int)connectWatch.ElapsedMilliseconds;
        Check(declared < 1000, "connect handed back instantly while its work runs (" + declared + "ms)");
        var dev = op.Task.Result;
        Check(dev != null, "device object produced (" + (int)connectWatch.ElapsedMilliseconds + "ms total)");

        // Existence proof that the async GATT machinery survives this BCL too.
        if (dev != null)
        {
            var services = dev.GetGattServicesAsync(BluetoothCacheMode.Uncached).Task.Result;
            Check(services.Status == GattCommunicationStatus.Success && services.Services.Count >= 1,
                  "GATT walk works (" + services.Services.Count + " service(s))");
            dev.Dispose();
        }

        Check(stubFails == 0, stubFails == 0 ? "all assertions held" : stubFails + " assertion(s) failed");
        return stubFails == 0 ? 0 : 1;
    }

    /// A helper whose radios op takes nine seconds: the shim is expected to
    /// cut the poll off at its own 2.5s bound and answer "no radios" rather
    /// than hang with the helper.  First-ever poll has no snapshot, so the
    /// result is an empty list -- the bound is the assertion.
    static int StubSlowRadios()
    {
        Console.WriteLine("== radios through a helper that takes 9s");
        var slowWatch = System.Diagnostics.Stopwatch.StartNew();
        var radios = Radio.GetRadiosAsync().Task.Result;
        slowWatch.Stop();
        int ms = (int)slowWatch.ElapsedMilliseconds;
        Check(ms < 7000, "radio poll cut off at the shim's bound, not the helper's 9s (was " + ms + "ms)");
        Check(radios.Count == 0, "no snapshot yet, so the answer is empty (Bluetooth reported off)");
        return stubFails == 0 ? 0 : 1;
    }

    static int StubScenario()
    {
        byte[] trainer = { 0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0x01 };
        ulong trainerUlong = 0;
        for (int i = 0; i < 6; i++) trainerUlong = (trainerUlong << 8) | trainer[i];
        byte[] hrm = { 0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0x02 };
        ulong hrmUlong = 0;
        for (int i = 0; i < 6; i++) hrmUlong = (hrmUlong << 8) | hrm[i];

        // ---- radio poll: instant (helper cache)
        Console.WriteLine("== radios (game's per-second poll)");
        var quiet = System.Diagnostics.Stopwatch.StartNew();
        var radios = Radio.GetRadiosAsync().Task.Result;
        quiet.Stop();
        int ms = (int)quiet.ElapsedMilliseconds;
        Check(radios.Count == 1 && radios[0].State == RadioState.On, "one Bluetooth radio reported on");
        Check(ms < 1000, "radio poll instant through a quiet helper (" + ms + "ms)");

        // ---- scan: adverts for both fake devices
        Console.WriteLine("== scanning");
        var seen = new Counter();
        var watcher = new BluetoothLEAdvertisementWatcher();
        watcher.add_Received((w, e) => { if (e.BluetoothAddress == trainerUlong || e.BluetoothAddress == hrmUlong) seen.Hit(w, e); });
        watcher.Start();
        var scanWatch = System.Diagnostics.Stopwatch.StartNew();
        bool ready = WaitUntil(() => seen.Value >= 2, 5000);
        scanWatch.Stop();
        ms = (int)scanWatch.ElapsedMilliseconds;
        Check(ready, "scan answered instantly and both devices advertised within " + ms + "ms");
        Check(seen.Value >= 2, "adverts reached the game's handler (" + seen.Value + ")");

        // ---- connect: declared instantly, done later.  The whole point of
        // the fix: six seconds of helper-side connect work, and the thread
        // that asked for the connect was free the entire time.
        Console.WriteLine("== connect (helper holds it 6s)");
        // One stopwatch for the whole connect: the first reading is how fast
        // the WinRT method handed back, the last is when the work actually
        // finished, and the distance between them is the fix in numbers.
        var connectWatch = System.Diagnostics.Stopwatch.StartNew();
        var deviceOp = BluetoothLEDevice.FromBluetoothAddressAsync(trainerUlong);
        ms = (int)connectWatch.ElapsedMilliseconds;
        Check(ms < 1500, "FromBluetoothAddressAsync returned while the connect was still running (" + ms + "ms)");

        int polls = 0, worstPoll = 0;
        while (!deviceOp.Task.IsCompleted && connectWatch.ElapsedMilliseconds < 12000)
        {
            var pollClock = System.Diagnostics.Stopwatch.StartNew();
            var rs = Radio.GetRadiosAsync().Task;
            rs.Wait();
            pollClock.Stop();
            polls++;
            worstPoll = Math.Max(worstPoll, (int)pollClock.ElapsedMilliseconds);
            Check(radiosResult(rs), "radio poll stayed instant mid-connect (" + (int)pollClock.ElapsedMilliseconds + "ms)");
            Thread.Sleep(200);
        }
        if (polls >= 3) Check(worstPoll < 700, "worst radio poll mid-connect " + worstPoll + "ms (bound is the shim's 2.5s, instant expected)");

        ms = (int)connectWatch.ElapsedMilliseconds;
        var dev = deviceOp.Task.Result;
        Check(dev != null, "device object produced");
        if (dev == null) return stubFails == 0 ? 0 : 1;
        Check((dev.Name ?? "") == "Fake Flux 2", "name came from the helper's connect reply");
        Check(ms >= 4500, "connect completed after the helper's 6s work (" + ms + "ms)");

        // ---- services and characteristics: async now, and quick here
        Console.WriteLine("== GATT walk");
        var servicesWatch = System.Diagnostics.Stopwatch.StartNew();
        var servicesOp = dev.GetGattServicesAsync(BluetoothCacheMode.Uncached);
        servicesWatch.Stop();
        ms = (int)servicesWatch.ElapsedMilliseconds;
        Check(ms < 1500, "GetGattServicesAsync returned its operation instead of doing the work inline (" + ms + "ms)");
        var services = servicesOp.Task.Result;
        Check(services.Status == GattCommunicationStatus.Success, "services answered Success");
        Check(services.Services.Count == 2, "two services on the trainer (got " + services.Services.Count + ")");

        GattCharacteristic powerChar = null, controlChar = null, featureChar = null;
        foreach (var s in services.Services)
        {
            var charsOp = s.GetCharacteristicsAsync(BluetoothCacheMode.Uncached);
            var chars = charsOp.Task.Result;
            Check(chars.Characteristics.Count == 2, "two characteristics on service " + Short(s.Uuid));
            foreach (var c in chars.Characteristics)
            {
                if (c.Uuid == new Guid("00002a63-0000-1000-8000-00805f9b34fb")) powerChar = c;
                if (c.Uuid == new Guid("00002a5e-0000-1000-8000-00805f9b34fb")) featureChar = c;
                if (c.Uuid == new Guid("00002ad9-0000-1000-8000-00805f9b34fb")) controlChar = c;
            }
        }
        Check(powerChar != null && controlChar != null && featureChar != null, "power, feature and control characteristics found");

        // ---- subscribe: notifications flow
        Console.WriteLine("== subscriptions");
        var values = new Counter();
        powerChar.add_ValueChanged((s, e) => values.Hit(s, e));
        var sub = powerChar.WriteClientCharacteristicConfigurationDescriptorAsync(
            GattClientCharacteristicConfigurationDescriptorValue.Notify);
        Check(sub.Task.Result == GattCommunicationStatus.Success, "subscribed to the power characteristic");
        Check(WaitUntil(() => values.Value >= 3, 5000), "power notifications flowing (" + values.Value + " arrived)");

        // ---- read: the helper holds it 350ms; the awaiter must not run early
        // and the calling thread must not wait for it.
        Console.WriteLine("== slow read (350ms)");
        var readWatch = System.Diagnostics.Stopwatch.StartNew();
        var readOp = featureChar.ReadValueAsync(BluetoothCacheMode.Uncached);
        readWatch.Stop();
        ms = (int)readWatch.ElapsedMilliseconds;
        Check(ms < 250, "ReadValueAsync declared, not awaited inline (" + ms + "ms)");
        var poll2 = System.Diagnostics.Stopwatch.StartNew();
        var r2 = Radio.GetRadiosAsync().Task;
        r2.Wait();
        poll2.Stop();
        Check((int)poll2.ElapsedMilliseconds < 700 && r2.Result.Count == 1, "radio poll instant during the slow read (" + (int)poll2.ElapsedMilliseconds + "ms)");
        var read = readOp.Task.Result;
        Check(read.Status == GattCommunicationStatus.Success, "read returned Success");
        Check(HexOf(read.Value) == "1b0200", "read payload matches (got " + HexOf(read.Value) + ")");

        // ---- FTMS control write, with its response indication
        Console.WriteLine("== FTMS control point");
        var indicate = controlChar.WriteClientCharacteristicConfigurationDescriptorAsync(
            GattClientCharacteristicConfigurationDescriptorValue.Indicate);
        Check(indicate.Task.Result == GattCommunicationStatus.Success, "subscribed to the control point");
        var answers = new Counter();
        controlChar.add_ValueChanged((s, e) => answers.Hit(s, e));
        var writer = new DataWriter();
        writer.WriteByte(0x00);                         // Request Control
        var writeWatch = System.Diagnostics.Stopwatch.StartNew();
        var writeOp = controlChar.WriteValueWithResultAsync(writer.DetachBuffer());
        writeWatch.Stop();
        ms = (int)writeWatch.ElapsedMilliseconds;
        Check(ms < 500, "the write returned its operation instead of waiting inline (" + ms + "ms)");
        var wr = writeOp.Task.Result;
        Check(wr.Status == GattCommunicationStatus.Success, "write with response returned Success");
        Check(WaitUntil(() => answers.Value >= 1, 3000), "control point answered with its indication");

        // ---- a second device: the helper delays every connect, so this one
        // also costs 6s -- and the same non-blocking contract must hold.
        Console.WriteLine("== second device (helper delays every connect 6s)");
        var hrmWatch = System.Diagnostics.Stopwatch.StartNew();
        var hrmOp = BluetoothLEDevice.FromBluetoothAddressAsync(hrmUlong);
        hrmWatch.Stop();
        Check((int)hrmWatch.ElapsedMilliseconds < 1500,
              "second connect also handed back instantly (" + (int)hrmWatch.ElapsedMilliseconds + "ms)");
        var hrmDev = hrmOp.Task.Result;
        Check(hrmDev != null, "HRM connected after the helper's work");

        // ---- disconnect reported through the event the game hooks
        Console.WriteLine("== dispose (the game's disconnect)");
        var gone = new Counter();
        dev.add_ConnectionStatusChanged((d, o) => { if (d.ConnectionStatus == BluetoothConnectionStatus.Disconnected) gone.Hit(d, o); });
        dev.Dispose();
        Check(WaitUntil(() => gone.Value >= 1, 5000), "disconnection event reached the game's handler");

        Check(stubFails == 0, stubFails == 0 ? "all assertions held" : stubFails + " assertion(s) failed");
        return stubFails == 0 ? 0 : 1;
    }

    static bool radiosResult(System.Threading.Tasks.Task<System.Collections.Generic.IReadOnlyList<Radio>> t)
    {
        var list = t.Result;
        return list.Count == 1 && list[0].State == RadioState.On;
    }

    static string Short(Guid g) { return g.ToString().Substring(4, 4); }

    static string HexOf(IBuffer buffer)
    {
        var bytes = new byte[buffer.Length];
        DataReader.FromBuffer(buffer).ReadBytes(bytes);
        return BitConverter.ToString(bytes).Replace("-", "").ToLowerInvariant();
    }

    /// Against the real helper on a private port: the fake one cannot model
    /// BlueZ, and the quiescence ladder lives in the real helper.  A device
    /// address that will never appear must cost the full look-for-it wait the
    /// first time and be refused almost immediately the second -- the
    /// difference between a phone asked to pair once and a phone asked every
    /// five seconds.
    static int QuiescenceRun()
    {
        int port = 27033;
        string env = Environment.GetEnvironmentVariable("MYWHOOSH_QU_PORT");
        if (!string.IsNullOrEmpty(env)) int.TryParse(env, out port);
        Environment.SetEnvironmentVariable("MYWHOOSH_BLE_PORT", port.ToString());
        string script = FindScript("blehelper.py");
        Console.WriteLine("== quiescence, real helper on port {0}", port);
        stubFails = 0;
        var helper = StartHelper(script + " --port " + port);
        try
        {
            WaitForPort(port, 8000);
            const string dead = "02:00:00:00:00:AB";
            ulong deadUlong = 0;
            foreach (string part in dead.Split(':'))
                deadUlong = (deadUlong << 8) | Convert.ToByte(part, 16);

            Console.WriteLine("   first attempt -- the helper looks for 20s before giving up");
            var firstWatch = System.Diagnostics.Stopwatch.StartNew();
            var dev1 = BluetoothLEDevice.FromBluetoothAddressAsync(deadUlong).Task.Result;
            firstWatch.Stop();
            int firstMs = (int)firstWatch.ElapsedMilliseconds;
            Check(dev1 == null, "no device object from a device that is not there");
            Check(firstMs >= 15000 && firstMs < 70000, "real first attempt spent the advert wait (" + firstMs + "ms)");

            Console.WriteLine("   second attempt, immediately -- quiescence should refuse it on sight");
            var secondWatch = System.Diagnostics.Stopwatch.StartNew();
            var dev2 = BluetoothLEDevice.FromBluetoothAddressAsync(deadUlong).Task.Result;
            secondWatch.Stop();
            int secondMs = (int)secondWatch.ElapsedMilliseconds;
            Check(dev2 == null, "still no device object");
            Check(secondMs < 5000, "second attempt refused fast instead of searching again (" + secondMs + "ms)");
        }
        catch (Exception e)
        {
            FailNow("harness itself failed: " + e);
            return 1;
        }
        finally
        {
            try { helper.Kill(); } catch { }
        }
        Check(stubFails == 0, stubFails == 0 ? "all assertions held" : stubFails + " assertion(s) failed");
        return stubFails == 0 ? 0 : 1;
    }
}
