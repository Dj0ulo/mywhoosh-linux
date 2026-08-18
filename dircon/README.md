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

The Dircon path is **not** currently usable, blocked on two independent
problems. Everything else on the path works.

| Step | Status |
|---|---|
| Apple Bonjour installs under Wine (`Bonjour64.msi`) | works |
| `Bonjour Service` registers and reports `RUNNING` to the SCM | works |
| `WD_GetNetworkState()` / `GetDirconServiceAvailability()` gate opens | works |
| `CoCreateInstance` of `DNSSDService` / `DNSSDEventManager` | works |
| `IConnectionPointContainer::FindConnectionPoint` + `Advise` with a managed sink | works |
| `IDNSSDService::Browse()` from an STA thread | works |
| **`ComAwareEventInfo.AddEventHandler`** — how the game wires its sinks | **wine-mono stub, throws** |
| **mDNSResponder actually discovering anything** | **never joins the multicast group** |

### Blocker 1 — `ComAwareEventInfo` is a throw-only stub in wine-mono

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
  It would mean shipping a patched wine-mono, or installing one into the prefix.
- **IL-rewriting `WFTNP_Init`** to call a helper assembly we ship instead. Keeps
  everything inside the game directory, but needs real metadata editing (new
  assembly/type/member refs) rather than the byte patch `patch/` does today.

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

### The conclusion this points at

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

It would answer `Browse`/`Resolve` straight from the bridge daemon over a
loopback socket — no mDNS on the wire at all, no Apple code, no port conflict,
and no second responder fighting avahi. `ComAwareEventInfo` still has to be
solved either way.

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

### Running them

```sh
./build.sh                 # builds TestDircon against the installed game DLL
./run.sh 20                # runs it in ~/Games/mywhoosh, scanning for 20s

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

- Everything here was run in a throwaway prefix. `~/Games/mywhoosh` was only
  read from; its registry differs from the pre-test backup by timestamps alone.
- `GetNetworkState()` checks for a service named exactly `"Bonjour Service"` with
  status `Running`. That gate alone is trivially satisfiable in Wine without
  Apple's code — but opening it just moves the failure to `WFTNP_Init`.
- The `.md` API reference in `wine-ble/test-ble/WindowsConnectivity.md` has
  several signatures wrong (`WD_RegisterDelegates` takes 5 delegates, not 6;
  `ConnectDelegate` and `ConnectivityDataInput` differ). Trust reflection over
  the document — `tools/ILDump.cs` and the dumpers used here.
