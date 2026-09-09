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
`Resolve()` from its own small table of sensors, and serves each sensor's GATT
services over a loopback TCP socket in Wahoo's Direct Connect protocol.

| File | Role |
|---|---|
| `fakebonjour.c` | The COM server (both CLSIDs) plus the Dircon TCP server, one in-proc DLL |
| `dotlocal_shim.c` | LD_PRELOAD shim resolving `*.local` to 127.0.0.1 — Wine resolves nothing else |
| `build.sh` | mingw-w64 build of the DLL, host build of the shim |
| `bonjourstub.c` | A Windows service that exists only to be named `Bonjour Service`, the name the game gates on |
| `install.sh` | Points the two CLSIDs at the DLL, installs that service, drops the shim in the prefix (`--restore` undoes it) |
| `run.sh` | Runs `../dircon/TestDircon` against it |
| `blebridge.py` | The same socket, backed by **real** BLE devices through BlueZ — a trainer, and a heart-rate strap beside it |

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
`FAKESENSOR_CAPS`, `FAKESENSOR_HR`, `FAKESENSOR_ADDR` (what `*.local` resolves
to), `FAKESENSOR_LOG`.

The DLL advertises a **table** of sensors, not one, because MyWhoosh pairs each
slot separately: a heart-rate strap beside a trainer is a second service with
its own name, host name, serial and Dircon port. `FAKESENSOR_CAPS` says which
slots one sensor offers (`power,cadence,controllable,hr`), and
`FAKESENSOR_HR=1` adds a second, heart-rate-only fake sensor on
`FAKESENSOR_PORT + 1` — enough to exercise the heart-rate slot with no strap in
the house:

```sh
FAKESENSOR_CAPS=power,cadence,controllable FAKESENSOR_HR=1 ...
```

The whole table can also come from `C:\fakesensor-device`, a `key=value` file
the DLL reads at load time, one record per `name=` key. `blebridge.py` writes
it, which is what keeps real hardware's identity out of the game's environment;
it wins over the variables above and implies `FAKESENSOR_EXTERNAL`.

However the table was configured, the DLL then writes it back out to
`C:\fakesensor-table` for `../exportshim/` to read — one file for the shim to
go on, so it can keep each sensor out of the slots it cannot fill.

A fake sensor advertises Cycling Power (`0x1818` / `0x2a63`) when its
capabilities include power, cadence or controllable, and Heart Rate (`0x180d` /
`0x2a37`) when they include `hr`; both are notify-only, and it pushes a
measurement on each once a second.

## Real hardware

`blebridge.py` replaces the hard-coded half. It connects to an actual BLE
fitness device through BlueZ -- on the Linux side, where Bluetooth works, rather
than through Wine's WinRT stack, which does not (`../README.md`, blocker 1) --
and proxies its GATT tree onto the same Dircon socket. Dircon maps onto GATT
almost one to one (1 lists services, 2 lists characteristics, 3 reads, 4 writes,
5 subscribes, 6 is a notification), so this is a proxy rather than a translation,
and the game sees the services the trainer really has.

Measured against a Tacx Flux, with `fakebonjour` advertising it instead of a
made-up sensor:

```
SCAN   t+5s: 1 device(s)
SCAN      "Tacx Flux 06189" uuid=6189:x:...:x:Tacx-Flux-06189.local.:x:36866
CONN   E_PowerSource E_Success "Tacx Flux 06189" ...
READ   t+4s  power=128W cadence=82 speed=-1 hr=0 connected=True
READ   t+22s power=142W cadence=76 speed=-1 hr=0 connected=True
```

Real watts and real cadence, out of the game's own `WD_GetPower()` and
`WD_GetCadence()`. Cadence works here where the fake sensor could never make it
work, because the trainer does send crank data.

```sh
./blebridge.py --list                       # scan, print candidates
./blebridge.py --mac FA:55:E5:BE:21:A5 &    # or no --mac: first fitness device seen
./run.sh 10 45
```

Nothing about the trainer is configured here. On connect the bridge writes the
device's name, serial, address, port and capabilities to
`<prefix>/drive_c/fakesensor-device`, and the DLL picks them up when the game
loads it; the serial is derived from the address, and the host name the game
resolves is derived from the name. The
file's presence also stops the DLL binding the port itself, so it only
advertises the one the bridge already serves — what `FAKESENSOR_EXTERNAL=1` did
by hand. The bridge removes it on exit, and `--no-handshake` goes back to the
variables.

Two things this proves beyond the readings:

**The game drives the trainer, not just reads it.** It found the FTMS control
point and wrote to it, and those writes reached the hardware: `00` (request
control), `01` (reset), then `11 00000000 0000` -- opcode 0x11, set indoor-bike
simulation parameters, grade zero. So resistance control has a path, on real
hardware, through this bridge.

**The game prefers FTMS over Cycling Power.** Offered both `0x1818` and
`0x1826`, it subscribed to `0x2ad2`/`0x2ada` and ignored `0x2a63`. Its power and
cadence come from Indoor Bike Data.

`speed` stays `-1`: the trainer has no Cycling Speed and Cadence service
(`0x1816`), and although FTMS Indoor Bike Data carries a speed field, the game
does not take it from there. `hr` is `0` because nothing here is a heart-rate
strap -- pair one as `E_HeartRate` and it would be a second device, not this one.

## Real hardware, in the game

`blebridge.py` serves an actual BLE trainer over the same socket, and with the
rest of the stack installed the game drives it by itself. Measured on a Tacx
Flux 06189, MyWhoosh 6.1.2, `FAKESENSOR_EXTERNAL=1`:

```
bridge: Tacx Flux 06189 connected: 2 service(s) exposed   Cycling Power + Fitness Machine
bridge: listening on 127.0.0.1:36866
[fakebonjour] ServiceFound("Tacx-Flux-06189")             -> 0x00000000
[fakebonjour] ServiceResolved(… at Tacx-Flux-06189.local.:36866)
bridge: client connected
bridge: notify 00002ad2 on            Indoor Bike Data
bridge: notify 00002ada on            Fitness Machine Status
bridge: notify 00002ad9 on            Control Point indications
bridge: write  00002ad9 <- 00         Request Control
bridge: write  00002ad9 <- 01         Reset
bridge: write  00002ad9 <- 11000000000000   Set Indoor Bike Simulation Parameters
```

and the game's Device Connection screen shows the trainer connected with watts
that move when you pedal. The control-point writes are the half that cannot be
faked: grade and resistance from the game reach the hardware.

### What the trainer says it can do

`blebridge.py` logs the first notification of each characteristic, and Indoor
Bike Data (`0x2AD2`) every few seconds with its flags decoded. That is not
decoration: MyWhoosh reads those flags *once*, on the first notification, to
decide what the device can provide, and only then starts parsing values.
`DirconSensor.ProcessIndoorBikeDataNotification` sets `hasCadenceFrom2AD2` from
flag bit 2, `SensorBase.SetDevicePreference` turns that into
`getCadenceFrom2AD2`, and the cadence field is parsed only when that is set --
so a trainer that omits the bit gets no cadence at all, no matter what else it
supports. Cadence, power and controllable are each announced to the game as a
separate device (`SendConnectCallback` with 3, 1 and 2), so one trainer fills
several slots.

Measured on the Tacx Flux 06189:

```
notification 00002ad2  flags=0x0044 [InstantaneousSpeed InstantaneousCadence
                                     InstantaneousPower] power=25W cadence=35rpm
notification 00002ad9  800001        Request Control -> Success
```

and its capability characteristics, read once it is awake:

| Characteristic | Value | Means |
|---|---|---|
| `0x2ACC` Fitness Machine Feature | `0x00004082` | Cadence, ResistanceLevel, PowerMeasurement |
| `0x2ACC` Target Setting Feature | `0x0000a00c` | ResistanceTarget, PowerTarget (ERG), IndoorBikeSimulation, SpinDownControl |
| `0x2A65` Cycling Power Feature | `0x0000000c` | WheelRevolutionData, CrankRevolutionData |
| `0x2AD8` Supported Power Range | `0`–`800` W, step 1 | ERG range |
| `0x2AD6` Supported Resistance Range | `0`–`1000`, step 1 | |

So cadence is available twice over -- through the FTMS flag the game actually
uses, and through crank revolutions on `0x2A63` as a fallback the game can parse
but does not subscribe to.

**A sleeping trainer looks like a bridge bug.** With the Flux asleep, the three
read-only capability characteristics fail with `org.bluez.Error.Failed` and the
game learns nothing about it, while the game still reports a connected device
because its Dircon socket to the bridge is perfectly healthy. The tell is that
BlueZ has dropped the device object entirely (it is not in `bluetoothctl
devices` and not advertising). Pedal to wake it and restart the bridge.
`Read not permitted` on `0x2AD2`/`0x2A63` is not that -- those are notify-only,
and the game never reads them.

Two things this turned up, neither in this stack:

- **Give a real trainer its own serial.** MyWhoosh keys its saved pairing on the
  Dircon serial and then displays the *stored* name, so a trainer advertised on
  fakebonjour's default serial comes up under whatever name last used it --
  observed, as a real Tacx reporting live watts while labelled `FakeTrainer`.
  `blebridge.py` derives a serial from the device address and hands it to the
  DLL through the handshake file, so every trainer is its own device.
- **A device the game already knows does not appear in a scan.** With the
  pairing saved, the game auto-connects on startup and `Browse`/`Resolve` keep
  firing while `WD_GetScannedDevicesList` returns 0, so the search list looks
  empty. Auto-connect covers the case; a first-time pairing on a fresh serial is
  the path that needs checking.

## Pairing a heart-rate strap

The trainer is the first slot, not the only one. `--hr-mac` connects a strap as
well, and its heart-rate service goes onto the *same* Dircon socket as the
trainer's services:

```sh
./blebridge.py --mac FA:55:E5:BE:21:A5 --hr-mac D1:23:6E:0C:47:B8
```

```
name=Tacx-Flux-06189   serial=1075253552437  port=36866  caps=power,cadence,controllable
name=TICKR-5A2C        serial=1075253552437  port=36866  caps=hr
```

Two records, one serial, one port -- which looks wrong until you read what the
game does with a second sensor.

**MyWhoosh holds exactly one Direct Connect connection at a time.** Both
`DirconSensor::Connect` and `WahooProgram::ServiceResolved` refuse to start
another while `WahooProgram::sensorTasks` is non-empty, and say so
(`ServiceResolved - connection task is already running`); the task they add to
that list is `WFTNP_Connect`, which *is* the socket's read loop and so only ends
when the connection does. A strap advertised on its own port is therefore
browsed, resolved, listed, clicked and pairable -- and never dialled. Measured:
the strap on 36867 got zero connections while the game showed `832823808 bpm`
for it, a number that cannot come from the heart-rate path at all, since
`SensorBase::GetHeart` clamps to `Constants::MAX_HEART_RATE_VALUE` (300) and
returns 0 below 1.

**The game's own answer for a device that fills several slots is the serial.**
`PairDevice` parses the serial out of the offered device's `deviceUuid`, looks
up the sensor already paired for `E_PowerSource`, and compares it with
`SensorBase::identifier`. On a match it does not dial anything -- it hooks that
same connected sensor into the new slot:

```
IL_029a: ldc.i4.4                            // deviceType == E_HeartRate
IL_02a7: ldc.i4.1
IL_02a8: stfld  CharacteristicsFeatures::heartConnectCallback
IL_02b1: call   WahooProgram::SendConnectCallbackManual
```

So the strap is not a second sensor here. Its `0x180D` joins the trainer's
services on one endpoint under the trainer's serial, and each device is still
advertised under its own name -- so the trainer's slots list the trainer and the
heart-rate slot lists the strap, with one connection behind both. The game
subscribes to `0x2A37` itself, without being asked and before any pairing, as
soon as it walks the service list:

```
bridge: notify 00002ad2 on rc=0     <- indoor bike data
bridge: notify 00002a37 on rc=0     <- heart rate, on the trainer's connection
bridge: write 00002ad9 <- 01        <- and still driving resistance
```

Two more things had to be added for the strap to be reachable at all, and both
are on our side of the line -- no game file is touched:

- **The game will not offer a network sensor for the heart-rate slot.**
  `GetAllScannedDevices` reports `scannedList` only for `E_PowerSource` and
  `E_SecondaryPower`; asked for `E_HeartRate` it walks *paired* sensors instead,
  so a strap is discovered, resolved, listed internally and then never shown. On
  Windows that is no gap, because straps arrive over BLE — the one path that is
  inert here. `../exportshim/`'s `ScanRescue` asks the game for the scan list
  under a slot it does answer for, relabels each entry as the slot actually
  requested, and appends it to the paired sensors the game meant to offer. The
  entries are the game's own, keyed on host names it resolved itself.
- **Capabilities have to be filtered, because the game cannot tell yet.** Before
  it connects, MyWhoosh knows nothing about a sensor beyond its name — so a
  power-only trainer would sit in the heart-rate list and a strap in the trainer
  list. The shim reads the same handshake file the DLL does and hides a sensor
  from a slot its `caps=` cannot fill. `blebridge.py` derives those from the
  GATT tree it found: power from `0x2A63`/`0x2AD2`, cadence from those or
  `0x2A5B`, controllable from the FTMS control point `0x2AD9`, `hr` from
  `0x2A37`, and attributes each to the device that actually owns the
  characteristic on the wire.

`FAKESENSOR_HR=1` walks the discovery and pairing path with a made-up strap, for
testing without hardware -- two fake sensors and `./run.sh`, the probe polling
the export the way the game's UI does:

```
[fakebonjour] loaded: name="FakeTrainer" port=36866 caps=power,cadence,controllable
[fakebonjour] loaded: name="FakeHRM"     port=36867 caps=hr
SCAN   t+5s: 2 device(s)
SLOT   E_HeartRate   t+1s: 1 device(s)   "FakeHRM"      type=E_HeartRate
SLOT   E_PowerSource t+1s: 1 device(s)   "FakeTrainer"  type=E_PowerSource
CONN   E_PowerSource E_Success "FakeTrainer"
CONN   E_HeartRate   E_Success "FakeHRM"
READ   t+2s power=152W cadence=-1 speed=-1 hr=76 connected=True
READ   t+3s power=154W cadence=-1 speed=-1 hr=77 connected=True
```

That covers the scan list, the per-slot filtering and both getters reading one
sensor each. It does *not* reproduce the game's one-connection rule: the probe
dials both ports itself, which is exactly what the game will not do -- so a
strap that passes here can still arrive with no data path in game, and the
bridge's shared endpoint is what settles that.

## It also starts the export shim

`fakebonjour.c` does one thing that has nothing to do with Bonjour: on its first
`CreateInstance` it calls `shim_kick()`, which loads `../exportshim/`'s
`MyWhooshShim.dll` through mono's embedding API and invokes
`MyWhoosh.ExportShim::Install`.

That shim replaces the four `Get*DevicesList` exports, which wine-mono refuses
to marshal and which the game's UI polls constantly — without it the game dies
with `MarshalDirectiveException` seconds after the objects below are created.
It has to run inside the game process, after `WindowsConnectivity.dll` is loaded
and before the first poll, and this DLL is the only thing of ours that is
reliably there at that moment: the game's own connectivity init is what creates
the Bonjour objects, which is what loads us. `MYWHOOSH_SHIM_DLL` overrides the
path it looks in, or disables the shim when set empty.

The coupling is worth knowing about: the shim arrives with the Bonjour path, so
anything that stops these coclasses being created also leaves the exports
unhooked. `../exportshim/README.md` covers what to do if that ever matters.

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

That last point is what a strap runs into: search the heart-rate slot and the
game walks its *paired* sensors instead of the scan list, so a strap that
announced itself over Direct Connect is browsed, resolved, written into
`scannedList` and then never offered. `../exportshim/`'s `ScanRescue` is where
that is answered — it asks the game for the scan list under the slot the game
does answer for, and relabels the result. See "Pairing a heart-rate strap"
above.

## Known rough edges

- Nothing filters the scan list except the shim, so without
  `../exportshim/` installed every advertised sensor is offered for every slot
  — and the heart-rate slot lists nothing at all.
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
- The prefix needs a Windows service named exactly `Bonjour Service` in state
  `Running`, because `GetNetworkState()` checks for it before anything else
  happens. `bonjourstub.c` is that service and nothing more: it reports
  `SERVICE_RUNNING` and waits to be stopped. Nothing behind the gate is ever
  asked of it — discovery is answered in-process by `fakebonjour.dll` — so no
  part of Apple's Bonjour has to be installed. `install.sh` registers it with
  `start= auto`, which is what brings it back after the `wineserver -k` between
  launches, and leaves Apple's own service alone if a prefix already has one.
- `blebridge.py` serves one client at a time and needs the device already
  paired-or-connectable by BlueZ; it does not pair for you. If the trainer is
  already connected to a phone or a head unit, BlueZ will not get it.
- One sensor, one client connection at a time, and the objects are only safe on
  the game's STA thread (registered `ThreadingModel=Apartment`, like Bonjour).
