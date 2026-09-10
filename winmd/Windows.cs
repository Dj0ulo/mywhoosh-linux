// A stand-in for the `Windows` winmd that MyWhoosh's WindowsConnectivity.dll
// references and that does not exist under Wine.
//
// Why a stub rather than a patch.  `BluetoothManager.BluetoothProgram` has one
// field, `advertisment`, typed `BluetoothLEAdvertisementWatcher`.  Mono cannot
// lay the class out without that type, so `BT_InitBluetoothManager` throws
// TypeLoadException and takes the process with it before the game's
// connectivity init finishes -- and that init is what reaches the Dircon stack.
//
// Editing WindowsConnectivity.dll to drop the field works on the metadata but
// not in the game: MyWhoosh hashes that DLL and silently skips loading it when
// a single byte differs, connectivity and all (measured -- see README.md in
// this directory).  So the fix has to leave every game file
// byte-identical, which means satisfying the reference instead of removing it.
//
// Mono probes `C:\windows\mono\mono-2.0\lib\Windows.dll` for it, outside the
// game tree entirely.
//
// Every type and member the game's DLL references is declared here -- see
// ./members.py, which lists them straight out of its metadata.  The bodies are
// inert: BLE needs WinRT, which Wine does not have, so nothing here can do the
// real work.  What matters is that **nothing throws**.  These run under mono's
// native-to-managed wrappers, where an escaping exception is a fatal crash
// rather than a caught error, which is the whole failure this stub exists to
// avoid.  So each member returns an empty or default value: no radios, no
// devices, no services.  Bluetooth reports itself off, and the game carries on
// to the transports that do work.

using System;
using System.Collections.Generic;
using System.Threading.Tasks;
using System.Runtime.InteropServices.WindowsRuntime;
using Windows.Foundation;
using Windows.Storage.Streams;

[assembly: System.Reflection.AssemblyVersion("255.255.255.255")]

namespace Windows.Foundation
{
    /// A completed WinRT async operation.  Real IAsyncOperation is an interface;
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

    public enum ByteOrder { LittleEndian, BigEndian }

    public sealed class DataReader
    {
        public static DataReader FromBuffer(IBuffer buffer) { return new DataReader(); }

        public void ReadBytes(byte[] value) { }
    }

    public sealed class DataWriter
    {
        public void put_ByteOrder(ByteOrder value) { }

        public void WriteByte(byte value) { }

        public void WriteBytes(byte[] value) { }

        public IBuffer DetachBuffer() { return null; }
    }
}

namespace Windows.Security.Cryptography
{
    public enum BinaryStringEncoding { Utf8, Utf16LE, Utf16BE }

    public static class CryptographicBuffer
    {
        public static IBuffer ConvertStringToBinary(string value, BinaryStringEncoding encoding)
        {
            return null;
        }

        public static void CopyToByteArray(IBuffer buffer, out byte[] value)
        {
            value = new byte[0];
        }
    }
}

namespace Windows.Devices.Radios
{
    public enum RadioKind { Other, WiFi, MobileBroadband, Bluetooth, FM }

    public enum RadioState { Unknown, On, Off, Disabled }

    public sealed class Radio
    {
        /// No radios: the caller looks for a Bluetooth one, finds none, and
        /// reports Bluetooth off.  That is the truthful answer under Wine.
        public static IAsyncOperation<IReadOnlyList<Radio>> GetRadiosAsync()
        {
            return new IAsyncOperation<IReadOnlyList<Radio>>(new Radio[0]);
        }

        public RadioKind Kind { get { return RadioKind.Other; } }

        public RadioState State { get { return RadioState.Unknown; } }
    }
}

namespace Windows.Devices.Enumeration
{
    public sealed class DeviceInformation
    {
        public string Id { get { return string.Empty; } }

        public string Name { get { return string.Empty; } }
    }
}

namespace Windows.Devices.Bluetooth
{
    public enum BluetoothCacheMode { Cached, Uncached }

    public enum BluetoothConnectionStatus { Disconnected, Connected }

    public sealed class BluetoothLEDevice
    {
        public static IAsyncOperation<BluetoothLEDevice> FromBluetoothAddressAsync(ulong address)
        {
            return new IAsyncOperation<BluetoothLEDevice>(null);
        }

        public IAsyncOperation<GenericAttributeProfile.GattDeviceServicesResult>
            GetGattServicesAsync(BluetoothCacheMode cacheMode)
        {
            return new IAsyncOperation<GenericAttributeProfile.GattDeviceServicesResult>(null);
        }

        public string Name { get { return string.Empty; } }

        public BluetoothConnectionStatus ConnectionStatus
        {
            get { return BluetoothConnectionStatus.Disconnected; }
        }

        public EventRegistrationToken add_ConnectionStatusChanged(
            Foundation.TypedEventHandler<BluetoothLEDevice, object> handler)
        {
            return default(EventRegistrationToken);
        }

        public void remove_ConnectionStatusChanged(EventRegistrationToken token) { }

        public void Dispose() { }
    }
}

namespace Windows.Devices.Bluetooth.Advertisement
{
    public enum BluetoothLEScanningMode { Passive, Active, None }

    public sealed class BluetoothLEAdvertisement
    {
        public string LocalName { get { return string.Empty; } }

        public IReadOnlyList<Guid> ServiceUuids { get { return new Guid[0]; } }
    }

    public sealed class BluetoothLEAdvertisementReceivedEventArgs
    {
        public ulong BluetoothAddress { get { return 0; } }

        public BluetoothLEAdvertisement Advertisement { get { return null; } }
    }

    public sealed class BluetoothLEAdvertisementWatcherStoppedEventArgs
    {
    }

    /// The type whose absence stops the whole class being laid out.  A scan
    /// started here simply never reports anything.
    public sealed class BluetoothLEAdvertisementWatcher
    {
        public void put_ScanningMode(BluetoothLEScanningMode value) { }

        public void Start() { }

        public void Stop() { }

        public EventRegistrationToken add_Received(
            Foundation.TypedEventHandler<BluetoothLEAdvertisementWatcher,
                                         BluetoothLEAdvertisementReceivedEventArgs> handler)
        {
            return default(EventRegistrationToken);
        }

        public void remove_Received(EventRegistrationToken token) { }

        public EventRegistrationToken add_Stopped(
            Foundation.TypedEventHandler<BluetoothLEAdvertisementWatcher,
                                         BluetoothLEAdvertisementWatcherStoppedEventArgs> handler)
        {
            return default(EventRegistrationToken);
        }

        public void remove_Stopped(EventRegistrationToken token) { }
    }
}

namespace Windows.Devices.Bluetooth.GenericAttributeProfile
{
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

    public sealed class GattSession
    {
        public void Dispose() { }
    }

    public sealed class GattDescriptor
    {
    }

    public sealed class GattValueChangedEventArgs
    {
        public Storage.Streams.IBuffer CharacteristicValue { get { return null; } }
    }

    public sealed class GattReadResult
    {
        public GattCommunicationStatus Status { get { return GattCommunicationStatus.Unreachable; } }

        public Storage.Streams.IBuffer Value { get { return null; } }
    }

    public sealed class GattWriteResult
    {
        public GattCommunicationStatus Status { get { return GattCommunicationStatus.Unreachable; } }
    }

    public sealed class GattCharacteristic
    {
        public Guid Uuid { get { return Guid.Empty; } }

        public string UserDescription { get { return string.Empty; } }

        public GattCharacteristicProperties CharacteristicProperties
        {
            get { return GattCharacteristicProperties.None; }
        }

        public Foundation.IAsyncOperation<GattReadResult> ReadValueAsync(BluetoothCacheMode cacheMode)
        {
            return new Foundation.IAsyncOperation<GattReadResult>(null);
        }

        public Foundation.IAsyncOperation<GattCommunicationStatus>
            WriteValueAsync(Storage.Streams.IBuffer value)
        {
            return new Foundation.IAsyncOperation<GattCommunicationStatus>(
                GattCommunicationStatus.Unreachable);
        }

        public Foundation.IAsyncOperation<GattWriteResult>
            WriteValueWithResultAsync(Storage.Streams.IBuffer value)
        {
            return new Foundation.IAsyncOperation<GattWriteResult>(null);
        }

        public Foundation.IAsyncOperation<GattCommunicationStatus>
            WriteClientCharacteristicConfigurationDescriptorAsync(
                GattClientCharacteristicConfigurationDescriptorValue value)
        {
            return new Foundation.IAsyncOperation<GattCommunicationStatus>(
                GattCommunicationStatus.Unreachable);
        }

        public EventRegistrationToken add_ValueChanged(
            Foundation.TypedEventHandler<GattCharacteristic, GattValueChangedEventArgs> handler)
        {
            return default(EventRegistrationToken);
        }

        public void remove_ValueChanged(EventRegistrationToken token) { }
    }

    public sealed class GattCharacteristicsResult
    {
        public IReadOnlyList<GattCharacteristic> Characteristics
        {
            get { return new GattCharacteristic[0]; }
        }
    }

    public sealed class GattDeviceService
    {
        public Guid Uuid { get { return Guid.Empty; } }

        public GattSession Session { get { return null; } }

        public Foundation.IAsyncOperation<GattCharacteristicsResult>
            GetCharacteristicsAsync(BluetoothCacheMode cacheMode)
        {
            return new Foundation.IAsyncOperation<GattCharacteristicsResult>(null);
        }

        public void Dispose() { }
    }

    public sealed class GattDeviceServicesResult
    {
        public GattCommunicationStatus Status { get { return GattCommunicationStatus.Unreachable; } }

        public IReadOnlyList<GattDeviceService> Services { get { return new GattDeviceService[0]; } }
    }

    /// The real assigned-number UUIDs, so a caller comparing against them
    /// compares against the right constants.
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
