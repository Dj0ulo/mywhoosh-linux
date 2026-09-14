# MyWhoosh on Linux

## What is MyWhoosh?

[MyWhoosh](https://www.mywhoosh.com/) is a free indoor cycling and running platform that lets you train and race in virtual worlds. It supports smart trainers, heart rate monitors, and other ANT+/Bluetooth fitness devices, and is compatible with popular training apps like Zwift-style structured workouts and group rides.

MyWhoosh is officially available on Windows, macOS, iOS, and Android — but **not** Linux. This repository provides the tools to run it on Linux via [Wine](https://www.winehq.org/) through [Lutris](https://lutris.net/).

---

## Prerequisites

- A 64-bit Linux distribution
- [Lutris](https://lutris.net/downloads/) installed
- [Python 3](https://www.python.org/) installed (`python3`)

For Bluetooth sensors (trainer, heart-rate strap), also:

- A working BlueZ — if `bluetoothctl scan on` shows your trainer when you
  pedal, you are fine
- `dbus-python` and `PyGObject` for Python 3:

  | | |
  |---|---|
  | Debian/Ubuntu | `sudo apt install python3-dbus python3-gi` |
  | Fedora | `sudo dnf install python3-dbus python3-gobject` |
  | Arch | `sudo pacman -S python-dbus python-gobject` |

The installer checks all of this at the end and tells you what is missing.

---

## Installation

### 1. Clone this repository

```bash
git clone https://github.com/Dj0ulo/mywhoosh-linux.git
cd mywhoosh-linux
```

### 2. Install via Lutris

Import the provided Lutris installer script:

```bash
lutris -i lutris/mywhoosh.yml
```

(For MyWhoosh HD, `lutris/mywhoosh-hd.yml`.)

This will:
1. Create a 64-bit Wine prefix
2. Download the MyWhoosh MSIX package directly from the Microsoft Store
3. Extract and install it into the Wine prefix
4. Install the Bluetooth shim into the prefix, and the Linux-side helper beside
   the game
5. Wire that helper to start and stop with the game, and check your setup

### 3. Launch MyWhoosh

Launch it from Lutris like any other game. The Bluetooth helper starts with it
and stops when you quit.

---

## Connecting fitness devices (smart trainer, HRM, etc.)

Pair them in MyWhoosh's own device screen, through your computer's own
Bluetooth adapter. Wake the sensor first — pedal a turn, or touch the strap —
since a trainer that is asleep does not advertise and so cannot be found.

Everything writes to one log, `<prefix>/bleshim/session.log`, which is the
first thing to read when a sensor does not appear.
[`bleshim/README.md`](bleshim/README.md) has a table of what the usual symptoms
mean, and [`lutris/README.md`](lutris/README.md) covers the install itself —
which adapter is used, where the log goes, and how the helper is started.

### Using a phone instead

If you would rather bridge your sensors from a phone, the **MyWhoosh Link**
companion app still works and needs nothing from this repository:

- **Android:** [MyWhoosh Link on Google Play](https://play.google.com/store/apps/details?id=com.whoosh.companion)
- **iOS:** [MyWhoosh Link on the App Store](https://apps.apple.com/be/app/mywhoosh-link/id1561724525)

---

## How it works

| File | Purpose |
|------|---------|
| `lutris/mywhoosh.yml` | Lutris installer script — the whole install, Bluetooth included |
| `lutris/mywhoosh-ble.sh` | Checks the setup, and starts/stops the Bluetooth helper around the game |
| `bleshim/` | The Bluetooth stack: a .NET assembly the game loads instead of WinRT, plus a Linux helper speaking BlueZ |
| `exportshim/` | Four game entry points wine-mono cannot marshal, replaced in memory |
| `dist/` | The built assemblies the installer downloads |
| `patch/` | Unused here — the startup patch from `main`, kept because that branch's installer needs it |

MyWhoosh talks to sensors over Bluetooth using a Windows API (WinRT) that Wine
does not have and mono cannot project. Rather than patch the game — it hashes
its own `WindowsConnectivity.dll` and stops loading it if a byte differs — the
shim satisfies the assembly reference the game already has: mono looks for an
assembly named `Windows` in the prefix's mono tree, and finds ordinary C# that
answers those calls from BlueZ. [`bleshim/README.md`](bleshim/README.md) is the
longer version.
