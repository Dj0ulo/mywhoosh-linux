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
documentation. `../winmd/members.py` reports 34 types and 61 members
referenced, with their signatures; most are plumbing (`DataReader`, `IBuffer`
and `CryptographicBuffer` are `byte[]` wrappers, `IAsyncOperation<T>` is a
`Task<T>`). The real surface is about fifteen operations, each one BlueZ call
wide:

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
`BluetoothLEAdvertisement.ServiceUuids` returning `IReadOnlyList<Guid>` compiles
and then throws `MissingMethodException` on every advertisement — the game's
metadata says `IList<Guid>`, because WinRT's `IVector<T>` projects as
`IList<T>`. The failure surfaces inside the game's event handler with no device
ever appearing, which looks exactly like "the scan found nothing".

`members.py --check` now decodes both sides' signature blobs and compares them,
so this one is caught at build time rather than in a ride. Run it after every
change to `src/Windows.cs`:

```sh
../winmd/members.py <prefix>/…/WindowsConnectivity.dll --check --build-dir build
```

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

## The Bonjour gate

This branch needs no Bonjour COM server and no patched wine-mono, and the reason
is one branch in the game's own IL rather than anything the shim does.

`OpenBikeManager::GetNetworkState` and `WahooProgram::GetNetworkState` are the
same method twice: walk `ServiceController.GetServices()`, look for a service
named exactly `"Bonjour Service"`, return true only if its `Status` is `4`
(`Running`). Each constructor stores that in `isBonjourEnabled`, and the two
places that touch COM branch over themselves when it is false:

```
OpenBikeManager::OBM_Initialize   IL_0001 ldfld isBonjourEnabled
                                  IL_0006 brfalse IL_0100      <- the ret
WahooProgram::.ctor               IL_0063 ldfld isBonjourEnabled
                                  IL_0068 brfalse.s IL_0070    <- past WFTNP_Init
```

Those two are the whole of it. Across all 126 types in
`WindowsConnectivity.dll` there are exactly four `Marshal::GetTypeFromCLSID`
call sites and twelve `ComAwareEventInfo::.ctor` sites, and every one of them is
inside `OBM_Initialize`, `WFTNP_Init` or `WFTNP_Dispose` — all three behind the
gate. Nothing else in the assembly reaches Bonjour at all.

So with no `"Bonjour Service"` running:

- `CoCreateInstance` is never called, so the `COMException` out of
  `OBM_Initialize` cannot happen and no COM server has to exist;
- `ComAwareEventInfo` is never constructed, so it does not matter that stock
  wine-mono leaves all of it as `NotImplementedException` — which is the entire
  reason `dev` carries `winemono/`;
- `WD_GetDirconServiceAvailability` returns false and the game simply does not
  offer Direct Connect, which on this branch is correct.

The trap is the other direction. A prefix that has run the `dev` branch has the
gate **propped open on purpose** — `fakesensor/install.sh` installs a
`"Bonjour Service"` stub, because everything Direct Connect needs lives behind
it. In such a prefix this branch does appear to require a Bonjour COM server and
a patched runtime, and that appearance is entirely an artefact of the leftover
service. `./install.sh --verify` reports the gate state, and
`./install.sh --close-gate` removes `dev`'s stub — only its own, identified by
the `fakesensor-bonjour-stub` marker; a `"Bonjour Service"` we did not install
is assumed to be Apple's and left alone.

Measured on 2026-09-14, MyWhoosh 6.1.2 under GE-Proton10-4, in a prefix with no
`"Bonjour Service"`, neither Bonjour CLSID registered, no `fakebonjour.dll`, and
**stock** `System.Core.dll` from the runner's wine-mono: the game starts, reaches
`hooked 4/4 exports`, connects to the helper, and auto-connects a Tacx Flux,
subscribing to `2a63`, `2ad2`, `2ada` and `2ad9` with the game reporting three
connected devices. No `COMException`, no `NotImplementedException`.

## Open

**A clean install has not been measured.** `../lutris/mywhoosh.yml` now installs
this branch's stack — the three assemblies into the prefix's mono tree,
`blehelper.py` and `../lutris/mywhoosh-ble.sh` beside the game, and Lutris'
`prelaunch_command`/`postexit_command` pointed at that script — and no longer
patches `WindowsConnectivity.dll`. Its file steps were dry-run through Lutris'
own `CommandsMixin` against a scratch prefix, and the session script was
exercised for start/stop/already-running/missing-helper. What has *not* been
done is `lutris -i` on a machine with no prefix, all the way to a ride. Two
things that can only be checked that way:

- whether the runner's `create_prefix` leaves a wine-mono tree at
  `drive_c/windows/mono/mono-2.0/lib` for every runner people actually use
  (it does for the GE-Proton builds here, and `mywhoosh-ble.sh check` reports
  it when it does not);
- whether the game, freshly installed and never patched, reaches the sensor
  screen — the 2026-09-14 measurement was made by stripping the Dircon stack
  out of an existing prefix, which is equivalent for everything named but is
  not the same as a clean install.

**The Flatpak path is reasoned, not ridden.** Flatpak Lutris gets no
`--socket=system-bus` and no `--system-talk-name=org.bluez`, so
`../lutris/mywhoosh-ble.sh` routes the probes and the helper through
`flatpak-spawn --host` when `/.flatpak-info` exists, and starts the helper with
`--watch-bus` so killing the `flatpak-spawn` in the pidfile takes the host
process with it.  That was exercised against a stand-in `flatpak-spawn`, which
proves the plumbing and the argument quoting but not the two things only a real
sandbox shows: whether `--watch-bus` really reaps the helper on `stop`, and
whether the portal's idea of the game directory matches the sandbox's.

**`dist/` is a second copy of the build.** The installer downloads the
assemblies from there by raw URL, so they must be rebuilt and committed with
`../dist.sh` whenever `src/` or `../exportshim/` changes. Nothing enforces it;
`dist/MANIFEST` records the commit each build came from, which is the only way
to notice afterwards.
