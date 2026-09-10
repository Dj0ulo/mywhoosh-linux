# bleshim — engineering notes

Reference for changing this code. `README.md` is the orientation; this is the
part that is expensive to rediscover.

## The constraint everything obeys

MyWhoosh hashes `WindowsConnectivity.dll` and silently declines to load it if a
single byte differs — including bytes in the DOS stub, which mean nothing to any
loader. The game then carries on to the UI as if the DLL were not there. So no
approach that edits a game file is available, and none is used: everything here
is either a file we add to the prefix, or memory we write inside our own
process. `../winmd/README.md` has the measurements.

## Why this design and not another

**Wine's own Bluetooth stack is not reachable from here, twice over.**

Mono has no WinRT projection at all — no `RoGetActivationFactory`, no WinMD type
resolution, no runtime-callable wrapper for `IAsyncOperation`. So even a
finished `windows.devices.bluetooth` in Wine would be invisible to the game's
managed code. That fact is what makes "substitute a managed assembly" the
approach rather than one option among several.

Wine's Win32 GATT API is real but incomplete, measured by inspecting the shipped
binaries of the runners installed here:

| | GE-Proton10-4 (wine 10.0) | GE-Proton11-6 (wine 11.0) |
|---|---|---|
| `winebth.so` knows `org.bluez.GattCharacteristic1` | no | yes |
| Device discovery (`StartDiscovery`) | no | yes |
| Read a characteristic | no | no |
| Write a characteristic | no | no |
| Notifications | no | no |
| Advertisement scanning | no | no |

Upstream is slightly ahead — `BluetoothGATTGetCharacteristicValue` (read) landed
in February 2026 — but `BluetoothGATTSetCharacteristicValue` and
`BluetoothGATTRegisterEvent` are still `@ stub`, and there is no advertisement
watcher. Write and notify are how a trainer takes ERG commands and reports
power; scanning is how it is found at all.

This is why `src/Backend.cs` is the *only* file that knows how hardware is
reached. If Wine's stack catches up, or if someone contributes to it, the
backend moves and nothing above it changes.

## What the game actually calls

Read out of the game's IL with `../tools/ildump.sh`, not from WinRT
documentation. `../winmd/members.py` reports 34 types and 60 members
referenced; most are plumbing (`DataReader`, `IBuffer` and `CryptographicBuffer`
are `byte[]` wrappers, `IAsyncOperation<T>` is a `Task<T>`). The real surface is
about fifteen operations, each one BlueZ call wide:

| The game calls | BlueZ |
|---|---|
| `BluetoothLEAdvertisementWatcher.Start` / `Stop` | `Adapter1.StartDiscovery` (`Transport=le`, `DuplicateData=true`) |
| `…Received` → `BluetoothAddress`, `Advertisement.LocalName` / `.ServiceUuids` | `Device1` `Address` / `Name` / `UUIDs` / `RSSI`, via `InterfacesAdded` + `PropertiesChanged` |
| `BluetoothLEDevice.FromBluetoothAddressAsync(ulong)` | uint64 ↔ MAC, then the `Device1` object |
| `get_Name`, `get_ConnectionStatus`, `ConnectionStatusChanged` | `Device1.Name` / `.Connected` + `PropertiesChanged` |
| `GetGattServicesAsync` | `Device1.Connect()`, wait for `ServicesResolved`, enumerate `GattService1` |
| `GetCharacteristicsAsync`, `get_Uuid`, `get_CharacteristicProperties` | `GattCharacteristic1` `UUID` + `Flags` |
| `ReadValueAsync` | `ReadValue` |
| `WriteValueAsync` / `WriteValueWithResultAsync` | `WriteValue`, `type=command` / `type=request` |
| `WriteClientCharacteristicConfigurationDescriptorAsync`, `ValueChanged` | `StartNotify` + `PropertiesChanged` on `Value` |
| `Radio.GetRadiosAsync` | `Adapter1.Powered` |

Three behaviours in the game's IL that are easy to get wrong:

- **An advertisement with no service UUIDs is discarded.**
  `BluetoothProgram::Advertisement_Received` returns immediately when
  `args.Advertisement.ServiceUuids` is empty, and otherwise classifies the
  device by comparing them against `GattServiceUuids.CyclingPower` (`1818`),
  `CyclingSpeedAndCadence` (`1816`), `HeartRate` (`180d`),
  `RunningSpeedAndCadence` (`1814`), FTMS
  (`00001826-0000-1000-8000-00805f9b34fb`) and Wahoo's
  `ce060000-43e5-11e4-916c-0800200c9a66`. A scan reporting names and addresses
  but no UUIDs produces an empty list in the game and nothing in any log to say
  why.
- **A missing name is fine.** With an empty `LocalName` the game builds a
  placeholder from the address (`GenerateTempName`, `"X12"`-formatted). The shim
  must not invent one.
- **The game fills its own slots.** `BluetoothProgram::GetAllScannedDevices`
  offers a sensor for the controllable, cadence and heart-rate slots according
  to `SensorBase::features` (`hasControllable`, `hasCadence`, `hasHeart`), which
  it derives after connecting. Nothing in the shim needs to filter by slot — a
  contrast with the Dircon path, where `../exportshim/`'s `ScanRescue` and
  `DeviceCaps` exist precisely because the game will not do it there.

## wine-mono traps

Each of these cost real time. They share a shape: the code is correct on the
host and wrong in the prefix, or wrong only at runtime.

**Signatures must match the game's metadata exactly, not just by name.**
`BluetoothLEAdvertisement.ServiceUuids` returning `IReadOnlyList<Guid>` compiles,
passes `members.py --check`, and then throws `MissingMethodException` on every
advertisement — the game's metadata says `IList<Guid>`, because WinRT's
`IVector<T>` projects as `IList<T>`. The failure surfaces inside the game's
event handler with no device ever appearing, which looks exactly like "the scan
found nothing". `members.py --check` compares member *names* only; a signature
comparison is written and working in scratch but not yet folded in — that is the
single highest-value improvement to make here.

**BCL types live in different assemblies than on the host.**
`System.Collections.Generic.Queue<T>` is in `System.dll` on wine-mono's
framework and in `mscorlib` on the host Mono. An assembly built against the
wrong one loads fine outside Wine and throws `TypeLoadException` in the prefix.
`Backend.Events` is a `List<BackendEvent>` used as a FIFO for that reason. This
is why `TestBle.exe` must be run under *both* runtimes before believing a
change.

**There is no JSON in the framework**, hence `src/Json.cs`.

**Unix paths are not paths.** wine-mono reads `/tmp/x` as `C:\tmp\x`, which does
not exist, so `MYWHOOSH_SHIM_LOG=/tmp/…` silently logged nothing at all — the
one moment the log mattered. `Backend.OpenLog` now tries `Z:\tmp\…` first (`Z:`
is Wine's mapping of the Linux root) and falls back to stderr rather than going
quiet.

**Nothing may escape into a native-to-managed thunk.** The game calls this code
through mono wrappers where an escaping exception is a process crash, not a
caught error. Every entry point catches. `Loader.Kick()` in particular is called
from static constructors, where a throw would take the whole type — and with it
the BLE path — down.

## Two rules inside the shim

**Every `IAsyncOperation` completes inline.** `BluetoothProgram::IsBluetoothEnabled`
awaits and then `Task.Wait()`s on the same thread; anything that actually defers
deadlocks the game at startup. `src/Windows.cs` wraps `Task.FromResult` — the
work has already happened by the time the operation is handed back.

**Events arrive on the shim's dispatcher thread**, which is not a thread the
game created. This is the risk that was named before any of it was written and
turned out to be fine in practice (the game tolerates it, as the Dircon path
also showed), but it is the kind of thing that surfaces as a hang rather than an
exception, so it is worth remembering when one appears.

## The helper protocol

Newline-delimited JSON on `127.0.0.1:27019`, one client at a time.

- Requests carry `id` and `op`: `radios`, `scan`, `connect`, `disconnect`,
  `services`, `chars`, `read`, `write`, `notify`. Replies carry the same `id`.
- Unsolicited events carry `event`: `advert`, `value`, `connection`.
- The shim connects lazily and retries every 5 s, so starting the helper after
  the game still works.

Deliberately dumb and transport-agnostic: the helper can later move in-process,
or be replaced by Wine's own API, without `src/Windows.cs` noticing.

## Open

**A Bonjour COM server is still required, and this branch does not ship one.**
The game creates Bonjour's COM objects at startup unconditionally and dies on a
`COMException` from `OpenBikeManager.OBM_Initialize` if `CoCreateInstance`
fails — nothing to do with Bluetooth; the object merely has to exist. Today that
is met by the `dev` branch's `fakesensor/fakebonjour.c`, which is 1400 lines of
COM plus a Dircon TCP server we have no use for. What it needs is the switch
below, currently applied only to a local build. Either port that patch to `dev`,
or — better — write a small COM server here that answers `CoCreateInstance`,
browses nothing, and is 300 lines instead of 1400.

```c
/* in load_config(), after the cfg_hr block */
{ const char *v = getenv("FAKESENSOR_NONE"); if (v && *v && *v != '0') ndevs = 0; }

/* at the top of shim_kick(): the BLE shim's own Loader does this job now,
   and doing it twice would hide whether that works */
env = getenv("FAKESENSOR_NONE");
if (env && *env && *env != '0') {
    logmsg("export shim: leaving it to the BLE shim (FAKESENSOR_NONE)");
    return;
}
```

**Whether the patched wine-mono (`winemono/` on `dev`) can be dropped.**
`ComAwareEventInfo` does not appear anywhere in `BluetoothProgram` or
`BluetoothSensor` (measured, zero hits), so the BLE path should not need it. It
has not been confirmed, because the prefix used for testing still carries the
Dircon stack's registrations.

**Packaging.** `install.sh` at the repository root, the Lutris installer and the
release bundle all describe the Dircon stack.

**Signature checking in `members.py`** — see the traps above. It would have
caught the one real bug this stack has had.
