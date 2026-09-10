# bleshim — real Bluetooth sensors for MyWhoosh under Wine

MyWhoosh is a Windows indoor-cycling game. It talks to smart trainers and
heart-rate straps over Bluetooth Low Energy, using a Windows API (WinRT) that
does not work under Wine. So on Linux the game finds no sensors, and the usual
workaround is to bridge them from a phone over the LAN.

This directory makes the game's **own** Bluetooth path work, against your
computer's **own** Bluetooth adapter, through BlueZ — the Linux Bluetooth stack.
No phone, no LAN, no pretending. Your trainer pairs in the game's normal device
screen and streams power and cadence; a heart-rate strap pairs beside it.

**Status: working.** Measured on 2026-09-11 with MyWhoosh 6.1.2 under
GE-Proton10-4: the game discovers a Tacx Flux and a heart-rate strap, connects
to both at once, subscribes to Cycling Power, Indoor Bike Data, FTMS Status,
FTMS Control Point and Heart Rate Measurement, and rides. Not packaged yet —
installing it is still the manual sequence below.

## Quick start

You need Python 3 with `dbus-python` and `PyGObject`, a working BlueZ (if
`bluetoothctl scan on` shows your trainer, you are fine), Mono's `mcs` compiler
to build, and a Wine prefix with MyWhoosh already installed.

```sh
./build.sh                                   # two .NET assemblies into build/
WINEPREFIX=<prefix> ./install.sh             # copy them into the prefix
WINEPREFIX=<prefix> ../exportshim/install.sh # and this one, see below
./run.sh                                     # start the helper + the game
```

`run.sh` launches `blehelper.py` and then the game, and points both at one log
file. Watch it:

```sh
tail -f /tmp/bleshim-*.log
```

A healthy start looks like this, and the last two lines only appear once you
open the game's sensor screen and wake the trainer up (pedal a turn):

```
[blehelper] listening on 127.0.0.1:27019
[bleshim]   export shim: invoking Install from C:\windows\mono\mono-2.0\lib\MyWhooshShim.dll
[exportshim] hooked 4/4 exports
[bleshim]   connected to blehelper on 127.0.0.1:27019
[blehelper] scanning
[bleshim]   connected to FA:55:E5:BE:21:A5 (Tacx Flux 06189)
[bleshim]   subscribed to 00002a63-… on FA:55:E5:BE:21:A5
```

Before the game, you can check the Linux half on its own — this needs nothing
from Wine:

```sh
./blehelper.py --list                   # what is advertising right now
./blehelper.py                          # serve BlueZ on 127.0.0.1:27019
```

## How it works

Three pieces, in the order the game meets them.

**1. A managed assembly the game loads instead of WinRT.** MyWhoosh's
`WindowsConnectivity.dll` references an assembly called `Windows`, which on
Windows is the WinRT metadata file. Mono resolves that reference *by simple
name*, from the prefix's own directory `c:\windows\mono\mono-2.0\lib`. So a
plain .NET assembly named `Windows.dll` sitting there satisfies it, and the
game's calls — `BluetoothLEAdvertisementWatcher.Start`, `ReadValueAsync`,
`ValueChanged` — land in ordinary C# we wrote. That is `src/Windows.cs`, and it
is why no Wine change and no game-file edit is needed. (The game will not load
its own DLL if a single byte of it changed; see `../winmd/README.md`.)

**2. A Linux helper, `blehelper.py`.** Code inside the Wine prefix cannot talk
to BlueZ: BlueZ is D-Bus, D-Bus is a Unix socket, and Wine's winsock has no
`AF_UNIX`. So the hardware half lives outside Wine as a small Python program
speaking BlueZ's D-Bus API, and the two halves exchange one JSON object per line
over `127.0.0.1:27019`. `src/Backend.cs` is the only file in the shim that knows
this; everything above it just calls methods.

**3. The export shim, `../exportshim/`.** Four of the game's C entry points hand
their device list back by reference, and wine-mono refuses to marshal that
shape — the game's first device-list poll would be a fatal exception. The export
shim replaces those four function pointers in memory with managed
implementations. `src/Loader.cs` starts it from a static constructor, so it is
in place before the game's first poll.

```
MyWhoosh                                        (the game)
   │
   ▼
WindowsConnectivity.dll                         (untouched — the game hashes it)
   │                          ▲
   ▼                          │ four exports replaced in memory
Windows.dll  ── src/ ──►  MyWhooshShim.dll      (both in the prefix's mono tree)
   │
   │  one JSON line per message, 127.0.0.1:27019
   ▼
blehelper.py  ──►  BlueZ (D-Bus)  ──►  your Bluetooth adapter
```

## What else the prefix needs

Two things this directory does not provide, which the game nevertheless
requires:

- **A Bonjour COM server.** At startup the game creates Apple Bonjour's COM
  objects unconditionally, and a failure there is fatal — it dies with a
  `COMException` out of `OpenBikeManager.OBM_Initialize` before Bluetooth is
  ever reached. Bonjour has nothing to do with BLE; the object merely has to
  exist. The `dev` branch's `fakesensor/fakebonjour.c` is one, and with
  `FAKESENSOR_NONE=1` it advertises nothing, which is what you want here. There
  is no minimal stub of our own yet — see `CLAUDE.md`.
- **wine-mono installed into the prefix** (not Wine's shared copy), because
  `install.sh` writes into that tree.

## When it does not work

| What you see | What it usually is |
|---|---|
| Nothing at all in the log | The helper is not running, or the game is not reaching the sensor screen |
| `scanning` but your trainer never appears | It is asleep. Pedal. Confirm with `./blehelper.py --list` |
| `advertises no service UUIDs; the game will ignore it` | Normal for phones and watches. The game only shows devices advertising a fitness service |
| The game reports Bluetooth off | The helper is not reachable, or the adapter is off (`bluetoothctl power on`) |
| Game exits at startup with `COMException` | No Bonjour COM server in the prefix — see above |
| The device list crashes on first poll | `../exportshim/` is not installed |

The log is the diagnostic tool. Every layer writes to it with its own tag —
`[blehelper]`, `[bleshim]`, `[exportshim]` — so you can see how far a request
got.

## Files

| File | What it is |
|---|---|
| `src/Windows.cs` | The WinRT surface the game calls: watcher, device, GATT service and characteristic, `Radio`, `DataReader`/`DataWriter` |
| `src/Backend.cs` | The only thing that knows about the helper: connection, request/response, event dispatch, logging |
| `src/Json.cs` | A small JSON reader/writer, because wine-mono's framework has none |
| `src/Loader.cs` | Starts `../exportshim/` from inside the game |
| `src/SystemRuntimeWindowsRuntime.cs` | The one member the game needs to `await` a WinRT call |
| `blehelper.py` | The Linux half: BlueZ over D-Bus, serving one client on loopback |
| `TestBle.cs` | Drives the shim the way the game does, without the game |
| `build.sh` / `install.sh` / `run.sh` | Build, install into a prefix, launch |

`install.sh --verify` says what is currently in a prefix; `--restore` puts the
inert stubs from `../winmd/` back, which turns Bluetooth off again without
breaking the game.

While working on the shim, `TestBle.cs` is a much faster loop than launching the
game:

```sh
mcs -out:build/TestBle.exe -r:build/Windows.dll TestBle.cs
mono build/TestBle.exe                              # radios, then a 10s scan
mono build/TestBle.exe FA:55:E5:BE:21:A5            # connect, walk GATT, subscribe
mono build/TestBle.exe FA:55:E5:BE:21:A5 --control  # ... and take FTMS control
```

Run it under wine-mono as well as the host's Mono — the two runtimes disagree
about details that only bite inside the prefix. `CLAUDE.md` explains which.

## Going deeper

`CLAUDE.md` in this directory is the engineering reference: what the game's own
IL requires, the traps in wine-mono that cost the most time, why the design is
shaped this way, and what is still open.
