// The `Windows` assembly MyWhoosh's WindowsConnectivity.dll references, with
// the answers made true.
//
// The game's BT_* stack is written against WinRT: BluetoothLEAdvertisementWatcher
// to scan, BluetoothLEDevice and the Gatt* family to connect and exchange data.
// Wine has no WinRT, and mono has no WinRT projection, so on the face of it that
// path is closed under Wine.  It is not, because of how mono resolves the
// reference: `Windows, Version=255.255.255.255` is looked up by *simple name*
// in the prefix's own mono tree, and an ordinary managed assembly with that name
// satisfies it.  The game then calls these members through ordinary IL call
// sites and cannot tell what is behind them.
//
// ../../winmd/ is that assembly with every member answering "no radio, no
// devices, no services" -- which is what keeps the game alive today.  This is
// the same surface (the same 34 types and 60 members ../../winmd/members.py
// reads out of the game's metadata) with the answers coming from real hardware
// through Backend.cs.
//
// Three things the game does that the shape of this file follows, all read out
// of its IL with ../../tools/ildump.sh rather than assumed:
//
//   * BluetoothProgram::Advertisement_Received returns immediately unless
//     Advertisement.ServiceUuids is non-empty, and classifies the device by
//     comparing those UUIDs against CyclingPower, CyclingSpeedAndCadence,
//     HeartRate, RunningSpeedAndCadence, FTMS (0x1826) and Wahoo's private
//     service.  An advertisement reported without service UUIDs is silently
//     dropped, so the helper reports what BlueZ knows and this passes it on
//     unfiltered.
//   * An empty LocalName is expected: the game builds a placeholder name from
//     the address itself (GenerateTempName).  So a nameless device is reported
//     nameless rather than given something invented here.
//   * The game fills its own pairing slots from SensorBase::features after
//     connecting, so nothing here decides what a sensor is good for.
//
// Everything returns rather than throws.  These run under mono's
// native-to-managed wrappers on the way back into the game, where an escaping
// exception is a crash and not an error; a failed call reports the WinRT
// failure value (null, Unreachable, an empty list) exactly as it would on
// Windows with the device switched off.
//
// The static constructors of the types the game touches first also start
// ../../exportshim -- see Loader.cs, which explains why that job landed here.
//
// One deliberate simplification: every operation completes on the calling
// thread and the IAsyncOperation it returns is already finished.  The game
// awaits these, and at least one caller (BluetoothProgram::IsBluetoothEnabled)
// awaits and then blocks the same thread on Task.Wait() -- which deadlocks the
// moment a continuation has to be posted back to a thread that is busy waiting.
// Completing inline cannot deadlock.  The cost is that a slow call blocks its
// caller, which is why connect is bounded on the helper's side rather than
// here.

using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text;
using System.Threading.Tasks;
using System.Runtime.InteropServices.WindowsRuntime;
using MyWhoosh.Ble;
using Windows.Foundation;
using Windows.Storage.Streams;

[assembly: System.Reflection.AssemblyVersion("255.255.255.255")]

namespace Windows.Foundation
{
    /// A finished WinRT async operation.  Real IAsyncOperation is an interface;
    /// nothing in WindowsConnectivity.dll cares, it only ever hands the value to
    /// GetAwaiter, so a sealed class carrying the Task is enough.
    public sealed class IAsyncOperation<TResult>
    {
        public readonly Task<TResult> Task;

        public IAsyncOperation(TResult result)
        {
            Task = System.Threading.Tasks.Task.FromResult(result);
        }
    }

    public delegate void TypedEventHandler<TSender, TResult>(TSender sender, TResult args);
}

namespace Windows.Storage.Streams
{
    public interface IBuffer
    {
        uint Length { get; }
    }

    /// The only IBuffer in this assembly.  WinRT keeps the bytes opaque and
    /// hands them out through DataReader; here they are simply a byte[], which
    /// is also what the helper protocol carries.
    sealed class Bytes : IBuffer
    {
        public readonly byte[] Data;

        public Bytes(byte[] data) { Data = data ?? new byte[0]; }

        public uint Length { get { return (uint)Data.Length; } }

        public static byte[] Of(IBuffer buffer)
        {
            var b = buffer as Bytes;
            return b == null ? new byte[0] : b.Data;
        }
    }

    public enum ByteOrder { LittleEndian, BigEndian }

    public sealed class DataReader
    {
        byte[] data = new byte[0];
        int position;

        public static DataReader FromBuffer(IBuffer buffer)
        {
            return new DataReader { data = Bytes.Of(buffer) };
        }

        /// Fills `value` completely, as WinRT does; a short buffer leaves the
        /// tail zeroed rather than throwing, because the game reads fixed-size
        /// arrays out of notifications whose length varies by flags byte.
        public void ReadBytes(byte[] value)
        {
            if (value == null) return;
            int n = Math.Min(value.Length, data.Length - position);
            if (n > 0)
            {
                Array.Copy(data, position, value, 0, n);
                position += n;
            }
            for (int i = Math.Max(n, 0); i < value.Length; i++) value[i] = 0;
        }
    }

    public sealed class DataWriter
    {
        readonly List<byte> bytes = new List<byte>();

        /// The game writes whole bytes and byte arrays only -- control-point
        /// payloads it has already laid out itself -- so there is nothing here
        /// for byte order to reorder.
        public void put_ByteOrder(ByteOrder value) { }

        public void WriteByte(byte value) { bytes.Add(value); }

        public void WriteBytes(byte[] value)
        {
            if (value != null) bytes.AddRange(value);
        }

        public IBuffer DetachBuffer()
        {
            var buffer = new Bytes(bytes.ToArray());
            bytes.Clear();
            return buffer;
        }
    }
}

namespace Windows.Security.Cryptography
{
    public enum BinaryStringEncoding { Utf8, Utf16LE, Utf16BE }

    public static class CryptographicBuffer
    {
        public static IBuffer ConvertStringToBinary(string value, BinaryStringEncoding encoding)
        {
            Encoding e;
            switch (encoding)
            {
                case BinaryStringEncoding.Utf16LE: e = new UnicodeEncoding(false, false); break;
                case BinaryStringEncoding.Utf16BE: e = new UnicodeEncoding(true, false); break;
                default: e = new UTF8Encoding(false); break;
            }
            return new Bytes(e.GetBytes(value ?? string.Empty));
        }

        public static void CopyToByteArray(IBuffer buffer, out byte[] value)
        {
            value = (byte[])Bytes.Of(buffer).Clone();
        }
    }
}

namespace Windows.Devices.Radios
{
    public enum RadioKind { Other, WiFi, MobileBroadband, Bluetooth, FM }

    public enum RadioState { Unknown, On, Off, Disabled }

    public sealed class Radio
    {
        static Radio() { MyWhoosh.Ble.Loader.Kick(); }

        readonly string name;
        readonly bool up;

        Radio(string name, bool up) { this.name = name; this.up = up; }

        /// Every Bluetooth adapter Linux has, powered or not.  The game walks
        /// this looking for a Bluetooth radio that is On
        /// (BluetoothProgram::CheckRadioState) and shows the user Bluetooth as
        /// off when it finds none -- so an adapter that exists but is blocked
        /// has to be reported, and reported off, or the user is told they have
        /// no Bluetooth at all.
        ///
        /// This is the one question answered without the helper: it is asked
        /// before anything else and sysfs is right there behind Wine's Z:.
        public static IAsyncOperation<IReadOnlyList<Radio>> GetRadiosAsync()
        {
            var radios = new List<Radio>();
            foreach (var adapter in Backend.Radios())
                radios.Add(new Radio(adapter.Key, adapter.Value));
            if (radios.Count == 0) Backend.Log("no Bluetooth adapter in /sys/class/bluetooth");
            return new IAsyncOperation<IReadOnlyList<Radio>>(radios);
        }

        public RadioKind Kind { get { return RadioKind.Bluetooth; } }

        public RadioState State { get { return up ? RadioState.On : RadioState.Off; } }

        public string Name { get { return name; } }
    }
}

namespace Windows.Devices.Enumeration
{
    /// Referenced for Id and Name, and never constructed on this path: the game
    /// gets its devices from advertisements, not from Windows' device
    /// enumeration.
    public sealed class DeviceInformation
    {
        public string Id { get { return string.Empty; } }

        public string Name { get { return string.Empty; } }
    }
}

namespace Windows.Devices.Bluetooth
{
    using Windows.Devices.Bluetooth.GenericAttributeProfile;

    public enum BluetoothCacheMode { Cached, Uncached }

    public enum BluetoothConnectionStatus { Disconnected, Connected }

    public sealed class BluetoothLEDevice
    {
        internal readonly string Address;       // "AA:BB:CC:DD:EE:FF"
        string name;
        bool connected = true;

        static readonly Dictionary<string, BluetoothLEDevice> Live =
            new Dictionary<string, BluetoothLEDevice>(StringComparer.OrdinalIgnoreCase);

        readonly List<Foundation.TypedEventHandler<BluetoothLEDevice, object>> statusHandlers =
            new List<Foundation.TypedEventHandler<BluetoothLEDevice, object>>();

        static BluetoothLEDevice()
        {
            Loader.Kick();
            Backend.Received += OnBackendEvent;
        }

        BluetoothLEDevice(string address, string name)
        {
            Address = address;
            this.name = name ?? string.Empty;
        }

        /// Connects, because WinRT's version does: it returns a device object
        /// whose GATT tree can be walked, and under BlueZ nothing can be walked
        /// before the link is up and the services have resolved.  A device that
        /// cannot be reached comes back as null, which is what the game handles
        /// on Windows for a sensor that has gone away.
        public static IAsyncOperation<BluetoothLEDevice> FromBluetoothAddressAsync(ulong address)
        {
            string mac = Addresses.ToMac(address);
            var reply = Backend.Call("op", "connect", "addr", mac);
            if (reply == null)
            {
                Backend.Log("connect to " + mac + " failed");
                return new IAsyncOperation<BluetoothLEDevice>(null);
            }

            string deviceName = Json.Str(reply, "name", string.Empty);
            BluetoothLEDevice device;
            lock (Live)
            {
                if (!Live.TryGetValue(mac, out device))
                {
                    device = new BluetoothLEDevice(mac, deviceName);
                    Live[mac] = device;
                }
                else
                {
                    device.name = deviceName;
                    device.connected = true;
                }
            }
            Backend.Log("connected to " + mac + " (" + deviceName + ")");
            return new IAsyncOperation<BluetoothLEDevice>(device);
        }

        public IAsyncOperation<GattDeviceServicesResult> GetGattServicesAsync(BluetoothCacheMode cacheMode)
        {
            var reply = Backend.Call("op", "services", "addr", Address);
            if (reply == null)
                return new IAsyncOperation<GattDeviceServicesResult>(
                    new GattDeviceServicesResult(GattCommunicationStatus.Unreachable, new GattDeviceService[0]));

            var services = new List<GattDeviceService>();
            foreach (object entry in Json.Arr(reply, "services"))
            {
                var o = Json.Obj(entry);
                Guid uuid;
                if (!Guid.TryParse(Json.Str(o, "uuid", ""), out uuid)) continue;
                services.Add(new GattDeviceService(this, uuid));
            }
            return new IAsyncOperation<GattDeviceServicesResult>(
                new GattDeviceServicesResult(GattCommunicationStatus.Success, services));
        }

        public string Name { get { return name; } }

        public BluetoothConnectionStatus ConnectionStatus
        {
            get
            {
                return connected ? BluetoothConnectionStatus.Connected
                                 : BluetoothConnectionStatus.Disconnected;
            }
        }

        public EventRegistrationToken add_ConnectionStatusChanged(
            Foundation.TypedEventHandler<BluetoothLEDevice, object> handler)
        {
            lock (statusHandlers) statusHandlers.Add(handler);
            return default(EventRegistrationToken);
        }

        /// WinRT unsubscribes by token; nothing in the game's code path keeps
        /// them apart, and it removes a handler only when it is about to drop
        /// the device, so the whole list goes.
        public void remove_ConnectionStatusChanged(EventRegistrationToken token)
        {
            lock (statusHandlers) statusHandlers.Clear();
        }

        /// Disposing a BluetoothLEDevice releases the link on Windows, and the
        /// game disposes when the user disconnects a sensor -- so this really
        /// disconnects, or a sensor could never be handed back to another app.
        public void Dispose()
        {
            lock (Live) Live.Remove(Address);
            Backend.Call("op", "disconnect", "addr", Address);
        }

        static void OnBackendEvent(BackendEvent ev)
        {
            if (ev.Kind != "connection") return;
            BluetoothLEDevice device;
            lock (Live)
            {
                if (!Live.TryGetValue(ev.Address, out device)) return;
            }
            if (device.connected == ev.Connected) return;
            device.connected = ev.Connected;
            Backend.Log(ev.Address + " is now " + (ev.Connected ? "connected" : "disconnected"));

            Foundation.TypedEventHandler<BluetoothLEDevice, object>[] handlers;
            lock (device.statusHandlers) handlers = device.statusHandlers.ToArray();
            foreach (var h in handlers)
            {
                try { h(device, null); }
                catch (Exception e) { Backend.Log("ConnectionStatusChanged handler threw: " + e); }
            }
        }
    }
}

namespace Windows.Devices.Bluetooth.Advertisement
{
    public enum BluetoothLEScanningMode { Passive, Active, None }

    public sealed class BluetoothLEAdvertisement
    {
        internal BluetoothLEAdvertisement(string localName, IList<Guid> serviceUuids)
        {
            LocalNameValue = localName ?? string.Empty;
            ServiceUuidsValue = serviceUuids ?? new Guid[0];
        }

        internal readonly string LocalNameValue;
        // IList, not IReadOnlyList: WinRT's IVector<Guid> projects as IList<T>,
        // and the game's metadata says so.  Returning the read-only interface
        // compiles, satisfies a name-only check, and then throws
        // MissingMethodException on every advertisement -- see winmd/members.py
        // --check, which compares signatures for exactly this reason.
        internal readonly IList<Guid> ServiceUuidsValue;

        public string LocalName { get { return LocalNameValue; } }

        public IList<Guid> ServiceUuids { get { return ServiceUuidsValue; } }
    }

    public sealed class BluetoothLEAdvertisementReceivedEventArgs
    {
        internal BluetoothLEAdvertisementReceivedEventArgs(ulong address, BluetoothLEAdvertisement advertisement)
        {
            AddressValue = address;
            AdvertisementValue = advertisement;
        }

        internal readonly ulong AddressValue;
        internal readonly BluetoothLEAdvertisement AdvertisementValue;

        public ulong BluetoothAddress { get { return AddressValue; } }

        public BluetoothLEAdvertisement Advertisement { get { return AdvertisementValue; } }
    }

    public sealed class BluetoothLEAdvertisementWatcherStoppedEventArgs
    {
    }

    /// The type whose absence stops BluetoothProgram being laid out at all, and
    /// now also the game's way of finding sensors.
    ///
    /// Scanning is a property of the adapter, not of a watcher, so several
    /// watchers share one BlueZ discovery: the helper is told to scan while at
    /// least one watcher is running.  Every advertisement reaches every running
    /// watcher, which is WinRT's behaviour too.
    public sealed class BluetoothLEAdvertisementWatcher
    {
        static readonly List<BluetoothLEAdvertisementWatcher> Running =
            new List<BluetoothLEAdvertisementWatcher>();

        readonly List<Foundation.TypedEventHandler<BluetoothLEAdvertisementWatcher,
                                                   BluetoothLEAdvertisementReceivedEventArgs>> received =
            new List<Foundation.TypedEventHandler<BluetoothLEAdvertisementWatcher,
                                                  BluetoothLEAdvertisementReceivedEventArgs>>();
        readonly List<Foundation.TypedEventHandler<BluetoothLEAdvertisementWatcher,
                                                   BluetoothLEAdvertisementWatcherStoppedEventArgs>> stopped =
            new List<Foundation.TypedEventHandler<BluetoothLEAdvertisementWatcher,
                                                  BluetoothLEAdvertisementWatcherStoppedEventArgs>>();

        static BluetoothLEAdvertisementWatcher()
        {
            MyWhoosh.Ble.Loader.Kick();
            Backend.Received += OnBackendEvent;
        }

        /// Active scanning asks for scan responses, which is where a device's
        /// name usually is; BlueZ scans actively anyway, so this is recorded
        /// and not acted on.
        public void put_ScanningMode(BluetoothLEScanningMode value) { }

        public void Start()
        {
            lock (Running)
            {
                if (Running.Contains(this)) return;
                Running.Add(this);
                if (Running.Count > 1) return;
            }
            if (Backend.Call("op", "scan", "enable", true) == null)
            {
                Backend.Log("scan could not be started");
                RaiseStopped();
            }
        }

        public void Stop()
        {
            bool last;
            lock (Running)
            {
                if (!Running.Remove(this)) return;
                last = Running.Count == 0;
            }
            if (last) Backend.Call("op", "scan", "enable", false);
            RaiseStopped();
        }

        public EventRegistrationToken add_Received(
            Foundation.TypedEventHandler<BluetoothLEAdvertisementWatcher,
                                         BluetoothLEAdvertisementReceivedEventArgs> handler)
        {
            lock (received) received.Add(handler);
            return default(EventRegistrationToken);
        }

        public void remove_Received(EventRegistrationToken token)
        {
            lock (received) received.Clear();
        }

        public EventRegistrationToken add_Stopped(
            Foundation.TypedEventHandler<BluetoothLEAdvertisementWatcher,
                                         BluetoothLEAdvertisementWatcherStoppedEventArgs> handler)
        {
            lock (stopped) stopped.Add(handler);
            return default(EventRegistrationToken);
        }

        public void remove_Stopped(EventRegistrationToken token)
        {
            lock (stopped) stopped.Clear();
        }

        static void OnBackendEvent(BackendEvent ev)
        {
            if (ev.Kind != "advert") return;

            var uuids = new List<Guid>();
            foreach (string u in ev.Uuids)
            {
                Guid g;
                if (Guid.TryParse(u, out g)) uuids.Add(g);
            }

            ulong address = Addresses.ToUlong(ev.Address);
            if (address == 0) return;

            var args = new BluetoothLEAdvertisementReceivedEventArgs(
                address, new BluetoothLEAdvertisement(ev.Name, uuids));

            BluetoothLEAdvertisementWatcher[] watchers;
            lock (Running) watchers = Running.ToArray();
            foreach (var w in watchers)
            {
                Foundation.TypedEventHandler<BluetoothLEAdvertisementWatcher,
                                             BluetoothLEAdvertisementReceivedEventArgs>[] handlers;
                lock (w.received) handlers = w.received.ToArray();
                foreach (var h in handlers)
                {
                    try { h(w, args); }
                    catch (Exception e) { Backend.Log("Received handler threw: " + e); }
                }
            }
        }

        void RaiseStopped()
        {
            Foundation.TypedEventHandler<BluetoothLEAdvertisementWatcher,
                                         BluetoothLEAdvertisementWatcherStoppedEventArgs>[] handlers;
            lock (stopped) handlers = stopped.ToArray();
            var args = new BluetoothLEAdvertisementWatcherStoppedEventArgs();
            foreach (var h in handlers)
            {
                try { h(this, args); }
                catch (Exception e) { Backend.Log("Stopped handler threw: " + e); }
            }
        }
    }
}

namespace Windows.Devices.Bluetooth.GenericAttributeProfile
{
    using Windows.Devices.Bluetooth;

    [Flags]
    public enum GattCharacteristicProperties : uint
    {
        None = 0,
        Broadcast = 1,
        Read = 2,
        WriteWithoutResponse = 4,
        Write = 8,
        Notify = 16,
        Indicate = 32,
        AuthenticatedSignedWrites = 64,
        ExtendedProperties = 128,
        ReliableWrites = 256,
        WritableAuxiliaries = 512,
    }

    public enum GattClientCharacteristicConfigurationDescriptorValue { None, Notify, Indicate }

    public enum GattCommunicationStatus { Success, Unreachable, ProtocolError, AccessDenied }

    /// WinRT's handle on the link a service is reached over.  BlueZ owns the
    /// link and the shim's Dispose on the device is what drops it, so this is a
    /// handle with nothing behind it -- kept because the game reads and disposes
    /// it.
    public sealed class GattSession
    {
        public void Dispose() { }
    }

    public sealed class GattDescriptor
    {
    }

    public sealed class GattValueChangedEventArgs
    {
        internal GattValueChangedEventArgs(byte[] value) { buffer = new Bytes(value); }

        readonly Bytes buffer;

        public Storage.Streams.IBuffer CharacteristicValue { get { return buffer; } }
    }

    public sealed class GattReadResult
    {
        internal GattReadResult(GattCommunicationStatus status, byte[] value)
        {
            this.status = status;
            buffer = new Bytes(value);
        }

        readonly GattCommunicationStatus status;
        readonly Bytes buffer;

        public GattCommunicationStatus Status { get { return status; } }

        public Storage.Streams.IBuffer Value { get { return buffer; } }
    }

    public sealed class GattWriteResult
    {
        internal GattWriteResult(GattCommunicationStatus status) { this.status = status; }

        readonly GattCommunicationStatus status;

        public GattCommunicationStatus Status { get { return status; } }
    }

    public sealed class GattCharacteristic
    {
        internal GattCharacteristic(BluetoothLEDevice device, Guid uuid, GattCharacteristicProperties properties)
        {
            this.device = device;
            this.uuid = uuid;
            this.properties = properties;
        }

        readonly BluetoothLEDevice device;
        readonly Guid uuid;
        readonly GattCharacteristicProperties properties;

        readonly List<Foundation.TypedEventHandler<GattCharacteristic, GattValueChangedEventArgs>> handlers =
            new List<Foundation.TypedEventHandler<GattCharacteristic, GattValueChangedEventArgs>>();

        /// Every characteristic the game holds, keyed by address and UUID, so a
        /// notification from the helper reaches the objects that asked for it.
        /// The game keeps one sensor object per device and holds its
        /// characteristics for the life of the connection, so this does not
        /// grow with time; it is cleared when a device is disposed.
        static readonly Dictionary<string, List<GattCharacteristic>> Subscribed =
            new Dictionary<string, List<GattCharacteristic>>(StringComparer.OrdinalIgnoreCase);

        static GattCharacteristic()
        {
            MyWhoosh.Ble.Loader.Kick();
            Backend.Received += OnBackendEvent;
        }

        string Key { get { return device.Address + "/" + uuid.ToString("D"); } }

        public Guid Uuid { get { return uuid; } }

        /// BlueZ reports a characteristic's user description as a descriptor,
        /// which nothing in the game's path reads: it identifies characteristics
        /// by UUID.
        public string UserDescription { get { return string.Empty; } }

        public GattCharacteristicProperties CharacteristicProperties { get { return properties; } }

        public Foundation.IAsyncOperation<GattReadResult> ReadValueAsync(BluetoothCacheMode cacheMode)
        {
            var reply = Backend.Call("op", "read", "addr", device.Address, "char", uuid.ToString("D"));
            if (reply == null)
                return new Foundation.IAsyncOperation<GattReadResult>(
                    new GattReadResult(GattCommunicationStatus.Unreachable, new byte[0]));

            byte[] value;
            try { value = Json.FromHex(Json.Str(reply, "value", "")); }
            catch (FormatException e)
            {
                Backend.Log("bad read payload for " + uuid + ": " + e.Message);
                return new Foundation.IAsyncOperation<GattReadResult>(
                    new GattReadResult(GattCommunicationStatus.ProtocolError, new byte[0]));
            }
            return new Foundation.IAsyncOperation<GattReadResult>(
                new GattReadResult(GattCommunicationStatus.Success, value));
        }

        public Foundation.IAsyncOperation<GattCommunicationStatus> WriteValueAsync(Storage.Streams.IBuffer value)
        {
            return new Foundation.IAsyncOperation<GattCommunicationStatus>(Write(value));
        }

        public Foundation.IAsyncOperation<GattWriteResult> WriteValueWithResultAsync(Storage.Streams.IBuffer value)
        {
            return new Foundation.IAsyncOperation<GattWriteResult>(new GattWriteResult(Write(value)));
        }

        /// WinRT's plain WriteValueAsync writes *with* response, and so does
        /// this -- except for a characteristic that cannot take one, where
        /// asking for a response is a guaranteed failure rather than a safer
        /// default.
        GattCommunicationStatus Write(Storage.Streams.IBuffer value)
        {
            bool withResponse = (properties & GattCharacteristicProperties.Write) != 0
                             || (properties & GattCharacteristicProperties.WriteWithoutResponse) == 0;
            var reply = Backend.Call("op", "write",
                                     "addr", device.Address,
                                     "char", uuid.ToString("D"),
                                     "value", Json.ToHex(Bytes.Of(value)),
                                     "response", withResponse);
            return reply == null ? GattCommunicationStatus.Unreachable : GattCommunicationStatus.Success;
        }

        /// Writing this descriptor is how a WinRT client subscribes; BlueZ has
        /// StartNotify/StopNotify instead and writes the descriptor itself.
        /// Indications and notifications are the same subscription from here --
        /// the difference is in what the peripheral does, which BlueZ knows
        /// from the characteristic's own flags.
        public Foundation.IAsyncOperation<GattCommunicationStatus>
            WriteClientCharacteristicConfigurationDescriptorAsync(
                GattClientCharacteristicConfigurationDescriptorValue value)
        {
            bool enable = value != GattClientCharacteristicConfigurationDescriptorValue.None;
            var reply = Backend.Call("op", "notify",
                                     "addr", device.Address,
                                     "char", uuid.ToString("D"),
                                     "enable", enable);
            if (reply == null)
                return new Foundation.IAsyncOperation<GattCommunicationStatus>(
                    GattCommunicationStatus.Unreachable);

            lock (Subscribed)
            {
                List<GattCharacteristic> subscribers;
                if (!Subscribed.TryGetValue(Key, out subscribers))
                    Subscribed[Key] = subscribers = new List<GattCharacteristic>();
                if (enable)
                {
                    if (!subscribers.Contains(this)) subscribers.Add(this);
                }
                else
                {
                    subscribers.Remove(this);
                }
            }
            Backend.Log((enable ? "subscribed to " : "unsubscribed from ") + uuid
                        + " on " + device.Address);
            return new Foundation.IAsyncOperation<GattCommunicationStatus>(GattCommunicationStatus.Success);
        }

        public EventRegistrationToken add_ValueChanged(
            Foundation.TypedEventHandler<GattCharacteristic, GattValueChangedEventArgs> handler)
        {
            lock (handlers) handlers.Add(handler);
            return default(EventRegistrationToken);
        }

        public void remove_ValueChanged(EventRegistrationToken token)
        {
            lock (handlers) handlers.Clear();
        }

        static void OnBackendEvent(BackendEvent ev)
        {
            if (ev.Kind != "value" || ev.Characteristic == null) return;

            Guid uuid;
            if (!Guid.TryParse(ev.Characteristic, out uuid)) return;

            GattCharacteristic[] subscribers;
            lock (Subscribed)
            {
                List<GattCharacteristic> list;
                if (!Subscribed.TryGetValue(ev.Address + "/" + uuid.ToString("D"), out list)) return;
                subscribers = list.ToArray();
            }

            var args = new GattValueChangedEventArgs(ev.Value ?? new byte[0]);
            foreach (var c in subscribers)
            {
                Foundation.TypedEventHandler<GattCharacteristic, GattValueChangedEventArgs>[] hs;
                lock (c.handlers) hs = c.handlers.ToArray();
                foreach (var h in hs)
                {
                    try { h(c, args); }
                    catch (Exception e) { Backend.Log("ValueChanged handler threw: " + e); }
                }
            }
        }
    }

    public sealed class GattCharacteristicsResult
    {
        internal GattCharacteristicsResult(IReadOnlyList<GattCharacteristic> characteristics)
        {
            this.characteristics = characteristics ?? new GattCharacteristic[0];
        }

        readonly IReadOnlyList<GattCharacteristic> characteristics;

        public IReadOnlyList<GattCharacteristic> Characteristics { get { return characteristics; } }
    }

    public sealed class GattDeviceService
    {
        internal GattDeviceService(BluetoothLEDevice device, Guid uuid)
        {
            this.device = device;
            this.uuid = uuid;
        }

        readonly BluetoothLEDevice device;
        readonly Guid uuid;

        public Guid Uuid { get { return uuid; } }

        public GattSession Session { get { return new GattSession(); } }

        public Foundation.IAsyncOperation<GattCharacteristicsResult> GetCharacteristicsAsync(
            BluetoothCacheMode cacheMode)
        {
            var reply = Backend.Call("op", "chars", "addr", device.Address, "service", uuid.ToString("D"));
            var found = new List<GattCharacteristic>();
            if (reply == null)
                return new Foundation.IAsyncOperation<GattCharacteristicsResult>(
                    new GattCharacteristicsResult(found));

            foreach (object entry in Json.Arr(reply, "chars"))
            {
                var o = Json.Obj(entry);
                Guid charUuid;
                if (!Guid.TryParse(Json.Str(o, "uuid", ""), out charUuid)) continue;

                var flags = new List<string>();
                foreach (object f in Json.Arr(o, "flags"))
                {
                    string s = f as string;
                    if (s != null) flags.Add(s);
                }
                found.Add(new GattCharacteristic(device, charUuid, Flags.ToProperties(flags)));
            }
            return new Foundation.IAsyncOperation<GattCharacteristicsResult>(
                new GattCharacteristicsResult(found));
        }

        /// The link belongs to the device, so disposing one service must not
        /// take the others down with it.
        public void Dispose() { }
    }

    public sealed class GattDeviceServicesResult
    {
        internal GattDeviceServicesResult(GattCommunicationStatus status, IReadOnlyList<GattDeviceService> services)
        {
            this.status = status;
            this.services = services ?? new GattDeviceService[0];
        }

        readonly GattCommunicationStatus status;
        readonly IReadOnlyList<GattDeviceService> services;

        public GattCommunicationStatus Status { get { return status; } }

        public IReadOnlyList<GattDeviceService> Services { get { return services; } }
    }

    /// The real assigned-number UUIDs.  The game compares advertised service
    /// UUIDs against these as strings to decide what a device is, so they have
    /// to be exactly the assigned numbers.
    public static class GattServiceUuids
    {
        public static Guid HeartRate
        {
            get { return new Guid("0000180d-0000-1000-8000-00805f9b34fb"); }
        }

        public static Guid CyclingPower
        {
            get { return new Guid("00001818-0000-1000-8000-00805f9b34fb"); }
        }

        public static Guid CyclingSpeedAndCadence
        {
            get { return new Guid("00001816-0000-1000-8000-00805f9b34fb"); }
        }

        public static Guid RunningSpeedAndCadence
        {
            get { return new Guid("00001814-0000-1000-8000-00805f9b34fb"); }
        }
    }
}

namespace MyWhoosh.Ble
{
    /// A Bluetooth address is a 48-bit number to WinRT and a string to BlueZ,
    /// most significant byte first in both.  The game stores the number as a
    /// sensor's identity and hands it straight back to
    /// FromBluetoothAddressAsync, so this pair has to round-trip exactly.
    static class Addresses
    {
        public static string ToMac(ulong address)
        {
            var sb = new StringBuilder(17);
            for (int shift = 40; shift >= 0; shift -= 8)
            {
                if (sb.Length > 0) sb.Append(':');
                sb.Append(((byte)(address >> shift)).ToString("X2", CultureInfo.InvariantCulture));
            }
            return sb.ToString();
        }

        public static ulong ToUlong(string mac)
        {
            if (string.IsNullOrEmpty(mac)) return 0;
            string[] parts = mac.Split(':');
            if (parts.Length != 6) return 0;
            ulong value = 0;
            foreach (string part in parts)
            {
                byte b;
                if (!byte.TryParse(part, NumberStyles.HexNumber, CultureInfo.InvariantCulture, out b)) return 0;
                value = (value << 8) | b;
            }
            return value;
        }
    }

    /// BlueZ names a characteristic's abilities in words; WinRT names them in
    /// bits.  Only the ones the game reads are mapped -- it checks Notify,
    /// Indicate, Write and WriteWithoutResponse before deciding how to use a
    /// characteristic -- but the rest cost a line each and a missing bit reads
    /// as a device that cannot do something it can.
    static class Flags
    {
        public static Windows.Devices.Bluetooth.GenericAttributeProfile.GattCharacteristicProperties
            ToProperties(IEnumerable<string> flags)
        {
            var p = Windows.Devices.Bluetooth.GenericAttributeProfile.GattCharacteristicProperties.None;
            foreach (string flag in flags)
            {
                switch (flag)
                {
                    case "broadcast": p |= Windows.Devices.Bluetooth.GenericAttributeProfile.GattCharacteristicProperties.Broadcast; break;
                    case "read": p |= Windows.Devices.Bluetooth.GenericAttributeProfile.GattCharacteristicProperties.Read; break;
                    case "write-without-response": p |= Windows.Devices.Bluetooth.GenericAttributeProfile.GattCharacteristicProperties.WriteWithoutResponse; break;
                    case "write": p |= Windows.Devices.Bluetooth.GenericAttributeProfile.GattCharacteristicProperties.Write; break;
                    case "notify": p |= Windows.Devices.Bluetooth.GenericAttributeProfile.GattCharacteristicProperties.Notify; break;
                    case "indicate": p |= Windows.Devices.Bluetooth.GenericAttributeProfile.GattCharacteristicProperties.Indicate; break;
                    case "authenticated-signed-writes": p |= Windows.Devices.Bluetooth.GenericAttributeProfile.GattCharacteristicProperties.AuthenticatedSignedWrites; break;
                    case "extended-properties": p |= Windows.Devices.Bluetooth.GenericAttributeProfile.GattCharacteristicProperties.ExtendedProperties; break;
                    case "reliable-write": p |= Windows.Devices.Bluetooth.GenericAttributeProfile.GattCharacteristicProperties.ReliableWrites; break;
                    case "writable-auxiliaries": p |= Windows.Devices.Bluetooth.GenericAttributeProfile.GattCharacteristicProperties.WritableAuxiliaries; break;
                }
            }
            return p;
        }
    }
}
