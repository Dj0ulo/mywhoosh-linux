# Fake Dircon sensor

A stand-in for Apple's Bonjour COM server and for the trainer behind it, used to
prove MyWhoosh's whole network-sensor path end to end under Wine — discovery,
pairing, and live data — with no mDNS on the wire and no Apple code involved.

It works:

```
SCAN    t+5s: 1 device(s)
SCAN       "FakeTrainer" uuid=1234567890:x:FakeTrainer:x:local.:x:_wahoo-fitness-tnp._tcp.:x:FakeTrainer.local.:x:36866
CONN    E_PowerSource E_Success "FakeTrainer" ...
CONN    E_HeartRate   E_Success "FakeTrainer" ...
READ    t+2s power=152W cadence=-1 speed=-1 hr=76 connected=True
READ    t+3s power=154W cadence=-1 speed=-1 hr=77 connected=True
```

Those numbers come back out of `WD_GetPower()` and `WD_GetHeart()` — the same
calls the game itself makes.

## What it replaces, and why

`../dircon/README.md` ends on a conclusion: Apple's mDNSResponder never joins
the multicast group under Wine, so nothing is ever discovered, and the fix is
not to repair 2011 mDNSResponder but to **replace the two Bonjour coclasses**.
That is what `fakebonjour.c` is. Registered under Bonjour's CLSIDs, it is loaded
into the game's own process instead of `dnssdX.dll`, answers `Browse()` and
`Resolve()` from a hard-coded description of one sensor, and serves that sensor's
GATT services over a loopback TCP socket in Wahoo's Direct Connect protocol.

| File | Role |
|---|---|
| `fakebonjour.c` | The COM server (both CLSIDs) plus the Dircon TCP server, one in-proc DLL |
| `dotlocal_shim.c` | LD_PRELOAD shim resolving `*.local` to 127.0.0.1 — Wine resolves nothing else |
| `build.sh` | mingw-w64 build of the DLL, host build of the shim |
| `install.sh` | Points the two CLSIDs at the DLL inside one prefix (`--restore` undoes it) |
| `run.sh` | Runs `../dircon/TestDircon` against it |

## Usage

```sh
./build.sh
WINEPREFIX=~/Games/dircon-test ../winemono/install.sh   # ComAwareEventInfo, needed first
WINEPREFIX=~/Games/dircon-test ./install.sh
./run.sh 12 8            # 12s to discover, then 8s of readings
WINEPREFIX=~/Games/dircon-test ./install.sh --restore    # give Bonjour its CLSIDs back
```

Knobs, all read from the environment by the DLL: `FAKESENSOR_NAME`,
`FAKESENSOR_SERIAL` (digits only — the game parses it as a `UInt64` device id),
`FAKESENSOR_MAC`, `FAKESENSOR_PORT`, `FAKESENSOR_POWER`, `FAKESENSOR_BPM`,
`FAKESENSOR_ADDR` (what `*.local` resolves to), `FAKESENSOR_LOG`.

The sensor advertises Cycling Power (`0x1818` / `0x2a63`) and Heart Rate
(`0x180d` / `0x2a37`), both notify-only, and pushes a measurement on each once a
second.

## What had to be measured

Four things about this path are not what you would guess, and each was measured
rather than assumed. They are the reason the code looks the way it does.

**The sink is called early-bound, not through `IDispatch::Invoke`.**
`_IDNSSDEvents` is a pure dispinterface, so a real source calls it by dispid —
and against a Mono sink that fails. Mono's CCW does answer `QueryInterface` for
`IID_IDispatch`, and its `GetIDsOfNames` even answers with an id, but `Invoke`
rejects both that id (`E_INVALIDARG`) and the dispid from the type library
(`DISP_E_MEMBERNOTFOUND`). Calling the same method through the interface vtable
works and lands in the managed handler. `../winemono/SinkInvokeProbe.cs` is the
measurement:

```
OK     QueryInterface(IDispatch) -> 0x00000000
OK     GetIDsOfNames("ServiceFound") -> 0x00000000 dispid=1610743808
FAIL   Invoke(dispid=3 from type library) -> 0x80020003
FAIL   Invoke(dispid=1610743808 from GetIDsOfNames) -> 0x80070057
OK     vtable ServiceFound(slot 7) -> 0x00000000
OK     early-bound ServiceFound reached the handler: True
```

This also means **real Bonjour could never have delivered these events** to
MyWhoosh under wine-mono, even with mDNS working: it calls `Invoke`. Replacing
the coclasses is not a shortcut around blocker 2, it is the only route.

Slot numbers follow from the emitted interface being declared IDispatch-derived:
IUnknown (3), IDispatch (4), then the events in the order `../winemono` emits
them — `ServiceFound` at 7, `ServiceLost` 8, `ServiceResolved` 9,
`OperationFailed` 10. `FAKESENSOR_SINKBASE` overrides the 7 if that ever moves.

**The callbacks must arrive after `Browse()` returns, on the calling thread.**
`WFTNP_Init` does `this.browser = mainService.Browse(...)` and its `ServiceFound`
handler calls `browser.Resolve(...)`, so firing from inside `Browse()` hits a
null `browser`. The DLL posts to a message-only window it creates on the
browsing (STA) thread instead, which is also how Bonjour delivers its own
callbacks — and why `../dircon/TestDircon.cs` has to pump a message loop.

**The host name has to be `<service name>.local.`, and Wine cannot resolve it.**
`ServiceFound` keys its service table on the service name with spaces replaced by
dashes plus `.local.`, and `ServiceResolved` looks the entry up again by the host
name it is given: report anything else and the resolve is dropped. But
`DirconSensor` then does `Dns.GetHostAddresses` on that name, and under Wine
nothing answers — the Windows hosts file is ignored (measured), and the host
resolver only answers `.local` through nss-mdns with something publishing the
name. Hence `dotlocal_shim.c`. It returns exactly one address on purpose: given
several, `DirconSensor` keeps only one containing `192.168` and ends up with
none.

**The scan list is only reported for the power-source slot.**
`GetAllScannedDevices` returns the Dircon scan list only when `scanDeviceType` is
`E_PowerSource` or `E_SecondaryPower`, and only `WD_StartScanning(type)` sets it.
`WD_StartScanningAll()` leaves it `E_DeviceTypeNone`, and the list then comes
back empty however many services resolved. Slots are independent afterwards, so
one device has to be claimed once per slot — `WD_GetHeart()` stays `-1` until the
same sensor is also connected as `E_HeartRate`.

## Known rough edges

- **Reconnection cannot work under Wine as-is.** After a disconnect,
  `DirconSensor.TryToReconnect` polls `IsTrainerAvailable`, which pings the host;
  raw sockets are denied, so it logs `Access denied.` in a tight loop forever.
  Harmless at shutdown (that is when the probe sees it), but a mid-ride drop
  would never recover. Fixable outside the game — `net.ipv4.ping_group_range`, or
  `CAP_NET_RAW` on the wine binary — or by not dropping the connection, which is
  in our hands now that we serve it.
- `cadence` and `speed` stay `-1`: nothing here advertises `0x2a5b`/`0x2a5c`, and
  the power notification sets no crank-revolution flag.
- Wine's 32-bit helper process prints
  `ERROR: ld.so: object '.../dotlocal_shim.so' ... wrong ELF class: ELFCLASS64`.
  The shim only matters in the 64-bit game process; building a 32-bit copy needs
  32-bit libc headers, which this machine does not have.
- The prefix still needs a Windows service named exactly `Bonjour Service` in
  state `Running`, because `GetNetworkState()` checks for it before anything else
  happens. The test prefix has Apple's, installed from the game's own SDK
  bundle — only its two COM classes are taken over. Satisfying that gate without
  Apple's code is easy but not done here.
- One sensor, one client connection at a time, and the objects are only safe on
  the game's STA thread (registered `ThreadingModel=Apartment`, like Bonjour).
