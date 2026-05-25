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

This will:
1. Create a 64-bit Wine prefix
2. Download the MyWhoosh MSIX package directly from the Microsoft Store
3. Extract and install it into the Wine prefix
4. Apply a patch to `WindowsConnectivity.dll` to bypass a Bluetooth state check that would otherwise crash the game at launch

### 3. Launch MyWhoosh

Once installed, launch MyWhoosh from Lutris like any other game.

---

## Connecting fitness devices (smart trainer, HRM, etc.)

Bluetooth and ANT+ are **not currently supported** directly from Wine. To connect your smart trainer, heart rate monitor, and other devices, use the **MyWhoosh Link** companion app on your phone:

- **Android:** [MyWhoosh Link on Google Play](https://play.google.com/store/apps/details?id=com.whoosh.companion)
- **iOS:** [MyWhoosh Link on the App Store](https://apps.apple.com/be/app/mywhoosh-link/id1561724525)

The companion app bridges your fitness devices to the desktop client over your local network.

---

## How it works

| File | Purpose |
|------|---------|
| `lutris/mywhoosh.yml` | Lutris installer script — automates the full install process |
| `patch/patch_windows_connectivity_dll.py` | Patches `WindowsConnectivity.dll` to bypass a Bluetooth state check that crashes MyWhoosh under Wine |

The patcher modifies two bytes in the DLL at offset `0xc50`, replacing the original method body with `ldc.i4.1; ret` so that the Bluetooth availability check always returns `true`.
