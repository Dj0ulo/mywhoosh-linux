# MyWhoosh on Linux via Wine

[MyWhoosh](https://www.mywhoosh.com/) is a free indoor cycling / virtual training app (similar to Zwift) that is officially only available on Windows, iOS, Android, and Apple TV. This repo contains notes and scripts to run it on Linux using Wine.

---

## What is MyWhoosh?

MyWhoosh is a free-to-use virtual cycling and running platform. It connects to ANT+ and Bluetooth Low Energy (BLE) sensors — smart trainers, heart rate monitors, power meters — so you can ride virtual routes while your real-world effort is captured. It hosts structured workouts and races, and is used by many cyclists as a Zwift alternative.

---

## Setup

### 1. Get the installer

Use [https://store.rg-adguard.net/](https://store.rg-adguard.net/) to download the `.msix` / `.appx` package directly from the Microsoft Store, then unpack it:

```bash
unzip -d MyWhoosh file.msix
```

### 2. Rename the executable

```bash
mv MyWhoosh/Binaries/Win64/MyWhoosh.exe MyWhoosh/Binaries/Win64/MyWhoosh-Win64-Shipping.exe
```

### 3. Install ICU

Download the 64-bit `icu.dll` from [dll-files.com](https://www.dll-files.com/icu.dll.html) and place it in `MyWhoosh/Binaries/Win64/`.

### 4. Install dependencies

```bash
sudo apt-get install winbind winetricks

WINEPREFIX=~/Documents/mywhoosh-wine/mywhoosh-bottle/MyWhoosh winetricks \
  dotnet461 dxvk vcrun2022 \
  atmlib corefonts gdiplus msxml3 msxml6 fontsmooth-rgb gecko
```

### 5. Fix BLE (Bluetooth Low Energy) — see below

```bash
./fix-ble.sh
```

### 6. Run

```bash
WINEPREFIX=~/Documents/mywhoosh-wine/mywhoosh-bottle/MyWhoosh wine MyWhoosh/MyWhoosh.exe
```

---

## BLE Fix (`fix-ble.sh`)

### The problem

When launching the game you may see:

```
Unhandled Exception:
System.TypeLoadException: Could not load type of field
  'BluetoothManager.BluetoothProgram:advertisment' (0) due to:
  Could not load file or assembly
  'Windows, Version=255.255.255.255, Culture=neutral, PublicKeyToken=null'
  or one of its dependencies.
```

**Why it happens:**

MyWhoosh uses a .NET DLL (`WindowsConnectivity10.dll`) that talks to BLE sensors via the Windows Runtime (WinRT) API — specifically `Windows.Devices.Bluetooth` and `Windows.Devices.Bluetooth.Advertisement`. In .NET, all WinRT types are exposed through a special assembly called `Windows, Version=255.255.255.255` (the union Windows metadata assembly, `Windows.winmd`).

Wine ships with only 10 generic WinRT namespace metadata files (`windows.storage.winmd`, `windows.ui.winmd`, etc.) and **does not include `Windows.Devices.*`** (Bluetooth, sensors, enumeration). When Mono tries to load `WindowsConnectivity10.dll` it immediately fails because the `Windows` assembly cannot be resolved.

### The fix

`fix-ble.sh` downloads `Windows.WinMD` from Microsoft's [`Microsoft.Windows.SDK.Contracts`](https://www.nuget.org/packages/Microsoft.Windows.SDK.Contracts/) NuGet package. This file is the complete WinRT type metadata for Windows 10 — it contains every WinRT type definition including the full Bluetooth stack — and installs it in four locations so Wine's Mono runtime can find it:

| Location | Why |
|---|---|
| `Mono GAC/Windows/255.255.255.255__/Windows.dll` | Mono checks the GAC first when resolving named assemblies |
| `system32/winmetadata/Windows.winmd` | WinRT namespace resolution via `RoResolveNamespace` |
| `Content/Libraries/Win64/Windows.dll` | Mono probes the directory of the referencing assembly |
| `MyWhoosh/Binaries/Win64/Windows.dll` | Mono's ApplicationBase fallback probing path |

To re-apply after rebuilding the bottle:

```bash
./fix-ble.sh
```

The script caches the downloaded file at `/tmp/mywhoosh-Windows.WinMD` so subsequent runs are instant.
