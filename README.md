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

When launching the game you may see one of two errors:

**Error 1** (assembly not found):
```
System.TypeLoadException: Could not load type of field
  'BluetoothManager.BluetoothProgram:advertisment' (0) due to:
  Could not load file or assembly
  'Windows, Version=255.255.255.255, Culture=neutral, PublicKeyToken=null'
```

**Error 2** (type not found inside the assembly):
```
System.TypeLoadException: Could not load type of field
  'BluetoothManager.BluetoothProgram:advertisment' (0) due to:
  Could not resolve type with token 01000090 from typeref
  (expected class 'Windows.Devices.Bluetooth.Advertisement.BluetoothLEAdvertisementWatcher'
  in assembly 'Windows, Version=255.255.255.255, ...')
```

**Why it happens:**

MyWhoosh uses a .NET DLL (`WindowsConnectivity10.dll`) that talks to BLE sensors via the Windows Runtime (WinRT) API — specifically `Windows.Devices.Bluetooth` and `Windows.Devices.Bluetooth.Advertisement`. In .NET, all WinRT types are accessed through a special assembly called `Windows, Version=255.255.255.255` (the union Windows metadata assembly, `Windows.winmd`).

Wine ships with only 10 generic WinRT namespace stubs and **does not include `Windows.Devices.*`** (Bluetooth, sensors, enumeration).

The `Windows.WinMD` file in Microsoft's NuGet package is **not** a monolithic union assembly — it is a **type-forwarding facade**. Its `ExportedType` table redirects every type (including `BluetoothLEAdvertisementWatcher`) to a *contract assembly* such as `Windows.Foundation.UniversalApiContract`. When Mono follows that forwarder it looks in the GAC for `Windows.Foundation.UniversalApiContract` — but that assembly is never installed, so the type still can't be found (Error 2).

### The fix

`fix-ble.sh` downloads the `Microsoft.Windows.SDK.Contracts` NuGet package and installs four WinMD files so that both the `Windows` assembly reference and all type-forwarder targets resolve correctly:

| Assembly installed | What it provides |
|---|---|
| `Windows` (`Windows.WinMD`) | Resolves the top-level `Windows, v255.255.255.255` assembly ref; contains `ExportedType` forwarders to the contracts below |
| `Windows.Foundation.UniversalApiContract` | Contains the actual `BluetoothLEAdvertisementWatcher` and the full BLE API type definitions |
| `Windows.Foundation.FoundationContract` | Dependency of `UniversalApiContract` |
| `Windows.Devices.DevicesLowLevelContract` | Dependency referenced by `Windows.WinMD` |

Each is installed in three places:

| Location | Why |
|---|---|
| `Mono GAC/{Name}/{Version}__/{Name}.dll` | Primary resolution path; installed at both the version the facade expects (1.0.0.0) and the file's actual version |
| `system32/winmetadata/` + `syswow64/winmetadata/` | WinRT namespace resolution via `RoResolveNamespace` |
| `Content/Libraries/Win64/` + `Binaries/Win64/` | Mono's ApplicationBase and assembly-directory fallback probing paths |

To re-apply after rebuilding the bottle:

```bash
./fix-ble.sh
```

The script caches the downloaded NuGet package at `/tmp/mywhoosh-sdk-contracts.nupkg` and extracted WinMDs at `/tmp/mywhoosh-winmd-cache/` so subsequent runs are instant.
