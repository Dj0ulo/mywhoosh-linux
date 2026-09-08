# Dircon path investigation

MyWhoosh's `WindowsConnectivity.dll` exposes two independent sensor stacks:

| Stack | Prefix | Transport | Uses WinRT? |
|---|---|---|---|
| Bluetooth LE | `BT_*` | `Windows.Devices.Bluetooth` | yes — unusable under wine-mono |
| Wahoo Direct Connect | `WD_*` | GATT over TCP, mDNS discovery | **no** |
| OpenBike | `OBC_*` | network | no |

The `WD_*` stack never touches WinRT, so it is the only in-app path that can
reach real sensors under Wine without reimplementing the WinRT projection.
This directory contains the probes used to find out how far it actually gets.

## Result

**The whole path runs from the game itself.** On MyWhoosh 6.1.2, with all four
blockers dealt with, the game discovers a Dircon sensor, resolves it, connects
to it and subscribes to power and heart rate — on its own threads, with no probe
and no GUI interaction involved.

- Blocker 1 is **fixed** — `../winemono/` patches wine-mono's
  `ComAwareEventInfo` and installs the result into a prefix.
- Blocker 2 is **bypassed** — `../fakesensor/` replaces Bonjour's two coclasses
  with an in-proc COM server of our own, so nothing depends on mDNSResponder
  hearing a packet. With both in place the game's own `WD_GetPower()` and
  `WD_GetHeart()` return live values from a sensor served over loopback TCP.
- Blocker 3 is **fixed** — the patch that stopped 6.1.2 crashing at launch was
  also what stopped it ever loading `WindowsConnectivity.dll`, because the game
  hashes that file. `../winmd/` satisfies the missing assembly reference from
  outside the game tree instead, leaving every game file byte-identical.
- Blocker 4 is **fixed** — wine-mono cannot marshal the byref array the four
  device-list exports take, and the refusal is compiled into the wrapper as a
  throw. `../exportshim/` serves those four exports itself, redirecting the
  vtable-fixup slots they jump through in memory.

| Step | Status |
|---|---|
| Apple Bonjour installs under Wine (`Bonjour64.msi`) | works |
| `Bonjour Service` registers and reports `RUNNING` to the SCM | works |
| `WD_GetNetworkState()` / `GetDirconServiceAvailability()` gate opens | works |
| `CoCreateInstance` of `DNSSDService` / `DNSSDEventManager` | works |
| `IConnectionPointContainer::FindConnectionPoint` + `Advise` with a managed sink | works |
| `IDNSSDService::Browse()` from an STA thread | works |
| `ComAwareEventInfo.AddEventHandler` — how the game wires its sinks | **fixed** by `../winemono/` |
| `WD_InitWahooDirconManager()` … `WD_StopScanningAll()` | works |
| **mDNSResponder actually discovering anything** | **never joins the multicast group** |
| Discovery, pairing and live data with Bonjour replaced | works — see `../fakesensor/` |
| The game's own native `LoadLibrary` + `GetProcAddress` call path | works |
| The game loading the DLL at all on 6.1.2 | **fixed** by `../winmd/` |
| The four `Get*DevicesList` exports the UI polls | **fixed** by `../exportshim/` |
| **The game discovering, connecting and reading a sensor by itself** | **works** |

### Blocker 1 — `ComAwareEventInfo` is a throw-only stub in wine-mono (fixed)

`WahooProgram..ctor` calls `GetNetworkState()`, and if a service named exactly
`"Bonjour Service"` is `Running` it proceeds to `WFTNP_Init()`, which wires its
four Bonjour event handlers like this:

```
new ComAwareEventInfo(typeof(_IDNSSDEvents_Event), "ServiceFound")
    .AddEventHandler(eventManager, new _IDNSSDEvents_ServiceFoundEventHandler(ServiceFound))
```

`ComAwareEventInfo` lives in `System.Core.dll`, and wine-mono's copy throws
`NotImplementedException` from every member. The exception escapes the
constructor, so `WD_InitWahooDirconManager()` leaves `dirconManager` null and
every later `WD_*` call throws `NullReferenceException`.

This is a *much* smaller gap than the WinRT one: `ComAwareEventInfo` is only a
convenience wrapper over `IConnectionPointContainer::Advise`, and `SinkProbe`
proves Mono can do that part — it builds a CCW for a managed sink and Advise
returns a cookie. What's missing is ~100 lines of managed code, not runtime
support.

Where the fix can live:

- **Patched `System.Core.dll`.** Not droppable next to the game: wine-mono
  resolves `System.Core` from its own GAC
  (`.../wine-mono-10.0.0/lib/mono/gac/System.Core/4.0.0.0__b77a5c561934e089/`),
  and neither an app-directory copy nor `MONO_PATH` overrides it (both tested).
  It means shipping a patched wine-mono, or installing one into the prefix.
  **This is what `../winemono/` does** — `mscoree` probes
  `<prefix>/drive_c/windows/mono/mono-2.0` before the runner's shared tree, so
  the patch is per-prefix and the runner is never touched.
- **IL-rewriting `WFTNP_Init`** to call a helper assembly we ship instead. Keeps
  everything inside the game directory, but needs real metadata editing (new
  assembly/type/member refs) rather than the byte patch `patch/` does today.
  Still open, and still the option to take if a patched runtime turns out to be
  awkward to distribute.

With the patched runtime installed, `TestDircon` gets through the entire API:

```
[  0.748] CALL   WD_InitWahooDirconManager -> ok
[  0.752] PROBE  DirconServiceAvailability = True
[  0.752] CALL   WD_RegisterDelegates -> ok
[  0.760] CALL   WD_OnPairWidgetOpen -> ok
[  0.777] CALL   WD_StartScanningAll -> ok
[ 10.903] SCAN   t+10s: 0 device(s)          <- blocker 2
[ 10.904] CALL   WD_StopScanningAll -> ok
```

### Blocker 2 — mDNSResponder discovers nothing under Wine

Two separate faults, one fixed here and one not:

**Port 5353 (fixed).** Apple's mDNSResponder never sets `SO_REUSEADDR` — on
Windows it doesn't have to, since several sockets may share a UDP port by
default. On Linux the bind fails with `EADDRINUSE` because the host's
`avahi-daemon` already holds `0.0.0.0:5353`. mDNSResponder then silently falls
back to an ephemeral port and keeps serving clients while being deaf to all mDNS
traffic. `reuseaddr_shim.c` (LD_PRELOAD) restores the Windows behaviour and the
bind succeeds alongside avahi — no root, no Wine patch, no stopping avahi.

**Interface enumeration (not fixed).** Even with 5353 bound, the daemon never
calls `IP_ADD_MEMBERSHIP` and never sends or receives a single packet. It gets
as far as `SIO_GET_INTERFACE_LIST` and stops, so it seems to find no usable
interface through Wine. A browse started against it returns cleanly and reports
nothing — including for services registered through the same daemon moments
earlier.

Because of this, the "publish from Linux with avahi, discover in the game" story
is unproven end to end.

### The conclusion this points at, and what came of it

Fixing Apple's 2011 mDNSResponder under Wine is the wrong thing to chase. Since
Mono's COM interop is demonstrably healthy here — activation, RCW calls,
connection points, CCW sinks all work — the cleaner architecture is to **replace
the two Bonjour coclasses with our own COM server** registered under the same
CLSIDs:

```
DNSSDService       {24CD4DE9-FF84-4701-9DC1-9B69E0D1090A}
DNSSDEventManager  {BEEB932A-8D4A-4619-AEFE-A836F988B221}
_IDNSSDEvents      {21AE8D7F-D5FE-45CF-B632-CFA2C2C6B498}  (dispinterface)
```

It answers `Browse`/`Resolve` from its own state over a loopback socket — no mDNS
on the wire at all, no Apple code, no port conflict, and no second responder
fighting avahi. `ComAwareEventInfo` had to be solved for either route, and now
is. **`../fakesensor/` is that server**, and it drives the game's API end to end.

Building it settled the open question, in the direction that makes this the only
route rather than merely the cleaner one: Mono's CCW does **not** dispatch
`IDispatch::Invoke` to the sink — it rejects the type library's dispid with
`DISP_E_MEMBERNOTFOUND` and the id from its own `GetIDsOfNames` with
`E_INVALIDARG` — while the same call through the interface vtable arrives
normally. Real Bonjour calls `Invoke`, so it could never have delivered these
events under wine-mono even with mDNS working.
`../winemono/SinkInvokeProbe.cs` is the measurement; `../fakesensor/README.md`
has the details and the other three things that had to be measured.

## Blocker 3 — the game never asks (MyWhoosh 6.1.2)

Everything above was measured from `TestDircon`, a *managed* process calling the
`WD_*` methods directly. The game is a *native* process, and it reaches the same
code a different way. Measured on 6.1.2, that difference turns out to be where
the whole thing now stands or falls: **the stack works, and the game does not
call it.**

6.1.2 itself changes nothing relevant. The `WD_*` surface is name-for-name
identical to the build everything above was measured against (44 methods), and
`WFTNP_Init`, `ComAwareEventInfo`, `DirconSensor` and `_IDNSSDEvents` (same IID)
are all still there. Only the path moved: `WindowsConnectivity.dll` now lives in
`MyWhoosh/Binaries/Win64/`, not `Content/Libraries/Win64/`, which keeps only
`Dircon/bonjoursdksetup.exe`.

### The game's real call path works

`WindowsConnectivity.dll` carries 98 unmanaged exports — `BT_*`, `WD_*`, `OBC_*`
— in `.sdata`, which `objdump -p` prints as an empty export table (parse the
export directory directly; it is there). So the native exe does
`LoadLibrary` + `GetProcAddress` and lets `mscoree` bootstrap the CLR on load.
That path is healthy under Wine, patched DLL and all:

```
LoadLibrary("WindowsConnectivity.dll")  OK at 0000000180000000
mscoree loaded                          yes
WD_InitWahooDirconManager()             returned
WD_GetDirconServiceAvailability()       = 1
WD_GetNetworkState()                    = 1
```

Two things worth knowing about calling it this way. Order is not optional: a
`WD_*` getter before `WD_InitWahooDirconManager` does not return false, it
raises `NullReferenceException` as a **fatal unhandled exception** and takes the
process with it (`dirconManager` is null and the getters `callvirt` it). And the
apartment matters as much as it does for the harness — `CoInitialize(NULL)` plus
a message pump, since the Bonjour objects are STA.

### The patch and the connectivity stack are mutually exclusive

This is the finding. Same prefix, same launch, only the two patch bytes differ:

| `WindowsConnectivity.dll` | What the game does at startup |
|---|---|
| **unpatched** | `mscoree` → `WindowsConnectivity.dll` → `libmono` → `mscorlib` → `System` → `System.ServiceProcess` → `System.Core` → crash |
| **patched** | never loads the DLL at all; game runs |

Reproduced twice in each direction under `WINEDEBUG=+loaddll`. The patch is what
makes the game start, and it is also why nothing here ever gets called.

The crash names its own cause, and it is not the Bluetooth *state* check:

```
Unhandled Exception:
System.TypeLoadException: Could not load type of field
  'BluetoothManager.BluetoothProgram:advertisment' (0) due to:
  Could not load file or assembly 'Windows, Version=255.255.255.255, ...'
  at (wrapper native-to-managed) FunctionsManager.MyWhoosh.BT_InitBluetoothManager()
```

A field typed from the `Windows` winmd, so the *class* cannot be laid out.
Patching `IsBluetoothEnabled` does not fix that; it only stops anything ever
touching the class.

**And the caller we need exists.** In that same unpatched trace,
`System.ServiceProcess` (that is `ServiceController.GetServices()` inside
`GetBonjourService()` — `svcctl_EnumServicesStatusExW` right behind it) and
`System.Core` (that is `ComAwareEventInfo`) both load *before* the Bluetooth
exception. The game really does run `WD_InitWahooDirconManager` at startup,
ahead of Bluetooth. It just dies on Bluetooth immediately afterwards, and the
patch avoids that death by preventing the whole init.

### What the UI does once you are past startup

With the patch in place the game reaches its Device Connection screen and gates
each transport *natively*, before any managed call:

- **BLE** hints "enable bluetooth" — even with `IsBluetoothEnabled` patched to
  return true, because nothing consults it.
- **Direct Connect** shows "Dircon Service Unavailable … proceed with
  installation?" Answering yes changes nothing: `WD_InstallDirconService` is
  managed code that never loads. The prompt string lives in
  `MyWhoosh-Win64-Shipping.exe`, not in the managed DLL, along
  `IsDirconServiceAvailabe`, `SetDirconServiceFeatureAvailability` and
  `IsWahooDirconAllowed` (a field of a native `ConnectivityFeatures` struct next
  to `IsBluetoothAllowed`/`IsANTAllowed`).

Measured, so it is not guesswork: with `+loaddll`, `+module` and `+reg` on a
full session including clicking the option, `WindowsConnectivity`, `mscoree` and
mono appear **zero** times in the game process, and there are no game-side
registry reads of any `Bonjour`/`Apple Inc.` key. The gate is not a local
Bonjour check either — the prefix it was measured in has the service `RUNNING`,
both `dnssd.dll` and `dnssdX.dll` in `system32`, and
`SOFTWARE\Apple Inc.\Bonjour` fully populated.

### Why patching suppresses the load: the game hashes the DLL

It is not the patch's *content*. **MyWhoosh checks `WindowsConnectivity.dll`'s
bytes before loading it, and skips the load entirely if any of them changed.**
Four runs in the never-logged-in prefix, `WINEDEBUG=+loaddll`:

| `WindowsConnectivity.dll` | `mscoree` + the DLL load? |
|---|---|
| pristine | yes → `TypeLoadException` in `BT_InitBluetoothManager` |
| pristine bytes, fresh mtime | yes → same crash |
| `advertisment` retyped to `object` (4 bytes of metadata) | **no** |
| one character of the DOS stub, `'T'`→`'t'` (1 byte of padding) | **no** |

The mtime run rules out a timestamp check; the DOS-stub run is decisive, since
those bytes mean nothing to any loader, so nothing about the *edit* can be what
was rejected. The game reaches the same point either way — the branch where
`mscoree` would load, right after DXVK spins up its compiler threads — and in
the modified case simply carries on to `hid.dll`, XINPUT and the UI.

Whose check it is, is not in doubt: the DLL has no Authenticode signature (empty
certificate table) and no PE checksum, so Wine verifies nothing. Where the
expected value lives is still open — the DLL's MD5, SHA-1 and SHA-256 appear
nowhere in the exe (raw, ASCII or UTF-16) nor anywhere in the game tree, so it
is neither a plain stored hash nor a manifest.

This kills every approach that edits the DLL — retyping the field, dropping it,
early-returning `BT_InitBluetoothManager` alike — and it means the working patch
today "works" only by preventing the load it was meant to survive.

### The fix: satisfy the reference instead of removing it

The reference is what is missing, so supply it. `../winmd/` builds stub
`Windows` and `System.Runtime.WindowsRuntime` assemblies into the prefix's
wine-mono tree, where mono probes for them
(`C:\windows\mono\mono-2.0\lib\Windows.dll`) — outside the game tree, so
every game file stays byte-identical.

With them installed and the DLL pristine, on the never-logged-in prefix:

```
mscoree → WindowsConnectivity.dll → libmono → mscorlib → Windows.dll
  → System → System.ServiceProcess → System.Core → System.Runtime.WindowsRuntime
```

no exception anywhere in the trace, and the game keeps running — measured over
100 s, `svcctl_EnumServicesStatusExW` recurring, which is `GetBonjourService()`
inside the Dircon manager. **Blocker 3 is closed: the game asks.**

## Blocker 4 — byref array marshalling (wine-mono, fixed)

On the full stack — patched wine-mono plus `../fakesensor` — the game's own init
goes all the way into our Bonjour replacement, in the game process:

```
[fakebonjour] created DNSSDEventManager -> 0x00000000
[fakebonjour] Advise: sink … (_IDNSSDEvents …), events start at vtable slot 7
[fakebonjour] created DNSSDService -> 0x00000000
```

Twice over, one pair per manager. Then, about nine seconds later, unprompted:

```
[ERROR] FATAL UNHANDLED EXCEPTION:
System.Runtime.InteropServices.MarshalDirectiveException:
  Byref array marshalling to managed code is not implemented.
```

Four exports take a byref-array parameter, and they are exactly the ones the
game polls for its device lists:

```
BT_GetScannedDevicesList    int (out DeviceInformationStruct[])
BT_GetConnectedDevicesList  int (out DeviceInformationStruct[])
WD_GetScannedDevicesList    int (out DeviceInformationStruct[])
WD_GetConnectedDevicesList  int (out DeviceInformationStruct[])
```

mono compiles the refusal *into* the native-to-managed wrapper as a throw, so
the wrapper builds and the first call to any of the four takes the process down
from a frame where nothing can catch it. This never showed up before because
`TestDircon` and the probes call the managed methods directly; only the game
goes through the reverse-P/Invoke thunk.

Nothing above the runtime can reach it. The error string is in
`mono-2.0/bin/libmono-2.0-x86_64.dll` and in no managed assembly in the prefix,
so unlike `ComAwareEventInfo` the Cecil pass in `../winemono/` has nothing to
rewrite; and both wine-mono 10.0.0 and 11.1.0 carry it, so a newer runtime is
not the fix. Removing the check would not be enough either — the same function
then demands a `[MarshalAs]` and a `SizeConst`/`SizeParamIndex` that the game's
metadata does not have, so the out-direction would have to be implemented from
scratch inside `libmono`.

### The fix: replace the four exports, not the runtime

The exports are reached through pointers we can rewrite. Each is a 12-byte stub
`mov rax,[slot]; jmp rax`, and every slot belongs to the CLI header's
VTableFixups array in `.sdata`, which `mscoree` fills with mono's thunks at load.
The game caches the export addresses from `GetProcAddress`, but those are the
stubs, so it re-reads the slot on every call: writing a slot redirects calls that
were looked up long before, needs nothing but a store to already-writable memory
in our own process, and touches no file — which keeps the game's hash check on
`WindowsConnectivity.dll` happy.

`../exportshim/` is that replacement: managed code, so the shape mono will not
marshal is not marshalled at all on our side. It calls the managed method
directly, then hands the array out the way the CLR does on Windows — a fresh
`CoTaskMemAlloc` block, its address stored through the pointer, the count
returned. `../fakesensor/fakebonjour.c` loads and starts it through mono's
embedding API, being already in the game process at the right moment.

**Blocker 4 is closed.** Same prefix, same launch, no GUI interaction, the
game's own threads throughout:

```
[exportshim]  hooked 4/4 exports
[fakebonjour] Browse(flags=0, ifIndex=0, "_wahoo-fitness-tnp._tcp.") on the main
[fakebonjour] ServiceFound("FakeTrainer")                      -> 0x00000000
[fakebonjour] ServiceResolved(… at FakeTrainer.local.:36866)   -> 0x00000000
[exportshim]  WD_GetScannedDevicesList   -> 1 device(s)
[fakebonjour] dircon: client connected
[fakebonjour] dircon: notifications power=1 hr=1
[exportshim]  WD_GetConnectedDevicesList -> 1 device(s)            (every 500 ms)
```

The game found the sensor, resolved it, opened the Dircon socket, subscribed to
power and heart rate, and settled into polling its connected-device list twice a
second — where the previous run had `FATAL UNHANDLED EXCEPTION` there is now
nothing at all. It also re-browses every couple of minutes and keeps running.

And it shows up where it counts: on that run the Device Connection screen listed
`FakeTrainer` with live watts. Repeated against a real trainer through
`../fakesensor/blebridge.py`, the game auto-connects a Tacx Flux, subscribes to
its Indoor Bike Data and writes grade to its control point, and the screen shows
watts that move when you pedal. **The in-app path works end to end, on real
hardware.**

Worth noting for the next MyWhoosh update: neither `BT_*` poller was called in
any run measured, BLE being gated natively long before, and the shim is started
by fakebonjour — so it arrives with the Bonjour path. See
`../exportshim/README.md` for what to do if that ordering ever changes.

### Launching outside Lutris

Every measurement above was taken this way, and anything touching the game
process needs it. Lutris supplies environment the game needs; a bare `wine` invocation fails with
`c0000135` because builtin `dxgi` pulls in `wined3d` → `libvkd3d-1.dll`. What is
needed:

```sh
R=$HOME/.local/share/lutris/runners/wine/GE-Proton10-4
export WINEDLLOVERRIDES="d3d11,d3d10core,d3d9,dxgi=n"
export WINEDLLPATH="$R/lib/vkd3d/x86_64-windows:$R/lib/wine/x86_64-windows"
export LD_LIBRARY_PATH="$R/lib:$LD_LIBRARY_PATH"
export WINEFSYNC=1 WINEESYNC=1 WINEDEBUG=+loaddll
cd "$WINEPREFIX/drive_c/MyWhoosh/MyWhoosh/Binaries/Win64"
"$R/bin/wine" MyWhoosh-Win64-Shipping.exe MyWhoosh
```

`wine`/`sc query` cannot attach to a prefix Lutris is running without the same
`WINEFSYNC`, which is worth remembering before concluding a service is dead.

## Probes

All of them run against the game's own `WindowsConnectivity.dll` and print a
step-by-step trace, so a failure names the exact missing piece.

| File | What it answers |
|---|---|
| `TestDircon.cs` | Drives the real `WD_*` API the way the game does — init, features, scan, device list |
| `BonjourProbe.cs` | Replicates `WFTNP_Init` exactly, using the interop types embedded in the game's DLL |
| `SinkProbe.cs` | Bypasses `ComAwareEventInfo` and wires the sink through `IConnectionPoint::Advise` by hand |
| `DnssdProbe.cs` | Talks to `dnssd.dll`'s C API directly — separates "is the daemon alive" from "does COM eventing work" |
| `reuseaddr_shim.c` | LD_PRELOAD shim letting mDNSResponder share port 5353 with avahi |

`TestDircon` goes all the way through when `../fakesensor/` is installed: it
scans as a power source (the only scan type whose results are reported), connects
the first device it finds in both the power and heart-rate slots, and prints a
second-by-second reading.

`TestDircon` and `BonjourProbe` run as `[STAThread]` and pump a message loop:
Bonjour's objects are apartment-threaded and deliver callbacks through the
thread's queue, and from an MTA thread the browse crashes in the marshaller
rather than merely failing. The game, being Unreal, pumps anyway.

### Running them

```sh
./build.sh                 # builds TestDircon against the installed game DLL
./run.sh 20 10             # 20s scan, then 10s of readings

# or against another prefix / another install:
GAME_LIBS=/path/to/MyWhoosh/Binaries/Win64 WINEPREFIX=/path/to/prefix ./run.sh
DIRCON_TRACE=1 ./run.sh    # full stack traces on failure
```

The other three build alongside it:

```sh
mcs -platform:x64 -out:build/SinkProbe.exe -r:build/WindowsConnectivity.dll SinkProbe.cs
cc -shared -fPIC -o build/reuseaddr_shim.so reuseaddr_shim.c -ldl
```

### Reproducing the Bonjour setup

Bonjour is not needed for `TestDircon` to show the gate closed, but it is needed
to get past it. The game ships the installer; the service MSI is inside it:

```sh
7z x -objx "<game>/Content/Libraries/Win64/Dircon/bonjoursdksetup.exe"
WINEPREFIX=<prefix> wine msiexec /i bjx/Bonjour64.msi /quiet /norestart
WINEPREFIX=<prefix> wine net start "Bonjour Service"
```

Note the wrapper's own `/quiet` install fails with MSI 1603 — it runs
`BonjourSDK64.msi`, not the service package. Install `Bonjour64.msi` directly.

## Notes

- Everything here was run in throwaway prefixes (latest: `~/Games/dircon-test`).
  `~/Games/mywhoosh` was only read from; its registry differs from the pre-test
  backup by timestamps alone.
- Reflection over the embedded interop *source* dispinterface aborts wine-mono
  outright (`method->slot < nslots`); `../winemono/ReflProbe.cs` reproduces it in
  four lines. Anything written against these types has to stay off that path.
- `GetNetworkState()` checks for a service named exactly `"Bonjour Service"` with
  status `Running`. That gate alone is trivially satisfiable in Wine without
  Apple's code — but opening it just moves the failure to `WFTNP_Init`. The test
  prefix still uses Apple's service for it; only the COM classes are replaced.
- `DirconSensor.TryToReconnect` pings the sensor's host, and raw sockets are
  denied under Wine (`IsTrainerAvailable - exception Access denied.`, in a tight
  loop). Nothing reconnects after a drop until that is dealt with outside the
  game.
- The `.md` API reference in `wine-ble/test-ble/WindowsConnectivity.md` has
  several signatures wrong (`WD_RegisterDelegates` takes 5 delegates, not 6;
  `ConnectDelegate` and `ConnectivityDataInput` differ). Trust reflection over
  the document — `tools/ILDump.cs` and the dumpers used here.
