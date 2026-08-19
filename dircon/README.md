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

Both blockers are dealt with, and the whole path runs:

- Blocker 1 is **fixed** — `../winemono/` patches wine-mono's
  `ComAwareEventInfo` and installs the result into a prefix.
- Blocker 2 is **bypassed** — `../fakesensor/` replaces Bonjour's two coclasses
  with an in-proc COM server of our own, so nothing depends on mDNSResponder
  hearing a packet. With both in place the game's own `WD_GetPower()` and
  `WD_GetHeart()` return live values from a sensor served over loopback TCP.

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
GAME_LIBS=/path/to/Content/Libraries/Win64 WINEPREFIX=/path/to/prefix ./run.sh
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
