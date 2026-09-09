// Serve the four byref-array exports of WindowsConnectivity.dll ourselves,
// because wine-mono's native marshaller will not.
//
// The problem.  Four of the DLL's 98 unmanaged exports take the device list by
// reference:
//
//     int WD_GetScannedDevicesList  (out DeviceInformationStruct[] devices)
//     int WD_GetConnectedDevicesList(out DeviceInformationStruct[] devices)
//     int BT_GetScannedDevicesList  (out DeviceInformationStruct[] devices)
//     int BT_GetConnectedDevicesList(out DeviceInformationStruct[] devices)
//
// and they are exactly the pollers the game's UI calls to fill its device list.
// mono's array marshaller refuses that shape from native code -- see
// mono/metadata/marshal-ilgen.c, emit_marshal_array_ilgen,
// MARSHAL_ACTION_MANAGED_CONV_IN:
//
//     if (t->byref) { ... "Byref array marshalling to managed code is not
//                          implemented." }
//
// The refusal is compiled *into* the wrapper as a throw, so the wrapper builds
// and the first call raises MarshalDirectiveException from a native-to-managed
// frame, which is fatal.  It is native runtime code, so neither the Cecil pass
// in ../winemono nor a newer wine-mono reaches it (10.0.0 and 11.1.0 both carry
// the string in libmono-2.0-x86_64.dll, and it is in no managed assembly).
//
// Two further walls stand behind that one, which is why "delete the byref
// check" is not the fix either: the same function needs a [MarshalAs] on the
// parameter to pick a native array shape, and then a SizeConst or
// SizeParamIndex to know how many elements to read -- and the game's metadata
// has none of them (measured: the parameter carries only [Out]).  Nothing short
// of implementing the out-direction from scratch inside libmono works, and that
// means shipping a self-built runtime.
//
// What this does instead.  The exports are not ordinary code.  Each is a
// 12-byte stub
//
//     48 A1 <abs64>   mov rax, [slot]
//     FF E0           jmp rax
//
// reading a slot in `.sdata` that belongs to the CLI header's VTableFixups
// array (98 slots at RVA 0x36000, type COR_VTABLE_64BIT|FROM_UNMANAGED).  The
// slots hold MethodDef tokens on disk, and mscoree overwrites each with mono's
// native-to-managed thunk at load.  So:
//
//   * the game reads the slot on *every* call, even through a function pointer
//     it cached from GetProcAddress long before -- there is no window to miss;
//   * writing a slot needs nothing but a store to already-writable memory in
//     our own process, and touches no file.
//
// That last point is the constraint everything here lives under: MyWhoosh
// hashes WindowsConnectivity.dll and silently declines to load it if a single
// byte differs (see ../dircon/README.md), so the file must stay pristine.  It
// does -- this rewrites four pointers in a loaded image, after the hash check
// has already passed.
//
// The replacement is managed, which is the point: the marshalling mono will not
// generate is three lines of Marshal calls when written by hand, against the
// same layout mono itself computes (Marshal.SizeOf reports 64 bytes for
// DeviceInformationStruct under wine-mono).  We call the managed method
// directly -- no marshalling at all on that side -- and hand the result out the
// way the CLR does on Windows: a fresh CoTaskMemAlloc block of `count`
// elements, its address stored through the pointer, the count returned.
//
// Loaded and kicked from ../fakesensor/fakebonjour.c, which is already in the
// game process by the time any of this is polled.

using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;

namespace MyWhoosh
{
    public static class ExportShim
    {
        const string GameAssembly = "WindowsConnectivity";
        const string GameType = "FunctionsManager.MyWhoosh";

        static readonly string[] Exports =
        {
            "WD_GetScannedDevicesList",
            "WD_GetConnectedDevicesList",
            "BT_GetScannedDevicesList",
            "BT_GetConnectedDevicesList",
        };

        // The stub every export starts with: mov rax,[abs64] / jmp rax.  The
        // absolute address is the vtable-fixup slot the game dereferences.
        const int StubSlotOffset = 2;
        static readonly byte[] StubPrefix = { 0x48, 0xA1 };
        static readonly byte[] StubSuffix = { 0xFF, 0xE0 };

        const uint PAGE_READWRITE = 0x04;

        [DllImport("kernel32", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern IntPtr GetModuleHandleW(string name);

        [DllImport("kernel32", CharSet = CharSet.Ansi, SetLastError = true,
                   BestFitMapping = false, ExactSpelling = true)]
        static extern IntPtr GetProcAddress(IntPtr module, string name);

        [DllImport("kernel32", SetLastError = true)]
        static extern bool VirtualProtect(IntPtr addr, IntPtr size, uint prot, out uint old);

        [UnmanagedFunctionPointer(CallingConvention.StdCall)]
        delegate int GetListFn(IntPtr ppDevices);

        static readonly object Gate = new object();
        static bool installed;
        // GetFunctionPointerForDelegate does not root the delegate, and a
        // collected one leaves the slot pointing at freed trampoline code.
        static readonly List<object> Rooted = new List<object>();

        static TextWriter log;

        /// Redirect the four exports.  Idempotent, and never throws: it is
        /// called from native code, where an escaping exception is fatal.
        public static void Install()
        {
            lock (Gate)
            {
                if (installed) return;
                installed = true;
                try { InstallCore(); }
                catch (Exception e) { Log("Install failed: " + e); }
            }
        }

        static void InstallCore()
        {
            Log("installing (pid " + System.Diagnostics.Process.GetCurrentProcess().Id
                + ", runtime " + Environment.Version + ")");

            Type game = FindGameType();
            if (game == null) { Log("FATAL: " + GameType + " not found; nothing hooked"); return; }

            IntPtr module = GetModuleHandleW("WindowsConnectivity.dll");
            if (module == IntPtr.Zero) module = GetModuleHandleW("WindowsConnectivity");
            if (module == IntPtr.Zero) { Log("FATAL: WindowsConnectivity.dll is not loaded"); return; }
            Log("module at 0x" + module.ToString("x16"));

            int done = 0;
            foreach (string name in Exports)
                if (Hook(module, game, name)) done++;
            Log("hooked " + done + "/" + Exports.Length + " exports");
        }

        static Type FindGameType()
        {
            foreach (Assembly a in AppDomain.CurrentDomain.GetAssemblies())
            {
                if (a.GetName().Name != GameAssembly) continue;
                Type t = a.GetType(GameType, false);
                if (t != null) return t;
            }
            return null;
        }

        static bool Hook(IntPtr module, Type game, string name)
        {
            MethodInfo mi = game.GetMethod(name, BindingFlags.Public | BindingFlags.NonPublic
                                                 | BindingFlags.Static);
            if (mi == null) { Log(name + ": no such managed method"); return false; }

            ParameterInfo[] ps = mi.GetParameters();
            if (ps.Length != 1 || !ps[0].ParameterType.IsByRef
                || !ps[0].ParameterType.GetElementType().IsArray)
            {
                Log(name + ": unexpected signature, leaving it alone");
                return false;
            }
            Type elem = ps[0].ParameterType.GetElementType().GetElementType();

            IntPtr stub = GetProcAddress(module, name);
            if (stub == IntPtr.Zero) { Log(name + ": not exported"); return false; }

            IntPtr slot = DecodeStub(stub, name);
            if (slot == IntPtr.Zero) return false;

            var poller = new Poller(name, mi, elem, ScanRescue.For(name, game, elem));
            GetListFn fn = poller.Call;
            IntPtr thunk = Marshal.GetFunctionPointerForDelegate(fn);
            Rooted.Add(fn);
            Rooted.Add(poller);

            IntPtr was = Marshal.ReadIntPtr(slot);

            uint old;
            bool reprotected = VirtualProtect(slot, (IntPtr)IntPtr.Size, PAGE_READWRITE, out old);
            Marshal.WriteIntPtr(slot, thunk);
            if (reprotected) VirtualProtect(slot, (IntPtr)IntPtr.Size, old, out old);

            if (Marshal.ReadIntPtr(slot) != thunk) { Log(name + ": slot write did not stick"); return false; }

            Log(name + ": slot 0x" + slot.ToString("x16") + " 0x" + was.ToString("x16")
                + " -> 0x" + thunk.ToString("x16") + "  (element " + elem.FullName
                + ", " + Marshal.SizeOf(elem) + " bytes native)");
            return true;
        }

        /// Read the vtable-fixup slot address out of an export's mov/jmp stub.
        static IntPtr DecodeStub(IntPtr stub, string name)
        {
            var bytes = new byte[12];
            Marshal.Copy(stub, bytes, 0, bytes.Length);

            bool shaped = bytes[0] == StubPrefix[0] && bytes[1] == StubPrefix[1]
                       && bytes[10] == StubSuffix[0] && bytes[11] == StubSuffix[1];
            if (!shaped)
            {
                Log(name + ": export stub is not mov rax,[abs64]/jmp rax -- "
                    + BitConverter.ToString(bytes) + "; not hooked");
                return IntPtr.Zero;
            }
            return (IntPtr)BitConverter.ToInt64(bytes, StubSlotOffset);
        }

        /// One export: calls the managed method directly, then marshals the
        /// array out by hand the way the CLR would.
        /// Call one of the four managed pollers, boxing each element so that a
        /// caller can edit a struct on its way out.
        static List<object> Invoke(string name, MethodInfo method)
        {
            var args = new object[] { null };
            object ret = method.Invoke(null, args);
            var list = new List<object>();
            Array a = args[0] as Array;
            if (a != null) foreach (object o in a) list.Add(o);
            if (ret is int && (int)ret != list.Count)
                Log(name + ": returned " + (int)ret + " but the array holds " + list.Count);
            return list;
        }

        /// The heart-rate slot, and every other slot the game will not fill
        /// from the network.
        ///
        /// `GetAllScannedDevices` reports `scannedList` -- the sensors Bonjour
        /// found and we resolved -- only when `scanDeviceType` is
        /// `E_PowerSource` (1) or `E_SecondaryPower` (8), and only when
        /// `scanMechanism` is 0.  Ask it for `E_HeartRate` (4) and it walks
        /// `pairedList` instead: a strap that announced itself over Direct
        /// Connect is browsed, resolved, written into `scannedList` and then
        /// never offered, because the branch that would offer it does not
        /// exist.  On Windows that is no gap -- straps arrive over BLE, which
        /// is the one path that is inert here (`../winmd/` stubs WinRT out).
        ///
        /// So for a slot the game answers only from `pairedList`, ask twice:
        /// once as it stands, for the paired sensors it does mean to offer,
        /// and once with `scanDeviceType` forced to `E_PowerSource`, for the
        /// scan list.  The second answer's structs say `deviceType = 1`,
        /// because the method copies the very field it was asked about
        /// (`IL_01c1`), so each is rewritten to the slot actually requested --
        /// which is all `ConnectDevice` and `PairDevice` read it for.  The
        /// entries themselves are the game's own, keyed on a host name it
        /// resolved itself; nothing is fabricated here.
        ///
        /// The second call is also the one that clears `scannedList`, exactly
        /// as the unrescued slots do, so the list still empties once per poll.
        internal sealed class ScanRescue
        {
            const string Export = "WD_GetScannedDevicesList";

            // ConnectivityConstants.EDeviceTypeEnum
            const int E_PowerSource = 1, E_Controllable = 2, E_Cadence = 3,
                      E_HeartRate = 4, E_SecondaryPower = 8;

            readonly FieldInfo managerField;   // FunctionsManager.MyWhoosh::dirconManager
            readonly FieldInfo scanType;       // WahooProgram::scanDeviceType
            readonly FieldInfo scanMech;       // WahooProgram::scanMechanism
            readonly FieldInfo devType;        // DeviceInformationStruct::deviceType
            readonly FieldInfo devName;        // DeviceInformationStruct::deviceName
            readonly FieldInfo scannedList;    // WahooProgram::scannedList
            bool complained;

            /// Null for every export but the scanned-device poller, and null if
            /// this MyWhoosh does not look the way the comment above describes,
            /// in which case the poller keeps its plain behaviour.
            internal static ScanRescue For(string name, Type game, Type elem)
            {
                if (name != Export) return null;
                try
                {
                    var r = new ScanRescue(game, elem);
                    Log(Export + ": rescuing the slots the game fills only from pairedList");
                    return r;
                }
                catch (Exception e)
                {
                    Log(Export + ": no slot rescue (" + e.Message + ")");
                    return null;
                }
            }

            ScanRescue(Type game, Type elem)
            {
                managerField = Field(game, "dirconManager",
                                     BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic);
                var prog = managerField.FieldType;
                scanType = Field(prog, "scanDeviceType", Instance);
                scanMech = Field(prog, "scanMechanism", Instance);
                scannedList = Field(prog, "scannedList", Instance);
                devType = Field(elem, "deviceType", Instance);
                devName = Field(elem, "deviceName", Instance);
            }

            const BindingFlags Instance =
                BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic;

            static FieldInfo Field(Type t, string name, BindingFlags flags)
            {
                FieldInfo f = t.GetField(name, flags);
                if (f == null) throw new MissingFieldException(t.FullName + "::" + name);
                return f;
            }

            // Every distinct scan the game has asked for, logged once each.  What
            // slot it searches with which mechanism is the whole question when a
            // list comes back empty, and it is not visible from anywhere else.
            readonly List<string> asked = new List<string>();

            // ... and every sensor already offered for a slot, likewise.
            readonly List<string> offered = new List<string>();

            internal List<object> Poll(MethodInfo method)
            {
                object mgr = managerField.GetValue(null);
                if (mgr == null)
                {
                    if (!asked.Contains("null")) { asked.Add("null"); Log(Export + ": dirconManager is null"); }
                    return Invoke(Export, method);
                }

                int want = Convert.ToInt32(scanType.GetValue(mgr));
                int mech = Convert.ToInt32(scanMech.GetValue(mgr));
                bool fromNetwork = want == E_PowerSource || want == E_SecondaryPower;

                // Before reading the list, not only on the rescued slots: the
                // trainer's own search is cleared the same way when you come
                // back to it from another slot.
                if (want != 0 && mech == 0) Announce(mgr);

                List<object> devices = Invoke(Export, method);
                int raw = devices.Count;
                if (!fromNetwork && want != 0 && mech == 0)
                    Rescue(method, mgr, want, devices);
                Filter(want, devices);

                string seen = want + "/" + mech;
                if (!asked.Contains(seen))
                {
                    asked.Add(seen);
                    Log(Export + ": scanDeviceType=" + want + " scanMechanism=" + mech
                        + " -> " + raw + " from the game, " + devices.Count + " after us");
                }
                return devices;
            }

            /// Append the scan list, as the slot that was asked for.
            void Rescue(MethodInfo method, object mgr, int want, List<object> devices)
            {
                var paired = new List<string>();
                foreach (object d in devices) paired.Add(NameOf(d));

                object was = scanType.GetValue(mgr);
                List<object> net;
                try
                {
                    scanType.SetValue(mgr, Enum.ToObject(scanType.FieldType, E_PowerSource));
                    net = Invoke(Export, method);
                }
                finally { scanType.SetValue(mgr, was); }

                object slot = Enum.ToObject(devType.FieldType, want);
                foreach (object d in net)
                {
                    // A sensor the game already offers for this slot out of
                    // pairedList must not arrive twice under two identifiers.
                    if (paired.Contains(NameOf(d))) continue;
                    devType.SetValue(d, slot);
                    devices.Add(d);

                    // Once per sensor and slot: this runs on every poll of an
                    // open search, twice a second.
                    string once = NameOf(d) + "/" + want;
                    if (!offered.Contains(once))
                    {
                        offered.Add(once);
                        Log(Export + ": offering \"" + NameOf(d) + "\" for device type " + want);
                    }
                }
            }

            /// Put our sensors back into the game's scan list.
            ///
            /// This is the other half of the problem, and it is a timing one.
            /// `StartScan` -- which is what opening a slot's search calls --
            /// clears `scannedList`, and re-browses only when it is not already
            /// scanning.  Moving from the trainer's slot to the heart-rate one
            /// does exactly that: the list is emptied, no new `ServiceResolved`
            /// arrives because the browse is already running, and the single
            /// poll the widget makes finds nothing.  Measured: resolves at
            /// 22:47:53, the search opened at 22:47:59, `0 from the game`.
            ///
            /// So write the entries the resolve would have written, in the
            /// format `ServiceResolved` builds them in
            /// (`serial:x:name:x:domain:x:regtype:x:host:x:port`, keyed on the
            /// host name) and let the game's own code turn them into structs.
            /// Nothing is invented: `../fakesensor/fakebonjour.c` publishes the
            /// table of what it is advertising, and this is the same six fields
            /// it hands to `ServiceResolved`.
            ///
            /// A resolve writes an entry whether or not the sensor is already
            /// connected, so this puts back exactly what a browse would have --
            /// including for the trainer's own slot, which empties the same way
            /// when you return to it from another slot.
            void Announce(object mgr)
            {
                IDictionary list = scannedList.GetValue(mgr) as IDictionary;
                if (list == null) return;
                foreach (DeviceTable.Entry e in DeviceTable.Read())
                {
                    if (e.Host == null || e.Serial == null || e.Port == 0) continue;
                    if (list.Contains(e.Host)) continue;
                    list[e.Host] = e.Serial + ":x:" + e.Name + ":x:local.:x:"
                                 + "_wahoo-fitness-tnp._tcp.:x:" + e.Host + ":x:" + e.Port;
                    if (!announced.Contains(e.Host))
                    {
                        announced.Add(e.Host);
                        Log(Export + ": announcing \"" + e.Name + "\" at " + e.Host + ":" + e.Port
                            + "; the scan list had been cleared without a re-browse");
                    }
                }
            }

            readonly List<string> announced = new List<string>();

            /// Drop sensors that cannot fill the slot being scanned for.  A
            /// power-only trainer has no business in the heart-rate list, and
            /// a strap has none in the trainer list; the game cannot tell,
            /// since it has not connected to either yet, but the handshake
            /// file says what each one serves.
            void Filter(int want, List<object> devices)
            {
                uint need = Needed(want);
                if (need == 0) return;
                List<DeviceTable.Entry> table = DeviceTable.Read();
                if (table.Count == 0) return;

                for (int i = devices.Count - 1; i >= 0; i--)
                {
                    string n = NameOf(devices[i]);
                    DeviceTable.Entry e = table.Find(x => x.Name == n);
                    if (n == null || e == null) continue;
                    if ((e.Caps & need) != 0) continue;
                    devices.RemoveAt(i);
                    Log(Export + ": \"" + n + "\" cannot serve device type " + want + ", hiding it");
                }
            }

            static uint Needed(int slot)
            {
                switch (slot)
                {
                    case E_PowerSource:
                    case E_SecondaryPower: return DeviceTable.Power;
                    case E_Controllable:   return DeviceTable.Controllable;
                    case E_Cadence:        return DeviceTable.Cadence;
                    case E_HeartRate:      return DeviceTable.Heart;
                    default:               return 0;
                }
            }

            string NameOf(object dev)
            {
                try { return (string)devName.GetValue(dev); }
                catch (Exception e)
                {
                    if (!complained) { complained = true; Log(Export + ": " + e.Message); }
                    return null;
                }
            }
        }

        /// What each advertised sensor actually serves.  `fakebonjour.c` writes
        /// this at load time, whatever the sensors were configured from --
        /// `blebridge.py`'s handshake, or the environment -- so there is one
        /// file to read and the two sides cannot disagree about which strap is
        /// which.  One `name=` per record, a `caps=` naming what it serves; a
        /// record with no `caps=` is taken to have everything, which is what
        /// the DLL assumes too.
        /// What `../fakesensor/fakebonjour.c` is advertising: one record per
        /// sensor, written at load time whatever the sensors were configured
        /// from -- `blebridge.py`'s handshake, or the environment -- so there is
        /// one file to read and the two sides cannot disagree about which strap
        /// is which.  A `name=` starts a record; `serial=`, `host=`, `port=` are
        /// what a scan entry is made of, and `caps=` says which slots the sensor
        /// can fill.  A record with no `caps=` is taken to serve everything,
        /// which is what the DLL assumes too.
        static class DeviceTable
        {
            internal const uint Power = 1, Cadence = 2, Controllable = 4, Heart = 8;
            internal const uint All = Power | Cadence | Controllable | Heart;

            const string Path = @"C:\fakesensor-table";

            internal sealed class Entry
            {
                internal string Name, Serial, Host;
                internal int Port;
                internal uint Caps = All;
            }

            static List<Entry> cache = new List<Entry>();
            static DateTime stamp;
            static long size = -1;

            /// Empty when there is no file to go on, which means no filtering
            /// and nothing to announce.
            internal static List<Entry> Read()
            {
                try
                {
                    var fi = new FileInfo(Path);
                    if (!fi.Exists) { cache = new List<Entry>(); size = -1; return cache; }
                    if (fi.LastWriteTimeUtc == stamp && fi.Length == size) return cache;
                    stamp = fi.LastWriteTimeUtc;
                    size = fi.Length;
                    cache = Parse(File.ReadAllLines(Path));
                    return cache;
                }
                catch (Exception e) { Log("device table: " + e.Message); return cache; }
            }

            static List<Entry> Parse(string[] lines)
            {
                var table = new List<Entry>();
                Entry cur = null;
                foreach (string line in lines)
                {
                    int eq = line.IndexOf('=');
                    if (eq <= 0) continue;
                    string key = line.Substring(0, eq).Trim();
                    string val = line.Substring(eq + 1).Trim();
                    if (key == "name") { cur = new Entry { Name = val }; table.Add(cur); continue; }
                    if (cur == null) continue;
                    if (key == "serial") cur.Serial = val;
                    else if (key == "host") cur.Host = val;
                    else if (key == "port") int.TryParse(val, out cur.Port);
                    else if (key == "caps") cur.Caps = Bits(val);
                }
                return table;
            }

            static uint Bits(string list)
            {
                uint bits = 0;
                foreach (string raw in list.Split(','))
                {
                    string c = raw.Trim();
                    if (c == "power") bits |= Power;
                    else if (c == "cadence") bits |= Cadence;
                    else if (c == "controllable") bits |= Controllable;
                    else if (c == "hr" || c == "heart") bits |= Heart;
                    else if (c.Length > 0) Log("device table: unknown capability \"" + c + "\"");
                }
                return bits;
            }
        }

        sealed class Poller
        {
            readonly string name;
            readonly MethodInfo method;
            readonly int esize;
            readonly ScanRescue rescue;   // null except on WD_GetScannedDevicesList
            bool firstCall = true;

            internal Poller(string name, MethodInfo method, Type elem, ScanRescue rescue)
            {
                this.name = name;
                this.method = method;
                this.esize = Marshal.SizeOf(elem);
                this.rescue = rescue;
            }

            internal int Call(IntPtr ppDevices)
            {
                try { return CallCore(ppDevices); }
                catch (Exception e)
                {
                    Log(name + ": " + e.GetType().Name + ": " + e.Message);
                    if (ppDevices != IntPtr.Zero) Marshal.WriteIntPtr(ppDevices, IntPtr.Zero);
                    return 0;
                }
            }

            int CallCore(IntPtr ppDevices)
            {
                if (firstCall)
                {
                    firstCall = false;
                    // For an out parameter the CLR never reads what the caller
                    // put there, so this is only evidence about the contract:
                    // a plausible pointer would mean a caller-owned buffer.
                    Log(name + ": first call, ppDevices=0x" + ppDevices.ToString("x16")
                        + " *ppDevices=0x" + (ppDevices == IntPtr.Zero ? 0L
                            : Marshal.ReadIntPtr(ppDevices).ToInt64()).ToString("x16"));
                }

                // A direct managed call: the signature mono cannot marshal is
                // not marshalled at all on this side.
                List<object> devices = rescue == null ? Invoke(name, method) : rescue.Poll(method);

                int count = devices.Count;

                // Mirror the CLR's out-direction: a fresh CoTaskMemAlloc block,
                // its address stored through the pointer, the count returned.
                // Never null, so a caller that dereferences before checking the
                // count reads zeroed memory instead of faulting.
                int bytes = esize * Math.Max(count, 1);
                IntPtr block = Marshal.AllocCoTaskMem(bytes);
                for (int i = 0; i < bytes; i++) Marshal.WriteByte(block, i, 0);
                for (int i = 0; i < count; i++)
                    Marshal.StructureToPtr(devices[i],
                                           new IntPtr(block.ToInt64() + (long)i * esize), false);

                if (ppDevices != IntPtr.Zero) Marshal.WriteIntPtr(ppDevices, block);
                if (count > 0) Log(name + " -> " + count + " device(s) at 0x" + block.ToString("x16"));
                return count;
            }
        }

        static void Log(string msg)
        {
            string line = "[" + DateTime.Now.ToString("HH:mm:ss.fff", CultureInfo.InvariantCulture)
                        + " exportshim] " + msg;
            try
            {
                if (log == null)
                {
                    string path = Environment.GetEnvironmentVariable("MYWHOOSH_SHIM_LOG")
                               ?? Environment.GetEnvironmentVariable("FAKESENSOR_LOG");
                    log = path == null ? Console.Error
                        : TextWriter.Synchronized(new StreamWriter(
                              new FileStream(path, FileMode.Append, FileAccess.Write,
                                             FileShare.ReadWrite)) { AutoFlush = true });
                }
                log.WriteLine(line);
            }
            catch { }
            if (log != Console.Error) { try { Console.Error.WriteLine(line); } catch { } }
        }
    }
}
