# MyWhoosh on Linux

## What is MyWhoosh?

[MyWhoosh](https://www.mywhoosh.com/) is a free indoor cycling and running platform that lets you train and race in virtual worlds. It supports smart trainers, heart rate monitors, and other ANT+/Bluetooth fitness devices, and is compatible with popular training apps like Zwift-style structured workouts and group rides.

MyWhoosh is officially available on Windows, macOS, iOS, and Android — but **not** Linux. This repository provides the tools to run it on Linux via [Wine](https://www.winehq.org/) through [Lutris](https://lutris.net/).

---

## Prerequisites

- A 64-bit Linux distribution
- [Lutris](https://lutris.net/downloads/) installed
- [Python 3](https://www.python.org/) installed (`python3`)

---

## Installation

There are two installers. Both download MyWhoosh itself from the Microsoft
Store; they differ in how they get the game to start under Wine.

### The in-app connectivity installer (recommended)

```bash
lutris -i lutris/mywhoosh-connectivity.yml
```

The game reaches its **own** device connection screen, so a trainer can be
paired from inside MyWhoosh instead of through the phone app. It installs a
small prebuilt stack alongside the prefix and leaves every game file
byte-identical. Lutris' own Wine is the only requirement — nothing is compiled
at install time, and no part of Apple's Bonjour is needed.

It downloads that stack from this repository's latest release. With a checkout
you can build the same bundle yourself (`release/make-release.sh`) or install it
straight into an existing prefix, as below.

### The patch installer

```bash
lutris -i lutris/mywhoosh.yml
```

Older and simpler: it patches `WindowsConnectivity.dll` so the game starts. That
works because the patch makes MyWhoosh stop loading the DLL at all, which also
removes every in-app sensor route — so a phone running MyWhoosh Link is the only
way to connect a trainer.

### Installing the stack by hand

With a checkout, any prefix that already has MyWhoosh in it can be converted:

```bash
./install.sh ~/Games/mywhoosh          # install
./install.sh --verify ~/Games/mywhoosh # check what landed
./install.sh --restore ~/Games/mywhoosh
```

From a checkout this builds what it needs (`mono-devel`, `mingw-w64`, `cc`); a
release bundle from `release/make-release.sh` ships those artifacts prebuilt.
Either way the game then has to be launched with

```
LD_PRELOAD=<prefix>/dotlocal_shim.so
```

which is what the connectivity installer sets for you. **Do not run the patcher
on a prefix set up this way** — the two exclude each other.

---

---

## Connecting fitness devices (smart trainer, HRM, etc.)

With the **patch installer**, Bluetooth and ANT+ are not usable from Wine at
all: use the **MyWhoosh Link** companion app on your phone, which bridges your
devices to the desktop client over the local network.

- **Android:** [MyWhoosh Link on Google Play](https://play.google.com/store/apps/details?id=com.whoosh.companion)
- **iOS:** [MyWhoosh Link on the App Store](https://apps.apple.com/be/app/mywhoosh-link/id1561724525)

### Connecting a trainer without the phone

With the **connectivity installer**, `fakesensor/blebridge.py` does the
companion app's job from Linux instead: it connects to a Bluetooth LE trainer
through BlueZ and serves it to the game over Wahoo Direct Connect, so the
trainer shows up on MyWhoosh's own device screen with its real name, power and
cadence. Measured working on a Tacx Flux, resistance control included.

```bash
fakesensor/blebridge.py --list                  # find your trainer
fakesensor/blebridge.py --mac AA:BB:CC:DD:EE:FF # serve it
```

Start it before the game; nothing has to be configured per trainer. It hands
the DLL the device's name, serial and port through a file in the prefix, so the
trainer comes up under its own name and its own device id. Without a bridge
running, the prefix serves one hard-coded sensor (`FAKESENSOR_NAME` /
`FAKESENSOR_POWER` / `FAKESENSOR_BPM`), which is enough to check that the path
works.

Still experimental: one trainer and one connection at a time, and BlueZ has to
be able to reach the device — see `fakesensor/README.md`.

---

## How it works

| File | Purpose |
|------|---------|
| `install.sh` | Installs the whole in-app connectivity stack into one prefix (`--verify`, `--restore`) |
| `lutris/mywhoosh-connectivity.yml` | Lutris installer using that stack — the game's own device screen works |
| `lutris/mywhoosh.yml` | Lutris installer using the patch — trainer through the phone app only |
| `release/` | Builds the prebuilt bundle the connectivity installer downloads |
| `winemono/` | Patches `ComAwareEventInfo`, a throw-only stub in wine-mono, in the prefix's own copy |
| `winmd/` | Stub `Windows` / `System.Runtime.WindowsRuntime` assemblies — the replacement for the patch |
| `exportshim/` | Serves the four device-list exports wine-mono refuses to marshal, by redirecting them in memory |
| `fakesensor/` | Stands in for Bonjour: its two COM classes, the service name the game gates on, `.local` lookup, and a bridge to a real BLE trainer |
| `patch/patch_windows_connectivity_dll.py` | The older fix: patches `WindowsConnectivity.dll` to bypass a Bluetooth state check that crashes MyWhoosh under Wine |

The patcher rewrites the body of
`BluetoothManager.BluetoothProgram::IsBluetoothEnabled` to `ldc.i4.1; ret` so
the check always returns `true` — two bytes, at whatever offset the method is
found at by name (`0xc70` on 6.1.2), rather than a hardcoded one.

**Why it works is not why it was written, measured on 6.1.2:** MyWhoosh hashes
`WindowsConnectivity.dll` and skips loading it if *any* byte changed — a single
altered character of the DOS stub is enough. So the patch buys a game that
launches by stopping the DLL loading at all, and with it every in-app sensor
route: Bluetooth, ANT+ and Direct Connect alike.

`winmd/` replaces it. Unpatched, the game dies in `BT_InitBluetoothManager` with
a `TypeLoadException` on a field typed from the `Windows` winmd, which Wine does
not have; supplying that winmd as a stub assembly in the prefix's wine-mono tree
fixes the crash with every game file left byte-identical, and the game then runs
its own connectivity init. See `winmd/README.md` and `dircon/README.md`,
*Blocker 3*.
